# Spotify Smart Sync

Automated Spotify playlist management powered by **Last.fm** scrobble data and **ListenBrainz** recommendations. Runs entirely on **GitHub Actions** — no server required.

## What It Does

### 1. Neglected Tracks Playlist (`playlist_sync.py`)

Automatically maintains a Spotify playlist of your **Liked Songs that you haven't listened to** in the last 30 days.

**How it works:**
- **Hourly**: Fetches your recent Last.fm scrobbles, compares against cached Liked Songs, and removes/adds tracks via lightweight diff sync (~3 API calls)
- **Daily** (first run after midnight in your timezone): Re-fetches all Liked Songs from Spotify, performs a full playlist rebuild to restore your Liked Songs order
- Tracks return to the playlist automatically when they haven't been scrobbled for 30 days

### 2. Weekly Exploration Playlist (`listenbrainz_sync.py`)

Syncs your **ListenBrainz Weekly Exploration** playlist to Spotify — and removes tracks as you listen to them.

**How it works:**
- **Monday** (new playlist detected): Resolves all 50 ListenBrainz tracks on Spotify and populates the playlist
- **Hourly** (rest of the week): Checks Last.fm scrobbles since the playlist was created and removes any tracks you've already played
- The playlist acts as a **to-listen queue** that shrinks throughout the week
- Next Monday: new playlist drops → wipes and starts fresh

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Last.fm API │     │ ListenBrainz │     │ Spotify API  │
│  (scrobbles) │     │  (playlists) │     │  (playlists) │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       └────────┬───────────┘                    │
                │                                │
       ┌────────▼────────┐              ┌────────▼────────┐
       │ GitHub Actions  │──── sync ───▶│ Your Spotify    │
       │ (hourly cron)   │              │ Playlists       │
       └────────┬────────┘              └─────────────────┘
                │
       ┌────────▼────────┐
       │ State Cache     │
       │ (Actions Cache) │
       └─────────────────┘
```

## Setup

### Prerequisites

- A [Spotify Developer App](https://developer.spotify.com/dashboard) with a refresh token
- A [Last.fm API account](https://www.last.fm/api/account/create)
- A [ListenBrainz account](https://listenbrainz.org/) with scrobbling enabled
- Target Spotify playlists created manually

### 1. Fork / Clone

```bash
git clone https://github.com/ik0zy/spotify-smart-sync.git
cd spotify-smart-sync
```

### 2. Configure GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|---|---|
| `SPOTIFY_CLIENT_ID` | From your Spotify Developer App |
| `SPOTIFY_CLIENT_SECRET` | From your Spotify Developer App |
| `SPOTIFY_REFRESH_TOKEN` | OAuth refresh token ([guide](https://developer.spotify.com/documentation/web-api/tutorials/code-flow)) |
| `SPOTIFY_PLAYLIST_ID` | Target playlist ID for neglected tracks |
| `SPOTIFY_LISTENBRAINZ_PLAYLIST_ID` | Target playlist ID for Weekly Exploration |
| `LASTFM_API_KEY` | From your Last.fm API account |
| `LASTFM_API_SECRET` | From your Last.fm API account |
| `LASTFM_USERNAME` | Your Last.fm username |

### 3. Configure Environment Variables

In `.github/workflows/playlist-sync.yml`:

| Variable | Default | Description |
|---|---|---|
| `SYNC_TZ_OFFSET` | `6` | UTC offset for daily full-sync reset (6 = UTC+6 Bangladesh) |

In `.github/workflows/listenbrainz-sync.yml`:

| Variable | Default | Description |
|---|---|---|
| `LISTENBRAINZ_USERNAME` | `ikOzy` | Your ListenBrainz username |

### 4. Get a Spotify Refresh Token

```bash
# 1. Create a Spotify app at https://developer.spotify.com/dashboard
#    Set redirect URI to: http://localhost:8888/callback
#
# 2. Run a one-time auth flow to get a refresh token:
python3 -c "
import spotipy
from spotipy.oauth2 import SpotifyOAuth

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id='YOUR_CLIENT_ID',
    client_secret='YOUR_CLIENT_SECRET',
    redirect_uri='http://localhost:8888/callback',
    scope='playlist-modify-public playlist-modify-private user-library-read'
))
# This opens a browser. After authorizing, check .cache for the refresh_token
"
```

### 5. Enable Workflows

Workflows run automatically after pushing to `master`. You can also trigger manually:

```bash
gh workflow run playlist-sync.yml
gh workflow run listenbrainz-sync.yml
```

## Rate Limit Resilience

Both scripts are heavily optimized to avoid Spotify's aggressive rate limits:

| Optimization | Detail |
|---|---|
| **Liked Songs caching** | Fetched once/day, cached in state for 23 hourly runs |
| **Diff sync** | Hourly runs only add/remove deltas (~1-3 API calls) |
| **Bounded retries** | Max 3 retries, max 60s wait per retry — fails fast instead of hanging |
| **Throttle delays** | 0.5s between Liked Songs pages, 1.0s between bulk playlist adds |
| **State hashing** | Skips playlist update entirely when nothing has changed |

**API usage breakdown:**

| Workflow | Hourly Run | Daily Full Sync |
|---|---|---|
| Neglected Tracks | ~3 calls | ~84 calls (once/day) |
| Weekly Exploration | ~1-3 calls | ~52 calls (once/week) |

## File Structure

```
├── playlist_sync.py                      # Neglected tracks sync logic
├── listenbrainz_sync.py                  # Weekly Exploration sync logic
├── requirements.txt                      # Python dependencies
├── test_lastfm.py                        # Last.fm API test script
└── .github/workflows/
    ├── playlist-sync.yml                 # Hourly cron + daily full sync
    └── listenbrainz-sync.yml             # Hourly cron + weekly full sync
```

## State Files

State is persisted between runs via GitHub Actions Cache:

| File | Contents |
|---|---|
| `.sync_state` | Hash, URI list, liked songs cache, last full sync date |
| `.listenbrainz_state` | Playlist MBID, track cache with artist/title metadata, current URIs |

## Local Development

```bash
pip install -r requirements.txt

# Set environment variables
export SPOTIFY_CLIENT_ID="..."
export SPOTIFY_CLIENT_SECRET="..."
export SPOTIFY_REFRESH_TOKEN="..."
export SPOTIFY_PLAYLIST_ID="..."
export LASTFM_API_KEY="..."
export LASTFM_USERNAME="..."

# Run manually
python playlist_sync.py
python listenbrainz_sync.py
```

## License

MIT
