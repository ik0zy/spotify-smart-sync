"""
backup_library.py
Automated Spotify library backup.
Exports all Liked Songs and Playlists to JSON and text formats.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests
import spotipy


def requests_retry(
    url: str,
    params: dict | None = None,
    data: dict | None = None,
    method: str = "GET",
    timeout: int = 30,
    max_retries: int = 3,
) -> requests.Response:
    """Execute HTTP request with automatic retries on timeouts and 5xx server errors."""
    for attempt in range(1, max_retries + 1):
        try:
            if method.upper() == "POST":
                resp = requests.post(url, data=data, timeout=timeout)
            else:
                resp = requests.get(url, params=params, timeout=timeout)

            if resp.status_code in (500, 502, 503, 504):
                if attempt < max_retries:
                    time.sleep(2 * attempt)
                    continue
            return resp
        except (requests.exceptions.RequestException, requests.exceptions.Timeout) as exc:
            if attempt >= max_retries:
                raise
            print(f"[Network] Request timeout/error ({exc.__class__.__name__}), retrying {attempt}/{max_retries} …")
            time.sleep(2 * attempt)
    raise RuntimeError(f"HTTP request failed after {max_retries} attempts.")


def get_spotify_client() -> spotipy.Spotify:
    """Exchange refresh token for access token and return Spotify client."""
    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    refresh_token = os.environ["SPOTIFY_REFRESH_TOKEN"]

    resp = requests_retry(
        "https://accounts.spotify.com/api/token",
        method="POST",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    return spotipy.Spotify(auth=access_token, retries=0, status_retries=0)


MAX_SPOTIFY_RETRIES = 3
MAX_SPOTIFY_RETRY_WAIT = 60


def spotify_retry(func, *args, **kwargs):
    """Call spotipy method with bounded retry on 429 rate limits."""
    for attempt in range(1, MAX_SPOTIFY_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except spotipy.exceptions.SpotifyException as exc:
            if exc.http_status != 429:
                raise
            retry_after = int(exc.headers.get("Retry-After", MAX_SPOTIFY_RETRY_WAIT))
            if retry_after > MAX_SPOTIFY_RETRY_WAIT:
                raise RuntimeError(f"[Spotify] Rate-limited Retry-After={retry_after}s exceeds cap — aborting.") from exc
            print(f"[Spotify] Rate-limited, waiting {retry_after}s (attempt {attempt}/{MAX_SPOTIFY_RETRIES}) …")
            time.sleep(retry_after)
    raise RuntimeError(f"[Spotify] Still rate-limited after {MAX_SPOTIFY_RETRIES} retries — aborting.")


def sanitize_filename(name: str) -> str:
    """Sanitize string to be safe for filenames."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name).strip(" ._")
    return name[:60] or "unnamed_playlist"


def backup_liked_songs(sp: spotipy.Spotify, backup_dir: str) -> list[dict]:
    """Fetch and export all Liked Songs."""
    print("[Backup] Fetching Liked Songs …")
    liked_tracks: list[dict] = []
    offset = 0
    limit = 50

    while True:
        results = spotify_retry(sp.current_user_saved_tracks, limit=limit, offset=offset)
        items = results.get("items", [])
        if not items:
            break

        for item in items:
            track = item.get("track")
            if not track:
                continue
            artists = [a.get("name", "") for a in track.get("artists", [])]
            external_ids = track.get("external_ids", {})
            external_urls = track.get("external_urls", {})

            liked_tracks.append({
                "added_at": item.get("added_at"),
                "title": track.get("name"),
                "artists": artists,
                "artist": ", ".join(artists),
                "album": track.get("album", {}).get("name"),
                "duration_ms": track.get("duration_ms"),
                "isrc": external_ids.get("isrc"),
                "uri": track.get("uri"),
                "spotify_url": external_urls.get("spotify"),
            })

        if results.get("next") is None:
            break
        offset += limit
        time.sleep(0.5)

    print(f"[Backup] Exporting {len(liked_tracks)} Liked Songs …")

    # Save structured JSON
    liked_json_path = os.path.join(backup_dir, "liked_songs.json")
    with open(liked_json_path, "w", encoding="utf-8") as f:
        json.dump(liked_tracks, f, indent=2, ensure_ascii=False)

    # Save human-readable text
    liked_txt_path = os.path.join(backup_dir, "liked_songs.txt")
    with open(liked_txt_path, "w", encoding="utf-8") as f:
        for t in liked_tracks:
            f.write(f"{t['artist']} - {t['title']}\n")

    return liked_tracks


