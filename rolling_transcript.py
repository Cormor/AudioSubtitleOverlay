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
    boundary_punctuation = set("。！？.!?,，；;：:…～")
    # 句末标点：分段推迟到收尾符号之后，避免把引号、括号拆到下一段。
    terminal_punctuation = set("。！？.!…～")
    # 句末之后允许继续吸附的收尾引号、括号。
    closing_punctuation = set("”’\"』」）》】〉)]}")
    # 补标点前不应出现标点的起始符号（如以左引号结尾时不补）。
    opening_punctuation = set("“‘『「（《【〈([{")
    # 长句在已确认的词边界分段，避免整段历史每次从头重新排版。
    long_block_limit = 160
    long_block_hard_limit = 260

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
        self._active_end_time = 0.0
        self._split_armed = None
        self._armed_terminal = ''
        self._preferred_break = 0
        self.safe_decode_start = 0.0

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
        positions = [(index, char) for index, unit in enumerate(units) for char in _key(unit.text)]
        incoming = ''.join(key for _, key in positions)
        for length in range(min(128, len(confirmed)), 1, -1):
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

    @staticmethod
    def _is_cjk(char: str) -> bool:
        """判断字符是否属于中日韩文字（不含标点）。"""
        return bool(char) and (
            "\u3400" <= char <= "\u9fff"
            or "\u3040" <= char <= "\u30ff"
            or "\uac00" <= char <= "\ud7af"
        )

    @classmethod
    def _ends_with_cjk(cls, text: str) -> bool:
        for char in reversed(text):
            if not char.isspace():
                return cls._is_cjk(char)
        return False

    @classmethod
    def _add_pause_punctuation(cls, text: str, gap: float, mark: str | None = None) -> str:
        """按停顿长度补入可修订标点；已有标点时不重复补入。

        中日韩文字后补全角标点，拉丁文字后补半角标点，避免英文被插入全角逗号。
        判断“已有标点”时会看穿末尾的收尾引号、括号（如“好。”后不再补）。
        """
        core = text.rstrip(" \t\r\n")
        suffix = text[len(core):]
        if not core:
            return text
        head = core
        while len(head) > 1 and head[-1] in cls.closing_punctuation:
            head = head[:-1]
        if head[-1] in cls.boundary_punctuation or head[-1] in cls.opening_punctuation:
            return text
        if mark is None:
            sentence_level = gap + 1e-6 >= cls.pause_sentence_seconds
            if cls._ends_with_cjk(head):
                mark = "。" if sentence_level else "，"
            else:
                mark = "." if sentence_level else ","
        return core + mark + suffix

    def _append_char(self, char: str, unit: TextUnit) -> None:
        """提交单个字符并推进已确认位置。"""
        self.active_text += char
        self._active_end_time = max(self._active_end_time, unit.end)
        self._last_stable_end = max(self._last_stable_end, unit.end)
        self.confirmed_text = (self.confirmed_text + char)[-128:]
        self.confirmed_end = max(self.confirmed_end, unit.end)

    def _close_block(self, updates) -> None:
        """把当前段落落段；段内收尾引号、括号已先吸附到正文。"""
        text = self.active_text.strip(" \t")
        if text:
            self._block_end_times[self.block_id] = self._active_end_time
            self._publish(updates, self.block_id, text)
            self.block_id += 1
        self.active_text = ''
        self._active_end_time = 0.0
        self._preferred_break = 0
        self._split_armed = None
        self._armed_terminal = ''

    def _absorbs_after_split(self, char: str) -> bool:
        """句末之后的收尾引号、括号、空白与重复句末标点留在上一段内。

        起始引号、括号不吸收，避免下一段开头的引号被并进上一段。
        """
        if self._split_armed != "sentence":
            return char in self.terminal_punctuation
        return (
            char.isspace()
            or char in self.terminal_punctuation
            or char in self.closing_punctuation
        )

    def _can_split_before(self, char: str) -> bool:
        """判断是否可以在当前字符之前落段。"""
        if self._split_armed == "sentence":
            return True
        if self._split_armed == "long":
            previous = self.active_text[-1:]
            return (
                char.isspace()
                or self._is_cjk(char)
                or unicodedata.category(char).startswith("P")
                or self._is_cjk(previous)
                or (previous and unicodedata.category(previous).startswith("P"))
            )
        return False

    def _note_split_hint(self, char: str) -> None:
        """记录可分段位置：句末标点、空白，以及长句软分段阈值。"""
        if char.isspace():
            self._preferred_break = len(self.active_text)
        if char in self.terminal_punctuation:
            self._split_armed = "sentence"
            self._armed_terminal = char
            self._preferred_break = len(self.active_text)
        elif self._split_armed is None and len(self.active_text) >= self.long_block_limit:
            self._split_armed = "long"

    def _maybe_hard_split(self, updates) -> None:
        """超长兜底：在最佳边界带标点切开，避免无限增长的段落。"""
        if len(self.active_text) < self.long_block_hard_limit:
            return
        split_at = self._preferred_break
        if split_at < self.long_block_limit // 2:
            split_at = self.long_block_limit
        split_at = min(split_at, len(self.active_text))
        remainder = self.active_text[split_at:]
        self.active_text = self._add_pause_punctuation(
            self.active_text[:split_at],
            self.pause_comma_seconds,
        )
        self._close_block(updates)
        self.active_text = remainder

    def _pending_display_text(self) -> str:
        """未确认尾部也按停顿显示分隔，排版分隔不改动词时间或确认状态。"""
        text = self.active_text
        previous_end = self._last_stable_end
        for unit in self.pending:
            gap = unit.start - previous_end
            if text.strip() and gap >= self.pause_comma_seconds and _key(unit.text):
                text = self._add_pause_punctuation(text, gap)
                # 草稿停顿只补标点；确认分段统一由提交路径处理，避免换行闪动。
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

    def _commit_units(self, stable, updates):
        """统一提交过期候选和一致候选，先推进去重时间再接收下一窗口。"""
        for index, unit in enumerate(stable):
            char = unit.text
            if self._split_armed and self._absorbs_after_split(char):
                # 收尾引号、括号与重复句末标点吸附到上一段，句末不拆行。
                self._append_char(char, unit)
                self._note_split_hint(char)
                continue
            if (
                self._split_armed == "sentence"
                and self._armed_terminal == "."
                and char.isascii()
                and (char.isdigit() or char.islower())
            ):
                # 小数点（3.14）与缩写（e.g.）不作为句末。
                self._split_armed = None
                self._armed_terminal = ''
            if self._split_armed and self._can_split_before(char):
                # 句末/长句分段：在下一个自然边界落段，正文不丢收尾符号。
                self._close_block(updates)
            previous_end = stable[index - 1].end if index else self._last_stable_end
            pause_gap = unit.start - previous_end
            if pause_gap >= self.pause_split_seconds:
                self.safe_decode_start = max(self.safe_decode_start, (previous_end + unit.start) / 2)
            pause_break = (
                pause_gap >= self.pause_split_seconds
                and self.active_text.strip(" \t")
            )
            if pause_break:
                # 识别结果没有标点时，词时间中的明显停顿也形成新的字幕段；
                # 段末按句末级补标点，避免新段以逗号结尾。
                self.active_text = self._add_pause_punctuation(
                    self.active_text,
                    max(pause_gap, self.pause_sentence_seconds),
                )
                self._close_block(updates)
            elif pause_gap >= self.pause_comma_seconds and self.active_text.strip(" \t"):
                # 停顿后的首字符即使是空白也先补标点，避免逗号丢失在空格上。
                self.active_text = self._add_pause_punctuation(self.active_text, pause_gap)
            self._append_char(char, unit)
            self._note_split_hint(char)
            self._maybe_hard_split(updates)

    def update(self, words, offset, audio_end):
        # 离开修订窗口的候选按已有文字提交，不因窗口前移丢失正文。
        expired = [unit for unit in self.pending if unit.end <= offset]
        self.pending = [unit for unit in self.pending if unit.end > offset]
        units = self._units(words, offset)
        updates = []
        # 过期前缀属于已提交音频，必须先推进确认边界；否则新窗重定时
        # 的同一前缀会与 expired 一起追加，形成 hellohello 这样的重复。
        self._commit_units(expired, updates)
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
        stable = incoming[:stable_count]
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
        self._commit_units(stable, updates)
        if end_of_speech or (
            self.active_text and not self.pending
            and audio_end - self._last_stable_end >= self.pause_sentence_seconds
        ):
            self.safe_decode_start = max(self.safe_decode_start, (self._last_stable_end + audio_end) / 2)
            self.active_text = self._add_pause_punctuation(
                self.active_text,
                self.pause_sentence_seconds,
            )
            self._close_block(updates)
        self._safe_end = audio_end
        current_text = self._pending_display_text()
        if current_text:
            self._publish(updates, self.block_id, current_text)
        return updates

    def _publish(self, updates, block_id, text):
        if self._published.get(block_id) != text:
            self._published[block_id] = text
            updates.append((block_id, text))
