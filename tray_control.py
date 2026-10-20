"""Windows notification-area controls; all application actions go through Tk's queue."""

import logging
import threading

from brand import APP_NAME, asset_path


class TrayController:
    def __init__(self, post, *, show_settings, show_overlay, hide_overlay, start, stop, close):
        self._post = post
        self._actions = dict(show_settings=show_settings, show_overlay=show_overlay,
                             hide_overlay=hide_overlay, start=start, stop=stop, close=close)
        self._icon = None
        self._ready = threading.Event()
        self._recognizing = False

    @property
    def available(self):
        return self._icon is not None and self._ready.is_set()

    def start(self):
        if self._icon is not None:
            return
        try:
            import pystray
            from PIL import Image

            def action(name):
                return lambda _icon, _item: self._post(self._actions[name])

            item = pystray.MenuItem
            menu = pystray.Menu(
                item("打开设置", action("show_settings"), default=True),
                item("显示字幕", action("show_overlay")),
                item("隐藏字幕", action("hide_overlay")),
                pystray.Menu.SEPARATOR,
                item("开始识别", action("start"), enabled=lambda _: not self._recognizing),
                item("停止识别", action("stop"), enabled=lambda _: self._recognizing),
                pystray.Menu.SEPARATOR,
                item("退出声幕", action("close")),
            )
            with Image.open(asset_path("icon.png")) as image:
                self._icon = pystray.Icon("AudioSubtitleOverlay", image.copy(),
                                          f"{APP_NAME} · 已停止", menu)
            self._icon.run_detached(setup=self._on_ready)
        except Exception:
            logging.exception("托盘图标初始化失败；保留任务栏入口")
            self.close()

    def _on_ready(self, icon):
        icon.visible = True
        self._ready.set()

    def set_running(self, recognizing):
        self._recognizing = bool(recognizing)
        if self.available:
            self._icon.title = f"{APP_NAME} · {'识别中' if recognizing else '已停止'}"
            self._icon.update_menu()

    def close(self):
        icon, self._icon = self._icon, None
        self._ready.clear()
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logging.exception("停止托盘图标失败")
