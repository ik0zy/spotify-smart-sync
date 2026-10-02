"""Tests for the three sync scripts.

These use fake Spotify clients — no network, no credentials.
"""

import pytest

import backup_library
import listenbrainz_sync
import playlist_sync
from common import track_key_variants


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The scripts throttle between API calls; tests don't need to wait."""
    for module in (playlist_sync, listenbrainz_sync, backup_library):
        monkeypatch.setattr(module.time, "sleep", lambda *_: None)


# ── playlist_sync: _liked_songs_to_cache ─────────────────────────────────────

def test_cache_skips_removed_and_unavailable_tracks():
    """Regression: item['track'] is None for removed or region-blocked
    tracks, which used to raise TypeError and abort the daily rebuild."""
    liked = [
        {"track": {"uri": "spotify:track:OK", "name": "Song", "artists": [{"name": "Solo"}]}},
        {"track": None},
        {"track": {"uri": "spotify:track:NOART", "name": "X", "artists": []}},
        {"track": {"uri": None, "name": "NoURI", "artists": [{"name": "X"}]}},
        {"track": {"uri": "spotify:track:L", "name": "Local", "is_local": True,
                   "artists": [{"name": "X"}]}},
        {},
    ]
    cache = playlist_sync._liked_songs_to_cache(liked)
    assert cache == [{"uri": "spotify:track:OK", "artists": ["Solo"], "title": "Song"}]


def test_cache_keeps_every_artist():
    """Regression: only artists[0] was stored, so collaborations could
    never match their Last.fm scrobble."""
    cache = playlist_sync._liked_songs_to_cache([
        {"track": {"uri": "spotify:track:C", "name": "Collab",
                   "artists": [{"name": "Artist A"}, {"name": "Artist B"}]}}
    ])
    assert cache[0]["artists"] == ["Artist A", "Artist B"]


# ── playlist_sync: _filter_unplayed ──────────────────────────────────────────

def test_played_collaboration_is_filtered_out():
    """Regression: a collaboration that was scrobbled stayed in the
    neglected playlist forever."""
    cache = [{"uri": "spotify:track:C", "artists": ["Artist A", "Artist B"], "title": "Collab"}]
    scrobbled = track_key_variants("Artist A, Artist B", "Collab")
    assert playlist_sync._filter_unplayed(cache, scrobbled) == []


def test_unplayed_collaboration_is_kept():
    cache = [{"uri": "spotify:track:C", "artists": ["Artist A", "Artist B"], "title": "Collab"}]
    assert playlist_sync._filter_unplayed(cache, set()) == ["spotify:track:C"]


def test_legacy_state_format_still_matches():
    """State written before the refactor stored a joined artist string."""
    cache = [{"uri": "spotify:track:C", "artist": "Artist A, Artist B", "title": "Collab"}]
    scrobbled = track_key_variants("Artist A, Artist B", "Collab")
    assert playlist_sync._filter_unplayed(cache, scrobbled) == []


def test_filter_preserves_cache_order():
    cache = [
        {"uri": "a", "artists": ["X"], "title": "1"},
        {"uri": "b", "artists": ["X"], "title": "2"},
        {"uri": "c", "artists": ["X"], "title": "3"},
    ]
    assert playlist_sync._filter_unplayed(cache, set()) == ["a", "b", "c"]


# ── playlist_sync: diff sync ─────────────────────────────────────────────────

class FakeSpotify:
    def __init__(self):
        self.removed = []
        self.added = []

    def playlist_remove_all_occurrences_of_items(self, playlist_id, items):
        self.removed.extend(items)

    def playlist_add_items(self, playlist_id, items):
        self.added.extend(items)


def test_diff_sync_no_change_does_nothing():
    sp = FakeSpotify()
    playlist_sync.diff_sync_playlist(sp, "pl", ["a", "b"], ["b", "a"])
    assert sp.removed == [] and sp.added == []


def test_diff_sync_only_touches_the_delta():
    sp = FakeSpotify()
    playlist_sync.diff_sync_playlist(sp, "pl", ["a", "b", "c"], ["b", "c", "d"])
    assert sp.removed == ["a"]
    assert sp.added == ["d"]


