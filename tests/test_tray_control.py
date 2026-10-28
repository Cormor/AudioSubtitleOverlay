import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tray_control import TrayController


class FakeItem:
    def __init__(self,label,action,**kwargs):
        self.label=label
        self.action=action
        self.enabled=kwargs.get("enabled",lambda _:True)


class FakeMenu:
    SEPARATOR=object()
    def __init__(self,*items):self.items=items


class FakeIcon:
    def __init__(self,_name,_image,title,menu):
        self.title=title;self.menu=menu;self.stopped=0;self.updates=0
    def run_detached(self,setup):setup(self)
    def update_menu(self):self.updates+=1
    def stop(self):self.stopped+=1


class TrayTests(unittest.TestCase):
    def test_actions_are_queued_and_run_on_the_application_side(self):
        queued=[];called=[]
        actions={name:lambda n=name:called.append(n) for name in
                 ("show_settings","show_overlay","hide_overlay","start","stop","close")}
        tray=TrayController(queued.append,**actions)
        backend=SimpleNamespace(Icon=FakeIcon,Menu=FakeMenu,MenuItem=FakeItem)
        with patch.dict(sys.modules,{"pystray":backend}):
            tray.start()
        self.assertTrue(tray.available)
        icon=tray._icon
        icon.menu.items[0].action(icon,None)
        self.assertFalse(called)
        queued.pop()()
        self.assertEqual(called,["show_settings"])
        self.assertTrue(icon.menu.items[4].enabled(None))
        tray.set_running(True)
        self.assertFalse(icon.menu.items[4].enabled(None))
        self.assertTrue(icon.menu.items[5].enabled(None))
        tray.close();tray.close()
        self.assertEqual(icon.stopped,1)
        self.assertFalse(tray.available)

    def test_backend_failure_leaves_tray_unavailable_for_taskbar_fallback(self):
        actions={name:lambda:None for name in
                 ("show_settings","show_overlay","hide_overlay","start","stop","close")}
        tray=TrayController(lambda _:None,**actions)
        backend=SimpleNamespace(Icon=lambda *_a,**_k:(_ for _ in ()).throw(RuntimeError("backend unavailable")),
                                Menu=FakeMenu,MenuItem=FakeItem)
        with patch.dict(sys.modules,{"pystray":backend}),self.assertLogs(level="ERROR"):
            tray.start()
        self.assertFalse(tray.available)


class TrayMouseRecoveryTests(unittest.TestCase):
    def controller(self, queued, called):
        actions={name:lambda n=name:called.append(n) for name in
                 ("show_settings","show_overlay","hide_overlay","start","stop","close","restore_mouse")}
        return TrayController(queued.append, **actions)

    def test_recovery_is_an_external_queued_command_and_late_actions_are_ignored(self):
        queued, called = [], []
        tray=self.controller(queued, called)
        backend=SimpleNamespace(Icon=FakeIcon,Menu=FakeMenu,MenuItem=FakeItem)
        with patch.dict(sys.modules,{"pystray":backend}): tray.start()
        icon=tray._icon
        recovery=next(item for item in icon.menu.items if getattr(item,"label",None)=="恢复鼠标操作")
        recovery.action(icon,None)
        self.assertFalse(called)
        queued.pop()()
        self.assertEqual(called,["restore_mouse"])
        recovery.action(icon,None)
        tray.close()
        queued.pop()()
        recovery.action(icon,None)
        self.assertEqual(called,["restore_mouse"])
        self.assertFalse(queued)

    def test_late_ready_cannot_make_a_closed_or_replaced_tray_available(self):
        tray=self.controller([],[])
        backend=SimpleNamespace(Icon=FakeIcon,Menu=FakeMenu,MenuItem=FakeItem)
        with patch.dict(sys.modules,{"pystray":backend}): tray.start()
        icon=tray._icon
        tray.close()
        icon.visible=False
        tray._on_ready(icon)
        self.assertFalse(icon.visible)
        self.assertFalse(tray.available)
        with patch.dict(sys.modules,{"pystray":backend}): tray.start()
        self.assertIsNone(tray._icon)
