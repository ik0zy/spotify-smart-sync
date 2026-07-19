"""
playlist_sync.py
Syncs neglected Spotify tracks (Liked Songs not scrobbled in the last 30 days)
to a target Spotify playlist.
"""

import hashlib
import time
import os
import unicodedata
import re
from datetime import datetime, timedelta, timezone


import requests
import spotipy


# ── helpers ──────────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace, remove punctuation."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def standardise_track_key(artist: str, title: str) -> str:
    """Return a canonical 'artist - title' key for matching."""
    return f"{_normalise(artist)} - {_normalise(title)}"


def compute_hash(uris: list[str]) -> str:
    """Return a SHA-256 hex digest of the sorted URI list."""
    payload = "\n".join(sorted(uris)).encode()
    return hashlib.sha256(payload).hexdigest()


# ── Last.fm ──────────────────────────────────────────────────────────────────

def fetch_lastfm_scrobbles(days: int = 30) -> set[str]:
    """Return a set of standardised 'artist - title' keys scrobbled in the
    last *days* days.

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

        resp = requests.get(API_URL, params=params, timeout=30)

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
                scrobbled.add(standardise_track_key(artist, title))

        # Check pagination
        attrs = recent.get("@attr", {})
        total_pages = int(attrs.get("totalPages", 1))
        print(f"[Last.fm] Page {page}/{total_pages} — {len(scrobbled)} unique tracks so far.")

        if page >= total_pages:
            break
        page += 1

        # Be gentle on the API — 0.25s between pages
        time.sleep(0.25)

    print(f"[Last.fm] Fetched {len(scrobbled)} unique scrobbled tracks from the last {days} days.")
    return scrobbled


# ── Spotify auth (headless refresh-token flow) ──────────────────────────────

def get_spotify_client() -> spotipy.Spotify:
    """Exchange the refresh token for an access token and return a Spotify
    client – no browser interaction required."""

    client_id = os.environ["SPOTIFY_CLIENT_ID"]
    client_secret = os.environ["SPOTIFY_CLIENT_SECRET"]
    refresh_token = os.environ["SPOTIFY_REFRESH_TOKEN"]

    resp = requests.post(
        "https://accounts.spotify.com/api/token",
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

    return spotipy.Spotify(
        auth=access_token,
        retries=0,
        status_retries=0,
    )


# ── Spotify helpers ──────────────────────────────────────────────────────────

MAX_SPOTIFY_RETRIES = 3
MAX_SPOTIFY_RETRY_WAIT = 60  # seconds — fail fast rather than wait hours


def spotify_retry(func, *args, **kwargs):
    """Call a spotipy method with bounded retry on 429 rate limits.

    Retries up to MAX_SPOTIFY_RETRIES times, waiting at most
    MAX_SPOTIFY_RETRY_WAIT seconds per attempt.  Raises on any other
    error or when retries are exhausted.
    """
    for attempt in range(1, MAX_SPOTIFY_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except spotipy.exceptions.SpotifyException as exc:
            if exc.http_status != 429:
                raise  # not a rate limit — propagate immediately

            retry_after = int(exc.headers.get("Retry-After", MAX_SPOTIFY_RETRY_WAIT))
            if retry_after > MAX_SPOTIFY_RETRY_WAIT:
                raise RuntimeError(
                    f"[Spotify] Rate-limited with Retry-After={retry_after}s "
                    f"(exceeds {MAX_SPOTIFY_RETRY_WAIT}s cap) — aborting."
                ) from exc

            print(
                f"[Spotify] Rate-limited, waiting {retry_after}s "
                f"(attempt {attempt}/{MAX_SPOTIFY_RETRIES}) …"
            )
            time.sleep(retry_after)

    raise RuntimeError(
        f"[Spotify] Still rate-limited after {MAX_SPOTIFY_RETRIES} retries — aborting."
    )


def fetch_liked_songs(sp: spotipy.Spotify) -> list[dict]:
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
        time.sleep(0.2)  # Avoid rate limits from rapid sequential requests

    print(f"[Spotify] Fetched {len(liked)} Liked Songs.")
    return liked


def sync_playlist(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Wipe the target playlist and bulk-add *uris* 100 at a time."""

    # Clear the playlist
    spotify_retry(sp.playlist_replace_items, playlist_id, [])
    print(f"[Spotify] Cleared playlist {playlist_id}.")

    # Add in chunks of 100
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        spotify_retry(sp.playlist_add_items, playlist_id, chunk)
        print(f"[Spotify] Added tracks {i + 1}–{i + len(chunk)} / {len(uris)}.")
        time.sleep(0.5)  # Prevent rate limits during bulk additions


