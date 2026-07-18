"""
Quick diagnostic: test Last.fm API rate limiting.
Usage:
    export LASTFM_API_KEY="your_key"
    export LASTFM_USERNAME="your_username"
    python test_lastfm.py
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

api_key = os.environ.get("LASTFM_API_KEY")
username = os.environ.get("LASTFM_USERNAME")

if not api_key or not username:
    print("Set LASTFM_API_KEY and LASTFM_USERNAME environment variables.")
    sys.exit(1)

API_URL = "https://ws.audioscrobbler.com/2.0/"
cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())

print(f"Testing Last.fm API for user '{username}', last 30 days...\n")

page = 1
total_tracks = 0

while True:
    params = {
        "method": "user.getrecenttracks",
        "user": username,
        "api_key": api_key,
        "format": "json",
        "limit": 200,
        "from": cutoff_ts,
        "page": page,
        "extended": 0,
    }

    start = time.time()
    resp = requests.get(API_URL, params=params, timeout=30)
    elapsed = time.time() - start

    print(f"  Page {page}: HTTP {resp.status_code} ({elapsed:.2f}s)", end="")

    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "unknown")
        print(f"  ⚠ RATE LIMITED — Retry-After: {retry_after}s")
        print("\n❌ Last.fm API is rate-limiting your key. Wait and retry later.")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"  ⚠ Unexpected status: {resp.text[:200]}")
        sys.exit(1)

    data = resp.json()
    recent = data.get("recenttracks", {})
    tracks = recent.get("track", [])
    attrs = recent.get("@attr", {})
    total_pages = int(attrs.get("totalPages", 1))
    total = int(attrs.get("total", 0))

    # Count non-nowplaying tracks
    real_tracks = [t for t in tracks if not ("@attr" in t and t["@attr"].get("nowplaying") == "true")]
    total_tracks += len(real_tracks)

    print(f"  — {len(real_tracks)} tracks (page {page}/{total_pages}, {total} total scrobbles)")

    if page >= total_pages:
        break
    page += 1
    time.sleep(0.25)

print(f"\n✅ Done! Fetched {total_tracks} tracks across {page} pages.")
print(f"   Total scrobbles in last 30 days: {total}")
