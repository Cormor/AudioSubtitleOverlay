"""组件下载、校验、暂停、继续和断点续传实现。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid
import zipfile


LOGGER = logging.getLogger(__name__)
COMPONENT_MARKER = ".audio-subtitle-overlay-component.json"


class DownloadError(RuntimeError):
    """组件下载或安装失败。"""


class DownloadCancelled(DownloadError):
    """用户取消了下载。"""


class _PauseRequested(Exception):
    """内部异常，用于关闭当前HTTP响应并保留断点。"""


@dataclass(frozen=True)
class ComponentFile:
    """组件中的一个远端文件。"""

    path: str
    url: str
    size: int | None = None
    sha256: str | None = None
    archive: bool = False
    headers: tuple[tuple[str, str], ...] = ()
    urls: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: dict) -> "ComponentFile":
        """从JSON对象创建文件定义。"""
        headers = payload.get("headers", {}) or {}
        if not isinstance(headers, dict):
            raise ValueError("下载文件headers必须是对象。")
        primary_url = str(payload["url"])
        alternate_urls = payload.get("urls", []) or []
        if isinstance(alternate_urls, str):
            alternate_urls = [alternate_urls]
        if not isinstance(alternate_urls, (list, tuple)):
            raise ValueError("下载文件urls必须是字符串数组。")
        download_urls = tuple(dict.fromkeys([primary_url, *(str(item) for item in alternate_urls)]))
        return cls(
            path=str(payload["path"]),
            url=primary_url,
            urls=download_urls,
            size=(None if payload.get("size") is None else int(payload["size"])),
            sha256=(None if not payload.get("sha256") else str(payload["sha256"]).lower()),
            archive=bool(payload.get("archive", False)),
            headers=tuple((str(key), str(value)) for key, value in headers.items()),
        )

    @property
    def download_urls(self) -> tuple[str, ...]:
        """返回按优先级排列的下载地址；旧代码只提供url时仍可工作。"""
        return self.urls or (self.url,)


@dataclass(frozen=True)
class ComponentSpec:
    """一个可下载并安装的组件。"""

    key: str
    version: str
    target_dir: Path
    files: tuple[ComponentFile, ...]
    marker_name: str = COMPONENT_MARKER

    @classmethod
    def from_mapping(cls, payload: dict, target_dir: Path) -> "ComponentSpec":
        """从JSON对象创建组件定义。"""
        files = tuple(ComponentFile.from_mapping(item) for item in payload.get("files", []))
        if not files:
            raise ValueError(f"组件{payload.get('key', '<未知>')}没有文件。")
        return cls(
            key=str(payload["key"]),
            version=str(payload["version"]),
            target_dir=target_dir,
            files=files,
            marker_name=str(payload.get("marker_name", COMPONENT_MARKER)),
        )

    @property
    def has_archives(self) -> bool:
        """返回组件是否需要从压缩文件安装。"""
        return any(item.archive for item in self.files)


@dataclass(frozen=True)
class DownloadProgress:
    """下载状态快照，回调方不得修改。"""

    component_key: str
    state: str
    completed: int
    total: int | None
    file_name: str
    speed: float | None
    resumable: bool
    detail: str = ""

    @property
    def fraction(self) -> float | None:
        """返回0到1之间的总进度；总大小未知时返回None。"""
        if self.total is None or self.total <= 0:
            return None
        return min(1.0, max(0.0, self.completed / self.total))


ProgressCallback = Callable[[DownloadProgress], None]
FinishedCallback = Callable[[str, str, str], None]
ComponentFactory = Callable[[], ComponentSpec]


class _DownloadJob:
    """一个下载任务的线程同步状态。"""

    def __init__(
        self,
        key: str,
        factory: ComponentFactory,
        on_progress: ProgressCallback | None,
        on_finished: FinishedCallback | None,
    ) -> None:
        self.key = key
        self.factory = factory
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.condition = threading.Condition()
        self.pause_requested = False
        self.cancel_requested = False
        self.state = "排队"
        self.progress: DownloadProgress | None = None
        self.done = threading.Event()
        self.error: Exception | None = None
        self.spec: ComponentSpec | None = None
        self.file_sizes: dict[str, int] = {}
        self.last_speed_time: float | None = None
        self.last_speed_bytes = 0
        self.last_speed: float | None = None

    def request_pause(self) -> None:
        """请求在当前数据块结束后暂停。"""
        with self.condition:
            self.pause_requested = True
            self.condition.notify_all()

    def request_resume(self) -> None:
        """继续当前任务。"""
        with self.condition:
            self.pause_requested = False
            self.condition.notify_all()

    def request_cancel(self) -> None:
        """请求取消当前任务。"""
        with self.condition:
            self.cancel_requested = True
            self.condition.notify_all()


class DownloadManager:
    """管理组件下载任务，并把用户数据留在可恢复的临时文件中。"""

    def __init__(
        self,
        cache_dir: Path,
        chunk_size: int = 256 * 1024,
        request_timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.chunk_size = max(16 * 1024, int(chunk_size))
        self.request_timeout = max(1.0, float(request_timeout))
        self.max_retries = max(1, int(max_retries))
        self._lock = threading.Lock()
        self._jobs: dict[str, _DownloadJob] = {}
        self._statuses: dict[str, DownloadProgress] = {}

    def start(
        self,
        spec: ComponentSpec,
        on_progress: ProgressCallback | None = None,
        on_finished: FinishedCallback | None = None,
    ) -> None:
        """启动一个已经构造好的组件下载任务。"""
        self.start_factory(
            spec.key,
            lambda: spec,
            on_progress=on_progress,
            on_finished=on_finished,
        )

    def start_factory(
        self,
        key: str,
        factory: ComponentFactory,
        on_progress: ProgressCallback | None = None,
        on_finished: FinishedCallback | None = None,
    ) -> None:
        """异步构造组件并启动下载，适合需要先查询远端清单的模型。"""
        with self._lock:
            current = self._jobs.get(key)
            if current is not None and not current.done.is_set():
                raise DownloadError(f"组件{key}已经在下载。")
            job = _DownloadJob(key, factory, on_progress, on_finished)
            self._jobs[key] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job,),
            name=f"组件下载-{key}",
            daemon=True,
        )
        thread.start()

    def ensure(
        self,
        spec: ComponentSpec,
        on_progress: ProgressCallback | None = None,
    ) -> Path:
        """同步确保组件完成，调用方应在后台线程中使用。"""
        return self.ensure_factory(
            spec.key,
            lambda: spec,
            on_progress=on_progress,
        )

    def ensure_factory(
        self,
        key: str,
        factory: ComponentFactory,
        on_progress: ProgressCallback | None = None,
    ) -> Path:
        """同步构造并确保组件完成，构造过程也在下载线程中执行。"""
        with self._lock:
            current = self._jobs.get(key)
        if current is None or current.done.is_set():
            self.start_factory(key, factory, on_progress=on_progress)
            with self._lock:
                current = self._jobs.get(key)
        if current is None:
            raise DownloadError(f"无法创建组件{key}的下载任务。")
        current.done.wait()
        if current.error is not None:
            raise DownloadError(str(current.error)) from current.error
        spec = current.spec
        if spec is None or not self.component_is_ready(spec):
            raise DownloadError(f"组件{key}下载后未通过完整性检查。")
        return spec.target_dir

    def pause(self, key: str) -> bool:
        """请求暂停任务，当前数据块完成后保留断点。"""
        with self._lock:
            job = self._jobs.get(key)
        if job is None or job.done.is_set():
            return False
        job.request_pause()
        self._emit(job, state="暂停中", detail="等待当前数据块结束。")
        return True

    def resume(self, key: str) -> bool:
        """继续暂停中的任务。"""
        with self._lock:
            job = self._jobs.get(key)
        if job is None or job.done.is_set():
            return False
        job.request_resume()
        self._emit(job, state="继续下载", detail="正在从已保存断点继续。")
        return True

    def cancel(self, key: str) -> bool:
        """取消任务并保留已经下载的.part文件。"""
        with self._lock:
            job = self._jobs.get(key)
        if job is None or job.done.is_set():
            return False
        job.request_cancel()
        self._emit(job, state="正在取消", detail="正在保存已下载断点。")
        return True

    def is_active(self, key: str) -> bool:
        """返回组件是否有活动任务。"""
        with self._lock:
            job = self._jobs.get(key)
        return job is not None and not job.done.is_set()

    def status(self, key: str) -> DownloadProgress | None:
        """返回最近一次状态快照。"""
        with self._lock:
            return self._statuses.get(key)

    def component_is_ready(self, spec: ComponentSpec) -> bool:
        """校验组件标记和组件文件是否完整。"""
        marker = spec.target_dir / spec.marker_name
        if not marker.is_file():
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if payload.get("key") != spec.key or payload.get("version") != spec.version:
            return False
        if spec.has_archives:
            installed_files = payload.get("installed_files", [])
            if not isinstance(installed_files, list) or not installed_files:
                return False
            for relative in installed_files:
                try:
                    path = self._safe_join(spec.target_dir, str(relative))
                except DownloadError:
                    return False
                if not path.is_file():
                    return False
            return True
        for item in spec.files:
            path = self._safe_join(spec.target_dir, item.path)
            if not path.is_file() or not self._file_matches(path, item):
                return False
        return True

    def _run_job(self, job: _DownloadJob) -> None:
        state = "失败"
        detail = ""
        try:
            spec = job.factory()
            job.spec = spec
            self._validate_spec(spec)
            if self.component_is_ready(spec):
                state = "完成"
                self._emit(job, state=state, detail="组件已存在且校验通过。")
                return
            job.file_sizes = {
                item.path: item.size
                for item in spec.files
                if item.size is not None and item.size >= 0
            }
            self._emit(job, state="准备下载", detail="正在准备组件清单。")
            for item in spec.files:
                self._download_file(job, spec, item)
            self._install_component(job, spec)
            if not self.component_is_ready(spec):
                raise DownloadError("组件安装后校验失败。")
            state = "完成"
            self._emit(job, state=state, detail="组件下载并校验完成。")
        except _PauseRequested:
            detail = "暂停状态未能正常恢复。"
            job.error = DownloadError(detail)
            self._emit(job, state="失败", detail=detail)
        except DownloadCancelled as error:
            state = "已取消"
            detail = str(error)
            self._emit(job, state=state, detail=detail)
        except Exception as error:
            job.error = error
            detail = str(error)
            LOGGER.exception("组件下载失败：%s", job.key)
            self._emit(job, state="失败", detail=detail)
        finally:
            job.done.set()
            if job.on_finished:
                try:
                    job.on_finished(job.key, state, detail)
                except Exception:
                    LOGGER.exception("组件完成回调失败：%s", job.key)
            with self._lock:
                self._jobs[job.key] = job

    def _download_file(self, job: _DownloadJob, spec: ComponentSpec, item: ComponentFile) -> None:
        if item.archive:
            stage_root = self._archive_stage_root(spec)
        else:
            stage_root = spec.target_dir
        final_path = self._safe_join(stage_root, item.path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = final_path.with_name(final_path.name + ".part")
        if final_path.is_file() and self._file_matches(final_path, item):
            return
        if final_path.exists() and not final_path.is_file():
            raise DownloadError(f"组件文件目标不是普通文件：{final_path}")

        download_urls = item.download_urls
        max_attempts = self.max_retries * len(download_urls)
        for attempt in range(max_attempts):
            self._wait_for_resume(job)
            if part_path.is_file() and item.size is not None and part_path.stat().st_size > item.size:
                part_path.unlink()
            existing = part_path.stat().st_size if part_path.is_file() else 0
            headers = dict(item.headers)
            if existing:
                headers["Range"] = f"bytes={existing}-"
            url = download_urls[attempt % len(download_urls)]
            request = Request(url, headers=headers, method="GET")
            response = None
            try:
                response = urlopen(request, timeout=self.request_timeout)
                status_code = int(getattr(response, "status", response.getcode()) or 0)
                resumed = bool(existing and status_code == 206)
                if existing and not resumed:
                    existing = 0
                    part_path.unlink(missing_ok=True)
                content_total = self._content_total(response, existing if resumed else 0)
                if content_total is not None:
                    job.file_sizes[item.path] = content_total
                mode = "ab" if resumed else "wb"
                self._emit(job, state="下载中", file_name=item.path, resumable=resumed)
                with part_path.open(mode, buffering=0) as output:
                    while True:
                        if job.cancel_requested:
                            raise DownloadCancelled("用户取消下载；已保留当前断点。")
                        if job.pause_requested:
                            raise _PauseRequested()
                        chunk = response.read(self.chunk_size)
                        if not chunk:
                            break
                        output.write(chunk)
                        self._emit(job, state="下载中", file_name=item.path, resumable=resumed)
                if item.size is not None and part_path.stat().st_size != item.size:
                    raise DownloadError(
                        f"文件大小不符：{item.path}，实际{part_path.stat().st_size}，预期{item.size}。"
                    )
                if item.sha256 and self._sha256(part_path) != item.sha256.lower():
                    part_path.unlink(missing_ok=True)
                    raise DownloadError(f"文件SHA256校验失败：{item.path}")
                os.replace(part_path, final_path)
                self._emit(job, state="已完成文件", file_name=item.path, resumable=resumed)
                return
            except _PauseRequested:
                partial_size = part_path.stat().st_size if part_path.is_file() else 0
                self._emit(job, state="已暂停", file_name=item.path, resumable=partial_size > 0)
                continue
            except DownloadCancelled:
                raise
            except HTTPError as error:
                error.close()
                if error.code == 416 and existing and item.size == existing:
                    if item.sha256 and self._sha256(part_path) != item.sha256.lower():
                        part_path.unlink(missing_ok=True)
                    else:
                        os.replace(part_path, final_path)
                        return
                if attempt + 1 >= max_attempts:
                    raise DownloadError(f"下载HTTP错误{error.code}：{url}") from error
                source_index = attempt % len(download_urls)
                detail = f"{error}；将尝试备用地址" if source_index + 1 < len(download_urls) else str(error)
                self._retry_wait(job, attempt % self.max_retries, detail)
            except (URLError, OSError, TimeoutError) as error:
                if attempt + 1 >= max_attempts:
                    raise DownloadError(f"下载失败：{url}；{error}") from error
                source_index = attempt % len(download_urls)
                detail = f"{error}；将尝试备用地址" if source_index + 1 < len(download_urls) else str(error)
                self._retry_wait(job, attempt % self.max_retries, detail)
            finally:
                if response is not None:
                    response.close()

    def _install_component(self, job: _DownloadJob, spec: ComponentSpec) -> None:
        self._wait_for_resume(job)
        if spec.has_archives:
            installed_files = self._install_archives(job, spec)
        else:
            spec.target_dir.mkdir(parents=True, exist_ok=True)
            installed_files = [self._safe_relative(item.path).as_posix() for item in spec.files]
        marker = spec.target_dir / spec.marker_name
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "key": spec.key,
                    "version": spec.version,
                    "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "files": [item.path for item in spec.files],
                    "installed_files": installed_files,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _install_archives(self, job: _DownloadJob, spec: ComponentSpec) -> list[str]:
        stage_root = self._archive_stage_root(spec)
        parent = spec.target_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary = parent / f".{spec.target_dir.name}.installing-{uuid.uuid4().hex}"
        backup = parent / f".{spec.target_dir.name}.backup-{uuid.uuid4().hex}"
        installed_files: list[str] = []
        try:
            temporary.mkdir(parents=True, exist_ok=False)
            for item in spec.files:
                if not item.archive:
                    continue
                self._wait_for_resume(job)
                archive_path = self._safe_join(stage_root, item.path)
                installed_files.extend(self._safe_extract_zip(archive_path, temporary))
            old_moved = False
            if spec.target_dir.exists():
                os.replace(spec.target_dir, backup)
                old_moved = True
            os.replace(temporary, spec.target_dir)
            if old_moved:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if backup.exists() and not spec.target_dir.exists():
                os.replace(backup, spec.target_dir)
            raise
        return list(dict.fromkeys(installed_files))

    def _safe_extract_zip(self, archive_path: Path, target_dir: Path) -> list[str]:
        installed_files: list[str] = []
        try:
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                for member in members:
                    target = self._safe_join(target_dir, member.filename)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output, length=self.chunk_size)
                        installed_files.append(self._safe_relative(member.filename).as_posix())
        except zipfile.BadZipFile as error:
            raise DownloadError(f"组件压缩包损坏：{archive_path.name}") from error
        return installed_files

    def _wait_for_resume(self, job: _DownloadJob) -> None:
        while True:
            with job.condition:
                if job.cancel_requested:
                    raise DownloadCancelled("用户取消下载；已保留当前断点。")
                if not job.pause_requested:
                    return
                should_emit = job.state != "已暂停"
                job.state = "已暂停"
                job.condition.wait(timeout=0.25)
            if should_emit:
                self._emit(job, state="已暂停", detail="已保留断点，等待继续。", speed=0.0)

    def _retry_wait(self, job: _DownloadJob, attempt: int, detail: str) -> None:
        delay = min(8.0, 2.0**attempt)
        self._emit(job, state="重试等待", detail=f"{detail}；{delay:.1f}秒后重试。")
        end = time.monotonic() + delay
        while time.monotonic() < end:
            self._wait_for_resume(job)
            if job.cancel_requested:
                raise DownloadCancelled("用户取消下载；已保留当前断点。")
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))

    def _emit(
        self,
        job: _DownloadJob,
        state: str | None = None,
        file_name: str = "",
        resumable: bool = True,
        speed: float | None = None,
        detail: str = "",
    ) -> None:
        if job.spec is None:
            total = None
            completed = 0
        else:
            total = sum(job.file_sizes.values()) or None
            completed = self._component_completed(job.spec, job.file_sizes)
        now = time.monotonic()
        if speed is None:
            if job.last_speed_time is None:
                job.last_speed_time = now
                job.last_speed_bytes = completed
            else:
                elapsed = now - job.last_speed_time
                delta = completed - job.last_speed_bytes
                if elapsed >= 0.2 and delta >= 0:
                    job.last_speed = delta / elapsed
                    job.last_speed_time = now
                    job.last_speed_bytes = completed
            speed = job.last_speed
        if state is None:
            state = job.state
        job.state = state
        progress = DownloadProgress(
            component_key=job.key,
            state=state,
            completed=completed,
            total=total,
            file_name=file_name,
            speed=speed,
            resumable=resumable,
            detail=detail,
        )
        job.progress = progress
        with self._lock:
            self._statuses[job.key] = progress
        if job.on_progress:
            try:
                job.on_progress(progress)
            except Exception:
                LOGGER.exception("组件进度回调失败：%s", job.key)

    def _component_completed(self, spec: ComponentSpec, file_sizes: dict[str, int]) -> int:
        root = self._archive_stage_root(spec) if spec.has_archives else spec.target_dir
        total = 0
        for item in spec.files:
            path = self._safe_join(root, item.path)
            if path.is_file():
                size = path.stat().st_size
            else:
                part = path.with_name(path.name + ".part")
                size = part.stat().st_size if part.is_file() else 0
            expected = file_sizes.get(item.path)
            total += min(size, expected) if expected is not None else size
        return total

    def _archive_stage_root(self, spec: ComponentSpec) -> Path:
        return self.cache_dir / spec.key / spec.version / "files"

    def _validate_spec(self, spec: ComponentSpec) -> None:
        if not spec.key or not spec.version:
            raise DownloadError("组件key和版本不能为空。")
        if not spec.files:
            raise DownloadError(f"组件{spec.key}没有文件。")
        for item in spec.files:
            self._safe_relative(item.path)
            for url in item.download_urls:
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https", "file"}:
                    raise DownloadError(f"不支持的下载协议：{url}")
            if item.size is not None and item.size < 0:
                raise DownloadError(f"文件大小不能为负数：{item.path}")
            if item.sha256 and len(item.sha256) != 64:
                raise DownloadError(f"SHA256格式错误：{item.path}")

    def _safe_relative(self, value: str) -> PurePosixPath:
        normalized = str(value).replace("\\", "/")
        path = PurePosixPath(normalized)
        if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise DownloadError(f"组件文件路径不安全：{value}")
        if len(path.parts) == 1 and ":" in path.parts[0]:
            raise DownloadError(f"组件文件路径不安全：{value}")
        return path

    def _safe_join(self, root: Path, relative: str) -> Path:
        path = self._safe_relative(relative)
        result = root.joinpath(*path.parts)
        root_resolved = root.resolve()
        result_resolved = result.resolve()
        if result_resolved != root_resolved and root_resolved not in result_resolved.parents:
            raise DownloadError(f"组件路径越界：{relative}")
        return result

    def _file_matches(self, path: Path, item: ComponentFile) -> bool:
        try:
            if item.size is not None and path.stat().st_size != item.size:
                return False
            if item.sha256 and self._sha256(path) != item.sha256.lower():
                return False
            return True
        except OSError:
            return False

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(self.chunk_size)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)

    @staticmethod
    def _content_total(response, start: int) -> int | None:
        content_range = str(response.headers.get("Content-Range", ""))
        if "/" in content_range:
            total_text = content_range.rsplit("/", 1)[-1].strip()
            if total_text.isdigit():
                return int(total_text)
        content_length = response.headers.get("Content-Length")
        if content_length and str(content_length).isdigit():
            return start + int(content_length)
        return None


def load_component_manifest(path: Path, install_root: Path) -> dict[str, ComponentSpec]:
    """加载JSON组件清单并把相对安装目录解析到用户目录。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    components = payload.get("components", [])
    if isinstance(components, dict):
        entries = []
        for key, value in components.items():
            item = dict(value)
            item.setdefault("key", key)
            entries.append(item)
    else:
        entries = list(components)
    result: dict[str, ComponentSpec] = {}
    for item in entries:
        relative = str(item.get("install_dir", item["key"]))
        target = Path(install_root) / Path(relative.replace("\\", "/"))
        result[item["key"]] = ComponentSpec.from_mapping(item, target)
    return result
