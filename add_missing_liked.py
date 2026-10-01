"""
add_missing_liked.py
Check specified tracks against live Spotify Liked Songs,
search Spotify for missing tracks, and attempt to add them to Liked Songs.
"""

import json
import os
import re
import sys
import time
import spotipy
from common import (
    get_spotify_client,
    requests_retry,
    spotify_retry,
    normalise,
    track_key_variants,
)

TARGET_SONGS = [
    {"title": "Passionfruit AG Remix AI", "artist": "JD Style", "album": "", "timestamp": "2025-11-13T14:24:24.843Z"},
    {"title": "Passionfruit", "artist": "Drake", "album": "More Life", "timestamp": "2025-11-13T14:24:37.608Z"},
    {"title": "Wicked Games", "artist": "The Weeknd", "album": "The Highlights (Deluxe Video Album)", "timestamp": "2025-11-13T14:31:04.086Z"},
    {"title": "Radio", "artist": "Bershy", "album": "Radio - Single", "timestamp": "2025-11-13T14:38:44.053Z"},
    {"title": "Can't Let Go", "artist": "Djvi", "album": "Can't Let Go - Single", "timestamp": "2025-12-01T14:30:09.324Z"},
    {"title": "Turn Me On (feat. Nicki Minaj)", "artist": "David Guetta", "album": "Nothing But the Beat", "timestamp": "2025-12-25T03:37:01.631Z"},
    {"title": "Goonies Never Say Day!", "artist": "Set Your Goals", "album": "The Reset Demo 10 Year Anniversary Edition - EP", "timestamp": "2025-12-25T03:44:47.729Z"},
    {"title": "I See Right Through To You", "artist": "sienna sleep", "album": "I See Right Through To You", "timestamp": "2026-01-16T03:06:27.008Z"},
    {"title": "Hearts Stay Unchanged (instrumental)", "artist": "Yamato Kasai", "album": "ENDER MAGNOLIA: Bloom in the Mist Original Soundtrack", "timestamp": "2026-01-23T13:13:54.275Z"},
    {"title": "Marechià (feat. Celia Kameni)", "artist": "Nu Genea", "album": "Bar Mediterraneo", "timestamp": "2026-03-14T17:19:13.152Z"},
    {"title": "Man On the Run (feat. Cerf, Mitiska & Jaren) [Original Vocal Mix]", "artist": "Dash Berlin", "album": "Man on the Run (feat. Jaren, Cerf & Mitiska)", "timestamp": "2026-03-18T17:15:26.386Z"},
    {"title": "As The Rush Comes", "artist": "Motorcycle", "album": "As The Rush Comes (Collected, Pt. 1)", "timestamp": "2026-03-18T17:17:25.329Z"},
    {"title": "Like a Prayer (Choir Version From “Deadpool & Wolverine”)", "artist": "I'll Take You There Choir", "album": "Deadpool & Wolverine: Madonna's Like a Prayer - EP", "timestamp": "2026-03-20T04:42:55.475Z"},
    {"title": "Walk On By", "artist": "Burt Bacharach", "album": "Hit Maker! (Expanded Edition)", "timestamp": "2026-03-30T04:16:52.780Z"},
    {"title": "Cry for You", "artist": "September", "album": "In Orbit", "timestamp": "2026-03-30T04:18:13.282Z"},
    {"title": "Deffro", "artist": "Tokomololo", "album": "Deffro - Single", "timestamp": "2026-04-10T13:45:09.679Z"},
    {"title": "Broken Clocks", "artist": "Minecraft", "album": "Minecraft: Chase the Skies (Original Game Soundtrack)", "timestamp": "2026-04-23T14:14:15.418Z"},
    {"title": "Eternal Flame", "artist": "The Bangles", "album": "Everything", "timestamp": "2026-05-04T12:26:48.281Z"},
    {"title": "Oui mais Non", "artist": "SMLY", "album": "Oui mais Non - Single", "timestamp": "2026-05-09T08:20:26.934Z"},
    {"title": "All The Stars", "artist": "Kendrick Lamar & SZA", "album": "Black Panther: The Album", "timestamp": "2026-05-10T15:01:56.754Z"},
    {"title": "Sad", "artist": "Mohammed Daji Fire", "album": "", "timestamp": "2026-05-18T14:56:32.009Z"},
    {"title": "Talk To You (feat. 54 Ultra)", "artist": "ANOTR", "album": "Talk To You (feat. 54 Ultra) - Single", "timestamp": "2026-05-20T13:19:51.645Z"},
    {"title": "Satellite (Original Above & Beyond Mix)", "artist": "OceanLab & Above & Beyond", "album": "Sirens Of The Sea Remixed (Bonus Track Version)", "timestamp": "2026-05-25T17:48:08.481Z"},
    {"title": "FOREVER ROLLING", "artist": "¥$, Kanye West & Ty Dolla $ign", "album": "VULTURES 2", "timestamp": "2026-06-02T04:35:25.480Z"},
    {"title": "Beautiful Lies", "artist": "B-Complex", "album": "Beautiful Lies - EP", "timestamp": "2026-06-06T15:39:46.772Z"},
    {"title": "golden hour (Version Française)", "artist": "JVKE & Blond", "album": "golden hour (Version Française) - Single", "timestamp": "2026-08-01T14:14:44.674Z"},
    {"title": "Heartless", "artist": "Kanye West", "album": "808s & Heartbreak", "timestamp": "2026-08-11T16:12:56.613Z"},
    {"title": "Omen", "artist": "The Prodigy", "album": "Invaders Must Die", "timestamp": "2026-08-26T03:36:39.480Z"},
    {"title": "Rehab", "artist": "Rihanna", "album": "Good Girl Gone Bad", "timestamp": "2026-08-27T16:10:53.024Z"},
    {"title": "Aria", "artist": "Argy & Omnya", "album": "New World", "timestamp": "2026-09-17T15:01:21.991Z"},
    {"title": "Dreamscape (Remastered)", "artist": "009 Sound System", "album": "The Hits", "timestamp": "2026-10-01T17:10:29.272Z"},
    {"title": "Epiphany", "artist": "TwoThirds", "album": "Epiphany (feat. Veela & Feint) - Single", "timestamp": "2027-10-01T17:11:38.839Z"},
]