# ── state management ─────────────────────────────────────────────────────────

import json


def load_state(state_file: str) -> dict:
    """Load sync state (hash, URI list, last full-sync hash).

    Returns an empty dict on first run, corrupt file, or old format
    — which causes the caller to fall through to a full sync.
    """
    try:
        with open(state_file) as f:
            data = json.load(f)
            if isinstance(data, dict) and "hash" in data:
                return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return {}


def save_state(state_file: str, state: dict) -> None:
    """Persist sync state as JSON."""
    with open(state_file, "w") as f:
        json.dump(state, f)


# ── main ─────────────────────────────────────────────────────────────────────

def diff_sync_playlist(
    sp: spotipy.Spotify,
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


def main() -> None:
    playlist_id = os.environ["SPOTIFY_PLAYLIST_ID"]
    state_file = os.environ.get("STATE_FILE", ".sync_state")

    # UTC hour that triggers a full wipe-and-rebuild (restores Liked Songs
    # order).  Default 18 = midnight in UTC+6 (Bangladesh).
    full_sync_utc_hour = int(os.environ.get("FULL_SYNC_UTC_HOUR", "18"))

    # 1. Scrobbles from Last.fm
    scrobbled = fetch_lastfm_scrobbles(days=30)

    # 2. Liked Songs from Spotify
    sp = get_spotify_client()
    liked = fetch_liked_songs(sp)

    # 3. Filter: keep only tracks NOT scrobbled in the last 30 days
    unplayed_uris: list[str] = []
    for item in liked:
        track = item["track"]
        artist = track["artists"][0]["name"]
        title = track["name"]
        key = standardise_track_key(artist, title)
        if key not in scrobbled:
            unplayed_uris.append(track["uri"])

    print(f"[Sync] {len(unplayed_uris)} neglected tracks identified.")

    # 4. Load state and determine sync mode
    state = load_state(state_file)
    current_hash = compute_hash(unplayed_uris)
    previous_hash = state.get("hash")
    previous_uris = state.get("uris", [])
    last_full_sync_hash = state.get("last_full_sync_hash")

    is_midnight = datetime.now(timezone.utc).hour == full_sync_utc_hour

    # 5. Decide whether to skip
    if current_hash == previous_hash:
        # Track list unchanged.  Skip unless it's midnight and the order
        # hasn't been restored since the last full sync.
        if not is_midnight or current_hash == last_full_sync_hash:
            print("[Sync] No changes since last run — skipping playlist update ✓")
            return

    # 6. Sync — choose mode
    if is_midnight or not previous_uris:
        # Full sync: wipe and rebuild to maintain Liked Songs order.
        # Also used on first run / state migration (no previous URIs).
        mode = "first run" if not previous_uris else "midnight refresh"
        print(f"[Sync] Full sync ({mode}) …")
        sync_playlist(sp, playlist_id, unplayed_uris)
        save_state(state_file, {
            "hash": current_hash,
            "uris": unplayed_uris,
            "last_full_sync_hash": current_hash,
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
        })
        print("[Sync] Diff sync complete ✓")


if __name__ == "__main__":
    main()

