"""
listenbrainz_sync.py
Syncs ListenBrainz "Weekly Exploration" playlist for a given user
to a target Spotify playlist.
"""

import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
import spotipy


# ── string normalization & matching helpers ──────────────────────────────────

def _normalise(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace, remove punctuation."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_track_metadata(artist: str, title: str) -> tuple[str, str]:
    """Remove common clutter like (Remastered 2011), feat. X, etc. for better search recall."""
    artist_clean = re.sub(r"(?i)\s*[\(\[]ft\.?|feat\.?.*[\)\]]", "", artist).strip()
    title_clean = re.sub(r"(?i)\s*[\(\[]ft\.?|feat\.?.*[\)\]]", "", title).strip()
    title_clean = re.sub(r"(?i)\s*[\(\[](remastered|deluxe|bonus|expanded|edition|live|version|single|radio edit).*[\)\]]", "", title_clean).strip()
    return artist_clean or artist, title_clean or title


# ── Spotify auth & retry helpers ─────────────────────────────────────────────

def get_spotify_client() -> spotipy.Spotify:
    """Exchange refresh token for access token and return Spotify client."""
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


# ── ListenBrainz API ─────────────────────────────────────────────────────────

def fetch_latest_weekly_exploration(username: str) -> tuple[str, str, list[dict]]:
    """Fetch the latest 'Weekly Exploration' playlist for *username*.

    Returns (playlist_mbid, playlist_title, track_list).
    """
    url = f"https://api.listenbrainz.org/1/user/{username}/playlists/createdfor"
    resp = requests.get(url, timeout=30)
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

    # Pick the top (most recent) Weekly Exploration playlist
    target_mbid, target_title = exploration_playlists[0]
    print(f"[ListenBrainz] Found latest playlist: '{target_title}' (MBID: {target_mbid})")

    # Fetch full track list
    detail_url = f"https://api.listenbrainz.org/1/playlist/{target_mbid}"
    detail_resp = requests.get(detail_url, timeout=30)
    detail_resp.raise_for_status()
    detail_data = detail_resp.json()

    playlist_obj = detail_data.get("playlist", {})
    tracks = playlist_obj.get("track", [])

    print(f"[ListenBrainz] Fetched {len(tracks)} tracks from ListenBrainz.")
    return target_mbid, target_title, tracks


# ── Spotify Track Matcher ───────────────────────────────────────────────────

def find_spotify_track_uri(sp: spotipy.Spotify, track_item: dict) -> str | None:
    """Find matching Spotify track URI using multi-tier matching strategy."""
    artist = track_item.get("creator", "").strip()
    title = track_item.get("title", "").strip()

    if not artist or not title:
        return None

    # Check identifiers for direct Spotify URI
    identifiers = track_item.get("identifier", [])
    for ident in identifiers:
        if isinstance(ident, str) and "spotify:track:" in ident:
            return ident
        if isinstance(ident, str) and "open.spotify.com/track/" in ident:
            track_id = ident.split("/track/")[-1].split("?")[0]
            return f"spotify:track:{track_id}"

    # Tier 1: Exact field query artist:"..." track:"..."
    query_exact = f'artist:"{artist}" track:"{title}"'
    res = spotify_retry(sp.search, q=query_exact, type="track", limit=3)
    items = res.get("tracks", {}).get("items", [])
    if items:
        return items[0]["uri"]

    # Tier 2: Cleaned field query
    clean_artist, clean_title = clean_track_metadata(artist, title)
    if clean_artist != artist or clean_title != title:
        query_clean_field = f'artist:"{clean_artist}" track:"{clean_title}"'
        res = spotify_retry(sp.search, q=query_clean_field, type="track", limit=3)
        items = res.get("tracks", {}).get("items", [])
        if items:
            return items[0]["uri"]

    # Tier 3: Loose keyword search
    query_loose = f"{clean_artist} {clean_title}"
    res = spotify_retry(sp.search, q=query_loose, type="track", limit=5)
    items = res.get("tracks", {}).get("items", [])
    if items:
        norm_clean_artist = _normalise(clean_artist)
        norm_clean_title = _normalise(clean_title)
        for cand in items:
            cand_artist = _normalise(cand["artists"][0]["name"])
            cand_title = _normalise(cand["name"])
            if norm_clean_artist in cand_artist or cand_artist in norm_clean_artist:
                if norm_clean_title in cand_title or cand_title in norm_clean_title:
                    return cand["uri"]
        return items[0]["uri"]

    return None


# ── State management ─────────────────────────────────────────────────────────

def load_state(state_file: str) -> dict:
    try:
        with open(state_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f)


# ── Main sync process ────────────────────────────────────────────────────────

def sync_spotify_playlist(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Wipe playlist and add tracks in chunks of 100."""
    spotify_retry(sp.playlist_replace_items, playlist_id, [])
    print(f"[Spotify] Cleared playlist {playlist_id}.")

    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        spotify_retry(sp.playlist_add_items, playlist_id, chunk)
        print(f"[Spotify] Added tracks {i + 1}–{i + len(chunk)} / {len(uris)}.")
        time.sleep(1.0)


def main() -> None:
    username = os.environ.get("LASTFM_USERNAME", "ikOzy")
    playlist_id = os.environ["SPOTIFY_LISTENBRAINZ_PLAYLIST_ID"]
    state_file = os.environ.get("LISTENBRAINZ_STATE_FILE", ".listenbrainz_state")

    # 1. Fetch latest Weekly Exploration playlist from ListenBrainz
    mbid, title, tracks = fetch_latest_weekly_exploration(username)

    state = load_state(state_file)
    last_mbid = state.get("mbid")

    if mbid == last_mbid:
        print(f"[ListenBrainz] Playlist '{title}' ({mbid}) has not changed since last run — skipping update ✓")
        return

    # 2. Match tracks to Spotify URIs
    sp = get_spotify_client()
    spotify_uris: list[str] = []
    matched_count = 0
    missing_tracks: list[str] = []

    print(f"[Spotify] Resolving {len(tracks)} tracks on Spotify …")
    for idx, track_item in enumerate(tracks, 1):
        artist = track_item.get("creator", "Unknown Artist")
        track_title = track_item.get("title", "Unknown Title")
        uri = find_spotify_track_uri(sp, track_item)

        if uri:
            spotify_uris.append(uri)
            matched_count += 1
        else:
            missing_tracks.append(f"{artist} - {track_title}")
            print(f"  ⚠️ [{idx}/{len(tracks)}] Could not find on Spotify: '{artist} - {track_title}'")
        time.sleep(0.1)  # Gentle search delay

    print(f"[Sync] Matched {matched_count}/{len(tracks)} tracks ({len(missing_tracks)} missing).")

    if not spotify_uris:
        raise RuntimeError("[Sync] No tracks were matched on Spotify — aborting playlist update.")

    # 3. Sync to Spotify Playlist
    sync_spotify_playlist(sp, playlist_id, spotify_uris)

    # 4. Save state
    save_state(state_file, {
        "mbid": mbid,
        "title": title,
        "matched": matched_count,
        "total": len(tracks),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"[Sync] Successfully updated Spotify playlist for '{title}' ✓")


if __name__ == "__main__":
    main()
