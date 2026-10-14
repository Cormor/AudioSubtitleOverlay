"""字幕排版、布局与动效计算。

本模块是纯 Python 几何/运动学计算，不依赖 Tk 或 PIL：
度量函数（measure）与样式尺寸由调用方提供，便于单元测试与离线渲染验证。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

# 同一段落内原文与译文之间的纵向间距（逻辑像素）。
PAIR_GAP = 4
# 相邻字幕段之间的纵向间距（逻辑像素）。
GROUP_GAP = 12
# 顶部留白（逻辑像素）。
TOP_PADDING = 12
# 历史区与底部固定区之间的呼吸间距（逻辑像素）。
HISTORY_FOOTER_GAP = 8
# 草稿行的不透明度倍率，用于区分即时预览与稳定正文。
DRAFT_LINE_ALPHA = 0.72


def wrap_text(text: str, max_width: float, measure: Callable[[str], float]) -> list[dict]:
    """按可用宽度换行，并保留每行在原文中的字符范围。

    拉丁文字优先在词边界断行；无词边界（如中文）时逐字符断行。
    """
    records: list[dict] = []
    line_start = 0
    line = ""
    for index, char in enumerate(text):
        if char == "\n":
            records.append({"text": line, "start": line_start, "end": index})
            line = ""
            line_start = index + 1
            continue
        if line and measure(line + char) > max_width:
            if char.isspace():
                # 空白处直接断行，空白不进入正文。
                records.append({"text": line, "start": line_start, "end": index})
                line = ""
                line_start = index + 1
                continue
            split = max(line.rfind(" "), line.rfind("\t"))
            if 0 < split < len(line):
                # 把未完成的单词整体移到下一行，不拆半个单词。
                records.append({
                    "text": line[:split],
                    "start": line_start,
                    "end": line_start + split,
                })
                line = line[split + 1:] + char
                line_start = line_start + split + 1
                continue
            # 中文收尾标点不能孤零零落在下一行，起始括号不能挂在上一行末。
            if len(line) > 1 and (char in "，。！？；：、）》】〉」』”’" or line[-1] in "（《【〈「『“‘"):
                records.append({"text": line[:-1], "start": line_start, "end": index - 1})
                line = line[-1] + char
                line_start = index - 1
            else:
                records.append({"text": line, "start": line_start, "end": index})
                line = char
                line_start = index
        else:
            if not line and char in " \t":
                line_start = index + 1
                continue
            line += char
    if line or not records or line_start < len(text):
        records.append({"text": line, "start": line_start, "end": len(text)})
    return records


class ParagraphLedger:
    """按段落身份缓存换行结果；文字增长时只重排末尾行。"""

    def __init__(self) -> None:
        self._states: dict[tuple, dict] = {}

    def clear(self) -> None:
        self._states.clear()

    def records(
        self,
        key: tuple,
        text: str,
        style: str,
        pixel_size: int,
        wrap_width: float,
        measure: Callable[[str], float],
    ) -> tuple[list[dict], str]:
        """返回段落行账本；稳定前缀沿用旧账本，只重排增长的尾部。"""
        signature = (style, pixel_size, wrap_width)
        state = self._states.get(key)
        if state is None or state["signature"] != signature:
            records = wrap_text(text, wrap_width, measure)
        else:
            previous = state["text"]
            if text == previous:
                return state["records"], previous
            old_records = state["records"]
            if len(old_records) <= 1:
                records = wrap_text(text, wrap_width, measure)
            else:
                frozen_count = len(old_records) - 1
                # 行结束不含被消费的空格/换行；必须从末行真正的起点续排。
                # 否则一个已消费的换行会再次生成空行。
                restart = old_records[frozen_count]["start"]
                if text[:restart] != previous[:restart]:
                    # 稳定通道明确给出修订时整体重建；该路径不受高频草稿触发。
                    records = wrap_text(text, wrap_width, measure)
                else:
                    records = [dict(record) for record in old_records[:frozen_count]]
                    suffix = wrap_text(text[restart:], wrap_width, measure)
                    for record in suffix:
                        record["start"] += restart
                        record["end"] += restart
                    records.extend(suffix)
        self._states[key] = {
            "signature": signature,
            "text": text,
            "records": records,
        }
        return records, text

    def prune(self, active_keys: Iterable[tuple]) -> None:
        """删除不再存在的段落账本，避免新段落复用旧文字。"""
        active = set(active_keys)
        for key in set(self._states) - active:
            self._states.pop(key, None)


@dataclass(frozen=True)
class StyleMetrics:
    """一种文字样式在布局阶段需要的度量。"""

    name: str
    line_height: float
    pixel_size: int


@dataclass(frozen=True)
class EntryView:
    """一条字幕：原文在上、译文在下，整体构成一个视觉组。"""

    source: str
    translation: str = ""
    draft: bool = False
    translation_pending: bool = False
    key: int | None = None


class ReadingCursor:
    """逐整行推进，给已经显示的行保留设定的最短阅读时间。"""

    def __init__(self):
        self.start = None
        self.advanced_at = 0.0

    def reset(self):
        self.start = None

    def select(self, total, capacity, now, dwell):
        latest = max(0, total - capacity)
        if dwell <= 0:
            self.start = latest
            self.advanced_at = now
            return latest, None
        if self.start is None:
            self.start = latest
            self.advanced_at = now
        # 实时阅读只容许一个视口的待显示行；更旧内容留在历史中回看，
        # 不能把突发结果逐行播放成数十秒的延迟。
        self.start = max(min(self.start, latest), latest - max(1, capacity))
        if self.start < latest and now >= self.advanced_at + dwell:
            self.start += 1
            self.advanced_at = now
        due = self.advanced_at + dwell if self.start < latest else None
        return self.start, due


class TranslationLaneState:
    """自动模式只在持续原文更新中，连续五秒无译文输出后收起。"""

    timeout = 5.0

    def __init__(self):
        self.context = None
        self.automatic = True
        self.visible = True
        self.missing_since = None
        self.last_source_at = None

    def configure(self, source, target, automatic):
        # 自动检测结果抖动不重置计时；仅用户选择的模式/目标变化才重置。
        context = (None if automatic else source, target, automatic)
        if context != self.context:
            self.context = context
            self.automatic = automatic
            self.reset()

    def reset(self):
        self.visible = self.automatic or self.context is None or self.context[0] != self.context[1]
        self.missing_since = None
        self.last_source_at = None

    def source_updated(self, now):
        if not self.automatic:
            return
        if self.last_source_at is None or now - self.last_source_at > self.timeout:
            self.missing_since = now
        self.last_source_at = now
        if self.missing_since is None:
            self.missing_since = now
        if now >= self.missing_since + self.timeout:
            self.visible = False

    def translation_output(self, now):
        if self.automatic:
            self.visible = True
            self.missing_since = now


def reading_window_height(preferred, source_row, translation_row, bilingual):
    """保存双语首选高度；无译文时仅保留同样容量的原文区。"""
    minimum = 98 + source_row + translation_row
    paired = max(preferred, minimum)
    if bilingual:
        return math.ceil(paired)
    return math.ceil(68 + (paired - 98) * source_row / (source_row + translation_row))


def paired_window_height(source_height, source_row, translation_row):
    if source_height <= math.ceil(68 + source_row):
        return 98 + source_row + translation_row
    return 98 + max(source_row, source_height - 68) * (source_row + translation_row) / source_row


def scrollbar_geometry(width, height, content_height, tail, offset):
    track = max(12, height - 64)
    thumb = min(track, max(16, track * max(height - 50, 1) / max(content_height, 1)))
    travel = track - thumb
    top = 12 + travel * max(0, min(offset, tail)) / max(tail, 1)
    return {"left": width - 20, "right": width, "top": 12, "bottom": 12 + track,
            "thumb_top": top, "thumb_height": thumb, "travel": travel}


def reading_positions(lines, viewport_top, viewport_bottom, scale, show_translation, row_heights, cursors=None, now=0.0, dwell=0.0, translation_current=True):
    """实时阅读使用独立、固定的语言区域；只在整行溢出时推进。

    历史浏览仍按段落成对排列。实时区域保留最近完整行，不把整个长段落
    绑在窗口底部；迟到的译文和新段落都不能改变原文区的基线。
    """
    source = [i for i, line in enumerate(lines) if line["style"] == "source"]
    translation = [i for i, line in enumerate(lines) if line["style"] == "translation"] if translation_current else []
    header = 18 * scale
    gap = 12 * scale
    available = max(0, viewport_bottom - viewport_top)
    if show_translation:
        source_row = row_heights["source"]
        translation_row = row_heights["translation"]
        usable = max(0, available - header * 2 - gap)
        # 相同的可见行数，比固定百分比更适合不同字号的双语字幕。
        source_height = usable * source_row / (source_row + translation_row)
        split = viewport_top + header + source_height + gap
        lanes = {"source": (viewport_top + header, split - gap),
                 "translation": (split + header, viewport_bottom)}
        labels = {"source": viewport_top, "translation": split}
    else:
        lanes = {"source": (viewport_top + header, viewport_bottom)}
        labels = {"source": viewport_top}
    positions = {}
    due_times = []
    for style, indices in (("source", source), ("translation", translation)):
        if style not in lanes or not indices:
            continue
        top, bottom = lanes[style]
        row_height = lines[indices[-1]]["height"]
        capacity = max(0, int((bottom - top + 0.01) // row_height))
        if not capacity:
            continue
        start = max(0, len(indices) - capacity)
        if cursors is not None:
            start, due = cursors[style].select(len(indices), capacity, now, dwell)
            if due is not None:
                due_times.append(due)
        for row, index in enumerate(indices[start:start + capacity]):
            positions[index] = top + row * row_height
    return {"reading_positions": positions, "reading_lanes": lanes,
            "reading_labels": labels,
            "next_reading_due": min(due_times) if due_times else None}


def build_lines(
    entries: Sequence[EntryView],
    styles: dict[str, StyleMetrics],
    wrap_width: float,
    scale: int,
    show_translation: bool,
    ledger: ParagraphLedger,
    measure: Callable[[str], float] | dict[str, Callable[[str], float]],
) -> tuple[list[dict], int]:
    """把字幕组堆叠成行；返回行列表和内容总高度（逻辑像素）。

    measure 可以是单个度量函数，也可以按样式名提供不同字体的度量函数。
    """
    measure_fns = measure if isinstance(measure, dict) else None
    paragraphs: list[tuple[str, str, int | None, bool]] = []
    for entry_index, entry in enumerate(entries):
        if entry.source:
            paragraphs.append((entry.source, "source", entry_index, entry.draft))
        if show_translation and entry.translation:
            # 译文作为独立段落排版，不与原文共用同一行。
            paragraphs.append((entry.translation, "translation", entry_index, False))
    if not paragraphs:
        paragraphs = [("等待识别", "source", None, False)]

    lines: list[dict] = []
    active_keys: set[tuple] = set()
    y = TOP_PADDING * scale
    previous_entry: int | None = None
    previous_style: str | None = None
    for paragraph, style, entry_index, draft in paragraphs:
        metrics = styles[style]
        key = (entries[entry_index].key if entry_index is not None and entries[entry_index].key is not None else entry_index, style)
        active_keys.add(key)
        records, _displayed = ledger.records(
            key,
            paragraph,
            style,
            metrics.pixel_size,
            wrap_width,
            measure_fns[style] if measure_fns is not None else measure,
        )
        for line_index, record in enumerate(records):
            if line_index == 0 and lines:
                # 同一组内原文到译文用小间距；跨组用大间距，成对阅读不串段。
                gap_before = (
                    PAIR_GAP * scale
                    if entry_index == previous_entry and style != previous_style
                    else GROUP_GAP * scale
                )
            else:
                gap_before = 0
            y += gap_before
            lines.append({
                "text": record["text"],
                "style": style,
                "entry": entry_index,
                "line_index": line_index,
                "char_start": record["start"],
                "char_end": record["end"],
                "y": y,
                "height": metrics.line_height,
                "gap_before": gap_before,
                "draft": draft,
            })
            y += metrics.line_height
            previous_entry = entry_index
            previous_style = style
    ledger.prune(active_keys)
    return lines, round(y / scale) + 4


def select_footer_lines(
    lines: Sequence[dict],
    latest_entry: int | None,
) -> list[int]:
    """选出底部固定区行：完整最新字幕组，永不按视口裁剪。

    最新组的原文与译文必须始终同区、同步运动；视口装不下时由绘制层
    裁掉顶部整行，而不是把行搬进滚动历史区，否则正文与译文会互相穿插。
    """
    return [
        index for index, line in enumerate(lines)
        if line["entry"] == latest_entry
    ]


def compute_geometry(
    lines: Sequence[dict],
    scale: int,
    viewport_top: float,
    viewport_bottom: float,
    content_height: int,
    follow_tail: bool,
    latest_entry: int | None,
) -> dict:
    """计算底部固定区、滚动边界与内容高度等布局结果。"""
    footer_indices = select_footer_lines(lines, latest_entry)
    # 跟随模式下整组固定；其余情况按普通滚动内容处理。
    footer_active = bool(follow_tail and footer_indices)
    footer_positions: dict[int, float] = {}
    footer_top = viewport_bottom
    footer_height = 0.0
    if footer_active:
        # 组内保持原文在上、译文在下；整组锚定在视口底部。
        footer_y = viewport_bottom
        for index in reversed(footer_indices):
            line = lines[index]
            footer_y -= line["height"]
            footer_positions[index] = footer_y
            footer_y -= line["gap_before"]
        footer_top = footer_positions[footer_indices[0]]
        footer_height = viewport_bottom - footer_top

    line_starts = [
        max(0, round((line["y"] - TOP_PADDING * scale) / scale))
        for line in lines
    ]
    full_tail_offset = max(
        0,
        round(
            (lines[-1]["y"] + lines[-1]["height"] - viewport_bottom) / scale
        ),
    ) if lines else 0

    if footer_active:
        footer_index_set = set(footer_indices)
        history_indices = [
            index for index in range(len(lines)) if index not in footer_index_set
        ]
        tail_offset = 0
        if history_indices:
            history_last = lines[history_indices[-1]]
            tail_offset = max(
                0,
                math.ceil(
                    (
                        history_last["y"]
                        + history_last["height"]
                        + HISTORY_FOOTER_GAP * scale
                        - footer_top
                    )
                    / scale
                ),
            )
        scroll_line_starts = [line_starts[index] for index in history_indices]
    else:
        tail_offset = full_tail_offset
        scroll_line_starts = list(line_starts)

    line_heights = [line["height"] for line in lines] or [1]
    visible_line_capacity = max(
        1,
        int(
            (viewport_bottom - viewport_top) // min(line_heights)
        ),
    )
    return {
        "footer_indices": footer_indices,
        "footer_positions": footer_positions,
        "footer_active": footer_active,
        "footer_top": footer_top,
        "footer_height": footer_height,
        "line_starts": line_starts,
        "tail_offset": tail_offset,
        "scroll_line_starts": scroll_line_starts,
        "scrollable": tail_offset > 0,
        "visible_line_capacity": visible_line_capacity,
        "content_height": content_height,
    }


def fit_window_height(
    content_height: int,
    minimum: int,
    maximum: int,
) -> int:
    """按内容行数与所需空间取窗口高度，并夹在用户上下限之间。"""
    return max(minimum, min(int(math.ceil(content_height)), int(maximum)))


def fit_rows_height(
    lines: Sequence[dict],
    footer_indices: Sequence[int],
    footer_active: bool,
    scale: int,
    top_padding: float,
    bottom_padding: float,
    minimum: int,
    maximum: int,
    hard_maximum: int | None = None,
) -> int:
    """自底向上按整行累计所需空间，返回对齐到整行的窗口高度。

    最新字幕组（固定区）无条件完整可见，其高度就是高度下限；历史行再
    向上按 maximum 扩展。不留半行空档，超过上限时丢弃顶部装不下的行，
    而不是裁切半行。hard_maximum 为屏幕级安全上限，缺省等于 maximum。
    """
    hard = maximum if hard_maximum is None else hard_maximum
    if not lines or scale <= 0:
        return max(minimum, min(hard, minimum))
    footer_set = set(footer_indices) if footer_active else set()
    total = float(bottom_padding * scale)
    pending_gap = 0.0
    # 1) 最新字幕组必须完整可见：正文与译文永远同区同高，不受上限截断。
    for index in reversed(list(footer_indices)):
        line = lines[index]
        total += line["height"] + pending_gap
        pending_gap = line["gap_before"]
    # 2) 历史行自底向上扩展，受 maximum 限制。
    for index in range(len(lines) - 1, -1, -1):
        if index in footer_set:
            continue
        line = lines[index]
        extra = line["height"] + pending_gap
        if total + extra > (maximum - top_padding) * scale:
            break
        total += extra
        pending_gap = line["gap_before"]
    height = round((top_padding * scale + total) / scale)
    return max(minimum, min(hard, height))


def ease_out_cubic(progress: float) -> float:
    """缓出三次方曲线：起步快、收尾稳，适合字幕过渡。"""
    p = min(1.0, max(0.0, progress))
    return 1.0 - (1.0 - p) ** 3


def fade_progress(elapsed_ms: float, duration_ms: float) -> float:
    """淡入进度（0～1）；未启用动画时立即完成。"""
    if duration_ms <= 0:
        return 1.0
    return ease_out_cubic(elapsed_ms / duration_ms)


class Motion1D:
    """一维缓动动画：从当前值平滑逼近目标值。"""

    def __init__(self, duration_ms: float, value: float = 0.0) -> None:
        self.duration_ms = max(0.0, float(duration_ms))
        self._value = float(value)
        self._target = float(value)
        self._start = float(value)
        self._started_at = time.monotonic()
        self._running = False

    @property
    def value(self) -> float:
        return self._value

    @property
    def target(self) -> float:
        return self._target

    @property
    def running(self) -> bool:
        return self._running

    def snap(self, value: float) -> None:
        """立即跳到目标值，不产生动画。"""
        self._value = self._target = self._start = float(value)
        self._running = False

    def set_target(self, value: float, *, instant: bool = False) -> bool:
        """设置目标值；返回是否发生可见变化。"""
        value = float(value)
        if value == self._target and (self._running or value == self._value):
            return False
        self._target = value
        if instant or self.duration_ms <= 0:
            changed = value != self._value
            self.snap(value)
            return changed
        self._start = self._value
        self._started_at = time.monotonic()
        self._running = True
        return True

    def advance(self, now: float | None = None) -> tuple[float, bool]:
        """推进到 now 时刻的值；返回 (当前值, 是否结束)。"""
        if not self._running:
            return self._value, True
        now = time.monotonic() if now is None else now
        elapsed_ms = (now - self._started_at) * 1000.0
        if elapsed_ms >= self.duration_ms or self._start == self._target:
            self._value = self._target
            self._running = False
            return self._value, True
        progress = ease_out_cubic(elapsed_ms / self.duration_ms)
        self._value = self._start + (self._target - self._start) * progress
        return self._value, False