def test_diff_sync_chunks_large_deltas():
    sp = FakeSpotify()
    old = [f"old{i}" for i in range(250)]
    new = [f"new{i}" for i in range(250)]
    playlist_sync.diff_sync_playlist(sp, "pl", old, new)
    assert len(sp.removed) == 250 and len(sp.added) == 250
    assert sp.removed[0] == "old0" and sp.removed[-1] == "old249"


# ── playlist_sync: full sync ─────────────────────────────────────────────────

class FakeReplace(FakeSpotify):
    def __init__(self):
        super().__init__()
        self.replaced = False

    def playlist_replace_items(self, playlist_id, items):
        self.replaced = True


def test_sync_playlist_clears_then_adds():
    sp = FakeReplace()
    playlist_sync.sync_playlist(sp, "pl", ["a", "b"])
    assert sp.replaced is True
    assert sp.added == ["a", "b"]


# ── backup_library ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("My Playlist", "My_Playlist"),
        ('bad/name*here?"<>|', "badnamehere"),
        ("  trailing  ", "trailing"),
        ("...", "unnamed_playlist"),
        ("a" * 200, "a" * 60),
    ],
)
def test_sanitize_filename(raw, expected):
    assert backup_library.sanitize_filename(raw) == expected


def test_track_fields_flattens_artists():
    record = backup_library._track_fields({
        "name": "Song",
        "artists": [{"name": "A"}, {"name": "B"}],
        "album": {"name": "Album"},
        "duration_ms": 1000,
        "external_ids": {"isrc": "ISRC1"},
        "external_urls": {"spotify": "https://open.spotify.com/track/x"},
    }, added_at="2026-01-01T00:00:00Z")

    assert record["artist"] == "A, B"
    assert record["artists"] == ["A", "B"]
    assert record["album"] == "Album"
    assert record["isrc"] == "ISRC1"
    assert record["added_at"] == "2026-01-01T00:00:00Z"


def test_track_fields_tolerates_missing_sections():
    record = backup_library._track_fields({"name": "Bare"})
    assert record["album"] == ""
    assert record["isrc"] is None


def test_one_failing_playlist_does_not_abort_the_backup(tmp_path, capsys):
    """A single unreadable playlist must not lose the whole weekly backup."""
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()

    good = {"id": "1", "name": "Good", "external_urls": {}, "owner": {},
            "tracks": {"items": [{"track": {"name": "T", "artists": []}}], "next": None}}

    class Failing(FakeSpotify):
        def playlist_items(self, pl_id, **kwargs):
            raise RuntimeError("403 forbidden")

        def playlist(self, playlist_id):
            return {}

    sp = Failing()
    sp.added = []

    good_with_id = dict(good)
    bad = {"id": "2", "name": "Bad", "external_urls": {}, "owner": {}}

    original = backup_library._export_playlist

    def fake_export(client, pl, directory):
        if pl.get("id") == "2":
            raise RuntimeError("403 forbidden")
        return original(client, good_with_id, directory)

    backup_library._export_playlist = fake_export
    try:
        summaries = backup_library.backup_playlists(sp, str(tmp_path))
    finally:
        backup_library._export_playlist = original

    assert len(summaries) == 1
    assert summaries[0]["name"] == "Good"
    assert "could not be exported" in capsys.readouterr().out


# ── listenbrainz_sync ────────────────────────────────────────────────────────

def test_cached_artists_handles_both_formats():
    assert listenbrainz_sync._cached_artists({"artists": ["A", "B"]}) == ["A", "B"]
    assert listenbrainz_sync._cached_artists({"artist": "A, B"}) == ["A, B"]
    assert listenbrainz_sync._cached_artists({}) == []


def test_clean_track_metadata_strips_for_search():
    artist, title = listenbrainz_sync.clean_track_metadata(
        "Akon", "Smack That (feat. Eminem) - Remastered 2011"
    )
    assert "feat" not in title.lower()
    assert "remaster" not in title.lower()


def test_clean_track_metadata_keeps_usable_values():
    artist, title = listenbrainz_sync.clean_track_metadata("Daft Punk", "(feat. X)")
    assert artist == "Daft Punk"
    assert title  # never empty
