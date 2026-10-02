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

import spotipy

from common import get_spotify_client, spotify_retry

__all__ = [
    "sanitize_filename",
    "backup_liked_songs",
    "backup_playlists",
    "main",
]


def sanitize_filename(name: str) -> str:
    """Sanitize string to be safe for filenames."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name).strip(" ._")
    return name[:60] or "unnamed_playlist"


def _track_fields(track: dict, added_at: str | None = None) -> dict:
    """Flatten a Spotify track object into the backup record shape."""
    artists = [a.get("name", "") for a in track.get("artists", []) if isinstance(a, dict)]
    album = track.get("album")
    external_ids = track.get("external_ids")
    external_urls = track.get("external_urls")
    return {
        "added_at": added_at,
        "title": track.get("name"),
        "artists": artists,
        "artist": ", ".join(artists),
        "album": album.get("name") if isinstance(album, dict) else "",
        "duration_ms": track.get("duration_ms"),
        "isrc": external_ids.get("isrc") if isinstance(external_ids, dict) else None,
        "uri": track.get("uri"),
        "spotify_url": external_urls.get("spotify") if isinstance(external_urls, dict) else None,
    }


def backup_liked_songs(sp, backup_dir: str) -> list[dict]:
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
            track = item.get("track") if isinstance(item, dict) else None
            if not isinstance(track, dict):
                continue  # removed or region-blocked track
            liked_tracks.append(_track_fields(track, item.get("added_at")))

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


def _fetch_playlist_tracks(sp, pl: dict) -> list[dict]:
    """Return flattened track records for one playlist."""
    tracks: list[dict] = []

    results = pl.get("tracks")
    if not isinstance(results, dict) or "items" not in results:
        results = spotify_retry(sp.playlist_items, pl["id"], limit=100, offset=0)

    while isinstance(results, dict):
        for item in results.get("items", []):
            if not isinstance(item, dict):
                continue
            track = item.get("item") or item.get("track") or item
            if not isinstance(track, dict) or not track.get("name"):
                continue
            tracks.append(_track_fields(track, item.get("added_at")))

        if not results.get("next"):
            break
        results = spotify_retry(sp.next, results)
        time.sleep(0.4)

    return tracks


def _export_playlist(sp, pl: dict, playlists_dir: str) -> dict:
    """Write one playlist JSON file and return its summary entry."""
    pl_id = pl.get("id")
    pl_name = pl.get("name", "Untitled")
    pl_owner = pl.get("owner", {}).get("display_name", "")
    pl_url = pl.get("external_urls", {}).get("spotify", "")

    pl_tracks = _fetch_playlist_tracks(sp, pl)

    safe_name = sanitize_filename(pl_name)
    filename = f"{safe_name}_{pl_id}.json"
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

    with open(os.path.join(playlists_dir, filename), "w", encoding="utf-8") as f:
        json.dump(pl_data, f, indent=2, ensure_ascii=False)

    print(f"  ✓ Saved playlist '{pl_name}' ({len(pl_tracks)} tracks)")
    time.sleep(0.3)

    return {
        "id": pl_id,
        "name": pl_name,
        "owner": pl_owner,
        "track_count": len(pl_tracks),
        "spotify_url": pl_url,
        "file": f"playlists/{filename}",
    }


def _list_user_playlists(sp) -> list[dict]:
    """All user playlists, falling back to the configured targets when the
    token lacks playlist-read-private."""
    all_playlists: list[dict] = []
    offset = 0
    limit = 50

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
        if exc.http_status != 403:
            raise
        print("[Backup] Note: Token lacks 'playlist-read-private' scope for /me/playlists. Backing up configured playlists …")
        known_ids = [
            os.environ.get("SPOTIFY_PLAYLIST_ID"),
            os.environ.get("SPOTIFY_LISTENBRAINZ_PLAYLIST_ID"),
        ]
        for pl_id in filter(None, known_ids):
            try:
                all_playlists.append(spotify_retry(sp.playlist, playlist_id=pl_id))
            except Exception as pl_exc:
                print(f"  ⚠️ Could not fetch playlist {pl_id}: {pl_exc}")

    return all_playlists


def backup_playlists(sp, backup_dir: str) -> list[dict]:
    """Fetch and export all user playlists and their contents."""
    print("[Backup] Fetching user playlists …")
    playlists_dir = os.path.join(backup_dir, "playlists")
    os.makedirs(playlists_dir, exist_ok=True)

    all_playlists = _list_user_playlists(sp)
    print(f"[Backup] Found {len(all_playlists)} playlists. Fetching tracks …")

    summaries: list[dict] = []
    failures = 0
    for pl in all_playlists:
        try:
            summaries.append(_export_playlist(sp, pl, playlists_dir))
        except Exception as exc:
            # One unreadable playlist must not abort the whole weekly backup.
            failures += 1
            name = pl.get("name") or pl.get("id")
            print(f"  ⚠️ Skipped playlist '{name}': {exc.__class__.__name__}: {exc}")

    if failures:
        print(f"[Backup] {failures} playlist(s) could not be exported.")

    summary_path = os.path.join(backup_dir, "playlists_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2, ensure_ascii=False)

    return summaries


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
