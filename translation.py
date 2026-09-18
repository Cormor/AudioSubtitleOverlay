"""本地和免费在线翻译适配层。"""

from __future__ import annotations

import re


class ArgosTranslator:
    """调用已安装的 Argos Translate 离线语言包。"""

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        if source_language == target_language:
            return text
        try:
            import argostranslate.translate as argos_translate
        except ImportError as error:
            raise RuntimeError(
                "未安装 Argos Translate。请安装 requirements-local-translation.txt，"
                "或切换到免费在线翻译。"
            ) from error

        installed = argos_translate.get_installed_languages()
        from_lang = next(
            (item for item in installed if item.code == source_language),
            None,
        )
        to_lang = next(
            (item for item in installed if item.code == target_language),
            None,
        )
        if from_lang is None or to_lang is None:
            raise RuntimeError(
                f"Argos 未安装 {source_language}->{target_language} 对应的语言包。"
            )
        translation = from_lang.get_translation(to_lang)
        if translation is None:
            raise RuntimeError(
                f"Argos 没有可用的 {source_language}->{target_language} 翻译路径。"
            )
        return translation.translate(text)


class MyMemoryTranslator:
    """调用 MyMemory 公共接口的免费在线翻译。"""

    endpoint = "https://api.mymemory.translated.net/get"
    max_bytes = 480

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        if source_language == target_language:
            return text
        if not source_language:
            raise RuntimeError("在线翻译需要先确定源语言；请在界面中选择源语言。")
        try:
            import requests
        except ImportError as error:
            raise RuntimeError("未安装 requests，无法使用免费在线翻译。") from error

        pieces = self._split_text(text)
        translated = []
        for piece in pieces:
            response = requests.get(
                self.endpoint,
                params={
                    "q": piece,
                    "langpair": f"{source_language}|{target_language}",
                    "mt": "1",
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            value = payload.get("responseData", {}).get("translatedText")
            if not value:
                raise RuntimeError("在线翻译接口没有返回译文。")
            translated.append(str(value))
        return "".join(translated)

    def _split_text(self, text: str) -> list[str]:
        """按句末和字节上限拆分，避免超过公共接口的单次请求限制。"""
        if len(text.encode("utf-8")) <= self.max_bytes:
            return [text]
        sentences = re.split(r"(?<=[。！？!?\.])", text)
        pieces: list[str] = []
        current = ""
        for sentence in sentences:
            if not sentence:
                continue
            if len(sentence.encode("utf-8")) > self.max_bytes:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.extend(self._split_by_bytes(sentence))
                continue
            candidate = current + sentence
            if current and len(candidate.encode("utf-8")) > self.max_bytes:
                pieces.append(current)
                current = sentence
            elif len(candidate.encode("utf-8")) <= self.max_bytes:
                current = candidate
            else:
                pieces.extend(self._split_by_bytes(sentence))
                current = ""
        if current:
            pieces.append(current)
        return pieces

    def _split_by_bytes(self, text: str) -> list[str]:
        pieces: list[str] = []
        current = ""
        for char in text:
            candidate = current + char
            if current and len(candidate.encode("utf-8")) > self.max_bytes:
                pieces.append(current)
                current = char
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces


class TranslationService:
    """根据用户选择执行本地、在线或本地优先翻译。"""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self._argos = ArgosTranslator()
        self._online = MyMemoryTranslator()

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        if not text or not target_language:
            return ""
        if not source_language or source_language == target_language:
            return text
        if self.backend == "仅使用本地 Argos":
            return self._argos.translate(text, source_language, target_language)
        if self.backend == "仅使用免费在线 MyMemory":
            return self._online.translate(text, source_language, target_language)
        try:
            return self._argos.translate(text, source_language, target_language)
        except Exception as local_error:
            try:
                return self._online.translate(text, source_language, target_language)
            except Exception as online_error:
                raise RuntimeError(
                    f"本地翻译失败：{local_error}；在线翻译失败：{online_error}"
                ) from online_error
