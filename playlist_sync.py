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

    return spotipy.Spotify(auth=access_token)


# ── Spotify helpers ──────────────────────────────────────────────────────────

def fetch_liked_songs(sp: spotipy.Spotify) -> list[dict]:
    """Return every track object from the user's Liked Songs."""

    liked: list[dict] = []
    offset = 0
    limit = 50  # Spotify max for this endpoint

    while True:
        results = sp.current_user_saved_tracks(limit=limit, offset=offset)
        items = results.get("items", [])
        if not items:
            break
        liked.extend(items)
        if results.get("next") is None:
            break
        offset += limit

    print(f"[Spotify] Fetched {len(liked)} Liked Songs.")
    return liked


def sync_playlist(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Wipe the target playlist and bulk-add *uris* 100 at a time."""

    # Clear the playlist
    sp.playlist_replace_items(playlist_id, [])
    print(f"[Spotify] Cleared playlist {playlist_id}.")

    # Add in chunks of 100
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        sp.playlist_add_items(playlist_id, chunk)
        print(f"[Spotify] Added tracks {i + 1}–{i + len(chunk)} / {len(uris)}.")


# ── state management ─────────────────────────────────────────────────────────

def load_previous_hash(state_file: str) -> str | None:
    """Read the saved hash from the state file, or None if missing."""
    try:
        with open(state_file) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def save_hash(state_file: str, digest: str) -> None:
    """Persist the current hash to the state file."""
    with open(state_file, "w") as f:
        f.write(digest)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    playlist_id = os.environ["SPOTIFY_PLAYLIST_ID"]
    state_file = os.environ.get("STATE_FILE", ".sync_state")

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

    # 4. Check if anything changed since last run
    current_hash = compute_hash(unplayed_uris)
    previous_hash = load_previous_hash(state_file)

    if current_hash == previous_hash:
        print("[Sync] No changes since last run — skipping playlist update ✓")
        return

    # 5. Sync to the target playlist
    sync_playlist(sp, playlist_id, unplayed_uris)
    save_hash(state_file, current_hash)
    print("[Sync] Playlist updated and state saved ✓")


if __name__ == "__main__":
    main()
