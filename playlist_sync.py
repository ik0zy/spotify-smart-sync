"""
playlist_sync.py
Syncs neglected Spotify tracks (Liked Songs not scrobbled in the last 30 days)
to a target Spotify playlist.
"""

import os
import time
from datetime import datetime, timedelta, timezone

from common import (
    compute_hash,
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
    "compute_hash",
    "fetch_lastfm_scrobbles",
    "fetch_liked_songs",
    "sync_playlist",
    "diff_sync_playlist",
    "update_playlist_description",
    "main",
]


# ── Last.fm ──────────────────────────────────────────────────────────────────

def fetch_lastfm_scrobbles(days: int = 30) -> set[str]:
    """Return a set of 'artist - title' keys scrobbled in the last *days* days.

    Every spelling variant of each scrobble is indexed, not just the
    canonical key, so a track described as 'A feat. B' on Last.fm and
    ['A', 'B'] on Spotify still matches.  See common.track_key_variants.

    Uses the Last.fm REST API directly (instead of pylast) so we have full
    control over pagination, rate-limit handling, and retry timeouts.
    pylast's internal retry logic silently waits hours on 429s, which was
    the root cause of 6-hour GitHub Actions timeouts.
    """

    api_key = os.environ["LASTFM_API_KEY"]
    username = os.environ["LASTFM_USERNAME"]

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = int(cutoff.timestamp())

    API_URL = "https://ws.audioscrobbler.com/2.0/"
    PER_PAGE = 200  # Last.fm default; max is 1000 but smaller = gentler
    MAX_RETRY_WAIT = 60  # seconds — fail fast rather than wait hours

    scrobbled: set[str] = set()
    page = 1

    MAX_RETRIES = 3  # don't loop forever on repeated 429s
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

        # Handle rate limiting with bounded retries
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", MAX_RETRY_WAIT))
            retries += 1
            if retry_after > MAX_RETRY_WAIT or retries > MAX_RETRIES:
                raise RuntimeError(
                    f"[Last.fm] Rate-limited (attempt {retries}, wait {retry_after}s) "
                    f"— aborting instead of blocking the CI job. Try again later."
                )
            print(f"[Last.fm] Rate-limited, waiting {retry_after}s (attempt {retries}/{MAX_RETRIES}) …")
            time.sleep(retry_after)
            continue  # retry the same page

        resp.raise_for_status()
        retries = 0  # reset on success
        data = resp.json()

        recent = data.get("recenttracks", {})
        tracks = recent.get("track", [])

        if not tracks:
            break

        for track in tracks:
            # Skip the "now playing" marker (it has no date)
            if "@attr" in track and track["@attr"].get("nowplaying") == "true":
                continue
            artist = track.get("artist", {}).get("#text", "")
            title = track.get("name", "")
            if artist and title:
                scrobbled.update(track_key_variants(artist, title))

        # Check pagination
        attrs = recent.get("@attr", {})
        total_pages = int(attrs.get("totalPages", 1))
        print(f"[Last.fm] Page {page}/{total_pages} — {len(scrobbled)} track keys so far.")

        if page >= total_pages:
            break
        page += 1

        # Be gentle on the API — 0.25s between pages
        time.sleep(0.25)

    print(f"[Last.fm] Fetched {len(scrobbled)} track keys from the last {days} days.")
    return scrobbled


# ── Spotify helpers ──────────────────────────────────────────────────────────