def check_token_scope() -> tuple[spotipy.Spotify, str]:
    """Get Spotify client and inspect scopes."""
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
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    granted_scopes = data.get("scope", "")
    print(f"[Auth] Granted token scopes: {granted_scopes}")

    sp = spotipy.Spotify(auth=token, retries=0, status_retries=0)
    return sp, granted_scopes


def fetch_all_liked_songs(sp: spotipy.Spotify) -> list[dict]:
    """Fetch complete current list of saved tracks."""
    print("[Spotify] Fetching live Liked Songs …")
    liked = []
    offset = 0
    limit = 50

    while True:
        res = spotify_retry(sp.current_user_saved_tracks, limit=limit, offset=offset)
        items = res.get("items", [])
        if not items:
            break
        for item in items:
            track = item.get("track")
            if track:
                artists = [a.get("name", "") for a in track.get("artists", [])]
                liked.append({
                    "id": track.get("id"),
                    "uri": track.get("uri"),
                    "title": track.get("name"),
                    "artist": ", ".join(artists),
                    "artists": artists,
                    "album": track.get("album", {}).get("name"),
                    "added_at": item.get("added_at"),
                })
        if res.get("next") is None:
            break
        offset += limit
        time.sleep(0.3)

    print(f"[Spotify] Total current Liked Songs: {len(liked)}")
    return liked


