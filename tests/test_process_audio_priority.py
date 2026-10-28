import unittest
from process_audio_capture import AudioProcessCandidate as Candidate, ProcessAudioCapture


class AudioPriorityTests(unittest.TestCase):
    def choose(self, rows, current=1, foreground=None):
        return ProcessAudioCapture._choose_process(rows, foreground, current, 10, 9.5)

    def test_real_signal_replaces_current_low_meter_floor(self):
        current=Candidate(1,"remote",.00025)
        browser=Candidate(2,"chrome.exe",.08)
        self.assertEqual(self.choose([current,browser]),browser)

    def test_comparable_sources_remain_stable(self):
        current=Candidate(1,"current",.03)
        other=Candidate(2,"other",.04)
        self.assertEqual(self.choose([current,other]),current)

    def test_noise_advantage_does_not_trigger_switch(self):
        current=Candidate(1,"current",.00001)
        other=Candidate(2,"noise",.0001)
        self.assertEqual(self.choose([current,other]),current)

    def test_audible_foreground_keeps_explicit_priority(self):
        foreground=Candidate(3,"foreground",.01)
        self.assertEqual(self.choose([Candidate(1,"current",.02),Candidate(2,"louder",.09),foreground],foreground=3),foreground)


@unittest.skipUnless(__import__('os').name == 'nt', 'Windows process identity boundary')
class NativeProcessIdentityTests(unittest.TestCase):
    def test_current_process_has_a_real_identity(self):
        import os
        from process_audio_capture import _process_name
        self.assertTrue(_process_name(os.getpid()).lower().endswith('.exe'))

    def test_invalid_process_does_not_become_a_synthetic_audio_target(self):
        from process_audio_capture import _process_name
        self.assertIsNone(_process_name(0))
