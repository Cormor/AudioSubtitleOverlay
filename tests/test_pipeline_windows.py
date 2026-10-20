import unittest

from pipeline import recognition_window_start
from rolling_transcript import RollingTranscript
from transcription import RecognizedWord


class RecognitionWindowTests(unittest.TestCase):
    def seconds(self, received, frontier, start=0):
        return recognition_window_start(int(received*16000), int(start*16000), frontier)/16000

    def test_no_confirmation_keeps_original_window(self):
        self.assertEqual(self.seconds(4, 0), 0)
        self.assertEqual(self.seconds(12, 0), 4)

    def test_window_starts_inside_confirmed_silence_not_at_a_character(self):
        transcript=RollingTranscript()
        words=[RecognizedWord(0,.3,"a"),RecognizedWord(1.1,1.15,"b")]
        transcript.update(words,0,1.8)
        transcript.update(words,0,1.8)
        boundary=transcript.safe_decode_start
        self.assertGreater(boundary,.3)
        self.assertLess(boundary,1.1)
        self.assertAlmostEqual(self.seconds(1.8,boundary),boundary,places=4)

    def test_continuous_speech_confirmation_does_not_shorten_context(self):
        transcript=RollingTranscript()
        words=[RecognizedWord(0,1.3,"continuous")]
        transcript.update(words,0,1.4)
        transcript.update(words,0,1.4)
        self.assertGreater(transcript.confirmed_end,0)
        self.assertEqual(transcript.safe_decode_start,0)

    def test_delayed_frontier_does_not_shorten_the_unconfirmed_window(self):
        self.assertEqual(self.seconds(20, 10), 12)

    def test_short_input_and_trimmed_capture_never_seek_outside_buffer(self):
        self.assertEqual(self.seconds(.4, 0), 0)
        self.assertEqual(self.seconds(20, 16, start=16.5), 16.5)

    def test_new_unconfirmed_tail_is_retained_after_a_pause(self):
        transcript=RollingTranscript()
        words=[RecognizedWord(0,.5,"hello")]
        transcript.update(words,0,.7)
        transcript.update(words,0,1.3)
        self.assertGreater(transcript.safe_decode_start,.5)
        self.assertLess(transcript.safe_decode_start,1.3)
