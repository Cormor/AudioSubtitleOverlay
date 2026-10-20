"""Public app identity; legacy storage and executable identity stay compatible."""

import json
from pathlib import Path
import sys

APP_NAME = "声幕"
APP_DESCRIPTION = "实时字幕与翻译"
APP_ID = "Cormor.AudioSubtitleOverlay"


def asset_path(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / "assets" / "brand" / name


def app_version() -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def build_info() -> dict:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    path = root / "BUILD_INFO.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"source_state": "source-checkout"}
