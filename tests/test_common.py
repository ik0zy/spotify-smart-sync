"""Tests for the shared helpers in common.py."""

import json

import pytest

from common import (
    _retry_after_seconds,
    artist_variants,
    compute_hash,
    load_state,
    local_timestamp,
    normalise,
    save_state,
    standardise_track_key,
    title_variants,
    track_key_variants,
    track_played,
)


# ── normalise ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Daft Punk", "daft punk"),
        ("MØ", "mø"),
        ("Björk", "bjork"),
        ("Sigur Rós", "sigur ros"),
        ("Hit 'Em Up!", "hit em up"),
        ("  spaced   out  ", "spaced out"),
        ("", ""),
    ],
)
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_standardise_track_key():
    assert standardise_track_key("Daft Punk", "Get Lucky") == "daft punk - get lucky"


# ── title variants ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "title,expected_clean",
    [
        ("Dirty Harry (feat. Bootie Brown)", "dirty harry"),
        ("Hit 'Em Up - Single Version", "hit em up"),
        ("Push The Feeling On - Mk Dub Revisited Edit", "push the feeling on"),
        ("Guillotine (Swordz)", "guillotine"),
    ],
)
def test_noise_is_stripped(title, expected_clean):
    assert any(normalise(t) == expected_clean for t in title_variants(title))


def test_leading_parenthetical_is_part_of_the_title():
    """'(I Can't Get No) Satisfaction' must not reduce to 'Satisfaction'."""
    variants = [normalise(t) for t in title_variants("(I Can't Get No) Satisfaction")]
    assert "(i cant get no) satisfaction" in variants
    assert "satisfaction" not in variants


def test_raw_title_is_always_a_variant():
    assert title_variants("Lean On")[0] == "Lean On"


# ── artist variants ──────────────────────────────────────────────────────────

def test_artist_list_offers_join_and_lead():
    variants = artist_variants(["Akon", "Eminem"])
    assert "Akon, Eminem" in variants
    assert "Akon" in variants


def test_feature_marker_on_a_single_string_yields_main_artist():
    variants = artist_variants("Major Lazer feat. DJ Snake")
    assert "Major Lazer feat. DJ Snake" in variants
    assert "Major Lazer" in variants


def test_ampersand_artist_is_not_split():
    """'Simon & Garfunkel' is one artist; splitting it risks false matches."""
    assert "Simon" not in artist_variants("Simon & Garfunkel")


def test_empty_input():
    assert artist_variants([]) == []
    assert artist_variants("") == []


# ── cross-provider matching ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "spotify_artists,spotify_title,lastfm_artist,lastfm_title",
    [
        (["Major Lazer", "DJ Snake", "MØ", "Diplo"], "Lean On",
         "Major Lazer, DJ Snake, MØ, Diplo", "Lean On"),
        (["Major Lazer", "DJ Snake", "MØ", "Diplo"], "Lean On",
         "Major Lazer feat. DJ Snake", "Lean On"),
        (["Gorillaz", "Bootie Brown"], "Dirty Harry (feat. Bootie Brown)",
         "Gorillaz", "Dirty Harry"),
        (["2Pac", "Outlawz"], "Hit 'Em Up - Single Version", "2Pac", "Hit Em Up"),
        (["Nas", "Ms. Lauryn Hill"], "If I Ruled The World (Imagine That) (feat. Lauryn Hill)",
         "Nas", "If I Ruled The World"),
        (["Nightcrawlers", "MK"], "Push The Feeling On - Mk Dub Revisited Edit",
         "Nightcrawlers", "Push The Feeling On"),
    ],
)
def test_collaborations_match(spotify_artists, spotify_title, lastfm_artist, lastfm_title):
    scrobbled = track_key_variants(lastfm_artist, lastfm_title)
    assert track_played(spotify_artists, spotify_title, scrobbled)


@pytest.mark.parametrize(
    "spotify_artists,spotify_title,lastfm_artist,lastfm_title",
    [
        (["Radiohead"], "Creep", "Radiohead", "Weezer - Island"),
        (["Daft Punk"], "Instant Crush", "Daft Punk", "Get Lucky"),
        (["Simon & Garfunkel"], "The Sound of Silence", "Simon & Garfunkel", "Bridge Over Troubled Water"),
    ],
)
def test_different_tracks_do_not_match(spotify_artists, spotify_title, lastfm_artist, lastfm_title):
    scrobbled = track_key_variants(lastfm_artist, lastfm_title)
    assert not track_played(spotify_artists, spotify_title, scrobbled)


def test_matching_is_symmetric():
    """Scrobble and liked track produce overlapping keys in either direction."""
    a = track_key_variants("Akon", "Smack That")
    b = track_key_variants(["Akon", "Eminem"], "Smack That")
    assert a & b


def test_variants_are_bounded():
    """A track should not explode into an unbounded number of keys."""
    assert len(track_key_variants(["A", "B", "C", "D"], "Song (feat. E) - Live")) <= 24


# ── hashing ──────────────────────────────────────────────────────────────────

def test_compute_hash_is_order_independent():
    assert compute_hash(["c", "a", "b"]) == compute_hash(["b", "c", "a"])


def test_compute_hash_changes_with_membership():
    assert compute_hash(["a", "b"]) != compute_hash(["a", "c"])


# ── state persistence ────────────────────────────────────────────────────────

def test_state_round_trip(tmp_path):
    path = tmp_path / "state.json"
    save_state(str(path), {"hash": "abc", "uris": ["x"]})
    assert load_state(str(path)) == {"hash": "abc", "uris": ["x"]}


def test_load_state_missing_file(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) == {}


def test_load_state_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert load_state(str(path)) == {}


def test_load_state_rejects_foreign_state(tmp_path):
    """A ListenBrainz state file has no 'hash', so it must not be accepted
    as playlist state (that would force a full rebuild every run)."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"mbid": "abc", "current_uris": ["x"]}))
    assert load_state(str(path)) == {}
    assert load_state(str(path), require_key="mbid") == {"mbid": "abc", "current_uris": ["x"]}


# ── Retry-After parsing ──────────────────────────────────────────────────────

def test_retry_after_numeric():
    assert _retry_after_seconds({"Retry-After": "12"}, 60) == 12


def test_retry_after_http_date():
    from email.utils import formatdate

    assert _retry_after_seconds({"Retry-After": formatdate(usegmt=True)}, 60) <= 1


def test_retry_after_missing_or_garbage():
    assert _retry_after_seconds({}, 60) == 60
    assert _retry_after_seconds(None, 60) == 60
    assert _retry_after_seconds({"Retry-After": "soon"}, 60) == 60


# ── timestamps ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("offset", [0, 6, -5, 14])
def test_local_timestamp_label(offset):
    _, label = local_timestamp(offset)
    sign = "+" if offset >= 0 else "-"
    assert label == f"UTC{sign}{abs(offset)}"
