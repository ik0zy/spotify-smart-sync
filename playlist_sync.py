"""
playlist_sync.py
Syncs neglected Spotify tracks (Liked Songs not scrobbled in the last 30 days)
to a target Spotify playlist.
"""

import os
import time
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

    # pylast handles pagination internally when limit=None
    tracks = user.get_recent_tracks(
        limit=None,
        time_from=cutoff_ts,
        now_playing=False,
    )

    scrobbled: set[str] = set()
    for item in tracks:
        artist = str(item.track.artist)
        title = str(item.track.title)
        scrobbled.add(standardise_track_key(artist, title))

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


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    playlist_id = os.environ["SPOTIFY_PLAYLIST_ID"]

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

    print(f"[Sync] {len(unplayed_uris)} neglected tracks to sync.")

    # 4. Sync to the target playlist
    sync_playlist(sp, playlist_id, unplayed_uris)
    print("[Sync] Done ✓")


if __name__ == "__main__":
    main()
