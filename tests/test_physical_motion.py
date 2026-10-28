import math
import time
import unittest

from subtitle_layout import Motion1D


class PhysicalMotionTests(unittest.TestCase):
    def test_default_spring_converges_at_common_refresh_rates(self):
        for hz in (30, 60, 120, 144):
            with self.subTest(hz=hz):
                motion = Motion1D(180, value=0)
                motion.set_target(100)
                now = time.monotonic()
                finished = False
                for frame in range(1, hz * 2 + 1):
                    value, finished = motion.advance(now + frame / hz)
                    if finished:
                        break
                self.assertTrue(finished)
                self.assertEqual(value, 100)
                self.assertFalse(motion.running)

    def test_long_gap_and_reverse_target_keep_position_and_velocity_continuous(self):
        motion = Motion1D(250, value=0)
        motion.set_target(100)
        now = time.monotonic()
        before, _ = motion.advance(now + .12)
        speed = motion.velocity
        self.assertGreater(speed, 0)
        motion.set_target(-20)
        self.assertAlmostEqual(motion.value, before, places=6)
        self.assertAlmostEqual(motion.velocity, speed, places=5)
        after, _ = motion.advance(now + 5)
        self.assertEqual(after, -20)
        self.assertEqual(motion.velocity, 0)

    def test_tiny_motion_snaps_at_pixel_and_velocity_threshold(self):
        motion = Motion1D(500, value=0)
        self.assertTrue(motion.set_target(.05))
        self.assertEqual(motion.value, .05)
        self.assertFalse(motion.running)

    def test_zero_distance_and_instant_target(self):
        motion = Motion1D(0, value=3)
        self.assertFalse(motion.set_target(3))
        self.assertTrue(motion.set_target(8, instant=True))
        self.assertEqual((motion.value, motion.target, motion.velocity), (8, 8, 0))

    def test_reduced_motion_snaps_even_when_target_is_unchanged(self):
        motion=Motion1D(0)
        motion.set_target(100)
        self.assertTrue(motion.running)
        self.assertTrue(motion.set_target(100,instant=True))
        self.assertEqual((motion.value,motion.velocity,motion.running),(100,0,False))

    def test_supports_under_and_over_damped_parameters(self):
        for ratio in (.4, 2.0):
            motion = Motion1D(100, value=0, dampingRatio=ratio)
            now = time.monotonic()
            motion.set_target(20)
            value, _ = motion.advance(now + .05)
            self.assertTrue(math.isfinite(value))
            self.assertTrue(math.isfinite(motion.velocity))

    def test_invalid_physical_parameters_and_nonfinite_values_are_rejected(self):
        for kwargs in ({"mass": 0}, {"stiffness": -1}, {"dampingRatio": -1}, {"dampingRatio": 0}, {"mass": math.inf}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Motion1D(100, **kwargs)
        motion = Motion1D(100)
        with self.assertRaises(ValueError):
            motion.set_target(math.nan)
        with self.assertRaises(ValueError):
            motion.advance(math.inf)

