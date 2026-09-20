"""本地模型和免费在线翻译适配层。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Callable
from urllib.request import Request, urlopen

from download_manager import ComponentFile, ComponentSpec, DownloadProgress, DownloadManager
from settings import application_data_dir


LOGGER = logging.getLogger(__name__)
ARGOS_PACKAGE_INDEX = (
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
)
TRANSLATION_MODEL_MARKER = ".audio-subtitle-overlay-translation.json"
LOCAL_TRANSLATION_BACKENDS = frozenset(("仅使用本地 Argos", "仅使用本地翻译"))


@dataclass(frozen=True)
class TranslationPackage:
    """远端清单中的一个语言模型包。"""

    from_code: str
    to_code: str
    package_code: str
    package_version: str
    urls: tuple[str, ...]


@dataclass
class _LoadedTranslationModel:
    """已经加载到内存的本地翻译模型。"""

    tokenizer: object
    translator: object


_package_index_lock = threading.Lock()
_package_index: tuple[dict, ...] | None = None
_translation_manager_lock = threading.Lock()
_translation_manager: DownloadManager | None = None
_loaded_models_lock = threading.Lock()
_loaded_models: dict[Path, _LoadedTranslationModel] = {}


def _translation_download_root() -> Path:
    """返回本地翻译模型的下载目录。"""
    path = application_data_dir() / "downloads" / "translations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _translation_model_root() -> Path:
    """返回本地翻译模型的安装目录。"""
    path = application_data_dir() / "translation_models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _translation_download_manager() -> DownloadManager:
    """返回共享下载管理器，避免重复下载同一个语言模型。"""
    global _translation_manager
    with _translation_manager_lock:
        if _translation_manager is None:
            _translation_manager = DownloadManager(_translation_download_root())
        return _translation_manager


def _load_package_index() -> tuple[dict, ...]:
    """读取语言模型清单；单次运行只读取一次。"""
    global _package_index
    with _package_index_lock:
        if _package_index is not None:
            return _package_index
        request = Request(
            ARGOS_PACKAGE_INDEX,
            headers={"User-Agent": "AudioSubtitleOverlay/1.0"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise RuntimeError("无法读取本地翻译模型清单，请检查网络连接。") from error
        if not isinstance(payload, list):
            raise RuntimeError("本地翻译模型清单格式不正确。")
        _package_index = tuple(item for item in payload if isinstance(item, dict))
        return _package_index


def _find_package(from_code: str, to_code: str) -> TranslationPackage | None:
    """查找指定方向的可下载模型包。"""
    for item in _load_package_index():
        if item.get("from_code") != from_code or item.get("to_code") != to_code:
            continue
        links = tuple(
            str(url)
            for url in item.get("links", ())
            if str(url).startswith(("https://", "http://"))
        )
        if not links:
            continue
        package_code = str(item.get("code") or f"translate-{from_code}_{to_code}")
        package_version = str(item.get("package_version") or "1")
        return TranslationPackage(
            from_code=from_code,
            to_code=to_code,
            package_code=package_code,
            package_version=package_version,
            urls=links,
        )
    return None


def _package_path_part(value: str) -> str:
    """把远端清单字段转换为安全的本地文件名片段。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _build_package_spec(package: TranslationPackage) -> ComponentSpec:
    """把远端语言模型转换为通用下载器组件。"""
    pair = f"{package.from_code}_{package.to_code}"
    key = f"translation.{pair}"
    version = (
        f"{_package_path_part(package.package_code)}-"
        f"{_package_path_part(package.package_version)}"
    )
    file_name = (
        f"{_package_path_part(package.package_code)}-"
        f"{_package_path_part(package.package_version)}.argosmodel"
    )
    return ComponentSpec(
        key=key,
        version=version,
        target_dir=_translation_model_root() / pair,
        marker_name=TRANSLATION_MODEL_MARKER,
        files=(
            ComponentFile(
                path=file_name,
                url=package.urls[0],
                urls=package.urls,
                archive=True,
                headers=(("User-Agent", "AudioSubtitleOverlay/1.0"),),
            ),
        ),
    )


def _find_model_root(path: Path, from_code: str, to_code: str) -> Path | None:
    """查找已经解压且包含核心文件的语言模型目录。"""
    if not path.is_dir():
        return None
    for metadata_path in sorted(path.rglob("metadata.json")):
        root = metadata_path.parent
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if metadata.get("from_code") != from_code or metadata.get("to_code") != to_code:
            continue
        required = (
            root / "model" / "config.json",
            root / "model" / "model.bin",
            root / "sentencepiece.model",
        )
        if all(item.is_file() for item in required):
            return root
    return None


