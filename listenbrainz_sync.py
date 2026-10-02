"""
listenbrainz_sync.py
Syncs ListenBrainz "Weekly Exploration" playlist for a given user
to a target Spotify playlist.

Behaviour:
  - New week (new MBID): resolve all tracks on Spotify, full sync.
  - Same week (hourly): fetch scrobbles since playlist start, remove any
    tracks that have been played. The playlist shrinks as you listen.
"""

import os
import re
import time
from datetime import datetime, timezone

from common import (
    get_spotify_client,
    load_state,
    local_timestamp,
    requests_retry,
    save_state,
    spotify_retry,
    track_key_variants,
    track_played,
)

__all__ = [
    "clean_track_metadata",
    "fetch_scrobbles_since",
    "fetch_latest_weekly_exploration",
    "find_spotify_track_uri",
    "update_playlist_description",
    "main",
]


# ── search-query cleanup ─────────────────────────────────────────────────────

def clean_track_metadata(artist: str, title: str) -> tuple[str, str]:
    """Strip clutter like (Remastered 2011), feat. X for Spotify search recall.

    This is about the *query* we send, not about matching, so it is
    deliberately more aggressive than common.title_variants.
    """
    artist_clean = re.sub(r"(?i)\s*[\(\[]ft\.?|feat\.?.*[\)\]]", "", artist).strip()
    title_clean = re.sub(r"(?i)\s*[\(\[]ft\.?|feat\.?.*[\)\]]", "", title).strip()
    title_clean = re.sub(
        r"(?i)\s*[\(\[](remastered|deluxe|bonus|expanded|edition|live|version|single|radio edit).*[\)\]]",
        "",
        title_clean,
    ).strip()
    return artist_clean or artist, title_clean or title


# ── Last.fm scrobble fetching ────────────────────────────────────────────────

def fetch_scrobbles_since(since_iso: str) -> set[str]:
    """Return 'artist - title' keys scrobbled since *since_iso*.

    Every spelling variant of each scrobble is indexed so that a track
    described as 'A feat. B' on Last.fm still matches the Spotify
    artist list ['A', 'B'].
    """

    api_key = os.environ["LASTFM_API_KEY"]
    username = os.environ["LASTFM_USERNAME"]

    cutoff = datetime.fromisoformat(since_iso)
    cutoff_ts = int(cutoff.timestamp())

    API_URL = "https://ws.audioscrobbler.com/2.0/"
    PER_PAGE = 200
    MAX_RETRY_WAIT = 60
    MAX_RETRIES = 3

    scrobbled: set[str] = set()
    page = 1
    retries = 0

    while True:
        params = {
            "method": "user.getrecenttracks",
            "user": username,
            "api_key": api_key,
            "format": "json",
            "limit": PER_PAGE,
            "from": cutoff_ts,
            "page": page,
            "extended": 0,
        }

        resp = requests_retry(API_URL, params=params, timeout=30)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", MAX_RETRY_WAIT))
            retries += 1
            if retry_after > MAX_RETRY_WAIT or retries > MAX_RETRIES:
                raise RuntimeError(
                    f"[Last.fm] Rate-limited (attempt {retries}, wait {retry_after}s) — aborting."
                )
            print(f"[Last.fm] Rate-limited, waiting {retry_after}s (attempt {retries}/{MAX_RETRIES}) …")
            time.sleep(retry_after)
            continue

        resp.raise_for_status()
        retries = 0
        data = resp.json()

        recent = data.get("recenttracks", {})
        tracks = recent.get("track", [])

        if not tracks:
            break

        for track in tracks:
            if "@attr" in track and track["@attr"].get("nowplaying") == "true":
                continue
            artist = track.get("artist", {}).get("#text", "")
            title = track.get("name", "")
            if artist and title:
                scrobbled.update(track_key_variants(artist, title))

        attrs = recent.get("@attr", {})
        total_pages = int(attrs.get("totalPages", 1))
        print(f"[Last.fm] Page {page}/{total_pages} — {len(scrobbled)} track keys so far.")

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.25)

    print(f"[Last.fm] Fetched {len(scrobbled)} track keys since playlist start.")
    return scrobbled


# ── ListenBrainz API ─────────────────────────────────────────────────────────

