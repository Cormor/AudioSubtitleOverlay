"""Exercise the production transparent-header drawing on its real pixel layer."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from PIL import Image,ImageDraw,ImageFont
from overlay import OverlayWindow
from theme import palette

class HeaderContrastTests(unittest.TestCase):
    def header(self,state):
        item=OverlayWindow.__new__(OverlayWindow)
        item._translation_lane_visible=lambda:True
        item._latest_translation_state=state
        return item
    def font(self):
        if os.name == 'nt':
            return ImageFont.truetype(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/msyh.ttc'),11)
        # Pixel alpha/stroke checks use Pillow's bundled FreeType font in CI.
        return ImageFont.load_default(size=11)
    def test_production_header_has_opaque_text_and_dark_stroke_without_backplate(self):
        p=palette('dark')
        foreground=OverlayWindow._rgba_with_alpha(p['text'],255)
        outline=OverlayWindow._rgba_with_alpha(p['canvas'],255)
        for state in ('draft','updating','final'):
            with self.subTest(state=state):
                image=Image.new('RGBA',(320,32))
                self.header(state)._draw_header(ImageDraw.Draw(image),self.font(),320)
                pixels=list(image.getdata())
                self.assertIn(foreground,pixels)
                self.assertIn(outline,pixels)
                self.assertEqual(image.getpixel((0,0)),(0,0,0,0))
                self.assertEqual(image.getpixel((319,31)),(0,0,0,0))
                self.assertLess(sum(pixel[3]>0 for pixel in pixels),320*20)
    def test_state_text_geometry_and_one_pixel_stroke_use_real_draw_method(self):
        for state,suffix in (('draft',' · 草稿'),('updating',' · 更新中'),('final','')):
            calls=[]
            draw=SimpleNamespace(text=lambda pos,text,**kwargs:calls.append((pos,text,kwargs)),line=lambda *_,**__:None)
            self.header(state)._draw_header(draw,self.font(),320)
            self.assertEqual(calls[0][0],(14,10))
            self.assertEqual(calls[0][1],'声幕 · 原文 / 译文'+suffix)
            self.assertEqual(calls[0][2]['stroke_width'],1)
    def test_render_does_not_mutate_existing_user_text_or_opacity_preferences(self):
        item=self.header('draft')
        item.settings=SimpleNamespace(overlay_background_opacity=0,overlay_text_opacity=.8,
                                      overlay_text_color='#55ddff',overlay_translation_text_color='#88ccff',
                                      overlay_font_size=30,overlay_translation_font_size=21)
        before=vars(item.settings).copy()
        item._draw_header(ImageDraw.Draw(Image.new('RGBA',(320,32))),self.font(),320)
        self.assertEqual(vars(item.settings),before)

if __name__=='__main__':unittest.main(verbosity=2)
