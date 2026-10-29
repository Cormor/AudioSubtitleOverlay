"""Exercise production layout and draw filtering without a desktop window."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

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

    def test_panel_cache_matches_uncached_pixels_after_each_dependency_changes(self):
        overlay = self.overlay()
        cases = [(680, 300, "#000000", 0.0, 0),
                 (680, 300, "#000000", 0.35, 0),
                 (680, 300, "#245678", 0.35, 0),
                 (680, 300, "#245678", 0.35, 23),
                 (720, 300, "#245678", 0.35, 23),
                 (720, 340, "#245678", 0.35, 23),
                 (680, 300, "#fafafa", 1.0, 12)]
        for width, height, color, opacity, radius in cases:
            with self.subTest(width=width, height=height, color=color, opacity=opacity, radius=radius):
                overlay.settings.overlay_background = color
                overlay.settings.overlay_background_opacity = opacity
                overlay.corner_radius = radius
                background, mask = overlay._panel_images(width, height)
                alpha = max(0, min(255, round(255 * opacity)))
                expected = Image.new("RGBA", (width, height))
                ImageDraw.Draw(expected).rounded_rectangle(
                    (0, 0, width - 1, height - 1), radius=radius,
                    fill=overlay._rgba_with_alpha(color, alpha),
                    outline=(255, 255, 255, min(48, alpha)), width=1,
                )
                expected_mask = Image.new("L", (width, height))
                ImageDraw.Draw(expected_mask).rounded_rectangle(
                    (0, 0, width - 1, height - 1), radius=radius, fill=255,
                )
                self.assertEqual(background.tobytes(), expected.tobytes())
                self.assertEqual(mask.tobytes(), expected_mask.tobytes())
                again = overlay._panel_images(width, height)
                self.assertIs(again[0], background)
                self.assertIs(again[1], mask)

    def test_frame_composition_does_not_modify_cached_panel(self):
        overlay = self.overlay()
        self.render(overlay)
        cached = overlay._panel_cache
        background, mask = (image.tobytes() for image in cached[1:])
        first_frame = overlay._last_image.tobytes()
        self.render(overlay)
        self.assertIs(overlay._panel_cache, cached)
        self.assertEqual(cached[1].tobytes(), background)
        self.assertEqual(cached[2].tobytes(), mask)
        self.assertEqual(overlay._last_image.tobytes(), first_frame)


if __name__ == "__main__":
    unittest.main()