def fetch_latest_weekly_exploration(username: str) -> tuple[str, str, list[dict]]:
    """Fetch the latest 'Weekly Exploration' playlist for *username*.

    Returns (playlist_mbid, playlist_title, track_list).
    """
    url = f"https://api.listenbrainz.org/1/user/{username}/playlists/createdfor"
    resp = requests_retry(url, timeout=45, expect_json=True)
    resp.raise_for_status()
    data = resp.json()

    playlists = data.get("playlists", [])
    exploration_playlists = []
    for item in playlists:
        pl = item.get("playlist", {})
        title = pl.get("title", "")
        if "weekly exploration" in title.lower():
            identifier = pl.get("identifier", "")
            mbid = identifier.rstrip("/").split("/")[-1]
            exploration_playlists.append((mbid, title))

    if not exploration_playlists:
        raise RuntimeError(f"[ListenBrainz] No 'Weekly Exploration' playlists found for user '{username}'.")

    target_mbid, target_title = exploration_playlists[0]
    print(f"[ListenBrainz] Found latest playlist: '{target_title}' (MBID: {target_mbid})")

    detail_url = f"https://api.listenbrainz.org/1/playlist/{target_mbid}"
    detail_resp = requests_retry(detail_url, timeout=45, expect_json=True)
    detail_resp.raise_for_status()
    detail_data = detail_resp.json()

    playlist_obj = detail_data.get("playlist", {})
    tracks = playlist_obj.get("track", [])

    print(f"[ListenBrainz] Fetched {len(tracks)} tracks from ListenBrainz.")
    return target_mbid, target_title, tracks


# ── Spotify Track Matcher ───────────────────────────────────────────────────

def find_spotify_track_uri(sp, track_item: dict) -> tuple[str | None, str, str]:
    """Find matching Spotify track URI.

    Returns (uri_or_None, artist, title).
    """
    artist = track_item.get("creator", "").strip()
    title = track_item.get("title", "").strip()

    if not artist or not title:
        return None, artist, title

    # Check identifiers for direct Spotify URI
    identifiers = track_item.get("identifier", [])
    for ident in identifiers:
        if isinstance(ident, str) and "spotify:track:" in ident:
            return ident, artist, title
        if isinstance(ident, str) and "open.spotify.com/track/" in ident:
            track_id = ident.split("/track/")[-1].split("?")[0]
            return f"spotify:track:{track_id}", artist, title

    clean_artist, clean_title = clean_track_metadata(artist, title)

    # Primary query: artist:"..." track:"..."
    query = f'artist:"{clean_artist}" track:"{clean_title}"'
    res = spotify_retry(sp.search, q=query, type="track", limit=3)
    items = res.get("tracks", {}).get("items", [])
    if items:
        return items[0]["uri"], artist, title

    # Fallback loose query
    query_loose = f"{clean_artist} {clean_title}"
    res = spotify_retry(sp.search, q=query_loose, type="track", limit=3)
    items = res.get("tracks", {}).get("items", [])
    if items:
        return items[0]["uri"], artist, title

    return None, artist, title


# ── Playlist sync helpers ───────────────────────────────────────────────────

def sync_spotify_playlist(sp, playlist_id: str, uris: list[str]) -> None:
    """Wipe playlist and add tracks in chunks of 100."""
    spotify_retry(sp.playlist_replace_items, playlist_id, [])
    print("[Spotify] Cleared playlist.")

    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        spotify_retry(sp.playlist_add_items, playlist_id, chunk)
        print(f"[Spotify] Added tracks {i + 1}–{i + len(chunk)} / {len(uris)}.")
        time.sleep(1.0)


def remove_from_playlist(sp, playlist_id: str, uris: list[str]) -> None:
    """Remove specific tracks from a playlist."""
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        spotify_retry(sp.playlist_remove_all_occurrences_of_items, playlist_id, chunk)
    print(f"[Spotify] Removed {len(uris)} track(s).")


# ── Main sync process ────────────────────────────────────────────────────────

def update_playlist_description(
    sp,
    playlist_id: str,
    remaining_count: int,
    total_count: int,
    title: str,
    tz_offset_hours: int = 6,
) -> None:
    """Update ListenBrainz playlist description with queue progress."""
    played_count = max(0, total_count - remaining_count)
    time_str, tz_str = local_timestamp(tz_offset_hours)

    desc = (
        f"ListenBrainz: {title} · "
        f"{remaining_count}/{total_count} tracks remaining ({played_count} played) · "
        f"Last synced: {time_str} ({tz_str})"
    )
    try:
        spotify_retry(sp.playlist_change_details, playlist_id, description=desc)
        print(f"[Spotify] Updated playlist description: '{desc}'")
    except Exception as exc:
        print(f"[Spotify] Note: Could not update playlist description ({exc})")


