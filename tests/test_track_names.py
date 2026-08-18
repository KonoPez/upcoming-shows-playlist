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
