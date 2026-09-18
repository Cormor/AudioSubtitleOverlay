"""兼容旧导入路径；显示历史实现位于独立显示模块。"""

from display_module import (
    DisplayEvent,
    DisplayModule,
    DisplaySnapshot,
    SubtitleBlock,
    SubtitleHistory,
)

__all__ = [
    "DisplayEvent",
    "DisplayModule",
    "DisplaySnapshot",
    "SubtitleBlock",
    "SubtitleHistory",
]