def _cached_artists(entry: dict) -> list[str]:
    """Artist list for a cache entry, tolerating the older string format."""
    artists = entry.get("artists")
    if isinstance(artists, list) and artists:
        return artists
    single = entry.get("artist")
    return [single] if single else []


def main() -> None:
    username = os.environ.get("LISTENBRAINZ_USERNAME", "ikOzy")
    playlist_id = os.environ["SPOTIFY_LISTENBRAINZ_PLAYLIST_ID"]
    state_file = os.environ.get("LISTENBRAINZ_STATE_FILE", ".listenbrainz_state")

    # 1. Fetch latest Weekly Exploration playlist from ListenBrainz
    mbid, title, tracks = fetch_latest_weekly_exploration(username)

    state = load_state(state_file, require_key="mbid")
    last_mbid = state.get("mbid")

    sp = get_spotify_client()

    is_new_playlist = (mbid != last_mbid) or ("track_cache" not in state)

    if is_new_playlist:
        # ── NEW WEEK: resolve all tracks and full sync ──────────────────
        print("[Sync] New playlist detected — syncing all tracks …")

        track_cache: list[dict] = []   # {uri, artists, title}
        spotify_uris: list[str] = []
        missing_tracks: list[str] = []

        print(f"[Spotify] Resolving {len(tracks)} tracks on Spotify …")
        for idx, track_item in enumerate(tracks, 1):
            uri, artist, track_title = find_spotify_track_uri(sp, track_item)

            if uri:
                spotify_uris.append(uri)
                track_cache.append({
                    "uri": uri,
                    "artists": [artist],
                    "title": track_title,
                })
            else:
                missing_tracks.append(f"{artist} - {track_title}")
                print(f"  ⚠️ [{idx}/{len(tracks)}] Could not find on Spotify: '{artist} - {track_title}'")
            time.sleep(0.25)

        print(f"[Sync] Matched {len(spotify_uris)}/{len(tracks)} tracks ({len(missing_tracks)} missing).")

        if not spotify_uris:
            raise RuntimeError("[Sync] No tracks were matched on Spotify — aborting.")

        sync_spotify_playlist(sp, playlist_id, spotify_uris)

        save_state(state_file, {
            "mbid": mbid,
            "title": title,
            "playlist_start": datetime.now(timezone.utc).isoformat(),
            "track_cache": track_cache,
            "current_uris": spotify_uris,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        update_playlist_description(sp, playlist_id, len(spotify_uris), len(tracks), title)
        print(f"[Sync] Full sync complete — {len(spotify_uris)} tracks in playlist ✓")

    else:
        # ── SAME WEEK: remove scrobbled tracks ──────────────────────────
        track_cache = state.get("track_cache", [])
        current_uris = state.get("current_uris", [])
        playlist_start = state.get("playlist_start", "")

        if not track_cache or not current_uris:
            print("[Sync] No track cache found — skipping hourly check.")
            return

        if not playlist_start:
            print("[Sync] No playlist_start timestamp — skipping hourly check.")
            return

        # Fetch scrobbles since the playlist was synced
        scrobbled = fetch_scrobbles_since(playlist_start)

        # Find which current tracks have been scrobbled, in any spelling.
        to_remove: list[str] = []
        for uri in current_uris:
            entry = next((e for e in track_cache if e.get("uri") == uri), None)
            if entry and track_played(_cached_artists(entry), entry.get("title", ""), scrobbled):
                to_remove.append(uri)

        if not to_remove:
            print(f"[Sync] No scrobbled tracks to remove — {len(current_uris)} tracks remain ✓")
            update_playlist_description(sp, playlist_id, len(current_uris), len(track_cache), title)
            return

        # Remove scrobbled tracks from playlist
        remove_from_playlist(sp, playlist_id, to_remove)

        removed = set(to_remove)
        new_uris = [u for u in current_uris if u not in removed]

        save_state(state_file, {
            "mbid": mbid,
            "title": title,
            "playlist_start": playlist_start,
            "track_cache": track_cache,
            "current_uris": new_uris,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        update_playlist_description(sp, playlist_id, len(new_uris), len(track_cache), title)
        print(f"[Sync] Removed {len(to_remove)} scrobbled track(s) — {len(new_uris)} tracks remain ✓")


if __name__ == "__main__":
    main()
