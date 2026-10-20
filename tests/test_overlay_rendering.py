"""Exercise production layout and draw filtering without a desktop window."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import ImageFont

from overlay import OverlayWindow
from settings import Settings
from subtitle_layout import EntryView, Motion1D, ParagraphLedger, TranslationLaneState


class OverlayRenderingTests(unittest.TestCase):
    def overlay(self, source="First caption", translation="Translation"):
        overlay = OverlayWindow.__new__(OverlayWindow)
        overlay.settings = Settings()
        overlay.render_scale = 1
        overlay.canvas = SimpleNamespace(winfo_width=lambda: 680, winfo_height=lambda: 300)
        overlay.window = SimpleNamespace(winfo_fpixels=lambda _: 96)
        overlay._fonts = {("label", 1): ImageFont.load_default()}
        overlay._layout_cache_key = None
        overlay._layout_cache = None
        overlay._entries_view = [EntryView(source, translation)]
        overlay._ledger = ParagraphLedger()
        overlay._show_translation = True
        overlay._same_language = False
        overlay._follow_tail = True
        overlay._translation_lane_state = TranslationLaneState()
        overlay._translation_lane_state.configure(None, "zh", True)
        overlay._scroll_motion = Motion1D(0, value=0)
        overlay._scroll_target = 0
        overlay._auto_scroll_after = None
        overlay._drag_mode = ""
        overlay._scrollbar_show_until = 0
        overlay._update_auto_height = lambda _: False
        overlay._layer = SimpleNamespace(present=lambda _: None)
        overlay.on_presented = None
        return overlay

    def render(self, overlay):
        drawn = []
        font = ImageFont.load_default()
        with patch("overlay.ImageFont.truetype", return_value=font), patch.object(
            overlay, "_draw_line", side_effect=lambda _target, line, *_: drawn.append(line)
        ):
            overlay._draw_content(24, 0)
        return drawn

    def test_initial_single_line_source_and_translation_are_actually_drawn(self):
        overlay = self.overlay()
        self.assertEqual([line["text"] for line in self.render(overlay)],
                         ["First caption", "Translation"])

    def test_idle_visibility_controls_actual_lines_not_only_state(self):
        overlay = self.overlay(translation="")
        self.assertIn("translation", [line["style"] for line in self.render(overlay)])
        overlay._translation_lane_state.source_updated(0)
        overlay._translation_lane_state.maybe_collapse_if_idle(6)
        self.assertEqual([line["style"] for line in self.render(overlay)], ["source"])

    def test_existing_translation_remains_drawn_after_source_becomes_idle(self):
        overlay = self.overlay()
        lane = overlay._translation_lane_state
        lane.source_updated(0)
        lane.translation_output(1)
        lane.maybe_collapse_if_idle(100)
        self.assertIn("Translation", [line["text"] for line in self.render(overlay)])


if __name__ == "__main__":
    unittest.main()
