import unittest
from unittest.mock import patch
import theme


class ThemeTests(unittest.TestCase):
    def test_system_preference_and_explicit_choice(self):
        with patch.object(theme, 'system_theme', return_value='light'):
            self.assertEqual(theme.palette()['theme'], 'light')
            self.assertEqual(theme.palette('dark')['theme'], 'dark')
            self.assertNotEqual(theme.palette('dark')['canvas'], theme.palette('light')['canvas'])
    def test_light_accent_fill_has_own_foreground(self):
        p = theme.palette('light')
        self.assertIn('accentFill', p)
        self.assertIn('onAccentFill', p)


def contrast(a, b):
    def luminance(value):
        parts = [int(value[i:i+2], 16)/255 for i in (1, 3, 5)]
        parts = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in parts]
        return sum(v*w for v, w in zip(parts, (.2126, .7152, .0722)))
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high+.05)/(low+.05)


class DesktopContrastTests(unittest.TestCase):
    def test_available_text_and_selection_remain_readable_in_both_themes(self):
        for mode in ("dark", "light"):
            p = theme.palette(mode)
            for text in ("text", "textMuted", "accent", "info", "danger", "success"):
                for surface in ("canvas", "surface", "surfaceRaised"):
                    with self.subTest(mode=mode, text=text, surface=surface):
                        self.assertGreaterEqual(contrast(p[text], p[surface]), 4.5)
            self.assertGreaterEqual(contrast(p["text"], p["selection"]), 4.5)
            for fill in (p.get("accentFill", p["accent"]), p["accentHover"], p["accentPressed"]):
                self.assertGreaterEqual(contrast(p.get("onAccentFill", p["onAccent"]), fill), 4.5)

    def test_control_edges_and_keyboard_focus_are_visible(self):
        for mode in ("dark", "light"):
            p = theme.palette(mode)
            for surface in ("canvas", "surface", "surfaceRaised"):
                for border in ("controlBorder", "focus"):
                    self.assertGreaterEqual(contrast(p[border], p[surface]), 3)
            self.assertGreaterEqual(contrast(p["textDisabled"], p["disabledSurface"]), 3)
