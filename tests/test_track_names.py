"""
Tests for track_names — pure functions, no external dependencies.
"""

from track_names import normalize_track_name


class TestNormalizeTrackName:
    def test_dash_live_variant_stripped(self):
        assert normalize_track_name('Dancers - Live at Bush Hall') == 'dancers'

    def test_paren_live_variant_stripped(self):
        assert normalize_track_name('Song (Live at Glastonbury)') == 'song'

    def test_paren_acoustic_variant_stripped(self):
        assert normalize_track_name('Song (Acoustic Version)') == 'song'

    def test_plain_name_unchanged(self):
        assert normalize_track_name('Concorde') == 'concorde'

    def test_uppercase_is_lowercased(self):
        assert normalize_track_name('CONCORDE') == 'concorde'

    def test_live_wire_not_stripped(self):
        # "Live" appears in the title itself, not as a parenthetical/dash
        # variant suffix, so it must be left intact.
        assert normalize_track_name('Live Wire') == 'live wire'

    def test_dash_demo_variant_stripped(self):
        assert normalize_track_name('Song - Demo') == 'song'


class TestNormalizeEditionSuffixes:
    # Edition suffixes name the pressing, not the performance — they have to
    # collapse onto the plain title so the song deduplicates and so setlist.fm
    # and Last.fm, which index under the plain title, are actually hit.

    def test_dash_ep_version_stripped(self):
        # Endswell's "Heart Container" shipped on a 2023 single and again on the
        # 2024 EP *Keepsake*. Both reached the playlist, and the EP pressing
        # scored 0.0 on setlist frequency for a song they play at every show.
        assert normalize_track_name('Heart Container - EP Version') == 'heart container'

    def test_paren_ep_version_stripped(self):
        # Last.fm carries the same song under both spellings.
        assert normalize_track_name('Heart Container (EP Version)') == 'heart container'

    def test_radio_edit_stripped(self):
        assert normalize_track_name('Song - Radio Edit') == 'song'

    def test_extended_mix_stripped(self):
        assert normalize_track_name('Song (Extended Mix)') == 'song'

    def test_clean_edit_stripped(self):
        assert normalize_track_name('Heart Container (Clean Edit)') == 'heart container'

    def test_deluxe_version_stripped_at_track_level(self):
        assert normalize_track_name('Song (Deluxe Version) [Bonus]') == 'song'

    # ── The qualifier is required ─────────────────────────────────────────────

    def test_bare_edition_word_in_title_not_stripped(self):
        # "Mix"/"Edit"/"Version" alone are ordinary words. Only a qualified
        # "<qualifier> version/edit/mix" suffix strips, so these stay intact.
        assert normalize_track_name('Mixtape') == 'mixtape'
        assert normalize_track_name('The Remix') == 'the remix'

    def test_qualifier_without_edition_word_not_stripped(self):
        assert normalize_track_name('Single Ladies') == 'single ladies'

    def test_edition_phrase_as_whole_title_not_stripped(self):
        # No preceding title to reduce to — stripping would leave nothing.
        assert normalize_track_name('Album Version') == 'album version'
