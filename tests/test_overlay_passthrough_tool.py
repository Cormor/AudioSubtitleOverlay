"""Pure ownership/provenance checks for the optional native acceptance tool."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import ctypes
import copy
from ctypes import wintypes


def load_tool():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'verify_overlay_passthrough.py'
    spec = importlib.util.spec_from_file_location('passthrough_acceptance_test_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeAcceptanceIdentityTests(unittest.TestCase):
    def test_independent_drag_waits_past_the_actual_system_double_click_interval(self):
        tool=load_tool()
        for milliseconds,seconds in [(1,.55),(250,.55),(500,.55),(5000,5.05)]:
            self.assertEqual(tool.independent_gesture_gap(milliseconds),seconds)
            self.assertGreater(seconds,milliseconds/1000)
        for invalid in (0,5001,None,True,float('nan')):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError):tool.independent_gesture_gap(invalid)

    def target_fixture(self, containing):
        class Widget:
            def __init__(self,path,kind): self.path=path;self.kind=kind
            def __str__(self): return self.path
            def winfo_class(self): return self.kind
            def winfo_rootx(self): return 60
            def winfo_rooty(self): return 100
            def winfo_width(self): return 680
            def winfo_height(self): return 240
            def winfo_viewable(self): return True
        canvas=Widget('.overlay.canvas','Canvas');window=Widget('.overlay','Toplevel')
        item=SimpleNamespace(canvas=canvas,window=window,_drag_mode='resize')
        root=SimpleNamespace(winfo_containing=Mock(return_value={'canvas':canvas,'window':window,None:None}[containing]))
        return root,item

    def test_tk_target_reports_canvas_and_actual_geometry_without_dispatch(self):
        tool=load_tool();root,item=self.target_fixture('canvas')
        result=tool.tk_input_target_snapshot(root,item,(100,150),SimpleNamespace(widget=item.canvas))
        self.assertEqual(result['canvas']['root_rect'],[60,100,740,340])
        self.assertEqual(result['canvas']['actual_size'],[680,240])
        self.assertTrue(result['containing']['is_canvas'])
        self.assertEqual(result['event_widget']['path'],'.overlay.canvas')
        self.assertEqual(result['product_drag_mode'],'resize')
        root.winfo_containing.assert_called_once_with(100,150)

    def test_tk_target_distinguishes_toplevel_beyond_canvas_and_no_widget(self):
        tool=load_tool();root,item=self.target_fixture('window')
        result=tool.tk_input_target_snapshot(root,item,(726,344),SimpleNamespace(widget=item.window))
        self.assertEqual(result['canvas']['root_rect'][3],340)
        self.assertTrue(result['containing']['is_overlay'])
        self.assertFalse(result['event_widget']['is_canvas'])
        root.winfo_containing.return_value=None
        result=tool.tk_input_target_snapshot(root,item,(999,999))
        self.assertIsNone(result['containing']);self.assertIsNone(result['event_widget'])

    def fixture(self):
        tool = load_tool()
        def owner(_, pointer):
            ctypes.cast(pointer,ctypes.POINTER(wintypes.DWORD)).contents.value = 123
            return 44
        def title(_, buffer, __):
            buffer.value = 'Owned tokenABC'
            return len(buffer.value)
        user = SimpleNamespace(IsWindow=lambda _: True,GetWindowThreadProcessId=owner,GetWindowTextW=title)
        return tool,user
    def test_window_identity_binds_pid_hwnd_creation_and_run_token(self):
        tool,user = self.fixture()
        with patch.object(tool,'process_created',return_value=987654):
            identity=tool.window_identity(user,object(),55,'tokenABC',123)
        self.assertEqual(identity,dict(pid=123,hwnd=55,created_filetime=987654,run_token='tokenABC'))
    def test_foreign_pid_and_wrong_title_rejected(self):
        tool,user = self.fixture()
        for token,pid in [('tokenABC',456),('another-token',123)]:
            with self.assertRaises(RuntimeError):
                tool.window_identity(user,object(),55,token,pid)
    def test_handle_reuse_detected_by_process_creation_time(self):
        tool,user = self.fixture()
        identity=dict(pid=123,hwnd=55,created_filetime=1,run_token='tokenABC')
        with patch.object(tool,'process_created',return_value=2),self.assertRaises(RuntimeError):
            tool.assert_identity(user,object(),identity)
    def test_invalid_hwnd_rejected_before_process_query(self):
        tool,user = self.fixture()
        user.IsWindow=lambda _:False
        with patch.object(tool,'process_created') as created,self.assertRaises(RuntimeError):
            tool.window_identity(user,object(),55,'tokenABC',123)
        created.assert_not_called()
    def test_process_handle_closed_when_creation_query_fails(self):
        tool,_ = self.fixture()
        kernel=SimpleNamespace(OpenProcess=Mock(return_value=88),GetProcessTimes=Mock(return_value=False),CloseHandle=Mock())
        with patch.object(tool.c,'get_last_error',return_value=5,create=True),self.assertRaises(OSError):
            tool.process_created(kernel,123)
        kernel.CloseHandle.assert_called_once_with(88)
    def test_main_rejects_nonfinite_or_unbounded_time_without_native_api(self):
        tool,_ = self.fixture()
        for value in ('nan','inf','0','121'):
            with patch.object(sys,'argv',['verify','--source','x','--output','y','--seconds',value]),patch.object(tool,'run') as run:
                with self.assertRaises(SystemExit):
                    tool.main()
                run.assert_not_called()

    def test_fixed_glyph_does_not_move_when_other_pixels_change(self):
        tool,_=self.fixture()
        cells={(1,1):200,(2,1):255}
        image=SimpleNamespace(size=(4,4),getpixel=lambda point:(0,0,0,cells.get(point,0)))
        self.assertEqual(tool.fixed_pixel_alpha(image,(1,1),'glyph'),200)
        cells.update({(0,0):255,(0,1):255,(0,2):255})
        self.assertEqual(tool.fixed_pixel_alpha(image,(1,1),'glyph'),200)
    def test_original_pixel_alpha_loss_or_geometry_loss_rejected(self):
        tool,_=self.fixture()
        image=SimpleNamespace(size=(4,4),getpixel=lambda point:(0,0,0,0 if point==(1,1) else 255))
        for point,kind in [((1,1),'glyph'),((1,1),'resize'),((1,1),'opaque'),((8,8),'glyph')]:
            with self.assertRaises(RuntimeError):
                tool.fixed_pixel_alpha(image,point,kind)
    def test_reading_fixture_restores_tool_targets_and_clears_velocity(self):
        tool,_=self.fixture()
        class Motion:
            def __init__(self): self.value=8;self.velocity=99
            def snap(self,value): self.value=value;self.velocity=0
        item=SimpleNamespace(_follow_tail=False,_scroll_target=30,_trans_scroll_target=40,
                             _scroll_motion=Motion(),_trans_scroll_motion=Motion(),
                             _scrollbar_show_until=99,personal_style='untouched')
        tool.restore_reading_fixture(item,dict(follow_tail=True,source_target=5,translation_target=6))
        self.assertTrue(item._follow_tail)
        self.assertEqual((item._scroll_target,item._scroll_motion.value,item._scroll_motion.velocity),(5,5,0))
        self.assertEqual((item._trans_scroll_target,item._trans_scroll_motion.value,item._trans_scroll_motion.velocity),(6,6,0))
        self.assertEqual(item._scrollbar_show_until,0)
        self.assertEqual(item.personal_style,'untouched')

    def test_out_of_plan_input_is_not_filtered_or_misreported_as_routing_failure(self):
        tool,_=self.fixture()
        events=[dict(x=418,y=219,kind='left'),dict(x=824,y=336,kind='release')]
        with self.assertRaises(tool.UnexpectedInput):
            tool.assert_planned_events(events,(418,219))
        self.assertEqual(len(events),2)
    def test_fixed_point_and_short_drag_path_are_accepted(self):
        tool,_=self.fixture()
        tool.assert_planned_events([dict(x=418,y=219),dict(x=422,y=217),dict(x=426,y=215)],(418,219))

    def test_setup_activation_refuses_buttons_capture_menus_and_unknown_threads(self):
        tool,_=self.fixture()
        user=SimpleNamespace(GetAsyncKeyState=lambda _:0,GetGUIThreadInfo=lambda *_:False)
        with self.assertRaises(RuntimeError): tool.assert_activation_idle(user,[0])
        user.GetAsyncKeyState=lambda key:0x8000 if key==1 else 0
        with self.assertRaises(tool.UnexpectedInput): tool.assert_activation_idle(user,[0])
        user.GetAsyncKeyState=lambda _:0
        for field,value in [('capture',55),('menu_owner',55),('move_size',55),('flags',2),('flags',4),('flags',8),('flags',16)]:
            def query(thread,pointer,field=field,value=value):
                setattr(ctypes.cast(pointer,ctypes.POINTER(tool.GUIThreadInfo)).contents,field,value)
                return True
            user.GetGUIThreadInfo=query
            with self.assertRaises(tool.UnexpectedInput): tool.assert_activation_idle(user,[0])
    def test_setup_activation_records_all_queried_clean_threads(self):
        tool,_=self.fixture()
        user=SimpleNamespace(GetAsyncKeyState=lambda _:0,GetGUIThreadInfo=lambda *_:True)
        states=tool.assert_activation_idle(user,[0,42,43])
        self.assertEqual([item['thread_id'] for item in states],[0,42,43])
        self.assertTrue(all(item['capture']==0 and item['flags']==0 for item in states))
    def test_setup_click_point_must_be_inside_owned_visible_client_and_hit_counter(self):
        tool,user=self.fixture()
        identity=dict(pid=123,hwnd=55,created_filetime=1,run_token='tokenABC')
        user.IsWindowVisible=lambda _:True
        def client(hwnd,pointer):
            rect=ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents
            rect.left=0;rect.top=0;rect.right=200;rect.bottom=100
            return True
        def origin(hwnd,pointer):
            point=ctypes.cast(pointer,ctypes.POINTER(wintypes.POINT)).contents
            point.x=10;point.y=20
            return True
        user.GetClientRect=client;user.ClientToScreen=origin
        user.WindowFromPoint=lambda point:55
        user.GetAncestor=lambda hwnd,_:hwnd
        with patch.object(tool,'process_created',return_value=1):
            self.assertEqual(tool.owned_client_center(user,None,identity),(110,70))
            user.WindowFromPoint=lambda point:99
            with self.assertRaises(RuntimeError): tool.owned_client_center(user,None,identity)
            user.IsWindowVisible=lambda _:False
            with self.assertRaises(RuntimeError): tool.owned_client_center(user,None,identity)

    def test_zero_foreground_is_reported_as_zero_not_another_application(self):
        tool,user=self.fixture()
        user.GetForegroundWindow=lambda:0
        user.GetGUIThreadInfo=lambda *_:False
        state=tool.foreground_snapshot(user,None)
        self.assertEqual(state['hwnd'],0)
        self.assertFalse(state['valid'])
        self.assertEqual(state['pid'],0)
        self.assertEqual(state['title'],'')
    def test_hidden_tk_foreground_keeps_actual_identity_without_expanding_ownership(self):
        tool,user=self.fixture()
        user.GetForegroundWindow=lambda:55
        user.IsWindowVisible=lambda _:False
        def class_name(_,buffer,__): buffer.value='TkTopLevel';return 10
        user.GetClassNameW=class_name
        user.GetGUIThreadInfo=lambda *_:True
        with patch.object(tool,'process_created',return_value=987):
            state=tool.foreground_snapshot(user,None)
        self.assertEqual((state['hwnd'],state['pid'],state['thread_id']),(55,123,44))
        self.assertEqual(state['class'],'TkTopLevel')
        self.assertFalse(state['visible'])
        self.assertEqual(state['created_filetime'],987)
        self.assertNotIn('allowed_input_target',state)
    def test_foreground_creation_query_denial_is_observed_without_overwriting_identity(self):
        tool,user=self.fixture()
        user.GetForegroundWindow=lambda:55;user.IsWindowVisible=lambda _:True
        user.GetClassNameW=lambda *_:0;user.GetGUIThreadInfo=lambda *_:False
        with patch.object(tool,'process_created',side_effect=OSError('denied')):
            state=tool.foreground_snapshot(user,None)
        self.assertEqual(state['pid'],123)
        self.assertIn('creation_query_error',state)

    def cursor_fixture(self):
        tool=load_tool();state={'point':(10,20),'clamp':None,'set_ok':True,'moves':[]}
        def logical(pointer):
            point=ctypes.cast(pointer,ctypes.POINTER(wintypes.POINT)).contents
            point.x,point.y=state['point'];return True
        def physical(pointer):
            point=ctypes.cast(pointer,ctypes.POINTER(wintypes.POINT)).contents
            point.x,point.y=state['point'][0]*2,state['point'][1]*2;return True
        def clip(pointer):
            rect=ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents
            rect.left=0;rect.top=0;rect.right=1000;rect.bottom=800;return True
        def move(x,y):
            state['moves'].append((x,y))
            if state['set_ok']:state['point']=state['clamp'] or (x,y)
            return state['set_ok']
        user=SimpleNamespace(GetCursorPos=logical,GetPhysicalCursorPos=physical,GetClipCursor=clip,SetCursorPos=move)
        return tool,user,state

    def test_cursor_observation_preserves_logical_physical_and_clip_without_moving(self):
        tool,user,state=self.cursor_fixture();result=tool.cursor_snapshot(user)
        self.assertEqual(result['logical']['point'],[10,20])
        self.assertEqual(result['physical']['point'],[20,40])
        self.assertEqual(result['clip']['rect'],[0,0,1000,800])
        self.assertEqual(state['moves'],[])
        self.assertIn('does not identify',result['clip_owner'])

    def test_clamped_cursor_keeps_exact_planned_and_actual_data_then_stops(self):
        tool,user,state=self.cursor_fixture();state['clamp']=(99,50);record={}
        with self.assertRaises(tool.UnexpectedInput):tool.checked_move(user,(418,219),record)
        self.assertEqual(record['set_cursor_result']['point'],[418,219])
        self.assertTrue(record['set_cursor_result']['success'])
        self.assertEqual(record['checked_cursor']['point'],[99,50])
        self.assertEqual(record['cursor_after_set']['physical']['point'],[198,100])
        self.assertEqual(state['moves'],[(418,219)])

    def test_cursor_move_api_failure_keeps_observations_and_is_not_called_success(self):
        tool,user,state=self.cursor_fixture();state['set_ok']=False;record={}
        with patch.object(tool.c,'get_last_error',return_value=5,create=True):
            with self.assertRaises(OSError):tool.checked_move(user,(418,219),record)
        self.assertFalse(record['set_cursor_result']['success'])
        self.assertEqual(record['set_cursor_result']['last_error'],5)
        self.assertIn('clip',record['cursor_after_set'])

    def test_failed_physical_cursor_query_is_retained_as_unknown(self):
        tool,user,state=self.cursor_fixture();user.GetPhysicalCursorPos=lambda _:False
        result=tool.cursor_snapshot(user)
        self.assertFalse(result['physical']['success'])
        self.assertNotIn('point',result['physical'])
        self.assertEqual(state['moves'],[])

    def test_actual_dpi_contexts_use_native_equality_instead_of_pseudo_handle_integers(self):
        tool,user,state=self.cursor_fixture()
        def semantic(handle):
            value=handle.value if hasattr(handle,'value') else handle
            if value==24592:return -1
            if value==170:return -4
            return ctypes.c_ssize_t(value).value
        user.GetDpiAwarenessContextForProcess=lambda _:24592
        user.GetThreadDpiAwarenessContext=lambda:170
        user.GetWindowDpiAwarenessContext=lambda _:170
        user.GetAwarenessFromDpiAwarenessContext=lambda value:0 if semantic(value)==-1 else 2
        user.AreDpiAwarenessContextsEqual=lambda a,b:semantic(a)==semantic(b)
        user.GetDpiForWindow=lambda _:168
        result=tool.cursor_snapshot(user,123)
        self.assertEqual(result['dpi']['process']['name'],'unaware')
        self.assertEqual(result['dpi']['thread']['name'],'per-monitor-v2')
        self.assertEqual(result['dpi']['window_dpi'],168)
        self.assertEqual(state['moves'],[])

    def movement_fixture(self):
        tool=load_tool();values={'pressed':set(),'capture':0,'pid':123,'flags':0,'ok':True}
        def gui(thread,pointer):
            item=ctypes.cast(pointer,ctypes.POINTER(tool.GUIThreadInfo)).contents
            item.capture=values['capture'];item.flags=values['flags'];return values['ok']
        def owner(hwnd,pointer):
            ctypes.cast(pointer,ctypes.POINTER(wintypes.DWORD)).contents.value=values['pid'];return 77
        user=SimpleNamespace(GetAsyncKeyState=lambda key:0x8000 if key in values['pressed'] else 0,
                             GetGUIThreadInfo=gui,GetWindowThreadProcessId=owner)
        return tool,user,values

    def test_every_move_allows_only_own_held_button_and_verified_process_capture(self):
        tool,user,values=self.movement_fixture();values.update(pressed={1},capture=55);record={}
        tool.assert_movement_state(user,[0,77],{123,124},{0x2},record)
        self.assertEqual(record['gui_threads'][0]['capture_pid'],123)
        values['pid']=999
        with self.assertRaises(tool.UnexpectedInput):tool.assert_movement_state(user,[0],{123,124},{0x2},{})
        values.update(pid=123,pressed={1,4})
        with self.assertRaises(tool.UnexpectedInput):tool.assert_movement_state(user,[0],{123,124},{0x2},{})

    def test_movement_refuses_unknown_gui_or_modal_state_without_releasing_it(self):
        tool,user,values=self.movement_fixture();values['ok']=False
        with self.assertRaises(RuntimeError):tool.assert_movement_state(user,[0],{123},set(),{})
        values.update(ok=True,flags=4)
        with self.assertRaises(tool.UnexpectedInput):tool.assert_movement_state(user,[0],{123},set(),{})
        self.assertEqual(values['flags'],4)


class NativeMessageObservationTests(unittest.TestCase):
    def fixture(self,**kwargs):
        tool=load_tool();self.tool=tool;self.installed={}
        def thread(hwnd,pointer):
            ctypes.cast(pointer,ctypes.POINTER(wintypes.DWORD)).contents.value=tool.os.getpid()
            return tool.threading.get_native_id()
        def class_name(hwnd,buffer,length):buffer.value='TkFixture';return len(buffer.value)
        self.user=SimpleNamespace(IsWindow=Mock(return_value=True),GetWindowThreadProcessId=Mock(side_effect=thread),
            GetAncestor=Mock(side_effect=lambda hwnd,flag:100 if flag==2 or hwnd!=100 else 0),
            GetClassNameW=Mock(side_effect=class_name),GetMessageTime=Mock(return_value=123),
            GetMessageExtraInfo=Mock(return_value=0x1234),GetCapture=Mock(return_value=102),
            ClientToScreen=Mock(return_value=True))
        def install(hwnd,callback,subclass_id,reference):self.installed[hwnd]=reference;return True
        def get(hwnd,callback,subclass_id,reference):
            if hwnd not in self.installed:return False
            ctypes.cast(reference,ctypes.POINTER(ctypes.c_size_t)).contents.value=self.installed[hwnd];return True
        def remove(hwnd,callback,subclass_id):self.installed.pop(hwnd,None);return True
        self.cc=SimpleNamespace(SetWindowSubclass=Mock(side_effect=install),GetWindowSubclass=Mock(side_effect=get),
            RemoveWindowSubclass=Mock(side_effect=remove),DefSubclassProc=Mock(return_value=-12345))
        identity=dict(pid=tool.os.getpid(),hwnd=100,created_filetime=99,run_token='fixture')
        self.args=(self.user,object(),identity,dict(wrapper=100,client=101,canvas=102))
        def create():
            with patch.object(tool,'assert_identity'):
                return tool.NativeMessageObserver(*self.args,comctl=self.cc,**kwargs)
        return create

    def test_observer_records_raw_parameters_and_forwards_every_message_unchanged(self):
        observer=self.fixture()()
        self.user.GetCapture.side_effect=[102,0]
        result=observer._dispatch(102,0x202,0x123456789,0x23456789,observer.subclass_id,observer.subclass_id)
        self.assertEqual(result,-12345)
        self.cc.DefSubclassProc.assert_called_once_with(102,0x202,0x123456789,0x23456789)
        event=observer.snapshot()['events'][0]
        self.assertEqual((event['name'],event['capture_current_thread'],event['message_time']),('WM_LBUTTONUP',102,123))
        self.assertEqual(event['role'],'canvas')
        self.assertTrue(event['mk_lbutton'])
        self.assertTrue(event['capture_covered']);self.assertTrue(event['capture_after_covered'])
        self.assertEqual(event['capture_after_forward'],0)
        self.assertGreaterEqual(event['forward_return_perf'],event['perf'])
        observer._dispatch(101,0x000F,7,8,observer.subclass_id,observer.subclass_id)
        self.cc.DefSubclassProc.assert_called_with(101,0x000F,7,8)
        self.assertEqual(len(observer.snapshot()['events']),1)
        observer.close();self.assertEqual(self.installed,{})

    def test_unobserved_own_capture_is_marked_incomplete_for_absence_inference(self):
        observer=self.fixture()();self.user.GetCapture.return_value=999
        observer._dispatch(102,0x200,0,3,0,0)
        row=observer.snapshot()['events'][0]
        self.assertFalse(row['mk_lbutton']);self.assertFalse(row['capture_covered'])
        self.assertFalse(row['capture_after_covered'])
        self.assertEqual(observer.snapshot()['uncovered_capture_count'],2)
        observer.clear();self.assertEqual(observer.snapshot()['uncovered_capture_count'],2)
        observer.close()

    def test_phase_clear_preserves_native_button_prehistory(self):
        observer=self.fixture()()
        observer._dispatch(102,0x201,1,0,0,0)
        observer.clear()
        self.assertEqual(observer.snapshot()['buttons_at_phase_start'],['left'])
        observer._dispatch(102,0x202,0,0,0,0)
        self.assertEqual(observer.snapshot()['native_buttons'],[])
        self.assertEqual(observer.snapshot()['buttons_at_phase_start'],['left'])
        observer.close()

    def test_both_x_buttons_and_nonclient_buttons_keep_prehistory_across_clear(self):
        for identifier in (1,2):
            for down,up in ((0x20B,0x20C),(0xAB,0xAC)):
                observer=self.fixture()()
                observer._dispatch(102,down,identifier<<16,0,0,0);observer.clear()
                self.assertEqual(observer.snapshot()['buttons_at_phase_start'],['x'+str(identifier)])
                observer._dispatch(102,up,identifier<<16,0,0,0)
                self.assertEqual(observer.snapshot()['native_buttons'],[])
                observer.close()

    def test_complete_hwnd_set_is_validated_before_any_install(self):
        for failure in ('ancestor','thread','class'):
            create=self.fixture()
            if failure=='ancestor':self.user.GetAncestor.return_value=999;self.user.GetAncestor.side_effect=None
            elif failure=='thread':self.user.GetWindowThreadProcessId.return_value=999;self.user.GetWindowThreadProcessId.side_effect=None
            else:self.user.GetClassNameW.return_value=0;self.user.GetClassNameW.side_effect=None
            with self.subTest(failure=failure),self.assertRaises(RuntimeError):create()
            self.cc.SetWindowSubclass.assert_not_called()

    def test_partial_install_failure_removes_only_its_installed_callback(self):
        create=self.fixture();real_install=self.cc.SetWindowSubclass.side_effect
        self.cc.SetWindowSubclass.side_effect=lambda hwnd,*rest:False if hwnd==101 else real_install(hwnd,*rest)
        with self.assertRaises(RuntimeError):create()
        self.assertEqual(self.installed,{})
        self.assertEqual(self.cc.RemoveWindowSubclass.call_args.args[0],100)

    def test_overflow_or_callback_error_is_not_hidden_by_clear_and_still_forwards(self):
        observer=self.fixture(limit=1)()
        observer._dispatch(102,0x200,1,2,0,0);observer._dispatch(102,0x202,0,3,0,0)
        self.assertEqual(observer.snapshot()['overflow'],1)
        observer.clear()
        with self.assertRaises(RuntimeError):self.tool.require_complete_native_observation(observer.snapshot())
        self.user.GetMessageTime.side_effect=RuntimeError('read failed')
        observer._dispatch(102,0x202,0,3,0,0)
        self.cc.DefSubclassProc.assert_called_with(102,0x202,0,3)
        self.assertIn('read failed',observer.snapshot()['errors'])
        with self.assertRaises(RuntimeError):observer.close()
        self.assertEqual(self.installed,{})

    def test_cleanup_refuses_other_thread_and_keeps_callback_alive_on_remove_failure(self):
        observer=self.fixture()()
        with patch.object(self.tool.threading,'get_native_id',return_value=observer.thread+1),self.assertRaises(RuntimeError):observer.close()
        self.cc.RemoveWindowSubclass.assert_not_called()
        self.assertEqual(len(self.installed),3)
        self.cc.RemoveWindowSubclass.side_effect=lambda *args:False
        with self.assertRaises(RuntimeError):observer.close()
        self.assertTrue(all(x['installed'] for x in observer.bindings.values()))
        self.assertIsNotNone(observer.callback)
        self.assertIn(observer,self.tool.NativeMessageObserver._active)
        self.cc.RemoveWindowSubclass.side_effect=lambda hwnd,*args:self.installed.pop(hwnd,None) is not None
        with self.assertRaises(RuntimeError):observer.close()  # Failure remains recorded.
        self.assertEqual(self.installed,{})
        self.assertNotIn(observer,self.tool.NativeMessageObserver._active)

    def test_destroy_removes_only_own_subclass_before_forwarding(self):
        observer=self.fixture()()
        observer._dispatch(102,0x82,0,0,observer.subclass_id,observer.subclass_id)
        self.assertNotIn(102,self.installed)
        self.assertFalse(observer.bindings[102]['installed'])
        self.cc.DefSubclassProc.assert_called_with(102,0x82,0,0)
        observer.close();self.assertEqual(self.installed,{})

    def test_reused_or_foreign_hwnd_is_not_queried_or_removed_during_cleanup(self):
        observer=self.fixture()();owned=self.user.GetWindowThreadProcessId.side_effect
        self.user.GetWindowThreadProcessId.side_effect=lambda hwnd,pointer:0
        self.cc.GetWindowSubclass.reset_mock()
        with self.assertRaises(RuntimeError):observer.close()
        self.cc.GetWindowSubclass.assert_not_called();self.cc.RemoveWindowSubclass.assert_not_called()
        self.user.GetWindowThreadProcessId.side_effect=owned
        with self.assertRaises(RuntimeError):observer.close()
        self.assertEqual(self.installed,{})

    def test_alias_hwnds_are_observed_once_and_ownership_reference_cannot_be_replaced(self):
        create=self.fixture();self.args[3]['client']=102
        observer=create();self.assertEqual(self.cc.SetWindowSubclass.call_count,2)
        self.assertEqual(observer.bindings[102]['role'],'client+canvas')
        self.installed[102]=observer.subclass_id+1
        with self.assertRaises(RuntimeError):observer.close()
        self.assertIn(102,self.installed)
        self.installed[102]=observer.subclass_id
        with self.assertRaises(RuntimeError):observer.close()
        self.assertEqual(self.installed,{})


class NativeRoutingContractTests(unittest.TestCase):
    def observation(self,rows=None):
        return dict(events=rows or [],total=len(rows or []),overflow=0,errors=[],
                    bindings={str(i):dict(role=role,installed=True) for i,role in [(100,'wrapper'),(101,'client'),(102,'canvas')]},
                    buttons_at_phase_start=[],native_buttons=[])

    def row(self,number,perf,name='WM_MOUSEMOVE',point=(734,340),wparam=0):
        return dict(number=number,perf=perf,forward_return_perf=perf+.00001,hwnd=102,name=name,wparam=wparam,lparam=0,
                    mk_lbutton=bool(wparam&1),capture_current_thread=0,capture_after_forward=0,
                    capture_covered=True,capture_after_covered=True,screen_point=list(point))

    def release_fixture(self):
        return [dict(kind='release',x=734,y=340,perf=1.01,latest_native_message_number=1)],self.observation([self.row(1,1)])

    def test_only_evidenced_state_release_is_retained_as_diagnostic(self):
        tool=load_tool();events,observation=self.release_fixture();original=copy.deepcopy(events)
        result=tool.classify_blocked_receiver(events,observation)
        self.assertEqual(result['native_operations'],0)
        self.assertEqual(result['diagnostics'][0]['kind'],'tk_release_without_local_native_button_message')
        self.assertEqual(events,original)
        self.assertEqual(tool.classify_blocked_receiver([],self.observation())['diagnostics'],[])

    def test_empty_tk_callbacks_do_not_hide_unbalanced_native_button_history(self):
        tool=load_tool()
        for field in ('buttons_at_phase_start','native_buttons'):
            observation=self.observation();observation[field]=['x2']
            with self.subTest(field=field),self.assertRaises(tool.UnexpectedInput):
                tool.classify_blocked_receiver([],observation)

    def test_overlay_keeps_zero_tk_without_claiming_complete_previous_menu_history(self):
        tool=load_tool();events,observation=self.release_fixture()
        with self.assertRaises(tool.UnexpectedInput):tool.require_blocked_overlay(events,observation)
        observation['buttons_at_phase_start']=['right'];observation['native_buttons']=['right']
        result=tool.require_blocked_overlay([],observation)
        self.assertEqual(result['receiver'],'overlay')
        self.assertNotIn('diagnostics',result)
        observation['events'][0]['name']='WM_LBUTTONUP'
        with self.assertRaises(tool.UnexpectedInput):tool.require_blocked_overlay([],observation)

    def test_real_button_wheel_held_move_or_unknown_native_message_is_never_excused(self):
        tool=load_tool()
        for name,flags in [('WM_LBUTTONUP',0),('WM_LBUTTONDOWN',1),('WM_MOUSEWHEEL',120<<16),
                           ('WM_RBUTTONUP',0),('WM_MBUTTONDOWN',16),('WM_XBUTTONDOWN',32),
                           ('WM_MOUSEMOVE',1),('unknown',0)]:
            with self.subTest(name=name,flags=flags),self.assertRaises(tool.UnexpectedInput):
                tool.classify_blocked_receiver([],self.observation([self.row(1,1,name,wparam=flags)]))

    def test_unknown_callback_wrong_point_time_latest_or_button_prehistory_is_rejected(self):
        tool=load_tool()
        for mutation in ('kind','point','future','stale_latest','missing_latest','bool_latest','prehistory','end_state'):
            events,observation=self.release_fixture()
            if mutation=='kind':events[0]['kind']='left'
            elif mutation=='point':events[0]['x']+=1
            elif mutation=='future':events[0]['perf']=.9
            elif mutation=='stale_latest':observation['events'].append(self.row(2,1.005));observation['total']=2
            elif mutation=='missing_latest':events[0].pop('latest_native_message_number')
            elif mutation=='bool_latest':events[0]['latest_native_message_number']=True
            elif mutation=='prehistory':observation['buttons_at_phase_start']=['left']
            else:observation['native_buttons']=['left']
            with self.subTest(mutation=mutation),self.assertRaises(tool.UnexpectedInput):
                tool.classify_blocked_receiver(events,observation)

    def test_incomplete_coverage_overflow_errors_and_missing_history_never_prove_absence(self):
        tool=load_tool()
        for mutation in ('overflow','errors','coverage','after_coverage','binding','history'):
            events,observation=self.release_fixture()
            if mutation=='overflow':observation['overflow']=1
            elif mutation=='errors':observation['errors']=['observation failed']
            elif mutation=='coverage':observation['events'][0]['capture_covered']=False
            elif mutation=='after_coverage':observation['events'][0]['capture_after_covered']=False
            elif mutation=='binding':observation['bindings']['101']['installed']=False
            else:observation['events'][0]['number']=2
            with self.subTest(mutation=mutation),self.assertRaises(RuntimeError):tool.classify_blocked_receiver(events,observation)

    def delivery_fixture(self):
        flags=[2,4,[8,16],2,4,2,4,2,4,0x800]
        gestures=['left','left','right','double','double','double','double','drag','drag','wheel']
        names={2:'WM_LBUTTONDOWN',4:'WM_LBUTTONUP',8:'WM_RBUTTONDOWN',16:'WM_RBUTTONUP',0x800:'WM_MOUSEWHEEL'}
        actions=[];rows=[]
        for i,(flag,gesture) in enumerate(zip(flags,gestures)):
            packet=flag if isinstance(flag,list) else [flag]
            point=(426,215) if i==8 else (418,219)
            actions.append(dict(flags=flag,gesture=gesture,point=point,inject_perf=10+i,sendinput_inserted=len(packet)))
            for j,item in enumerate(packet):
                rows.append(self.row(0,10+i+.1+j*.1,names[item],point,120<<16 if item==0x800 else 1 if item==2 else 2 if item==8 else 0))
            if i==7:rows.append(self.row(0,10+i+.5,point=(426,215),wparam=1))
        for number,row in enumerate(rows,1):row['number']=number
        events=[dict(kind=kind) for kind in ('left','release','right','double','drag','wheel')]
        return events,self.observation(rows),actions

    def test_receiving_process_requires_independent_complete_native_plan_and_drag(self):
        tool=load_tool();events,observation,actions=self.delivery_fixture()
        result=tool.require_native_delivery(events,observation,actions)
        self.assertEqual(result['matched_native_packets'],11)
        self.assertTrue(result['native_drag'])

    def test_actions_and_native_cannot_together_omit_or_relabel_a_planned_gesture(self):
        tool=load_tool()
        for mutation in ('omit_left','relabel','float_flags'):
            events,observation,actions=self.delivery_fixture()
            if mutation=='omit_left':
                actions=actions[2:];observation['events']=observation['events'][2:]
                for number,row in enumerate(observation['events'],1):row['number']=number
                observation['total']=len(observation['events'])
            elif mutation=='relabel':actions[0]['gesture']='double'
            else:actions[0]['flags']=2.0
            with self.subTest(mutation=mutation),self.assertRaises(RuntimeError):
                tool.require_native_delivery(events,observation,actions)

    def test_receiving_mouse_move_cannot_hide_an_unplanned_held_button_or_path(self):
        tool=load_tool()
        for flags,point in [(16,(418,219)),(32,(418,219)),(1,(999,999)),(0,(426,215))]:
            events,observation,actions=self.delivery_fixture()
            if flags==0:
                row=next(x for x in observation['events'] if x['name']=='WM_MOUSEMOVE')
                row['wparam']=0;row['mk_lbutton']=False
            else:
                observation['events'].insert(1,self.row(0,10.5,point=point,wparam=flags))
                for number,row in enumerate(observation['events'],1):row['number']=number
                observation['total']=len(observation['events'])
            with self.subTest(flags=flags,point=point),self.assertRaises(RuntimeError):
                tool.require_native_delivery(events,observation,actions)

    def test_native_delivery_rejects_missing_reordered_or_wrong_time_position_and_drag(self):
        tool=load_tool()
        for mutation in ('missing','order','time','point','drag','wheel','insertion','unknown','unknown_callback'):
            events,observation,actions=self.delivery_fixture()
            if mutation=='missing':observation['events'].pop();observation['total']-=1
            elif mutation=='order':observation['events'][0]['name']='WM_LBUTTONUP'
            elif mutation=='time':observation['events'][0]['perf']=9
            elif mutation=='point':observation['events'][0]['screen_point'][0]+=1
            elif mutation=='drag':
                next(x for x in observation['events'] if x['name']=='WM_MOUSEMOVE')['mk_lbutton']=False
            elif mutation=='wheel':observation['events'][-1]['wparam']=0
            elif mutation=='insertion':actions[2]['sendinput_inserted']=1
            elif mutation=='unknown':observation['events'][0]['name']='WM_MBUTTONDOWN'
            else:events.append(dict(kind='unknown'))
            with self.subTest(mutation=mutation),self.assertRaises(RuntimeError):tool.require_native_delivery(events,observation,actions)


if __name__ == '__main__':
    unittest.main()