def fetch_liked_songs(sp) -> list[dict]:
    """Return every track object from the user's Liked Songs."""

    liked: list[dict] = []
    offset = 0
    limit = 50  # Spotify max for this endpoint

    while True:
        results = spotify_retry(sp.current_user_saved_tracks, limit=limit, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        liked.extend(items)
        if results.get("next") is None:
            break
        offset += limit
        time.sleep(0.5)  # Avoid rate limits from rapid sequential requests

    print(f"[Spotify] Fetched {len(liked)} Liked Songs.")
    return liked


def sync_playlist(sp, playlist_id: str, uris: list[str]) -> None:
    """Wipe the target playlist and bulk-add *uris* 100 at a time."""

    # Clear the playlist
    spotify_retry(sp.playlist_replace_items, playlist_id, [])
    print(f"[Spotify] Cleared playlist {playlist_id}.")

    # Add in chunks of 100
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        spotify_retry(sp.playlist_add_items, playlist_id, chunk)
        print(f"[Spotify] Added tracks {i + 1}–{i + len(chunk)} / {len(uris)}.")
        time.sleep(1.0)  # Prevent rate limits during bulk additions


def diff_sync_playlist(
    sp,
    playlist_id: str,
    old_uris: list[str],
    new_uris: list[str],
) -> None:
    """Apply only the add/remove delta between *old_uris* and *new_uris*.

    Compared to a full wipe-and-rebuild (28+ API calls for ~2 700 tracks),
    this typically needs just 1–2 calls for the handful of tracks that
    changed since the last run.
    """
    old_set = set(old_uris)
    new_set = set(new_uris)

    to_remove = list(old_set - new_set)
    to_add = list(new_set - old_set)

    if not to_remove and not to_add:
        print("[Sync] Diff sync — nothing to change.")
        return

    # Remove tracks in chunks of 100
    for i in range(0, len(to_remove), 100):
        chunk = to_remove[i : i + 100]
        spotify_retry(sp.playlist_remove_all_occurrences_of_items, playlist_id, chunk)
        print(f"[Spotify] Removed {len(chunk)} track(s).")
        time.sleep(0.5)

    # Add tracks in chunks of 100
    for i in range(0, len(to_add), 100):
        chunk = to_add[i : i + 100]
        spotify_retry(sp.playlist_add_items, playlist_id, chunk)
        print(f"[Spotify] Added {len(chunk)} track(s).")
        time.sleep(0.5)

    print(f"[Sync] Diff sync complete: −{len(to_remove)}, +{len(to_add)}.")


# ── cache helpers ────────────────────────────────────────────────────────────

def _liked_songs_to_cache(liked: list[dict]) -> list[dict]:
    """Extract the fields we need from the full Spotify response.

    Removed, local-only and otherwise unavailable tracks are skipped
    rather than raising — one bad track must not abort the whole daily
    rebuild.  The full artist list is kept so collaborations can be
    matched against Last.fm.
    """
    cache = []
    skipped = 0

    for item in liked:
        track = item.get("track") if isinstance(item, dict) else None
        if not isinstance(track, dict):
            skipped += 1
            continue

        uri = track.get("uri")
        artists = [
            a.get("name")
            for a in track.get("artists", [])
            if isinstance(a, dict) and a.get("name")
        ]
        if not uri or track.get("is_local") or not artists:
            skipped += 1
            continue

        cache.append({
            "uri": uri,
            "artists": artists,
            "title": track.get("name") or "",
        })

    if skipped:
        print(f"[Spotify] Skipped {skipped} removed/unavailable track(s).")
    return cache


def _entry_artists(entry: dict) -> list[str]:
    """Artist list for a cache entry, tolerating the older string format."""
    artists = entry.get("artists")
    if isinstance(artists, list) and artists:
        return artists
    single = entry.get("artist")
    return [single] if single else []


def _filter_unplayed(liked_cache: list[dict], scrobbled: set[str]) -> list[str]:
    """Return URIs of cached liked songs NOT in the scrobbled set."""
    unplayed: list[str] = []
    for entry in liked_cache:
        if not track_played(_entry_artists(entry), entry.get("title", ""), scrobbled):
            unplayed.append(entry["uri"])
    return unplayed


def update_playlist_description(
    sp,
    playlist_id: str,
    unplayed_count: int,
    total_liked_count: int,
    scrobbled_count: int,
    tz_offset_hours: int,
) -> None:
    """Update playlist description with detailed stats and precise local timestamp."""
    pct = (unplayed_count / total_liked_count * 100) if total_liked_count > 0 else 0
    time_str, tz_str = local_timestamp(tz_offset_hours)

    desc = (
        f"{unplayed_count:,} neglected tracks ({pct:.1f}% of {total_liked_count:,} Liked Songs) · "
        f"{scrobbled_count:,} scrobbled in last 30d · "
        f"Last synced: {time_str} ({tz_str})"
    )
    try:
        spotify_retry(sp.playlist_change_details, playlist_id, description=desc)
        print(f"[Spotify] Updated playlist description: '{desc}'")
    except Exception as exc:
        print(f"[Spotify] Note: Could not update playlist description ({exc})")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    playlist_id = os.environ["SPOTIFY_PLAYLIST_ID"]
    state_file = os.environ.get("STATE_FILE", ".sync_state")

    # Timezone offset in hours for daily full-sync reset (default 6 = UTC+6 Bangladesh).
    tz_offset_hours = int(os.environ.get("SYNC_TZ_OFFSET", "6"))
    target_tz = timezone(timedelta(hours=tz_offset_hours))
    current_date = datetime.now(target_tz).strftime("%Y-%m-%d")

    # 1. Load state and determine sync mode
    state = load_state(state_file)
    previous_uris = state.get("uris", [])
    previous_hash = state.get("hash")
    last_full_sync_hash = state.get("last_full_sync_hash")
    last_full_sync_date = state.get("last_full_sync_date", "")
    liked_cache = state.get("liked_songs_cache", [])

    is_first_run = not previous_uris or not liked_cache
    # Perform a full rebuild once per calendar day (on the first run of the new day)
    should_full_sync = is_first_run or (current_date != last_full_sync_date)

    # 2. Scrobbles from Last.fm (always needed — lightweight, ~1 API call)
    scrobbled = fetch_lastfm_scrobbles(days=30)

    # 3. Liked Songs — only fetch from Spotify on full sync (first run of the day).
    #    Hourly runs reuse the cached list (saves ~56 API calls).
    sp = get_spotify_client()

    if should_full_sync:
        liked = fetch_liked_songs(sp)
        liked_cache = _liked_songs_to_cache(liked)
        print(f"[Spotify] Cached {len(liked_cache)} Liked Songs for hourly reuse.")
    else:
        print(f"[Spotify] Using cached Liked Songs ({len(liked_cache)} tracks).")

    # 4. Filter: keep only tracks NOT scrobbled in the last 30 days
    unplayed_uris = _filter_unplayed(liked_cache, scrobbled)
    print(f"[Sync] {len(unplayed_uris)} neglected tracks identified.")

    # 5. Decide whether to skip
    current_hash = compute_hash(unplayed_uris)

    if current_hash == previous_hash:
        # Track list unchanged.  Skip unless we need a full rebuild for the new day
        # and haven't restored order yet.
        if not should_full_sync or current_hash == last_full_sync_hash:
            print("[Sync] No changes since last run — skipping playlist update ✓")
            if should_full_sync:
                save_state(state_file, {
                    "hash": current_hash,
                    "uris": previous_uris,
                    "last_full_sync_hash": last_full_sync_hash,
                    "last_full_sync_date": current_date,
                    "liked_songs_cache": liked_cache,
                })
            update_playlist_description(
                sp, playlist_id, len(unplayed_uris), len(liked_cache), len(scrobbled), tz_offset_hours
            )
            return

    # 6. Sync — choose mode
    if should_full_sync:
        # Full sync: wipe and rebuild to maintain Liked Songs order.
        mode = "first run" if is_first_run else f"daily refresh ({current_date})"
        print(f"[Sync] Full sync ({mode}) …")
        sync_playlist(sp, playlist_id, unplayed_uris)
        save_state(state_file, {
            "hash": current_hash,
            "uris": unplayed_uris,
            "last_full_sync_hash": current_hash,
            "last_full_sync_date": current_date,
            "liked_songs_cache": liked_cache,
        })
        print("[Sync] Full sync complete — playlist order matches Liked Songs ✓")
    else:
        # Diff sync: only add/remove changed tracks (fast, low API usage).
        print("[Sync] Diff sync (hourly update) …")
        diff_sync_playlist(sp, playlist_id, previous_uris, unplayed_uris)
        save_state(state_file, {
            "hash": current_hash,
            "uris": unplayed_uris,
            "last_full_sync_hash": last_full_sync_hash or "",
            "last_full_sync_date": last_full_sync_date,
            "liked_songs_cache": liked_cache,
        })
        print("[Sync] Diff sync complete ✓")

    update_playlist_description(
        sp, playlist_id, len(unplayed_uris), len(liked_cache), len(scrobbled), tz_offset_hours
    )


if __name__ == "__main__":
    main()
