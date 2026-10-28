import random
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from screen_bounds import Rect,Monitor,constrain,parse_geometry
from overlay import OverlayWindow

PRIMARY=Monitor(Rect(0,0,1920,1080),Rect(0,0,1920,1040))
LEFT=Monitor(Rect(-1280,0,1280,1024),Rect(-1280,0,1280,984))


class ScreenBoundsTests(unittest.TestCase):
    def assert_inside(self,rect,work):
        self.assertGreaterEqual(rect.x,work.x);self.assertGreaterEqual(rect.y,work.y)
        self.assertLessEqual(rect.right,work.right);self.assertLessEqual(rect.bottom,work.bottom)

    def test_valid_position_does_not_move(self):
        rect=Rect(80,100,680,180)
        self.assertEqual(constrain(rect,[PRIMARY])[0],rect)

    def test_each_edge_and_taskbar(self):
        for rect in [Rect(-90,80,400,150),Rect(1800,80,400,150),Rect(80,-90,400,150),Rect(80,1000,400,150)]:
            with self.subTest(rect=rect):self.assert_inside(*constrain(rect,[PRIMARY]))
        self.assertEqual(constrain(Rect(80,1000,400,150),[PRIMARY])[0].bottom,1040)

    def test_window_larger_than_monitor_shrinks_in_both_axes(self):
        rect,work=constrain(Rect(3000,3000,5000,5000),[PRIMARY])
        self.assertEqual(rect,work)

    def test_very_small_work_area_takes_precedence_over_minimum_size(self):
        tiny=Monitor(Rect(0,0,200,60),Rect(0,0,200,40))
        self.assertEqual(constrain(Rect(50,50,500,500),[tiny])[0],tiny.work)

    def test_negative_monitor_coordinates_are_preserved_and_roundtrip(self):
        rect=Rect(-900,80,680,180)
        self.assertEqual(parse_geometry(rect.geometry()),rect)
        self.assertEqual(constrain(rect,[PRIMARY,LEFT])[0],rect)

    def test_pointer_allows_cross_monitor_drag_without_straddling_edges(self):
        rect,work=constrain(Rect(-50,80,600,200),[PRIMARY,LEFT],pointer=(-5,100))
        self.assertEqual(work,LEFT.work);self.assert_inside(rect,work)
        rect,work=constrain(rect,[PRIMARY,LEFT],pointer=(10,100))
        self.assertEqual(work,PRIMARY.work);self.assert_inside(rect,work)

    def test_gap_and_wholly_offscreen_position_choose_nearest_monitor(self):
        right=Monitor(Rect(2200,-100,1000,800),Rect(2200,-100,1000,760))
        rect,work=constrain(Rect(2100,1200,500,200),[PRIMARY,right],pointer=(2150,10))
        self.assertEqual(work,right.work);self.assert_inside(rect,work)

    def test_resize_preserves_top_left_and_clips_right_and_bottom(self):
        rect,work=constrain(Rect(1000,600,3000,3000),[PRIMARY],resize=True,pointer=(1001,601))
        self.assertEqual((rect.x,rect.y),(1000,600));self.assert_inside(rect,work)

    def test_monitor_removal_and_scaled_work_area_recover_existing_position(self):
        original=Rect(-1000,600,900,400)
        self.assert_inside(*constrain(original,[PRIMARY]))
        scaled=Monitor(Rect(0,0,960,540),Rect(0,0,960,500))
        self.assert_inside(*constrain(Rect(1500,800,1200,700),[scaled]))

    def test_randomized_four_edges_always_fit_one_real_monitor(self):
        randomizer=random.Random(7)
        for _ in range(500):
            rect=Rect(randomizer.randrange(-4000,5000),randomizer.randrange(-2000,3000),randomizer.randrange(1,5000),randomizer.randrange(1,3000))
            bounded,work=constrain(rect,[PRIMARY,LEFT]);self.assert_inside(bounded,work)
            self.assertEqual(constrain(bounded,[PRIMARY,LEFT])[0],bounded)


