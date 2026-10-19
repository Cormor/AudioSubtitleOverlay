"""可拖动和调整大小的双语悬浮窗。

实时阅读：原文左栏、译文右栏并排，共享行起点与推进节奏。
回看历史时两栏同步滚动，同一条目 y 对齐；窗口尺寸不追随实时段落变化。
"""

from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import messagebox
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont

from layered_window import LayeredWindow
from subtitle_layout import (
    EntryView,
    Motion1D,
    ParagraphLedger,
    StyleMetrics,
    build_lines,
    compute_geometry,
    reading_positions,
    ReadingCursor,
    TranslationLaneState,
    reading_window_height,
    paired_window_height,
    scrollbar_geometry,
)


class OverlayWindow:
    """在桌面顶层显示源语言和译文。"""

    minimum_font_size = 12
    minimum_window_width = 320
    # 默认字号下，保留上下边距后可以缩到一行字幕的高度。
    minimum_window_height = 68
    # PIL 已经提供字形抗锯齿；使用两倍分辨率保留清晰度，避免三倍重绘的额外开销。
    render_scale = 2
    # 各类过渡动画时长（毫秒）。
    scroll_animation_ms = 170.0
    # 重绘合帧周期（毫秒）：识别频率不直接决定上屏频率。
    frame_interval_ms = 16
    resize_grip_size = 40
    # 命中区大于绘制区，边角更好抓；分层窗透明像素会穿透点击。
    resize_grip_hit = 52
    resize_grip_hit_h = 44
    corner_radius = 14

    def __init__(self, master: tk.Misc, settings, on_open_settings, on_close) -> None:
        self.settings = settings
        self.on_presented = None
        self._on_open_settings = on_open_settings
        self._on_close = on_close
        self._entries: list[tuple[str, str]] = []
        self._entry_keys = []
        self._next_entry_key = 0
        self._entry_revision_modes: list[str] = []
        self._entry_translation_pending: list[bool] = []
        self._same_language = False
        self._translation_lane_state = TranslationLaneState()
        self._translation_activity = None
        # 每个条目分别标记为稳定正文或即时草稿；草稿只允许尾部修订。
        self._entries_view: list[EntryView] = []
        self._follow_tail = True
        self._auto_scroll_after = None
        self._scroll_target = 0.0
        self._trans_scroll_target = 0.0
        self._trans_scroll_motion = None  # 懒创建，与 _scroll_motion 同参
        self._source_tail_offset = 0
        self._trans_tail_offset = 0
        self._active_scroll_style = "source"
        self._ledger = ParagraphLedger()
        self._reading_cursors = {style: ReadingCursor() for style in ("source", "translation")}
        self._reading_signature = None
        self._layout_cache_key = None
        self._layout_cache = None
        self._source_text = ""
        self._translation_text = ""
        self._source_label = "源语言"
        self._translation_label = "译文"
        self._drag_mode = ""
        self._drag_origin = (0, 0)
        self._grip_hover = False
        self._scrollbar_show_until = 0.0
        self._lane_idle_after_id = None
        self._geometry_origin = (0, 0, 0, 0)
        self._redraw_pending = False
        self._redraw_after = None
        self._animation_after = None
        self._user_resized = False
        self._content_height = 0
        self._scrollable = False
        self._line_starts = []
        self._scroll_line_starts = []
        self._tail_offset = 0
        self._layout_lines = []
        self._display_font_size = max(int(self.settings.overlay_font_size), 10)
        self._display_translation_font_size = max(
            int(self.settings.overlay_translation_font_size), 10
        )

        self._show_translation = True
        self._separate_layers = False
        self.window = tk.Toplevel(master)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.geometry(self.settings.overlay_geometry)
        self.window.wm_attributes("-topmost", self.settings.overlay_topmost)
        self.canvas = tk.Canvas(self.window, highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.text_window = self.window
        self.text_canvas = self.canvas
        self.window.update_idletasks()
        self._layer = LayeredWindow(self.window)
        self._fonts = {}

        # 记录用户选定的阅读区高度，不追随段落长度变化。
        self._cap_height = max(
            self.minimum_window_height,
            self._parse_geometry_height(self.settings.overlay_geometry),
        )
        self._scroll_motion = Motion1D(self.scroll_animation_ms, value=0.0)
        self._trans_scroll_motion = Motion1D(self.scroll_animation_ms, value=0.0)
        self._height_anchor_bottom = False
        self._height_animation_bottom = 0

        for canvas in {self.canvas, self.text_canvas}:
            canvas.bind("<ButtonPress-1>", self._on_button_press)
            canvas.bind("<B1-Motion>", self._on_drag)
            canvas.bind("<ButtonRelease-1>", self._on_button_release)
            canvas.bind("<Motion>", self._on_motion)
            canvas.bind("<MouseWheel>", self._on_mouse_wheel)
            canvas.bind("<Button-4>", self._on_mouse_wheel)
            canvas.bind("<Button-5>", self._on_mouse_wheel)
            canvas.bind("<Button-3>", self._show_menu)
        self.window.bind("<Configure>", self._on_configure)

        self._menu = tk.Menu(self.window, tearoff=False)
        self._menu.add_command(label="打开设置窗口", command=self._on_open_settings)
        self._menu.add_command(label="回到最新字幕", command=self._scroll_to_latest)
        self._menu.add_command(label="导出字幕为 TXT…", command=self._export_txt)
        self._menu.add_command(label="隐藏悬浮窗", command=self.hide)
        self._menu.add_separator()
        self._menu.add_command(label="退出程序", command=self._on_close)

    @staticmethod
    def _parse_geometry_height(geometry: str) -> int:
        """从“宽x高+X+Y”几何串解析用户高度。"""
        try:
            return int(geometry.split("x", 1)[1].split("+", 1)[0])
        except (AttributeError, ValueError, IndexError):
            return 0

    @staticmethod
    def _rgba_with_alpha(color: str, alpha: int) -> tuple:
        """把 #rrggbb 转成带指定 Alpha 的 RGBA 元组。"""
        value = (color or "#101827").lstrip("#")
        if len(value) >= 6:
            r = int(value[0:2], 16)
            g = int(value[2:4], 16)
            b = int(value[4:6], 16)
        else:
            r = g = b = 16
        return (r, g, b, max(0, min(255, alpha)))

    def _in_resize_grip(self, x: int, y: int) -> bool:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        return x >= width - self.resize_grip_hit and y >= height - self.resize_grip_hit_h

    def _export_txt(self) -> None:
        """把当前原文+译文导出为本地 TXT，方便二次剪辑/存档。"""
        if not self._entries_view:
            try:
                messagebox.showinfo("导出字幕", "还没有可导出的字幕内容。")
            except Exception:
                pass
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        initial = f"字幕_{stamp}.txt"
        try:
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                parent=self.window,
                title="导出字幕为 TXT",
                defaultextension=".txt",
                initialfile=initial,
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
            )
        except Exception:
            path = ""
        if not path:
            return
        try:
            lines = []
            for index, entry in enumerate(self._entries_view, 1):
                source = (entry.source or "").strip()
                translation = (entry.translation or "").strip()
                if not source and not translation:
                    continue
                lines.append(f"[{index}]")
                lines.append(f"原文：{source or '（空）'}")
                if translation:
                    lines.append(f"译文：{translation}")
                elif entry.translation_pending:
                    lines.append("译文：（翻译中…）")
                else:
                    lines.append("译文：（无）")
                lines.append("")
            text = "\n".join(lines).rstrip() + "\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            messagebox.showinfo(
                "导出字幕",
                f"已保存：\n{path}\n\n共 {len(self._entries_view)} 条。",
            )
        except OSError as error:
            messagebox.showerror("导出字幕", f"保存失败：{error}")

    def show(self) -> None:
        """显示悬浮窗。"""
        self.window.deiconify()
        if self._separate_layers:
            self.text_window.deiconify()
            self.text_window.lift(self.window)
        self.window.update_idletasks()
        self._sync_layer_geometry()
        self._redraw()

    def hide(self) -> None:
        """隐藏悬浮窗。"""
        self.persist_geometry()
        self._cancel_auto_scroll()
        if self._separate_layers:
            self.text_window.withdraw()
        self.window.withdraw()

    def persist_geometry(self) -> None:
        """保存用户阅读区的宽高和位置。"""
        if not self.window.winfo_exists():
            return
        width = self.window.winfo_width()
        x = self.window.winfo_x()
        y = self.window.winfo_y()
        self.settings.overlay_geometry = f"{width}x{round(self._cap_height)}+{x}+{y}"

    def set_content(
        self,
        source_text: str,
        translation_text: str,
        source_label: str,
        translation_label: str,
    ) -> None:
        """兼容单段文本的更新入口。"""
        self._source_text = source_text
        self._translation_text = translation_text
        self._source_label = source_label
        self._translation_label = translation_label
        self.set_entries([(source_text, translation_text)])

    def set_entries(self, entries, revision_modes=None, translation_pending=None) -> None:
        """保留各段正文，并区分稳定结果和只应改动尾部的即时草稿。"""
        new_entries = [tuple(entry) for entry in entries]
        if revision_modes is None:
            # 直接使用悬浮窗接口时，输入默认为已经确认的正文。
            new_revision_modes = ["stable"] * len(new_entries)
        else:
            new_revision_modes = [
                mode if mode in {"stable", "preview"} else "stable"
                for mode in list(revision_modes)[: len(new_entries)]
            ]
            new_revision_modes.extend(
                ["stable"] * (len(new_entries) - len(new_revision_modes))
            )
        pending = list(translation_pending or ())[:len(new_entries)]
        pending.extend([False] * (len(new_entries) - len(pending)))
        if self._entries == new_entries and self._entry_revision_modes == new_revision_modes and pending == self._entry_translation_pending:
            return
        if self._entries and new_entries and len(new_entries) <= len(self._entries) and self._entries[0] != new_entries[0]:
            for cursor in self._reading_cursors.values():
                cursor.reset()
        now = time.perf_counter()
        if [entry[0] for entry in new_entries] != [entry[0] for entry in self._entries]:
            self._translation_lane_state.source_updated(now)
            self._schedule_lane_idle_check()
        # 兼容直接调用悬浮窗的入口；正式输出用显示模块的活动序号，能识别同文新版本。
        if self._translation_activity is None and any(entry[1].strip() and (i >= len(self._entries) or entry[1] != self._entries[i][1])
               for i, entry in enumerate(new_entries)):
            self._translation_lane_state.translation_output(now)
            self._schedule_lane_idle_check()
        # 淘汰历史头部后仍复用其余段落的换行缓存，避免每次重排全部历史。
        shift = 0
        if self._entries and new_entries and self._entries[0] != new_entries[0]:
            shift = next((i for i, entry in enumerate(self._entries) if entry == new_entries[0]), 0)
        keys = self._entry_keys[shift:shift + len(new_entries)]
        while len(keys) < len(new_entries):
            keys.append(self._next_entry_key)
            self._next_entry_key += 1
        self._entry_keys = keys
        self._entries = new_entries
        self._entry_revision_modes = new_revision_modes
        self._entry_translation_pending = pending
        self._entries_view = [
            EntryView(source, translation, mode == "preview", waiting, key)
            for (source, translation), mode, waiting, key in zip(new_entries, new_revision_modes, pending, keys)
        ]
        self._layout_cache_key = None
        self._request_redraw()

    def reset_content(self) -> None:
        """新识别会话清空旧会话的显示内容。"""
        self._translation_lane_state.reset()
        self._entries = []
        self._entry_keys = []
        self._entry_revision_modes = []
        self._entry_translation_pending = []
        self._entries_view = []
        self._follow_tail = True
        self._cancel_auto_scroll()
        self._ledger.clear()
        for cursor in self._reading_cursors.values():
            cursor.reset()
        self._layout_cache_key = None
        self._scroll_motion.snap(0.0)
        self._trans_scroll_motion.snap(0.0)
        self._scroll_target = 0.0
        self._trans_scroll_target = 0.0
        self._request_redraw()

    def _scroll_to_latest(self) -> None:
        self._cancel_auto_scroll()
        for cursor in self._reading_cursors.values():
            cursor.reset()
        self._follow_tail = True
        self._set_scroll_position(self._full_tail_offset(), style="source")
        self._set_scroll_position(getattr(self, "_trans_tail_offset", 0), style="translation")
        self._request_redraw()

    def _cancel_auto_scroll(self) -> None:
        if self._auto_scroll_after is not None:
            self.window.after_cancel(self._auto_scroll_after)
            self._auto_scroll_after = None
        if getattr(self, "_lane_idle_after_id", None) is not None:
            try:
                self.window.after_cancel(self._lane_idle_after_id)
            except tk.TclError:
                pass
            self._lane_idle_after_id = None

    def _set_scroll_position(self, target: float, instant: bool = False, style: str = "source") -> None:
        """设置某一栏的滚动目标；识别更新不跳变，由动画平滑逼近。"""
        del style
        motion = self._scroll_motion
        max_scroll = self._full_tail_offset()
        target = max(0.0, min(float(target), max_scroll))
        self._scroll_target = target
        interval_ms = float(getattr(self.settings, "overlay_scroll_interval", 0) or 0) * 1000.0
        if interval_ms > 0:
            motion.duration_ms = min(
                self.scroll_animation_ms,
                max(90.0, interval_ms),
            )
        motion.set_target(
            target,
            instant=instant or not self._animations_enabled,
        )

    @property
    def _animations_enabled(self) -> bool:
        return bool(getattr(self.settings, "overlay_animation", True))

    def _advance_reading(self) -> None:
        self._auto_scroll_after = None
        self._request_redraw()

    def apply_settings(self) -> None:
        """应用颜色、两层透明度、字号和置顶设置。"""
        self.window.wm_attributes("-topmost", bool(self.settings.overlay_topmost))
        self._layout_cache_key = None
        self._request_redraw()

    def close(self) -> None:
        """销毁悬浮窗。"""
        if self.window.winfo_exists():
            self._cancel_auto_scroll()
            if self._animation_after is not None:
                self.window.after_cancel(self._animation_after)
                self._animation_after = None
            if self._redraw_after is not None:
                self.window.after_cancel(self._redraw_after)
                self._redraw_after = None
            self.persist_geometry()
            if self._separate_layers and self.text_window.winfo_exists():
                self.text_window.destroy()
            self.window.destroy()

    # ------------------------------------------------------------------
    # 重绘与动画
    # ------------------------------------------------------------------

    def _request_redraw(self) -> None:
        """把多个后台结果合并为一帧重绘；动画期间由动画循环接管。"""
        if not self.window.winfo_exists() or not self.window.winfo_viewable():
            return
        self._ensure_animation_loop()
        if self._animation_after is not None or self._redraw_pending:
            return
        self._redraw_pending = True
        self._redraw_after = self.window.after(self.frame_interval_ms, self._finish_redraw)

    def _finish_redraw(self) -> None:
        """执行合并后的重绘请求。"""
        self._redraw_after = None
        self._redraw_pending = False
        self._redraw()

    def _ensure_animation_loop(self) -> None:
        """存在未完成的过渡时驱动动画循环；结束后自动停机。"""
        if self._animation_after is None and self._has_active_animation(time.perf_counter()):
            self._animation_after = self.window.after(self.frame_interval_ms, self._animation_tick)

    def _has_active_animation(self, now: float) -> bool:
        return self._scroll_motion.running or self._trans_scroll_motion.running

    def _animation_tick(self) -> None:
        self._animation_after = None
        if not self.window.winfo_exists():
            return
        self._advance_motions(time.perf_counter())
        self._redraw()
        self._ensure_animation_loop()

    def _advance_motions(self, now: float) -> None:
        """推进历史回看的滚动动画（两栏各自推进）。"""
        self._scroll_motion.advance(now)
        self._trans_scroll_motion.advance(now)

    def _redraw(self) -> None:
        """固定用户选定的窗口宽度与字号，内容按当前动画状态合成。"""
        if (
            not self.window.winfo_exists()
            or not self.window.winfo_viewable()
            or self.canvas.winfo_width() <= 1
        ):
            return
        font_size = max(int(self.settings.overlay_font_size), self.minimum_font_size)
        self._display_font_size = font_size
        self._draw_content(font_size, time.perf_counter())
        self._ensure_animation_loop()

    def set_translation_visible(self, visible: bool) -> None:
        """同语言时不绘制译文标签、占位文字和段落间距。"""
        if self._show_translation == visible:
            return
        self._show_translation = visible
        self._layout_cache_key = None
        self._request_redraw()

    def set_translation_context(self, source_language, target_language, *, automatic=False, translation_activity=None) -> None:
        before = self._translation_lane_visible()
        self._translation_lane_state.configure(source_language, target_language, automatic)
        if translation_activity is not None and translation_activity != self._translation_activity:
            if self._translation_activity is not None and translation_activity > self._translation_activity:
                self._translation_lane_state.translation_output(time.perf_counter())
                self._schedule_lane_idle_check()
            self._translation_activity = translation_activity
        self._run_lane_idle_check()
        same = bool(source_language and source_language == target_language)
        if same != self._same_language or before != self._translation_lane_visible():
            self._same_language = same
            self._reading_cursors["translation"].reset()
            self._layout_cache_key = None
            self._request_redraw()

    def _schedule_lane_idle_check(self) -> None:
        """最后一次原文/译文后按 timeout 定时检查，避免只靠快照回调。"""
        if getattr(self, "_lane_idle_after_id", None) is not None:
            try:
                self.window.after_cancel(self._lane_idle_after_id)
            except tk.TclError:
                pass
            self._lane_idle_after_id = None
        timeout = float(getattr(self._translation_lane_state, "timeout", 5.0) or 5.0)
        try:
            self._lane_idle_after_id = self.window.after(
                max(200, int(timeout * 1000) + 50),
                self._on_lane_idle_timer,
            )
        except tk.TclError:
            pass

    def _on_lane_idle_timer(self) -> None:
        self._lane_idle_after_id = None
        self._run_lane_idle_check()

    def _run_lane_idle_check(self) -> None:
        before = self._translation_lane_visible()
        self._translation_lane_state.maybe_collapse_if_idle(time.perf_counter())
        if before != self._translation_lane_visible():
            self._layout_cache_key = None
            self._request_redraw()

    # ------------------------------------------------------------------
    # 排版
    # ------------------------------------------------------------------

    def _translation_lane_visible(self):
        return bool(self._show_translation and self._translation_lane_state.visible)

    def _layout_key(self, font_size: int, width: int, height: int, dpi: float):
        """生成排版缓存键；滚动位置不参与排版缓存。"""
        return (
            int(font_size),
            width,
            height,
            round(float(dpi), 3),
            self.render_scale,
            max(int(self.settings.overlay_translation_font_size), self.minimum_font_size),
            bool(self._show_translation),
            bool(self._follow_tail),
        )

    def _apply_layout(self, layout) -> None:
        """把缓存的排版结果同步到悬浮窗当前状态。"""
        self._display_translation_font_size = layout["translation_font_size"]
        self._layout_lines = layout["lines"]
        self._tail_offset = layout["tail_offset"]
        self._source_tail_offset = layout.get("source_tail_offset", layout["tail_offset"])
        self._trans_tail_offset = layout.get("translation_tail_offset", 0)
        self._scroll_line_starts = layout["scroll_line_starts"]
        self._source_scroll_line_starts = layout.get("source_scroll_line_starts", layout["scroll_line_starts"])
        self._trans_scroll_line_starts = layout.get("translation_scroll_line_starts", [])
        self._scrollable = layout["scrollable"]
        self._line_starts = layout["line_starts"]
        self._visible_line_capacity = layout["visible_line_capacity"]
        self._content_height = layout["content_height"]

    def _prepare_layout(self, font_size: int, width: int, height: int, dpi: float):
        """生成或复用文字行、底部固定区和滚动边界。"""
        cache_key = self._layout_key(font_size, width, height, dpi)
        if self._layout_cache_key == cache_key and self._layout_cache is not None:
            self._apply_layout(self._layout_cache)
            return self._layout_cache

        scale = self.render_scale
        font_path = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msyh.ttc")
        style_specs = {
            "source": {
                "point_size": max(int(font_size), self.minimum_font_size),
                "color": self.settings.overlay_text_color,
                "outline": self.settings.overlay_outline,
            },
            "translation": {
                "point_size": max(
                    int(self.settings.overlay_translation_font_size),
                    self.minimum_font_size,
                ),
                "color": self.settings.overlay_translation_text_color,
                "outline": self.settings.overlay_translation_outline,
            },
        }
        for style, spec in style_specs.items():
            pixel_size = round(spec["point_size"] * dpi / 72)
            font_key = (style, pixel_size)
            if font_key not in self._fonts:
                self._fonts[font_key] = ImageFont.truetype(font_path, pixel_size * scale)
            spec["font"] = self._fonts[font_key]
            spec["pixel_size"] = pixel_size
            spec["line_height"] = sum(spec["font"].getmetrics()) + 3 * scale

        styles = {
            name: StyleMetrics(name, spec["line_height"], spec["pixel_size"])
            for name, spec in style_specs.items()
        }
        measures = {name: spec["font"].getlength for name, spec in style_specs.items()}
        column_gap = 20.0
        col_width = max(40.0, ((width - 42) * scale - column_gap * scale) / 2) if self._show_translation else (width - 42) * scale
        column_x = {
            "source": 0.0,
            "translation": (col_width + column_gap * scale) if self._show_translation else 0.0,
        }
        lines, content_height = build_lines(
            self._entries_view,
            styles,
            (width - 42) * scale,
            scale,
            self._show_translation,
            self._ledger,
            measures,
            column_gap=column_gap,
        )
        viewport_top = 10 * scale
        # 底部操作条不与正文共用像素，透明背景下缩放手柄也可命中。
        viewport_bottom = (height - self.resize_grip_hit_h) * scale
        latest_entry = len(self._entries_view) - 1 if self._entries_view else None
        geometry = compute_geometry(
            lines,
            scale,
            viewport_top,
            viewport_bottom,
            content_height,
            False,
            latest_entry,
        )
        layout = {
            "style_specs": style_specs,
            "lines": lines,
            "viewport_top": viewport_top,
            "viewport_bottom": viewport_bottom,
            "translation_font_size": style_specs["translation"]["point_size"],
            "column_x": column_x,
            **geometry,
        }
        layout.update(reading_positions(
            lines, viewport_top, viewport_bottom, scale, self._translation_lane_visible(),
            {style: spec["line_height"] for style, spec in style_specs.items()},
            translation_current=not self._same_language,
        ))
        self._layout_cache_key = cache_key
        self._layout_cache = layout
        self._apply_layout(layout)
        return layout

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    @staticmethod
    @lru_cache(maxsize=128)
    def _line_image(text, font, color, outline, scale):
        """缓存近期字形，避免历史行每次刷新重新光栅化。"""
        left, top, right, bottom = font.getbbox(text, anchor="la", stroke_width=scale)
        tile = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)))
        ImageDraw.Draw(tile).text(
            (-left, -top), text, font=font, fill=color, anchor="la",
            stroke_width=scale, stroke_fill=outline,
        )
        return tile, left, top

    def _draw_line(self, target, line, line_y, specs, alpha: float) -> None:
        """按当前排版坐标合成字形；缓存不参与文本选择或滚动判断。"""
        spec = specs[line["style"]]
        tile, left, top = self._line_image(
            line["text"], spec["font"], spec["color"], spec["outline"], self.render_scale,
        )
        base_x = line.get("x", 0) or 0
        position = (round(12 * self.render_scale + base_x + left), round(line_y) + top)
        if alpha >= 0.999:
            target.alpha_composite(tile, position)
            return
        faded = tile.copy()
        faded.putalpha(faded.getchannel("A").point(lambda value: round(value * alpha)))
        target.alpha_composite(faded, position)

    def _draw_column_tags(self, draw, layout, label_font, scale, viewport_top, latest_source, latest_translation, canvas_width=0, history=False) -> None:
        """标题字：只写字和细线，不再贴色块，避免和玻璃板拼贴感。"""
        labels = layout.get("reading_labels") or {}
        source_y = labels.get("source", viewport_top)
        if history:
            draw.text(
                (14 * scale, source_y),
                "字幕 · 原文在上 / 译文在下",
                font=label_font,
                fill=(210, 220, 232, 200),
            )
            draw.line(
                (10 * scale, source_y + 18 * scale, (canvas_width or 10**6) - 10 * scale, source_y + 18 * scale),
                fill=(255, 255, 255, 28),
                width=max(1, scale // 2),
            )
            return
        translation_y = labels.get("translation", source_y)
        if self._same_language:
            translation_label = "译文 · 同语言"
        elif latest_translation >= 0 and latest_translation < latest_source:
            translation_label = "译文 · 等待当前句"
        elif self._entries_view and self._entries_view[-1].translation_pending:
            translation_label = "译文 · 更新中"
        else:
            translation_label = "译文"
        draw.text(
            (14 * scale, source_y), "原文",
            font=label_font, fill=(210, 220, 232, 200),
        )
        if self._translation_lane_visible():
            draw.text(
                (14 * scale, translation_y), translation_label,
                font=label_font, fill=(210, 220, 232, 200),
            )

    def _draw_content(self, font_size: int, now: float) -> int:
        """使用超采样 RGBA 图像绘制，固定最新字幕组并保留历史滚动位置。"""
        scale = self.render_scale
        width = max(self.canvas.winfo_width(), self.minimum_window_width)
        height = max(self.canvas.winfo_height(), self.minimum_window_height)
        dpi = self.window.winfo_fpixels("1i")
        layout = self._prepare_layout(font_size, width, height, dpi)
        style_specs = layout["style_specs"]
        lines = layout["lines"]
        viewport_top = layout["viewport_top"]
        content_height = layout["content_height"]
        if self._update_auto_height(content_height):
            # geometry 尚未经过 Tk Configure，不能提交旧尺寸位图把新尺寸改回去。
            return content_height
        self._scrollable = (
            self._tail_offset > 0 or self._source_tail_offset > 0 or self._trans_tail_offset > 0
        )
        self._clamp_scroll_offset(height - 50)
        source_indices = set()
        foreground = Image.new("RGBA", (width * scale, height * scale))
        draw = ImageDraw.Draw(foreground)
        # 实时与回看统一为「原文在上 / 译文在下」成对列表。
        # 垂直成对流用完整 tail（含译文），避免只按原文 tail 裁掉最新译文。
        full_tail = max(
            float(getattr(self, "_tail_offset", 0)),
            float(getattr(self, "_source_tail_offset", 0)),
            float(getattr(self, "_trans_tail_offset", 0)),
        )
        if self._follow_tail:
            self._cancel_auto_scroll()
            self._scroll_motion.snap(full_tail)
            self._scroll_target = full_tail
        tag_bottom = viewport_top + 18 * scale
        last_entry = object()
        for line in lines:
            line_y = line["y"] - self._scroll_motion.value * scale
            if line_y < tag_bottom or line_y + line["height"] > layout["viewport_bottom"]:
                if line.get("entry") is not None:
                    last_entry = line.get("entry")
                continue
            # 分组线：一条内容（原文+译文）之后、下一条原文之前。
            entry = line.get("entry")
            if (
                line["style"] == "source"
                and entry is not None
                and entry != last_entry
                and last_entry is not object()
                and line_y - 8 * scale >= tag_bottom
            ):
                draw.line(
                    (12 * scale, line_y - 6 * scale, width * scale - 28 * scale, line_y - 6 * scale),
                    fill=(255, 255, 255, 32),
                    width=max(1, scale // 2),
                )
            if entry is not None:
                last_entry = entry
            self._draw_line(foreground, line, line_y, style_specs, 1.0)
            if (
                line["style"] == "source"
                and line["entry"] is not None
                and 0 <= line["entry"] < len(self._entries_view)
                and line["char_end"] >= len(self._entries_view[line["entry"]].source)
            ):
                source_indices.add(line["entry"])
        if ("label", scale) not in self._fonts:
            self._fonts[("label", scale)] = ImageFont.truetype(
                os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msyh.ttc"), 11 * scale,
            )
        self._draw_column_tags(
            draw, layout, self._fonts[("label", scale)], scale, viewport_top,
            -1, -1, canvas_width=width * scale, history=True,
        )
        foreground = foreground.resize((width, height), Image.Resampling.LANCZOS)
        if self.settings.overlay_text_opacity != 1.0:
            foreground.putalpha(foreground.getchannel("A").point(lambda value: round(value * self.settings.overlay_text_opacity)))
        # 一张圆角玻璃：底板 + 正文 + 控件，最后统一裁进同一圆角。
        opacity = max(0, min(255, round(255 * self.settings.overlay_background_opacity)))
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        panel = ImageDraw.Draw(image)
        panel.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=self.corner_radius,
            fill=self._rgba_with_alpha(self.settings.overlay_background, opacity),
            outline=(255, 255, 255, min(48, opacity)),
            width=1,
        )
        image = Image.alpha_composite(image, foreground)
        overlay_ui = ImageDraw.Draw(image)
        tail = self._full_tail_offset()
        # 隐藏式滚动条：悬停/滚轮时才出现，不改悬浮窗底色。
        show_bar = (
            tail > 0
            and (
                self._drag_mode == "scrollbar"
                or time.perf_counter() < getattr(self, "_scrollbar_show_until", 0.0)
            )
        )
        if show_bar:
            # 跟随背景不透明度，但滑块有可见度下限，最低透明度也能抓住。
            bar_alpha = max(48, round(255 * self.settings.overlay_background_opacity))
            thumb_alpha = max(150, round(255 * self.settings.overlay_background_opacity))
            bar = self._scrollbar_geometry()
            overlay_ui.rounded_rectangle(
                (bar["left"] + 6, bar["top"], bar["right"] - 6, bar["bottom"]),
                radius=3, fill=(30, 40, 56, bar_alpha),
            )
            overlay_ui.rounded_rectangle(
                (bar["left"] + 6, bar["thumb_top"], bar["right"] - 6,
                 bar["thumb_top"] + bar["thumb_height"]),
                radius=3, fill=(170, 184, 204, thumb_alpha),
            )
        # 同一圆角蒙版，角上不会露出第二层边角。
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=self.corner_radius,
            fill=255,
        )
        alpha = image.getchannel("A")
        image.putalpha(ImageChops.darker(alpha, mask))
        self._last_image = image
        self._layer.present(self._last_image)
        if self.on_presented is not None:
            self.on_presented(time.perf_counter(), source_indices)
        return content_height

    # ------------------------------------------------------------------
    # 窗口高度
    # ------------------------------------------------------------------

    def _should_anchor_height_bottom(self) -> bool:
        """贴近屏幕底部时向上生长，避免自动高度把窗口推出屏幕。"""
        try:
            y = self.window.winfo_y()
            window_height = self.window.winfo_height()
            screen_height = self.window.winfo_screenheight()
        except tk.TclError:
            return False
        return (y + window_height) >= screen_height * 0.55

    def _apply_window_height(self, height: int) -> None:
        """只改变高度；锚点在动画开始时确定，动画期间保持稳定。"""
        if self._drag_mode:
            return
        width = self.window.winfo_width()
        x = self.window.winfo_x()
        if self._height_anchor_bottom:
            y = self._height_animation_bottom - height
        else:
            y = self.window.winfo_y()
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _row_heights(self):
        specs = self._layout_cache["style_specs"] if self._layout_cache else None
        if not specs:
            return 24.0, 24.0
        return tuple(specs[style]["line_height"] / self.render_scale
                     for style in ("source", "translation"))

    def _reading_minimum_height(self, rows: int = 1) -> int:
        source, translated = self._row_heights()
        return reading_window_height(0, source * rows, translated * rows,
                                     self._translation_lane_visible() if self._follow_tail else False)

    def _update_auto_height(self, content_height: int) -> bool:
        """仅随译文区有无切换高度，不追随字幕长度；保留双语首选高度。"""
        del content_height
        if self._drag_mode or not self._follow_tail:
            return False
        source, translated = self._row_heights()
        current = self.window.winfo_height()
        if self.settings.overlay_auto_height:
            desired = reading_window_height(self._cap_height, source, translated,
                                            self._translation_lane_visible())
        else:
            desired = max(current, self._reading_minimum_height())
        desired = min(self._maximum_window_height(), desired)
        if abs(desired - current) <= 1:
            return False
        self._height_anchor_bottom = self._should_anchor_height_bottom()
        self._height_animation_bottom = self.window.winfo_y() + current
        self._apply_window_height(desired)
        self._layout_cache_key = None
        self._request_redraw()
        return True

    def _maximum_window_height(self) -> int:
        """根据屏幕高度限制自动扩展的最大值。"""
        try:
            screen_height = self.window.winfo_screenheight()
        except tk.TclError:
            screen_height = 800
        return max(240, int(screen_height * 0.72))

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _clamp_scroll_offset(self, viewport_height: int) -> None:
        del viewport_height
        max_scroll = max(
            float(getattr(self, "_tail_offset", 0)),
            float(getattr(self, "_source_tail_offset", 0)),
            float(getattr(self, "_trans_tail_offset", 0)),
        )
        self._scroll_target = max(0.0, min(float(self._scroll_target), max_scroll))
        if self._scroll_motion.value > max_scroll:
            self._scroll_motion.snap(max_scroll)
        self._scroll_motion.set_target(self._scroll_target)

    def _full_tail_offset(self) -> float:
        return max(
            float(getattr(self, "_tail_offset", 0)),
            float(getattr(self, "_source_tail_offset", 0)),
            float(getattr(self, "_trans_tail_offset", 0)),
        )

    def _scrollbar_geometry(self, style: str = "source"):
        # 垂直成对列表：一根滚动条覆盖完整内容（含译文）。
        del style
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        tail = self._full_tail_offset()
        offset = tail if self._follow_tail else self._scroll_motion.value
        return scrollbar_geometry(width, height, self._content_height, tail, offset)

    def _drag_scrollbar(self, y, style: str = "source"):
        del style
        self._reveal_scrollbar()
        bar = self._scrollbar_geometry()
        fraction = (y - self._scrollbar_grab - bar["top"]) / max(bar["travel"], 1)
        tail = self._full_tail_offset()
        self._scroll_target = max(0.0, min(1.0, fraction)) * tail
        self._scroll_motion.snap(self._scroll_target)
        self._follow_tail = False
        self._request_redraw()

    def _on_button_press(self, event) -> None:
        self._drag_origin = (event.x_root, event.y_root)
        self._geometry_origin = (
            self.window.winfo_width(),
            self.window.winfo_height(),
            self.window.winfo_x(),
            self.window.winfo_y(),
        )
        bar = self._scrollbar_geometry()
        tail = self._full_tail_offset()
        if tail > 0 and bar["left"] <= event.x <= bar["right"] and bar["top"] <= event.y <= bar["bottom"]:
            self._drag_mode = "scrollbar"
            self._active_scroll_style = "source"
            self._cancel_auto_scroll()
            self._reveal_scrollbar()
            self._scrollbar_grab = (event.y - bar["thumb_top"]
                                    if bar["thumb_top"] <= event.y <= bar["thumb_top"] + bar["thumb_height"]
                                    else bar["thumb_height"] / 2)
        elif self._in_resize_grip(event.x, event.y):
            self._drag_mode = "resize"
            self._user_resized = True
        else:
            self._drag_mode = "move"
        try:
            self.canvas.grab_set()
        except tk.TclError:
            pass

    def _on_drag(self, event) -> None:
        if not self._drag_mode:
            return
        if self._drag_mode == "scrollbar":
            self._drag_scrollbar(event.y, getattr(self, "_active_scroll_style", "source"))
            return
        delta_x = event.x_root - self._drag_origin[0]
        delta_y = event.y_root - self._drag_origin[1]
        old_width, old_height, old_x, old_y = self._geometry_origin
        if self._drag_mode == "resize":
            new_width = max(self.minimum_window_width, old_width + delta_x)
            new_height = max(self._reading_minimum_height(1), old_height + delta_y)
            self.window.geometry(f"{int(new_width)}x{int(new_height)}+{old_x}+{old_y}")
        else:
            self.window.geometry(f"{old_width}x{old_height}+{old_x + delta_x}+{old_y + delta_y}")

    def _on_button_release(self, _event) -> None:
        resized = self._drag_mode == "resize"
        scrolling = self._drag_mode == "scrollbar"
        self._drag_mode = ""
        try:
            self.canvas.grab_release()
        except tk.TclError:
            pass
        if scrolling:
            style = getattr(self, "_active_scroll_style", "source")
            if style == "translation":
                at_end = self._trans_scroll_target >= self._trans_tail_offset - 0.5
            else:
                at_end = self._scroll_target >= self._full_tail_offset() - 0.5
            if at_end:
                self._scroll_to_latest()
            self._request_redraw()
            return
        if resized:
            # 保留拖动后的阅读区高度，不再根据字幕长度反向缩回。
            source, translated = self._row_heights()
            actual = self.window.winfo_height()
            self._cap_height = (actual if not self.settings.overlay_auto_height or (self._translation_lane_visible() and self._follow_tail)
                                else paired_window_height(actual, source, translated))
        self.persist_geometry()
        self._request_redraw()

    def _on_motion(self, event) -> None:
        bar = self._scrollbar_geometry()
        tail = self._full_tail_offset()
        near_bar = (
            tail > 0
            and bar["left"] - 8 <= event.x <= bar["right"] + 8
            and 8 <= event.y <= bar["bottom"] + 8
        )
        if near_bar or self._drag_mode == "scrollbar":
            self._reveal_scrollbar()
            self.canvas.configure(cursor="hand2")
            return
        # 右下角仍可缩放，但不画按钮。
        if self._in_resize_grip(event.x, event.y):
            self.canvas.configure(cursor="size_nw_se")
            return
        self.canvas.configure(cursor="fleur")
        # 鼠标在窗内时也短暂露出滚动条，便于发现。
        if tail > 0 and time.perf_counter() < getattr(self, "_scrollbar_show_until", 0.0):
            self._reveal_scrollbar()

    def _reveal_scrollbar(self) -> None:
        self._scrollbar_show_until = time.perf_counter() + 1.2
        self._request_redraw()

    def _on_mouse_wheel(self, event) -> None:
        """上下栏共用一条滚动：滚轮整页同步移动。"""
        self._reveal_scrollbar()
        tail = self._full_tail_offset()
        if tail <= 0:
            return
        self._cancel_auto_scroll()
        starts = getattr(self, "_source_scroll_line_starts", self._scroll_line_starts)
        current = self._scroll_motion.value
        delta = getattr(event, "delta", 0)
        num = getattr(event, "num", None)
        if num == 4 or (delta > 0):
            direction = -1
        else:
            direction = 1
        if direction > 0:
            target = next((position for position in starts if position > current + 0.5), tail)
        else:
            target = next((position for position in reversed(starts) if position < current - 0.5), 0)
        self._set_scroll_position(target, style="source")
        self._follow_tail = self._scroll_target >= tail - 0.5
        self._request_redraw()

    def _on_configure(self, _event) -> None:
        self._sync_layer_geometry()
        self._layout_cache_key = None
        self._request_redraw()

    def _sync_layer_geometry(self) -> None:
        """使文字层与背景层保持相同的位置和大小。"""
        if not self._separate_layers:
            return
        if not self.window.winfo_exists() or not self.text_window.winfo_exists():
            return
        geometry = self.window.geometry()
        if self.text_window.geometry() != geometry:
            self.text_window.geometry(geometry)

    def _show_menu(self, event) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._menu.grab_release()
            except tk.TclError:
                pass