def backup_playlists(sp: spotipy.Spotify, backup_dir: str) -> list[dict]:
    """Fetch and export all user playlists and their contents."""
    print("[Backup] Fetching user playlists …")
    playlists_dir = os.path.join(backup_dir, "playlists")
    os.makedirs(playlists_dir, exist_ok=True)

    playlists_summary: list[dict] = []
    offset = 0
    limit = 50

    all_playlists: list[dict] = []
    try:
        while True:
            results = spotify_retry(sp.current_user_playlists, limit=limit, offset=offset)
            items = results.get("items", [])
            if not items:
                break
            all_playlists.extend(items)
            if results.get("next") is None:
                break
            offset += limit
            time.sleep(0.5)
    except spotipy.exceptions.SpotifyException as exc:
        if exc.http_status == 403:
            print("[Backup] Note: Token lacks 'playlist-read-private' scope for /me/playlists. Backing up configured playlists …")
            # Fall back to known target playlists configured in environment
            known_ids = [
                os.environ.get("SPOTIFY_PLAYLIST_ID"),
                os.environ.get("SPOTIFY_LISTENBRAINZ_PLAYLIST_ID"),
            ]
            for pl_id in filter(None, known_ids):
                try:
                    pl_info = spotify_retry(sp.playlist, playlist_id=pl_id)
                    all_playlists.append(pl_info)
                except Exception as pl_exc:
                    print(f"  ⚠️ Could not fetch playlist {pl_id}: {pl_exc}")
        else:
            raise

    print(f"[Backup] Found {len(all_playlists)} playlists. Fetching tracks …")

    for pl in all_playlists:
        pl_id = pl.get("id")
        pl_name = pl.get("name", "Untitled")
        pl_owner = pl.get("owner", {}).get("display_name", "")
        pl_url = pl.get("external_urls", {}).get("spotify", "")

        # Fetch tracks in playlist
        pl_tracks: list[dict] = []
        print(f"[Backup] Processing tracks for playlist '{pl_name}' (ID: {pl_id}) …")
        results = pl.get("tracks")
        if not results or not isinstance(results, dict) or "items" not in results:
            print("  Falling back to sp.playlist_items …")
            results = spotify_retry(sp.playlist_items, pl_id, limit=100, offset=0)

        total_in_obj = results.get("total") if isinstance(results, dict) else None
        items_count = len(results.get("items", [])) if isinstance(results, dict) else 0
        print(f"  Tracks page 1: total={total_in_obj}, items_count={items_count}")

        page_num = 1
        while results:
            items = results.get("items", []) if isinstance(results, dict) else []
            if items and page_num == 1:
                first_it = items[0]
                print(f"  First item keys: {list(first_it.keys()) if isinstance(first_it, dict) else type(first_it)}")
                if isinstance(first_it, dict) and "track" in first_it:
                    tr_obj = first_it.get("track")
                    print(f"  Track in first item: type={type(tr_obj)}, keys={list(tr_obj.keys()) if isinstance(tr_obj, dict) else tr_obj}")

            for item in items:
                if not isinstance(item, dict):
                    continue
                track = item.get("item") or item.get("track") or item
                if not track or not isinstance(track, dict) or not track.get("name"):
                    continue
                artists = [a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict)]
                external_ids = track.get("external_ids", {}) if isinstance(track.get("external_ids"), dict) else {}
                external_urls = track.get("external_urls", {}) if isinstance(track.get("external_urls"), dict) else {}

                pl_tracks.append({
                    "added_at": item.get("added_at"),
                    "title": track.get("name"),
                    "artists": artists,
                    "artist": ", ".join(artists),
                    "album": track.get("album", {}).get("name") if isinstance(track.get("album"), dict) else "",
                    "duration_ms": track.get("duration_ms"),
                    "isrc": external_ids.get("isrc"),
                    "uri": track.get("uri"),
                    "spotify_url": external_urls.get("spotify"),
                })

            if isinstance(results, dict) and results.get("next"):
                page_num += 1
                results = spotify_retry(sp.next, results)
                time.sleep(0.4)
            else:
                break

        safe_name = sanitize_filename(pl_name)
        filename = f"{safe_name}_{pl_id}.json"
        pl_path = os.path.join(playlists_dir, filename)

        pl_data = {
            "id": pl_id,
            "name": pl_name,
            "description": pl.get("description", ""),
            "owner": pl_owner,
            "public": pl.get("public"),
            "collaborative": pl.get("collaborative"),
            "spotify_url": pl_url,
            "track_count": len(pl_tracks),
            "tracks": pl_tracks,
        }

        with open(pl_path, "w", encoding="utf-8") as f:
            json.dump(pl_data, f, indent=2, ensure_ascii=False)

        playlists_summary.append({
            "id": pl_id,
            "name": pl_name,
            "owner": pl_owner,
            "track_count": len(pl_tracks),
            "spotify_url": pl_url,
            "file": f"playlists/{filename}",
        })
        print(f"  ✓ Saved playlist '{pl_name}' ({len(pl_tracks)} tracks)")
        time.sleep(0.3)

    summary_path = os.path.join(backup_dir, "playlists_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(playlists_summary, f, indent=2, ensure_ascii=False)

    return playlists_summary


def main() -> None:
    backup_dir = os.environ.get("BACKUP_DIR", "backups")
    os.makedirs(backup_dir, exist_ok=True)

    sp = get_spotify_client()
    backup_start = datetime.now(timezone.utc)

    liked_songs = backup_liked_songs(sp, backup_dir)
    playlists = backup_playlists(sp, backup_dir)

    total_playlist_tracks = sum(p["track_count"] for p in playlists)

    summary = {
        "backup_date_utc": backup_start.isoformat(),
        "total_liked_songs": len(liked_songs),
        "total_playlists": len(playlists),
        "total_playlist_tracks": total_playlist_tracks,
    }

    info_path = os.path.join(backup_dir, "latest_backup_info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(
        f"[Backup] Successfully completed backup:\n"
        f"  - Liked Songs: {len(liked_songs):,}\n"
        f"  - Playlists: {len(playlists):,} ({total_playlist_tracks:,} total tracks)\n"
        f"  - Directory: {backup_dir}/"
    )


if __name__ == "__main__":
    main()
