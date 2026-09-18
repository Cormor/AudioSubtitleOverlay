"""利用重叠窗口中的一致文字，合并已确认正文与可修订末尾。"""

from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class TextUnit:
    text: str
    start: float
    end: float


def _key(text):
    return ''.join(char.casefold() for char in text if not char.isspace() and not unicodedata.category(char).startswith('P'))


class RollingTranscript:
    """词级时间用于定位重叠区域，连续两次一致的前缀用于确认正文。"""

    pause_comma_seconds = 0.35
    pause_split_seconds = 0.55
    pause_sentence_seconds = 0.7
    backfill_punctuation = set("。！？.!?,，；;：:")
    boundary_punctuation = set("。！？.!?,，；;：:")

    def __init__(self):
        self.confirmed_end = 0.0
        self.confirmed_text = ''
        self.active_text = ''
        self.block_id = 1
        self.pending: list[TextUnit] = []
        self._published = {}
        self._safe_end = 0.0
        self._last_stable_end = 0.0
        self._block_end_times = {}

    @property
    def safe_trim_time(self):
        return self.confirmed_end if self.pending else self._safe_end

    def _units(self, words, offset):
        result = []
        for word in words:
            count = max(len(word.text), 1)
            duration = max(word.end - word.start, 0.0)
            for index, char in enumerate(word.text):
                result.append(TextUnit(char, offset + word.start + duration * index / count,
                                       offset + word.start + duration * (index + 1) / count))
        return result

    def _trim_confirmed(self, units):
        """用已确认末尾在重叠窗口中的时间与文字共同定位，避免重复追加。"""
        if not self.confirmed_text:
            return units

        def keep_boundary_punctuation(unit):
            """保留模型在下一窗口才补出的句末标点。"""
            return (
                unicodedata.category(unit.text[:1]).startswith("P")
                and not self.confirmed_text.rstrip().endswith(unit.text)
                and unit.start <= self.confirmed_end + 0.2
                and unit.end >= self.confirmed_end - 0.3
            ) if unit.text else False

        def after_confirmed_boundary(items):
            return [
                unit for unit in items
                if unit.end > self.confirmed_end + 0.02 or keep_boundary_punctuation(unit)
            ]

        confirmed = _key(self.confirmed_text[-128:])
        positions = [(index, _key(unit.text)) for index, unit in enumerate(units) if _key(unit.text)]
        incoming = ''.join(key for _, key in positions)
        for length in range(min(16, len(confirmed)), 1, -1):
            suffix = confirmed[-length:]
            start = 0
            candidates = []
            while (found := incoming.find(suffix, start)) >= 0:
                end_index = positions[found + length - 1][0]
                distance = abs(units[end_index].end - self.confirmed_end)
                if distance <= 0.8:
                    candidates.append((distance, end_index))
                start = found + 1
            if candidates:
                _, end_index = min(candidates)
                end_index += 1
                # 已确认的句末标点不在新窗口中重复显示。
                while (end_index < len(units) and unicodedata.category(units[end_index].text).startswith("P")
                       and self.confirmed_text.rstrip().endswith(units[end_index].text)
                       and units[end_index].end <= self.confirmed_end + 0.15):
                    end_index += 1
                return after_confirmed_boundary(units[end_index:])
        return after_confirmed_boundary(units)

    def _backfill_late_punctuation(self, units, updates):
        """把后续重叠窗口补出的标点回填到已经分段的原文。"""
        if not self._block_end_times or not units:
            return
        for index, unit in enumerate(units):
            if unit.text not in self.backfill_punctuation:
                continue
            prefix_key = _key("".join(item.text for item in units[:index]))
            if not prefix_key:
                continue
            for block_id in sorted(self._block_end_times, reverse=True):
                text = self._published.get(block_id, "")
                if not text or text[-1:] in self.backfill_punctuation:
                    continue
                block_end = self._block_end_times[block_id]
                if abs(unit.start - block_end) > 0.5:
                    continue
                block_key = _key(text[-16:])
                if not block_key:
                    continue
                # 模型在重叠窗开头可能替换少量字符，使用末尾共同片段定位段落。
                matched = any(
                    len(block_key) >= size and prefix_key.endswith(block_key[-size:])
                    for size in range(min(12, len(block_key)), 3, -1)
                )
                if matched:
                    core = text.rstrip(" \t\r\n")
                    suffix = text[len(core):]
                    corrected = core + unit.text + suffix
                    self._published[block_id] = corrected
                    updates.append((block_id, corrected))
                    break

    @classmethod
    def _add_pause_punctuation(cls, text: str, gap: float) -> str:
        """在模型未给出标点时，按停顿长度补入可修订的中文标点。"""
        core = text.rstrip(" \t\r\n")
        suffix = text[len(core):]
        if not core or core[-1:] in cls.boundary_punctuation:
            return text
        mark = "。" if gap + 1e-6 >= cls.pause_sentence_seconds else "，"
        return core + mark + suffix

    def _pending_display_text(self) -> str:
        """未确认尾部也按停顿显示分隔，排版分隔不改动词时间或确认状态。"""
        text = self.active_text
        previous_end = self._last_stable_end
        for unit in self.pending:
            gap = unit.start - previous_end
            if text.strip() and gap >= self.pause_comma_seconds and _key(unit.text):
                text = self._add_pause_punctuation(text, gap)
                if gap >= self.pause_split_seconds and not text.endswith("\n"):
                    text += "\n"
            text += unit.text
            previous_end = max(previous_end, unit.end)
        if text.strip() and self.pending and self._safe_end - self.pending[-1].end >= self.pause_sentence_seconds:
            text = self._add_pause_punctuation(text, self.pause_sentence_seconds)
        return text.strip(" \t")

    @staticmethod
    def _unit_key(unit):
        """返回单个识别单元的比对键，原始单元内容不在此处丢弃。"""
        return _key(unit.text)

    def _select_pending(self, old_units, new_units):
        """新版本直接修订候选；静音空结果保留尾部直到语音结束确认。"""
        return new_units if new_units else old_units

    def update(self, words, offset, audio_end):
        # 离开修订窗口的候选按已有文字提交，不因窗口前移丢失正文。
        expired = [unit for unit in self.pending if unit.end <= offset]
        self.pending = [unit for unit in self.pending if unit.end > offset]
        units = self._units(words, offset)
        updates = []
        self._backfill_late_punctuation(units, updates)
        incoming = self._trim_confirmed(units)
        stale_result = bool(incoming and self.pending and incoming[-1].end < self.pending[-1].end - 0.2)
        if stale_result:
            # 新结果只覆盖更早音频时，不能清除已显示的较新尾部。
            # 同一时间范围的字符纠错仍接受，不按字符前缀锁住候选。
            # 空候选保留已有尾部，但不把已有尾部与自身比较后错误确认为稳定。
            incoming = []
        stable_count = 0
        previous_index = 0
        committed_previous_index = 0
        current_index = 0
        while previous_index < len(self.pending) and current_index < len(incoming):
            separator_start = current_index
            while current_index < len(incoming) and not self._unit_key(incoming[current_index]):
                current_index += 1
            if current_index >= len(incoming):
                break
            current = incoming[current_index]
            if current.end > audio_end - 0.6:
                break
            while previous_index < len(self.pending) and not self._unit_key(self.pending[previous_index]):
                previous_index += 1
            if previous_index >= len(self.pending):
                break
            previous = self.pending[previous_index]
            if self._unit_key(previous) != self._unit_key(current) or abs(previous.end - current.end) > 0.8:
                current_index = separator_start
                break
            previous_index += 1
            current_index += 1
            committed_previous_index = previous_index
            stable_count = current_index
        # 语音结束后，静音窗口不应清除已经显示的末尾正文。
        end_of_speech = bool(
            not stale_result
            and self.pending
            and self.pending[-1].end <= audio_end - self.pause_sentence_seconds
            and (not incoming or incoming[-1].end <= audio_end - self.pause_sentence_seconds)
        )
        if end_of_speech:
            incoming = incoming or self.pending
            stable_count = len(incoming)
        stable = expired + incoming[:stable_count]
        if end_of_speech:
            # 静音窗口已经把整个待确认尾部提交为稳定正文，不再把同一批单元保留为候选。
            old_pending_remainder = []
            new_pending_remainder = []
        else:
            old_pending_remainder = self.pending[committed_previous_index:]
            new_pending_remainder = incoming[stable_count:]
        self.pending = self._select_pending(
            old_pending_remainder,
            new_pending_remainder,
        )
        for index, unit in enumerate(stable):
            previous_end = stable[index - 1].end if index else self._last_stable_end
            pause_gap = unit.start - previous_end
            pause_break = (
                pause_gap >= self.pause_split_seconds
                and self.active_text.strip(" \t")
            )
            if pause_break:
                # 识别结果没有标点时，词时间中的明显停顿也形成新的字幕段。
                self._block_end_times[self.block_id] = previous_end
                self._publish(
                    updates,
                    self.block_id,
                    self._add_pause_punctuation(self.active_text, pause_gap),
                )
                self.block_id += 1
                self.active_text = ''
            if not pause_break and pause_gap >= self.pause_comma_seconds and _key(unit.text):
                self.active_text = self._add_pause_punctuation(self.active_text, pause_gap)
            self.active_text += unit.text
            self._last_stable_end = max(self._last_stable_end, unit.end)
            self.confirmed_text = (self.confirmed_text + unit.text)[-128:]
            self.confirmed_end = max(self.confirmed_end, unit.end)
            next_text = stable[index + 1].text if index + 1 < len(stable) else (self.pending[0].text if self.pending else '')
            sentence_end = unit.text in '。！？' or (unit.text in '.!?' and (not next_text or next_text.isspace()))
            # 长句在已确认的词边界分段，避免整段历史每次从头重新排版。
            long_block = len(self.active_text) >= 160 and (unit.text.isspace() or '\u3400' <= unit.text <= '\u9fff')
            if (sentence_end or long_block) and self.active_text.strip(" \t"):
                self._block_end_times[self.block_id] = unit.end
                self._publish(updates, self.block_id, self.active_text.strip(" \t"))
                self.block_id += 1
                self.active_text = ''
        if end_of_speech or (
            self.active_text and not self.pending
            and audio_end - self._last_stable_end >= self.pause_sentence_seconds
        ):
            self.active_text = self._add_pause_punctuation(
                self.active_text,
                self.pause_sentence_seconds,
            )
        self._safe_end = audio_end
        current_text = self._pending_display_text()
        if current_text:
            self._publish(updates, self.block_id, current_text)
        return updates

    def _publish(self, updates, block_id, text):
        if self._published.get(block_id) != text:
            self._published[block_id] = text
            updates.append((block_id, text))