class FakeWindow:
    def __init__(self,rect):self.rect=rect;self.jobs={};self.next_id=0
    def geometry(self,value=None):
        if value is not None:self.rect=parse_geometry(value)
        return self.rect.geometry()
    def winfo_exists(self):return True
    def winfo_ismapped(self):return True
    def winfo_x(self):return self.rect.x
    def winfo_y(self):return self.rect.y
    def winfo_width(self):return self.rect.width
    def winfo_height(self):return self.rect.height
    def winfo_fpixels(self,_):return 96
    def after(self,_ms,callback):
        self.next_id+=1;self.jobs[self.next_id]=callback;return self.next_id
    def after_idle(self,callback):return self.after(0,callback)
    def after_cancel(self,token):self.jobs.pop(token,None)


class OverlayBoundsIntegrationTests(unittest.TestCase):
    def overlay(self,rect):
        overlay=OverlayWindow.__new__(OverlayWindow)
        overlay.window=FakeWindow(rect);overlay._cap_height=rect.height
        overlay._bounds_work_area=None;overlay._bounds_after_id=None;overlay._screen_poll_after_id=None
        overlay._screen_signature=None;overlay._requested_rect=rect
        overlay._layout_cache_key=None;overlay._height_anchor_bottom=False
        overlay._drag_mode='';overlay._request_redraw=Mock()
        return overlay

    def test_move_at_edge_can_reverse_in_same_drag(self):
        overlay=self.overlay(Rect(100,100,400,150))
        overlay._drag_mode='move';overlay._drag_origin=(100,100);overlay._geometry_origin=(400,150,100,100)
        with patch('overlay.monitor_areas',return_value=(PRIMARY,)):
            overlay._on_drag(SimpleNamespace(x_root=9000,y_root=9000))
            self.assertEqual(overlay.window.rect,Rect(1520,890,400,150))
            overlay._on_drag(SimpleNamespace(x_root=101,y_root=101))
            self.assertEqual(overlay.window.rect,Rect(101,101,400,150))

    def test_resize_and_auto_height_are_constrained_before_geometry_is_written(self):
        overlay=self.overlay(Rect(100,100,400,150))
        overlay._drag_mode='resize';overlay._drag_origin=(500,250);overlay._geometry_origin=(400,150,100,100)
        overlay._reading_minimum_height=Mock(return_value=68)
        with patch('overlay.monitor_areas',return_value=(PRIMARY,)):
            overlay._on_drag(SimpleNamespace(x_root=9000,y_root=9000))
            self.assertEqual(overlay.window.rect,Rect(100,100,1820,940))
            overlay._drag_mode='';overlay._apply_window_height(3000)
            self.assertLessEqual(overlay.window.rect.bottom,PRIMARY.work.bottom)

    def test_configuration_change_is_coalesced_and_repaired(self):
        overlay=self.overlay(Rect(2300,1300,700,400))
        overlay._queue_bounds_check();overlay._queue_bounds_check()
        self.assertEqual(len(overlay.window.jobs),1)
        callback=overlay.window.jobs.popitem()[1]
        with patch('overlay.monitor_areas',return_value=(PRIMARY,)):callback()
        self.assertEqual(overlay.window.rect,Rect(1220,640,700,400))
        self.assertIsNone(overlay._bounds_after_id)

    def test_poll_repairs_monitor_removal_without_a_window_configure_event(self):
        overlay=self.overlay(Rect(-1000,100,600,200))
        with patch('overlay.monitor_areas',return_value=(PRIMARY,LEFT)):overlay._poll_screen_bounds()
        overlay.window.jobs.pop(overlay._screen_poll_after_id)
        with patch('overlay.monitor_areas',return_value=(PRIMARY,)):overlay._poll_screen_bounds()
        self.assertEqual(overlay.window.rect,Rect(0,100,600,200))
        self.assertEqual(len(overlay.window.jobs),1)
