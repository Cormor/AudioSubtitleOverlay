"""Small local diagnostic export. Explicit allowlists keep transcripts and paths out."""
from __future__ import annotations
from dataclasses import asdict, is_dataclass
import json
import math
from pathlib import Path
import platform
import sys
from datetime import datetime, timezone

SETTINGS_FIELDS = frozenset({"schema_version", "ui_theme", "recognition_model", "recognition_device",
    "source_language", "target_language", "translation_enabled", "translation_backend", "overlay_animation"})
METRIC_FIELDS = frozenset({"phase", "input_rms", "inference_seconds", "backlog_seconds", "actual_device", "actual_compute_type"})


def safe_scalar(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def export_diagnostics(path, *, version, settings, metrics, state, build):
    values = asdict(settings) if is_dataclass(settings) else dict(settings)
    runtime = asdict(state) if is_dataclass(state) else dict(state)
    # Error messages can contain source text, usernames, paths or remote responses.
    runtime = {key: safe_scalar(value) for key, value in runtime.items() if key in ("session_id", "phase", "translation_phase")}
    payload = {
        "format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "application": "声幕", "version": version,
        "platform": {"system": platform.system(), "release": platform.release(), "python": sys.version.split()[0]},
        "build": {key: safe_scalar(build.get(key)) for key in ("version", "source_commit", "source_state")},
        "settings": {key: safe_scalar(value) for key, value in values.items() if key in SETTINGS_FIELDS},
        "runtime": runtime,
        "metrics": {key: safe_scalar(value) for key, value in metrics.items() if key in METRIC_FIELDS},
        "privacy": "不包含音源名称、模型路径、字幕正文、录音、完整日志或环境变量；本地报告不会自动上传。",
    }
    destination = Path(path)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return destination