def _model_package_pairs(from_code: str, to_code: str) -> list[tuple[str, str, TranslationPackage]]:
    """选择直连模型；没有直连时使用两个经过英语的模型。"""
    direct = _find_package(from_code, to_code)
    if direct is not None:
        return [(from_code, to_code, direct)]
    if from_code != "en" and to_code != "en":
        first = _find_package(from_code, "en")
        second = _find_package("en", to_code)
        if first is not None and second is not None:
            return [(from_code, "en", first), ("en", to_code, second)]
    raise RuntimeError(f"本地翻译暂不提供 {from_code}->{to_code} 语言模型。")


class ArgosTranslator:
    """使用 Argos 模型包和 CTranslate2 执行本地翻译。"""

    def __init__(self, on_status: Callable[[str], None] | None = None) -> None:
        self._on_status = on_status
        self._last_progress_at = 0.0

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        if source_language == target_language:
            return text
        try:
            import ctranslate2
            import sentencepiece as sentencepiece_module
        except ImportError as error:
            raise RuntimeError("本地翻译组件不完整，请重新安装当前版本。") from error

        translated = text
        for from_code, to_code, package in _model_package_pairs(
            source_language, target_language
        ):
            model_root = self._ensure_model(package)
            model = self._load_model(
                model_root,
                ctranslate2,
                sentencepiece_module,
            )
            translated = self._translate_with_model(model, translated)
        return translated

    def _ensure_model(self, package: TranslationPackage) -> Path:
        """首次使用时下载并安装语言模型。"""
        spec = _build_package_spec(package)
        existing = _find_model_root(spec.target_dir, package.from_code, package.to_code)
        if existing is not None:
            return existing
        self._notify_status(
            f"正在准备本地翻译模型：{package.from_code}→{package.to_code}，首次使用需要下载。"
        )

        def on_progress(progress: DownloadProgress) -> None:
            if progress.total:
                percentage = f"{progress.completed / progress.total * 100:.1f}%"
            else:
                percentage = ""
            now = time.monotonic()
            if now - self._last_progress_at < 0.4 and progress.state == "下载中":
                return
            self._last_progress_at = now
            detail = f"，{percentage}" if percentage else ""
            self._notify_status(f"正在下载本地翻译模型：{progress.state}{detail}。")

        _translation_download_manager().ensure(spec, on_progress=on_progress)
        result = _find_model_root(spec.target_dir, package.from_code, package.to_code)
        if result is None:
            raise RuntimeError(
                f"本地翻译模型 {package.from_code}->{package.to_code} 安装后未找到完整文件。"
            )
        archive_path = (
            _translation_download_root()
            / spec.key
            / spec.version
            / "files"
            / spec.files[0].path
        )
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("无法清理本地翻译模型压缩包：%s", archive_path)
        self._notify_status(f"本地翻译模型已准备：{package.from_code}→{package.to_code}。")
        return result

    @staticmethod
    def _load_model(root: Path, ctranslate2_module, sentencepiece_module) -> _LoadedTranslationModel:
        """加载模型并在同一进程内复用。"""
        root = root.resolve()
        with _loaded_models_lock:
            cached = _loaded_models.get(root)
            if cached is not None:
                return cached
            tokenizer = sentencepiece_module.SentencePieceProcessor(
                model_file=str(root / "sentencepiece.model")
            )
            translator = ctranslate2_module.Translator(
                str(root / "model"),
                device="cpu",
                compute_type="int8_float32",
            )
            loaded = _LoadedTranslationModel(tokenizer, translator)
            _loaded_models[root] = loaded
            return loaded

    @staticmethod
    def _translate_with_model(model: _LoadedTranslationModel, text: str) -> str:
        """按句末拆分短字幕，再调用本地模型。"""
        pieces = re.split(r"(?<=[。！？!?\.])", text)
        translated: list[str] = []
        for piece in pieces:
            if not piece:
                continue
            tokens = model.tokenizer.encode(piece, out_type=str)
            if not tokens:
                translated.append(piece)
                continue
            results = model.translator.translate_batch(
                [tokens],
                replace_unknowns=True,
                max_batch_size=32,
                batch_type="tokens",
                beam_size=4,
            )
            if not results or not results[0].hypotheses:
                raise RuntimeError("本地翻译模型没有返回结果。")
            value = model.tokenizer.decode(results[0].hypotheses[0])
            value = value.replace("▁", " ").replace("_", " ")
            translated.append(value[1:] if value.startswith(" ") else value)
        return "".join(translated)

    def _notify_status(self, text: str) -> None:
        if self._on_status is not None:
            self._on_status(text)


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

    def __init__(
        self,
        backend: str,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.backend = backend
        self._argos = ArgosTranslator(on_status=on_status)
        self._online = MyMemoryTranslator()

    def translate(self, text: str, source_language: str | None, target_language: str) -> str:
        if not text or not target_language:
            return ""
        if not source_language or source_language == target_language:
            return text
        if self.backend in LOCAL_TRANSLATION_BACKENDS:
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
