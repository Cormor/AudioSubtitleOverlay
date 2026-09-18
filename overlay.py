"""可拖动、可调整大小并支持缩放和滚动的双语悬浮窗。"""

from __future__ import annotations

import tkinter as tk
from PIL import Image, ImageChops, ImageDraw, ImageFont
from layered_window import LayeredWindow
import os
import math
import time
from functools import lru_cache


class OverlayWindow:
    """在桌面顶层显示源语言和译文。"""

    minimum_font_size = 12
    minimum_window_width = 320
    # 默认字号下，保留上下边距后可以缩到一行字幕的高度。
    minimum_window_height = 68
    # PIL 已经提供字形抗锯齿；使用两倍分辨率保留清晰度，避免三倍重绘的额外开销。
    render_scale = 2
    # 只允许当前段末尾的小窗口随即时识别结果修订。

    def __init__(self, master: tk.Misc, settings, on_open_settings, on_close) -> None:
        self.settings = settings
        self.on_presented = None
        self._on_open_settings = on_open_settings
        self._on_close = on_close
        self._entries = []
        # 每个条目分别标记为稳定正文或即时草稿；草稿只允许尾部修订。
        self._entry_revision_modes = []
        self._follow_tail = True
        self._auto_scroll_after = None
        self._scroll_target = 0.0
        self._paragraph_states = {}
        self._layout_cache_key = None
        self._layout_cache = None
        self._source_text = ""
        self._translation_text = ""
        self._source_label = "源语言"
        self._translation_label = "译文"
        self._drag_mode = ""
        self._drag_origin = (0, 0)
        self._geometry_origin = (0, 0, 0, 0)
        self._redraw_pending = False
        self._adjusting_geometry = False
        self._user_resized = False
        self._content_height = 0
        self._scroll_offset = 0
        self._scrollable = False
        self._line_starts = []
        self._scroll_line_starts = []
        self._tail_offset = 0
        self._layout_lines = []
        self._footer_start = 0
        self._footer_indices = []
        self._footer_line_positions = {}
        self._footer_active = False
        self._display_font_size = max(int(self.settings.overlay_font_size), 10)
        self._display_translation_font_size = max(
            int(self.settings.overlay_translation_font_size), 10
        )

        self._show_translation = True
        self._separate_layers = False
        self.window = tk.Toplevel(master)
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
        self._menu.add_command(label="隐藏悬浮窗", command=self.hide)
        self._menu.add_separator()
        self._menu.add_command(label="退出程序", command=self._on_close)

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
        self.settings.overlay_geometry = self.window.geometry()
        self._cancel_auto_scroll()
        if self._separate_layers:
            self.text_window.withdraw()
        self.window.withdraw()

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

    def set_entries(self, entries, revision_modes=None) -> None:
        """保留各段正文，并区分稳定结果和只应改动尾部的即时草稿。"""
        new_entries = list(entries)
        if revision_modes is None:
            # 直接使用悬浮窗接口时，输入默认为已经确认的正文。
            new_revision_modes = ["stable"] * len(new_entries)
        else:
            new_revision_modes = [
                mode if mode in {"stable", "preview"} else "stable"
                for mode in list(revision_modes)[: len(new_entries)]
            ]
            new_revision_modes.extend(
                ["stable"]
                * (len(new_entries) - len(new_revision_modes))
            )
        if self._entries == new_entries and self._entry_revision_modes == new_revision_modes:
            return
        self._entries = new_entries
        self._entry_revision_modes = new_revision_modes
        self._layout_cache_key = None
        self._request_redraw()

    def reset_content(self) -> None:
        """新识别会话清空旧会话的显示内容。"""
        self._entries = []
        self._entry_revision_modes = []
        self._follow_tail = True
        self._cancel_auto_scroll()
        self._paragraph_states.clear()
        self._layout_cache_key = None
        self._scroll_offset = 0
        self._scroll_target = 0
        self._request_redraw()

    def _scroll_to_latest(self) -> None:
        self._cancel_auto_scroll()
        self._follow_tail = True
        self._set_scroll_position(getattr(self, "_tail_offset", 0))

    def _cancel_auto_scroll(self) -> None:
        if self._auto_scroll_after is not None:
            self.window.after_cancel(self._auto_scroll_after)
            self._auto_scroll_after = None

    @staticmethod
    def _line_key(line):
        """返回同一段落内稳定的行身份，文字增长不会改变该身份。"""
        return (line.get("entry"), line.get("style"), line.get("line_index"))

    @staticmethod
    def _wrap_records(text, face_font, max_width):
        """按当前字体换行，并保留每行在原文中的字符范围。"""
        records = []
        line_start = 0
        line = ""
        for index, char in enumerate(text):
            if char == "\n":
                records.append({"text": line, "start": line_start, "end": index})
                line = ""
                line_start = index + 1
                continue
            if line and face_font.getlength(line + char) > max_width:
                records.append({"text": line, "start": line_start, "end": index})
                line = char
                line_start = index
            else:
                line += char
        if line or not records or line_start < len(text):
            records.append({"text": line, "start": line_start, "end": len(text)})
        return records

    def _paragraph_records(
        self,
        key,
        text,
        style,
        pixel_size,
        width,
        face_font,
        revision_mode,
    ):
        """更新段落行账本；草稿只重排末尾，稳定换稿才允许整体重排。"""
        signature = (style, pixel_size, width)
        state = self._paragraph_states.get(key)
        if state is None or state["signature"] != signature:
            displayed = text
            records = self._wrap_records(displayed, face_font, (width - 42) * self.render_scale)
        else:
            previous = state["text"]
            displayed = text
            if displayed == previous:
                return state["records"], previous
            old_records = state["records"]
            if len(old_records) <= 1:
                records = self._wrap_records(displayed, face_font, (width - 42) * self.render_scale)
            else:
                frozen_count = len(old_records) - 1
                frozen_end = old_records[frozen_count - 1]["end"]
                if displayed[:frozen_end] != previous[:frozen_end]:
                    # 稳定通道明确给出修订时，使用新文本重建账本；
                    # 这条路径不受高频即时草稿触发。
                    records = self._wrap_records(
                        displayed, face_font, (width - 42) * self.render_scale
                    )
                else:
                    records = [dict(record) for record in old_records[:frozen_count]]
                    suffix = self._wrap_records(
                        displayed[frozen_end:], face_font, (width - 42) * self.render_scale
                    )
                    for record in suffix:
                        record["start"] += frozen_end
                        record["end"] += frozen_end
                    records.extend(suffix)
        self._paragraph_states[key] = {
            "signature": signature,
            "text": displayed,
            "records": records,
        }
        return records, displayed

    def _set_scroll_position(self, target: float) -> None:
        """立即设置滚动位置；识别更新不启动界面动画。"""
        target = max(0.0, min(float(target), float(getattr(self, "_tail_offset", 0))))
        self._scroll_offset = target
        self._scroll_target = target
        self._request_redraw()
        self._schedule_auto_scroll()

    def _schedule_auto_scroll(self) -> None:
        if (
            self._follow_tail
            and self._scroll_target < self._tail_offset - 0.5
            and self._auto_scroll_after is None
        ):
            delay_ms = max(0, round(self.settings.overlay_scroll_interval * 1000))
            self._auto_scroll_after = self.window.after(delay_ms, self._advance_reading)

    def _advance_reading(self) -> None:
        """按阅读节奏每次推进一行，不随每次识别修订直接跳到末尾。"""
        self._auto_scroll_after = None
        if not self._follow_tail or not self.window.winfo_viewable():
            return
        next_position = next(
            (position for position in self._scroll_line_starts if position > self._scroll_target + 0.5),
            self._tail_offset,
        )
        self._set_scroll_position(next_position)

    def apply_settings(self) -> None:
        """应用颜色、两层透明度、字号和置顶设置。"""
        self.window.wm_attributes("-topmost", bool(self.settings.overlay_topmost))
        self._layout_cache_key = None
        self._request_redraw()

    def close(self) -> None:
        """销毁悬浮窗。"""
        if self.window.winfo_exists():
            self._cancel_auto_scroll()
            self.settings.overlay_geometry = self.window.geometry()
            if self._separate_layers and self.text_window.winfo_exists():
                self.text_window.destroy()
            self.window.destroy()

    def _redraw(self) -> None:
        """固定用户选定的窗口与字号，并把最新原文和译文固定在底部。"""
        if not self.window.winfo_exists() or not self.window.winfo_viewable() or self.canvas.winfo_width() <= 1:
            return
        font_size = max(int(self.settings.overlay_font_size), self.minimum_font_size)
        self._display_font_size = font_size
        self._content_height = self._measure_content(font_size)
        if self._follow_tail:
            # 最新行转入历史时在同一帧同步位置，不等待阅读定时器。
            self._cancel_auto_scroll()
            self._scroll_offset = self._tail_offset
            self._scroll_target = self._tail_offset
        viewport_height = max(self.canvas.winfo_height() - 20, 1)
        self._scrollable = self._tail_offset > 0
        self._clamp_scroll_offset(viewport_height)
        self._draw_content(font_size, self._scroll_offset, draw_scrollbar=True, layout=self._layout_cache)
        self._schedule_auto_scroll()

    def _request_redraw(self) -> None:
        """把多个后台结果合并为一次主线程重绘。"""
        if not self.window.winfo_exists() or not self.window.winfo_viewable():
            return
        if not self._redraw_pending:
            self._redraw_pending = True
            self.window.after_idle(self._finish_redraw)

    def _finish_redraw(self) -> None:
        """执行合并后的重绘请求。"""
        self._redraw_pending = False
        self._redraw()

    def _measure_content(self, font_size: int) -> int:
        """绘制一次无滚动内容并返回所需高度。"""
        return self._draw_content(font_size, 0, draw_scrollbar=False)

    def set_translation_visible(self, visible: bool) -> None:
        """同语言时不绘制译文标签、占位文字和段落间距。"""
        if self._show_translation == visible:
            return
        self._show_translation = visible
        self._layout_cache_key = None
        self._request_redraw()

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
        self._footer_start = layout["footer_start"]
        self._footer_indices = layout["footer_indices"]
        self._footer_active = layout["footer_active"]
        self._tail_offset = layout["tail_offset"]
        self._scroll_line_starts = layout["scroll_line_starts"]
        self._scrollable = layout["scrollable"]
        self._footer_line_positions = layout["footer_line_positions"]
        self._line_starts = layout["line_starts"]
        self._visible_line_capacity = layout["visible_line_capacity"]
        self._content_height = layout["content_height"]

    def _prepare_layout(self, font_size: int, width: int, height: int, dpi: float):
        """生成或复用文字行、尾部槽位和滚动边界。"""
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

        paragraphs = []
        for entry_index, (source, translation) in enumerate(self._entries):
            revision_mode = self._entry_revision_modes[entry_index]
            if source:
                paragraphs.append((source, "source", entry_index, revision_mode))
            if self._show_translation and translation:
                # 译文作为独立段落排版，不与原文共用同一行。
                paragraphs.append((translation, "translation", entry_index, "stable"))
        if not paragraphs:
            paragraphs = [("等待识别", "source", None, "stable")]

        lines = []
        active_paragraph_keys = set()
        y = 12 * scale
        for paragraph_index, (paragraph, style, entry_index, revision_mode) in enumerate(paragraphs):
            spec = style_specs[style]
            face_font = spec["font"]
            # 以段落身份而非当前文字作为键；当前文字增长时只更新末尾账本，
            # 已经出现的前部行不再因为每次识别刷新而重新换行。
            paragraph_key = (entry_index, style)
            active_paragraph_keys.add(paragraph_key)
            records, _displayed = self._paragraph_records(
                paragraph_key,
                paragraph,
                style,
                spec["pixel_size"],
                width,
                face_font,
                revision_mode,
            )
            for line_index, record in enumerate(records):
                line = record["text"]
                gap_before = 8 * scale if paragraph_index and line_index == 0 else 0
                y += gap_before
                lines.append({
                    "text": line,
                    "style": style,
                    "entry": entry_index,
                    "line_index": line_index,
                    "char_start": record["start"],
                    "char_end": record["end"],
                    "y": y,
                    "height": spec["line_height"],
                    "gap_before": gap_before,
                })
                y += spec["line_height"]

        # 会话清空或翻译隐藏后，删除不再存在的账本，避免新段落复用旧文字。
        for paragraph_key in set(self._paragraph_states) - active_paragraph_keys:
            self._paragraph_states.pop(paragraph_key, None)
        visible_line_capacity = max(
            1,
            int((height - 24) * scale // min(spec["line_height"] for spec in style_specs.values())),
        )
        line_starts = [
            max(0, round((line["y"] - 12 * scale) / scale))
            for line in lines
        ]
        viewport_top = 10 * scale
        viewport_bottom = (height - 10) * scale
        last_bottom = lines[-1]["y"] + lines[-1]["height"]
        full_tail_offset = max(0, round((last_bottom - viewport_bottom) / scale))

        latest_entry = paragraphs[-1][2]
        latest_indices = [
            index for index, line in enumerate(lines) if line["entry"] == latest_entry
        ]
        latest_source_indices = [
            index for index in latest_indices if lines[index]["style"] == "source"
        ]
        latest_translation_indices = [
            index for index in latest_indices if lines[index]["style"] == "translation"
        ]
        # 跟随模式只固定最新原文行和最新译文行，其余行属于历史区。
        # 固定槽位数量不随当前段继续换行而改变，避免两套布局坐标突然切换。
        footer_indices = []
        if latest_source_indices:
            footer_indices.append(latest_source_indices[-1])
        if latest_translation_indices:
            footer_indices.append(latest_translation_indices[-1])
        footer_indices.sort()

        footer_start = footer_indices[0] if footer_indices else len(lines) - 1
        footer_height = 0
        previous_footer_index = None
        for index in footer_indices:
            line = lines[index]
            if previous_footer_index is not None:
                # 原文和译文仍保持独立段落间距；同一段的换行行不增加额外间距。
                if line["style"] != lines[previous_footer_index]["style"]:
                    footer_height += 8 * scale
            footer_height += line["height"]
            previous_footer_index = index
        footer_height = max(footer_height, min(spec["line_height"] for spec in style_specs.values()))
        # 跟随时始终使用最新槽位，即使当前内容尚未溢出，也不切换到顶部布局。
        footer_active = bool(self._follow_tail and footer_indices)
        if footer_active:
            footer_top = viewport_bottom - footer_height
            footer_index_set = set(footer_indices)
            history_indices = [index for index in range(len(lines)) if index not in footer_index_set]
            history_tail_offset = 0
            if history_indices:
                history_last = lines[history_indices[-1]]
                history_tail_offset = max(
                    0,
                    math.ceil((history_last["y"] + history_last["height"] + 8 * scale - footer_top) / scale),
                )
            tail_offset = history_tail_offset
            scroll_line_starts = [line_starts[index] for index in history_indices]
        else:
            footer_top = viewport_bottom
            tail_offset = full_tail_offset
            scroll_line_starts = list(line_starts)
        scrollable = tail_offset > 0
        footer_line_positions = {}
        if footer_active:
            # 最新原文始终锚定在底部；译文从原文上方进入，译文到达不会推动原文下移。
            footer_y = viewport_bottom
            previous_style = None
            for index in footer_indices:
                line = lines[index]
                if previous_style is not None and line["style"] != previous_style:
                    footer_y -= 8 * scale
                footer_y -= line["height"]
                footer_line_positions[index] = footer_y
                previous_style = line["style"]
        footer_display_lines = {}
        if footer_active and visible_line_capacity == 1 and len(footer_indices) == 1:
            # 单行视口保留少量前文作衔接，只影响本帧，不追加到字幕历史。
            index = footer_indices[0]
            current = lines[index]
            previous = next((line for line in reversed(lines[:index]) if line["style"] == current["style"]), None)
            if previous is not None:
                context = previous["text"].rstrip()[-8:]
                separator = " " if previous["entry"] != current["entry"] else ""
                font = style_specs[current["style"]]["font"]
                while context and font.getlength(context + separator + current["text"]) > (width-42)*scale:
                    context = context[1:]
                if context:
                    footer_display_lines[index] = dict(current, text=context + separator + current["text"])
        content_height = round(y / scale) + 4
        layout = {
            "style_specs": style_specs,
            "lines": lines,
            "viewport_top": viewport_top,
            "viewport_bottom": viewport_bottom,
            "footer_top": footer_top,
            "footer_start": footer_start,
            "footer_indices": footer_indices,
            "footer_active": footer_active,
            "tail_offset": tail_offset,
            "scroll_line_starts": scroll_line_starts,
            "scrollable": scrollable,
            "footer_line_positions": footer_line_positions,
            "footer_display_lines": footer_display_lines,
            "line_starts": line_starts,
            "visible_line_capacity": visible_line_capacity,
            "content_height": content_height,
            "translation_font_size": style_specs["translation"]["point_size"],
        }
        self._layout_cache_key = cache_key
        self._layout_cache = layout
        self._apply_layout(layout)
        return layout

    @staticmethod
    @lru_cache(maxsize=128)
    def _line_image(text, font, color, outline, scale):
        """缓存近期字形，避免历史行每次刷新重新光栅化。"""
        left, top, right, bottom = font.getbbox(text, anchor="lt", stroke_width=scale)
        tile = Image.new("RGBA", (max(1, right-left), max(1, bottom-top)))
        ImageDraw.Draw(tile).text(
            (-left, -top), text, font=font, fill=color, anchor="lt",
            stroke_width=scale, stroke_fill=outline,
        )
        return tile, left, top

    def _draw_line(self, target, line, line_y, specs):
        """按当前排版坐标合成字形；缓存不参与文本选择或滚动判断。"""
        spec = specs[line["style"]]
        tile, left, top = self._line_image(
            line["text"], spec["font"], spec["color"], spec["outline"], self.render_scale,
        )
        target.alpha_composite(tile, (12*self.render_scale+left, round(line_y)+top))

    def _draw_content(self, font_size: int, scroll_offset: int, draw_scrollbar: bool, layout=None) -> int:
        """使用超采样 RGBA 图像绘制，固定最新段并保留历史滚动位置。"""
        scale = self.render_scale
        width = max(self.canvas.winfo_width(), self.minimum_window_width)
        height = max(self.canvas.winfo_height(), self.minimum_window_height)
        dpi = self.window.winfo_fpixels("1i")
        # 同次重绘复用测量阶段的排版，避免重复查询完整布局。
        if layout is None:
            layout = self._prepare_layout(font_size, width, height, dpi)
        style_specs = layout["style_specs"]
        lines = layout["lines"]
        viewport_top = layout["viewport_top"]
        footer_top = layout["footer_top"]
        footer_active = layout["footer_active"]
        content_height = layout["content_height"]
        if not draw_scrollbar:
            return content_height

        source_indices = set()
        foreground = Image.new("RGBA", (width * scale, height * scale))
        draw = ImageDraw.Draw(foreground)

        # 历史区只绘制完整行，并为底部正文的描边留出间距。
        # 离散滚动不显示跨边界的半行，避免残字紧贴最新正文。
        history_layer = Image.new("RGBA", foreground.size)
        for index, line in enumerate(lines):
            if footer_active and index in self._footer_indices:
                continue
            line_y = line["y"] - scroll_offset * scale
            history_bottom = footer_top - (8 * scale if footer_active else 0)
            if line_y < viewport_top or line_y + line["height"] > history_bottom:
                continue
            self._draw_line(history_layer, line, line_y, style_specs)
            if line["style"] == "source" and line["entry"] is not None:
                source_indices.add(line["entry"])
        history_mask = Image.new("L", foreground.size)
        ImageDraw.Draw(history_mask).rectangle(
            (0, viewport_top, width * scale, footer_top),
            fill=255,
        )
        history_layer.putalpha(ImageChops.multiply(history_layer.getchannel("A"), history_mask))
        foreground.alpha_composite(history_layer)

        if footer_active:
            for index in self._footer_indices:
                line = lines[index]
                # 新尾行在固定尾槽直接显示，识别更新不会触发过渡动画。
                displayed_line = layout["footer_display_lines"].get(index, line)
                self._draw_line(foreground, displayed_line, self._footer_line_positions[index], style_specs)
                if line["style"] == "source" and line["entry"] is not None:
                    source_indices.add(line["entry"])
        if self._scrollable:
            viewport = max(height - 20, 80)
            track = height - 36
            thumb = max(24, int(track * viewport / max(self._content_height, 1)))
            top = 12 + int((track - thumb) * scroll_offset / max(self._tail_offset, 1))
            draw.rectangle(((width - 7) * scale, 12 * scale, (width - 3) * scale, (height - 24) * scale), fill="#536273")
            draw.rectangle(((width - 8) * scale, top * scale, (width - 2) * scale, (top + thumb) * scale), fill="#c7d0da")
        # 右下角保留小型缩放标记；完全透明区域不绘制背景像素。
        for offset in (0, 5):
            draw.line(((width - 15 + offset) * scale, (height - 6) * scale,
                       (width - 6) * scale, (height - 15 + offset) * scale), fill=self.settings.overlay_text_color, width=scale)
        foreground = foreground.resize((width, height), Image.Resampling.LANCZOS)
        if self.settings.overlay_text_opacity != 1.0:
            foreground.putalpha(foreground.getchannel("A").point(lambda value: round(value * self.settings.overlay_text_opacity)))
        background = Image.new("RGBA", (width, height), self.settings.overlay_background)
        background.putalpha(round(255 * self.settings.overlay_background_opacity))
        self._last_image = Image.alpha_composite(background, foreground)
        self._layer.present(self._last_image)
        if self.on_presented is not None:
            self.on_presented(time.perf_counter(), source_indices)
        return content_height

    def _sync_layer_geometry(self) -> None:
        """使文字层与背景层保持相同的位置和大小。"""
        if not self._separate_layers:
            return
        if not self.window.winfo_exists() or not self.text_window.winfo_exists():
            return
        geometry = self.window.geometry()
        if self.text_window.geometry() != geometry:
            self.text_window.geometry(geometry)

    def _maximum_window_height(self) -> int:
        """根据屏幕高度限制自动扩展的最大值。"""
        try:
            screen_height = self.window.winfo_screenheight()
        except tk.TclError:
            screen_height = 800
        return max(240, int(screen_height * 0.72))

    def _clamp_scroll_offset(self, viewport_height: int) -> None:
        del viewport_height
        max_scroll = float(getattr(self, "_tail_offset", 0))
        target = max(0.0, min(float(self._scroll_target), max_scroll))
        current = float(self._scroll_offset)
        self._scroll_offset = max(0.0, min(current, max_scroll))
        self._scroll_target = target

    def _on_button_press(self, event) -> None:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        self._drag_origin = (event.x_root, event.y_root)
        self._geometry_origin = (
            self.window.winfo_width(),
            self.window.winfo_height(),
            self.window.winfo_x(),
            self.window.winfo_y(),
        )
        if event.x >= width - 28 and event.y >= height - 28:
            self._drag_mode = "resize"
            self._user_resized = True
        else:
            self._drag_mode = "move"

    def _on_drag(self, event) -> None:
        if not self._drag_mode:
            return
        delta_x = event.x_root - self._drag_origin[0]
        delta_y = event.y_root - self._drag_origin[1]
        old_width, old_height, old_x, old_y = self._geometry_origin
        if self._drag_mode == "resize":
            new_width = max(self.minimum_window_width, old_width + delta_x)
            new_height = max(self.minimum_window_height, old_height + delta_y)
            self.window.geometry(f"{new_width}x{new_height}+{old_x}+{old_y}")
        else:
            self.window.geometry(f"{old_width}x{old_height}+{old_x + delta_x}+{old_y + delta_y}")

    def _on_button_release(self, _event) -> None:
        self._drag_mode = ""
        self.settings.overlay_geometry = self.window.geometry()

    def _on_motion(self, event) -> None:
        if event.x >= self.canvas.winfo_width() - 28 and event.y >= self.canvas.winfo_height() - 28:
            self.canvas.configure(cursor="size_nw_se")
        else:
            self.canvas.configure(cursor="fleur")

    def _on_mouse_wheel(self, event) -> None:
        """滚动显示超出视口的长文本。"""
        if not self._scrollable:
            return
        self._cancel_auto_scroll()
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            direction = -1
        else:
            direction = 1
        if direction > 0:
            self._scroll_offset = next(
                (position for position in self._scroll_line_starts if position > self._scroll_offset),
                self._tail_offset,
            )
        else:
            self._scroll_offset = next(
                (position for position in reversed(self._scroll_line_starts) if position < self._scroll_offset),
                0,
            )
        self._clamp_scroll_offset(0)
        self._scroll_target = self._scroll_offset
        self._follow_tail = self._scroll_offset >= self._tail_offset - 0.5
        self._draw_content(self._display_font_size, self._scroll_offset, draw_scrollbar=True)

    def _on_configure(self, _event) -> None:
        self._sync_layer_geometry()
        self._layout_cache_key = None
        self._request_redraw()

    def _show_menu(self, event) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self._menu.grab_release()
            except tk.TclError:
                pass
