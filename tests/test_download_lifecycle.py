from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from download_manager import DownloadManager, _DownloadJob


class DownloadLifecycleTests(unittest.TestCase):
    def test_cancel_completion_cannot_be_overwritten_by_request_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager=DownloadManager(Path(temporary))
            reported=[]
            job=_DownloadJob("payload",lambda:None,lambda progress:reported.append(progress.state),None)
            manager._jobs[job.key]=job
            def complete_during_cancel():
                job.cancel_requested=True
                manager._emit(job,state="已取消")
                job.done.set()
            # Force the exact handover that caused a completed cancellation to
            # remain visibly stuck as "正在取消" despite no worker still running.
            with patch.object(job,"request_cancel",side_effect=complete_during_cancel):
                self.assertTrue(manager.cancel(job.key))
            self.assertEqual(manager.status(job.key).state,"已取消")
            self.assertEqual(reported,["已取消"])

    def test_finished_callback_replacement_retains_registry_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager=DownloadManager(Path(temporary))
            replacement=_DownloadJob("payload",lambda:None,None,None)
            def replace(*_):
                manager._jobs["payload"]=replacement
            old=_DownloadJob("payload",lambda:SimpleNamespace(),None,replace)
            manager._jobs[old.key]=old
            with patch.object(manager,"_validate_spec"),patch.object(manager,"component_is_ready",return_value=True),patch.object(manager,"_component_completed",return_value=0):
                manager._run_job(old)
            self.assertTrue(old.done.is_set())
            self.assertIs(manager._jobs["payload"],replacement)
            manager._emit(old,state="下载中")
            self.assertEqual(manager.status("payload").state,"完成")
