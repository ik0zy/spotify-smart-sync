"""
playlist_sync.py
Syncs neglected Spotify tracks (Liked Songs not scrobbled in the last 30 days)
to a target Spotify playlist.
"""

import hashlib
import os
import unicodedata
import re
from datetime import datetime, timedelta, timezone

import pylast
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
    last *days* days."""

    api_key = os.environ["LASTFM_API_KEY"]
    api_secret = os.environ["LASTFM_API_SECRET"]
    username = os.environ["LASTFM_USERNAME"]

    network = pylast.LastFMNetwork(
        api_key=api_key,
        api_secret=api_secret,
    )

    user = network.get_user(username)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = int(cutoff.timestamp())

    # Last.fm's API accepts limit in the range 1-1000.  We paginate
    # manually using time_to so we never rely on pylast's unbounded
    # limit=None (which caused 6-hour GitHub Actions timeouts).
    PAGE_LIMIT = 1000
    scrobbled: set[str] = set()
    time_to = int(datetime.now(timezone.utc).timestamp())

    while True:
        tracks = user.get_recent_tracks(
            limit=PAGE_LIMIT,
            time_from=cutoff_ts,
            time_to=time_to,
            now_playing=False,
        )

        if not tracks:
            break

        for item in tracks:
            artist = str(item.track.artist)
            title = str(item.track.title)
            scrobbled.add(standardise_track_key(artist, title))

        # If we got fewer than PAGE_LIMIT results, we've exhausted the window
        if len(tracks) < PAGE_LIMIT:
            break

        # Move the window: use the oldest track's timestamp minus 1 second
        # to avoid re-fetching the same boundary track.
        time_to = int(tracks[-1].timestamp) - 1

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