def search_track(sp: spotipy.Spotify, title: str, artist: str) -> dict | None:
    """Search Spotify for the best matching track."""
    queries = [
        f"track:{title} artist:{artist}",
        f"{title} {artist}",
    ]
    # Strip parenthetical annotations if needed
    clean_title = re.sub(r"[\(\[][^)\]]*[\)\]]", "", title).strip()
    if clean_title != title:
        queries.append(f"{clean_title} {artist}")

    for q in queries:
        try:
            results = spotify_retry(sp.search, q=q, type="track", limit=5)
            items = results.get("tracks", {}).get("items", [])
            if items:
                best = items[0]
                return {
                    "id": best.get("id"),
                    "uri": best.get("uri"),
                    "title": best.get("name"),
                    "artist": ", ".join(a.get("name", "") for a in best.get("artists", [])),
                    "url": best.get("external_urls", {}).get("spotify"),
                }
        except Exception as exc:
            print(f"  [Search Error] q='{q}': {exc}")
        time.sleep(0.3)

    return None


def main():
    sp, scopes = check_token_scope()
    can_modify = "user-library-modify" in scopes

    liked = fetch_all_liked_songs(sp)

    found = []
    missing = []

    for target in TARGET_SONGS:
        t_title = target["title"]
        t_artist = target["artist"]
        query_keys = track_key_variants(t_artist, t_title)

        matched = None
        for item in liked:
            item_keys = track_key_variants(item.get("artists") or item.get("artist"), item.get("title"))
            if query_keys & item_keys:
                matched = item
                break

            nt_q = normalise(t_title)
            nt_i = normalise(item.get("title", ""))
            na_q = normalise(t_artist)
            na_i = normalise(item.get("artist", ""))

            if nt_q and nt_i and (nt_q == nt_i or nt_q in nt_i or nt_i in nt_q):
                if (na_q in na_i or na_i in na_q or
                    any(normalise(a) in na_q or na_q in normalise(a) for a in item.get("artists", [])) or
                    any(normalise(w) in na_i for w in t_artist.split() if len(w) > 3)):
                    matched = item
                    break

        if matched:
            found.append((target, matched))
        else:
            missing.append(target)

    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(found)} ALREADY IN LIKED SONGS, {len(missing)} NOT IN LIKED SONGS")
    print("=" * 60)

    print(f"\n--- ALREADY IN LIKED SONGS ({len(found)}) ---")
    for t, m in found:
        print(f"✓ \"{t['title']}\" by {t['artist']} ==> \"{m['title']}\" by {m['artist']} (Added: {m.get('added_at')})")

    print(f"\n--- NOT IN LIKED SONGS ({len(missing)}) ---")
    to_add_uris = []
    search_results = []

    for t in missing:
        match = search_track(sp, t["title"], t["artist"])
        if match:
            search_results.append((t, match))
            to_add_uris.append(match["uri"])
            print(f"🔍 Found on Spotify: \"{t['title']}\" by {t['artist']} -> \"{match['title']}\" by {match['artist']} ({match['url']})")
        else:
            search_results.append((t, None))
            print(f"⚠️ Could NOT find on Spotify: \"{t['title']}\" by {t['artist']}")

    # Save detailed report to json
    report = {
        "found_count": len(found),
        "missing_count": len(missing),
        "found": [{"target": t, "matched": m} for t, m in found],
        "missing": [
            {"target": t, "spotify_match": m} for t, m in search_results
        ],
        "token_can_modify": can_modify,
    }
    with open("liked_songs_check_result.json", "w") as f:
        json.dump(report, f, indent=2)

    # Attempt to add if scope allows
    if can_modify:
        valid_uris = [m["uri"] for t, m in search_results if m]
        if valid_uris:
            print(f"\n[Spotify] Adding {len(valid_uris)} tracks to Liked Songs …")
            for i in range(0, len(valid_uris), 50):
                chunk = valid_uris[i : i + 50]
                spotify_retry(sp.current_user_saved_tracks_add, tracks=chunk)
                print(f"  Added {len(chunk)} tracks.")
            print("[Spotify] Successfully added all missing tracks to Liked Songs! ✓")
    else:
        print("\n[Spotify] NOTE: The current Spotify refresh token does NOT have the 'user-library-modify' scope.")
        print("[Spotify] Direct Spotify links have been generated for all missing tracks.")


if __name__ == "__main__":
    main()
