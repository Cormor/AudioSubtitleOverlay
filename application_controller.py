"""Session ownership and command/event boundaries, independent of Tk and models."""
from __future__ import annotations
from dataclasses import dataclass, replace
import threading
from typing import Callable


@dataclass(frozen=True)
class SessionState:
    session_id: int = 0
    phase: str = "idle"
    translation_phase: str = "idle"
    error: str = ""


class SessionController:
    """One active pipeline; every queued event is checked again on delivery."""
    def __init__(self, dispatch: Callable, pipeline_factory: Callable):
        self._dispatch = dispatch
        self._factory = pipeline_factory
        self._lock = threading.RLock()
        self._state = SessionState()
        self.pipeline = None
        self._closed = False

    @property
    def state(self):
        with self._lock:
            return self._state

    def start(self, options, handlers):
        with self._lock:
            if self._closed or self.pipeline is not None or self._state.phase == "stopping":
                raise RuntimeError("字幕会话尚未释放，不能重复开始")
            session = self._state.session_id + 1
            self._state = SessionState(session, "preparing")
            def post(name, *args):
                self._dispatch(self._deliver, session, name, handlers, args)
            callbacks = {name: (lambda *args, key=name: post(key, *args)) for name in handlers}
            try:
                pipeline = self._factory(options, **callbacks)
                self.pipeline = pipeline
                pipeline.start()
            except Exception:
                self.pipeline = None
                self._state = replace(self._state, phase="error")
                raise
            return pipeline

    def _deliver(self, session, name, handlers, args):
        with self._lock:
            if self._closed or session != self._state.session_id:
                return
            if self._state.phase == "stopping" and name != "on_stopped":
                return
            if name == "on_stopped":
                self.pipeline = None
                self._state = replace(self._state, phase="stopped")
            elif name == "on_error":
                self._state = replace(self._state, error=str(args[0]))
            elif name in {"on_translation", "on_translation_update"}:
                self._state = replace(self._state, translation_phase="ready")
            elif name == "on_metrics" and args:
                changes = {key:args[0][key] for key in ("phase", "translation_phase") if key in args[0]}
                self._state = replace(self._state, **changes)
            handlers[name](*args)

    def update_options(self, options):
        with self._lock:
            if self._closed or self.pipeline is None or self._state.phase == "stopping":
                return False
            return self.pipeline.update_options(options)

    def refresh_translation_context(self):
        with self._lock:
            if self._closed or self.pipeline is None or self._state.phase == "stopping":
                return False
            return self.pipeline.refresh_translation_context()

    def stop(self):
        with self._lock:
            if self.pipeline is None or self._state.phase == "stopping":
                return False
            self._state = replace(self._state, phase="stopping")
            self.pipeline.stop()
            return True

    def shutdown(self, timeout=7.0):
        self.stop()
        with self._lock:
            self._closed = True
            pipeline = self.pipeline
        thread = getattr(pipeline, "_thread", None)
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                return False
        with self._lock:
            self.pipeline = None
            self._state = replace(self._state, phase="stopped")
        return True
