"""
common.py
Shared helpers for the Smart Spotify Sync scripts.

Holds everything that used to be copy-pasted across playlist_sync.py,
listenbrainz_sync.py and backup_library.py: string normalisation, HTTP
retry policy, Spotify auth, state persistence, and track matching.
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


# ── string normalisation ─────────────────────────────────────────────────────

def normalise(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace, remove punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def standardise_track_key(artist: str, title: str) -> str:
    """Return a canonical 'artist - title' key for matching.

    This is the single-canonical-key form.  Prefer
    :func:`track_key_variants` for matching, which also tolerates
    differences in how a track is described by each provider.
    """
    return f"{normalise(artist)} - {normalise(title)}"


# ── track matching ───────────────────────────────────────────────────────────

# Words that mark a parenthetical/bracketed section or a title suffix as
# metadata rather than part of the song's identity.
_NOISE_WORD = (
    r"(?:remaster(?:ed)?|re-?master|deluxe|bonus|expanded|anniversary|"
    r"edition|version|live|single|radio\s*edit|mono|stereo|acoustic|"
    r"instrumental|demo|mix(?:ed)?|re-?mix|edit|feat\.?|featuring|ft\.?|with)"
)
_NOISE_GROUP_RE = re.compile(
    rf"(?i)\s*[\(\[][^)\]]*\b{_NOISE_WORD}\b[^)\]]*[\)\]]"
)
_NOISE_SUFFIX_RE = re.compile(rf"(?i)\s+[-–—]\s+[^-–—]*{_NOISE_WORD}[^-–—]*$")
# Any bracketed section, but only one preceded by whitespace — a leading
# bracket is part of the title ("(I Can't Get No) Satisfaction").
_ANY_GROUP_RE = re.compile(r"(?i)\s+[\(\[][^)\]]*[\)\]]")
_FEATURE_RE = re.compile(r"(?i)^(.*?)\s*[\(\[]?\s*(?:feat\.?|featuring|ft\.?)\s")


def title_variants(title: str) -> list[str]:
    """Return the raw title plus de-noised forms of it.

    Spotify frequently carries '(feat. X)', '(Remastered 2011)' or
    '- Single Version' while Last.fm reports the plain name.  Emitting
    both sides of that comparison is what makes cross-provider matching
    reliable.  Non-noise subtitles such as '(Imagine That)' are dropped
    by the third variant.
    """
    out = [title]

    stripped = _NOISE_GROUP_RE.sub("", title or "")
    stripped = _NOISE_SUFFIX_RE.sub("", stripped).strip()
    if stripped and stripped != title:
        out.append(stripped)

    bare = _ANY_GROUP_RE.sub("", title or "").strip()
    if bare and bare not in out:
        out.append(bare)

    return out


def artist_variants(artists: list[str] | str) -> list[str]:
    """Return candidate artist strings for matching.

    Accepts either a Spotify artist list or a single Last.fm artist
    string.  For a list, the full comma-join and the lead artist are
    both offered, because Last.fm writes collaborations either way
    ('A, B' on some responses, 'A feat. B' on others).  For a string,
    the portion before a 'feat.' marker and the portion before the
    first comma are added.
    """
    if isinstance(artists, str):
        parts = [artists]
    else:
        parts = [a for a in (artists or []) if a]

    if not parts:
        return []

    out = [", ".join(parts)]

    # Last.fm: 'Main Artist feat. Someone' -> 'Main Artist'
    single = parts[0]
    feature_match = _FEATURE_RE.match(single)
    if feature_match:
        main = feature_match.group(1).strip(" ,;-")
        if main and main != single:
            out.append(main)

    # A list longer than one entry, or a comma-joined string: offer the
    # lead artist on its own so 'A feat. B' style scrobbles still match.
    if len(parts) > 1:
        out.append(parts[0])
    elif "," in single and "&" not in single:
        out.append(single.split(",")[0].strip())

    return list(dict.fromkeys(out))


def track_key_variants(artists: list[str] | str, title: str) -> set[str]:
    """Return every 'artist - title' key this track may be known by.

    Applied to *both* the Spotify side and the Last.fm side, so a track
    counts as played when any of its keys appears in the scrobbled set.
    Matching is deliberately permissive: a false positive drops a track
    from the neglected playlist slightly early, whereas a false negative
    is the bug this replaces -- the track never leaves at all.
    """
    return {
        f"{normalise(artist)} - {normalise(title_variant)}"
        for artist in artist_variants(artists)
        for title_variant in title_variants(title or "")
    }


def track_played(artists: list[str] | str, title: str, scrobbled: set[str]) -> bool:
    """True when any variant of this track is in the scrobbled key set."""
    return bool(track_key_variants(artists, title) & scrobbled)


# ── HTTP retry policy ────────────────────────────────────────────────────────

def requests_retry(
    url: str,
    params: dict | None = None,
    data: dict | None = None,
    method: str = "GET",
    timeout: int = 30,
    max_retries: int = 3,
    expect_json: bool = False,
) -> requests.Response:
    """Execute an HTTP request with automatic retries on timeouts,
    connection errors, 5xx server errors, and empty/invalid JSON responses."""
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

            resp.raise_for_status()

            if expect_json:
                _ = resp.json()

            return resp
        except (requests.exceptions.RequestException, requests.exceptions.Timeout, json.JSONDecodeError, ValueError) as exc:
            if attempt >= max_retries:
                raise
            wait = 2 * attempt
            print(
                f"[Network] Request timeout/error ({exc.__class__.__name__}), "
                f"retrying in {wait}s ({attempt}/{max_retries}) …"
            )
            time.sleep(wait)
    raise RuntimeError(f"HTTP request failed after {max_retries} attempts.")


# ── Spotify auth & rate limits ───────────────────────────────────────────────

def get_spotify_client() -> spotipy.Spotify:
    """Exchange the refresh token for an access token and return a
    Spotify client with 30s timeout. No browser interaction required."""
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
        expect_json=True,
    )
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    # Retries are handled by spotify_retry() with bounded backoff,
    # and requests_timeout is raised from default 5s to 30s to prevent transient ReadTimeouts.
    return spotipy.Spotify(auth=access_token, requests_timeout=30, retries=0, status_retries=0)


MAX_SPOTIFY_RETRIES = 3
MAX_SPOTIFY_RETRY_WAIT = 60  # seconds — fail fast rather than wait hours


def _retry_after_seconds(headers: dict | None, default: int) -> int:
    """Parse a Retry-After header, tolerating both delay and HTTP-date
    forms and falling back to *default* when unparseable."""
    if not headers:
        return default
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(str(raw))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return default


def spotify_retry(func, *args, **kwargs):
    """Call a spotipy method with bounded retry on 429 rate limits and transient network timeouts.

    Retries up to MAX_SPOTIFY_RETRIES times, waiting at most
    MAX_SPOTIFY_RETRY_WAIT seconds per attempt.  Any other error, or
    exhausted retries, raises.
    """
    for attempt in range(1, MAX_SPOTIFY_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException as exc:
            if attempt >= MAX_SPOTIFY_RETRIES:
                raise
            wait = 2 * attempt
            print(
                f"[Spotify] Network timeout/error ({exc.__class__.__name__}), "
                f"retrying in {wait}s ({attempt}/{MAX_SPOTIFY_RETRIES}) …"
            )
            time.sleep(wait)
        except spotipy.exceptions.SpotifyException as exc:
            if exc.http_status != 429:
                raise  # not a rate limit — propagate immediately

            retry_after = _retry_after_seconds(exc.headers, MAX_SPOTIFY_RETRY_WAIT)
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


# ── hashing ──────────────────────────────────────────────────────────────────

def compute_hash(uris: list[str]) -> str:
    """Return a SHA-256 hex digest of the sorted URI list."""
    payload = "\n".join(sorted(uris)).encode()
    return hashlib.sha256(payload).hexdigest()


# ── state persistence ────────────────────────────────────────────────────────

def load_state(state_file: str, require_key: str = "hash") -> dict:
    """Load sync state.

    Returns an empty dict on first run, corrupt file, or old format,
    which causes the caller to fall through to a full sync.  *require_key*
    is the field that marks the file as this script's state — the
    playlist state is keyed on 'hash', the ListenBrainz state on 'mbid'.
    """
    try:
        with open(state_file) as f:
            data = json.load(f)
            if isinstance(data, dict) and require_key in data:
                return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return {}


def save_state(state_file: str, state: dict) -> None:
    """Persist sync state as JSON."""
    with open(state_file, "w") as f:
        json.dump(state, f)


# ── playlist description timestamps ──────────────────────────────────────────

def local_timestamp(tz_offset_hours: int) -> tuple[str, str]:
    """Return (formatted local time, 'UTC+6'-style label)."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    now = datetime.now(tz)
    sign = "+" if tz_offset_hours >= 0 else "-"
    return now.strftime("%d %b %Y, %I:%M %p"), f"UTC{sign}{abs(tz_offset_hours)}"
