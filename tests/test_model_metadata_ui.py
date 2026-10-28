"""Metadata refresh completion must not overwrite a newer download notice."""
import unittest
from unittest.mock import patch
import model_manager as module


class Value:
    def __init__(self): self.value = ''
    def get(self): return self.value
    def set(self, value): self.value = value


class MetadataStateTests(unittest.TestCase):
    def window(self):
        w = module.ModelManagerWindow.__new__(module.ModelManagerWindow)
        w._closed = False
        w._remote_sizes = {}
        w._populate_rows = lambda: None
        w.status_var = Value()
        return w

    def test_repeated_refresh_shares_one_query_and_ends_with_unknown_sizes(self):
        w = self.window()
        with patch.object(module.threading, 'Thread') as thread:
            w._refresh_remote_sizes()
            w._refresh_remote_sizes()
            self.assertEqual(thread.call_count, 1)
        for item in module.MODEL_DEFINITIONS:
            w._remote_size_ready(item.key, None)
        self.assertFalse(w._size_query_active)
        self.assertIn('模型状态已更新', w.status_var.get())
        self.assertIn('未取得远端大小', w.status_var.get())

    def test_late_metadata_does_not_replace_download_or_closed_window(self):
        w = self.window()
        with patch.object(module.threading, 'Thread'):
            w._refresh_remote_sizes()
        w.status_var.set('正在下载模型')
        for item in module.MODEL_DEFINITIONS:
            w._remote_size_ready(item.key, 100)
        self.assertEqual(w.status_var.get(), '正在下载模型')
        w._closed = True
        w._remote_size_ready('late', 200)
        self.assertNotIn('late', w._remote_sizes)
