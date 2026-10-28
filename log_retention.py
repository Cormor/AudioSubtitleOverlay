"""声幕运行日志的有界保留与轮转。"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path


最大天数 = 30
最大字节数 = 30 * 1024 * 1024
大小触发后保留字节数 = 20 * 1024 * 1024
单文件字节数 = 10 * 1024 * 1024
时间戳格式 = re.compile(rb"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})\b")


def _读取日志记录(路径: Path) -> list[tuple[Path, datetime | None, bytes]]:
    记录: list[tuple[Path, datetime | None, bytes]] = []
    当前时间: datetime | None = None
    当前行: list[bytes] = []

    def 收尾() -> None:
        nonlocal 当前时间, 当前行
        if 当前行:
            记录.append((路径, 当前时间, b"".join(当前行)))
        当前时间 = None
        当前行 = []

    if not 路径.exists():
        return 记录
    with 路径.open("rb") as 文件:
        for 行 in 文件:
            匹配 = 时间戳格式.match(行)
            if 匹配:
                收尾()
                try:
                    当前时间 = datetime.strptime(
                        匹配.group(1).decode("ascii") + "," + 匹配.group(2).decode("ascii"),
                        "%Y-%m-%d %H:%M:%S,%f",
                    )
                except ValueError:
                    当前时间 = None
                当前行 = [行]
            else:
                当前行.append(行)
        收尾()
    return 记录


def 清理轮转日志(基础路径: str | Path, 现在: datetime | None = None) -> bool:
    """删除30天前的完整日志记录；超30MiB时压到最近约20MiB。"""
    基础路径 = Path(基础路径)
    现在 = 现在 or datetime.now()
    截止时间 = 现在 - timedelta(days=最大天数)
    旧文件 = [基础路径.with_name(基础路径.name + f".{序号}") for 序号 in (2, 1)]
    全部记录: list[tuple[Path, datetime | None, bytes]] = []
    for 路径 in (*旧文件, 基础路径):
        全部记录.extend(_读取日志记录(路径))

    保留记录 = [
        项目 for 项目 in 全部记录
        if 项目[1] is None or 项目[1] >= 截止时间
    ]
    总字节数 = sum(len(内容) for _, _, 内容 in 保留记录)
    需要按时间清理 = len(保留记录) != len(全部记录)
    需要按大小清理 = 总字节数 > 最大字节数
    if not 需要按时间清理 and not 需要按大小清理:
        return False

    if 需要按大小清理:
        起始位置 = len(保留记录)
        保留字节数 = 0
        while 起始位置 > 0 and 保留字节数 < 大小触发后保留字节数:
            起始位置 -= 1
            保留字节数 += len(保留记录[起始位置][2])
        保留记录 = 保留记录[起始位置:]

    基础路径.parent.mkdir(parents=True, exist_ok=True)
    内容 = b"".join(记录[2] for 记录 in 保留记录)
    文件描述符, 临时路径 = tempfile.mkstemp(
        prefix=基础路径.name + ".retention-", suffix=".tmp", dir=基础路径.parent
    )
    try:
        with os.fdopen(文件描述符, "wb") as 文件:
            文件.write(内容)
            文件.flush()
            os.fsync(文件.fileno())
        os.replace(临时路径, 基础路径)
        for 路径 in 旧文件:
            try:
                路径.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # 主日志已原子替换；无法删除的旧备份留待下次清理。
                pass
    finally:
        if os.path.exists(临时路径):
            os.unlink(临时路径)
    return True


class 保留日志处理器(RotatingFileHandler):
    """按30天与30MiB上限清理，大小轮转后最多保留约30MiB。"""

    def __init__(self, filename: str | Path) -> None:
        super().__init__(filename, maxBytes=单文件字节数, backupCount=2, encoding="utf-8", delay=True)
        self._下次检查 = 0.0
        self._已关闭 = False
        self._每日定时器: threading.Timer | None = None
        with self._日志进程锁():
            self._检查保留期限()
        self._安排每日检查()

    def _日志进程锁(self):
        """串行化多个应用实例对同一日志组的轮转、写入与清理。"""
        from contextlib import contextmanager

        @contextmanager
        def 锁定():
            锁路径 = self.baseFilename + ".lock"
            文件描述符 = os.open(锁路径, os.O_CREAT | os.O_RDWR, 0o600)
            已锁定 = False
            try:
                if os.name == "nt":
                    import errno
                    import msvcrt

                    if os.fstat(文件描述符).st_size == 0:
                        os.write(文件描述符, b"\0")
                    os.lseek(文件描述符, 0, os.SEEK_SET)
                    while not 已锁定:
                        try:
                            msvcrt.locking(文件描述符, msvcrt.LK_NBLCK, 1)
                            已锁定 = True
                        except OSError as 错误:
                            if 错误.errno not in (errno.EACCES, errno.EDEADLK):
                                raise
                            time.sleep(0.05)
                else:
                    import fcntl

                    fcntl.flock(文件描述符, fcntl.LOCK_EX)
                    已锁定 = True
                yield
            finally:
                if 已锁定:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(文件描述符, 0, os.SEEK_SET)
                        msvcrt.locking(文件描述符, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(文件描述符, fcntl.LOCK_UN)
                os.close(文件描述符)

        return 锁定()

    def _安排每日检查(self) -> None:
        if self._已关闭:
            return
        self._每日定时器 = threading.Timer(24 * 60 * 60, self._每日检查)
        self._每日定时器.daemon = True
        self._每日定时器.start()

    def _每日检查(self) -> None:
        self.acquire()
        try:
            if not self._已关闭:
                with self._日志进程锁():
                    self._检查保留期限()
        finally:
            self.release()
        self._安排每日检查()

    def _检查保留期限(self) -> None:
        if self.stream is not None:
            self.flush()
            self.stream.close()
            self.stream = None
        try:
            清理轮转日志(self.baseFilename)
        except Exception as 错误:
            try:
                os.write(2, f"声幕日志保留清理失败：{type(错误).__name__}\n".encode("utf-8"))
            except OSError:
                pass
        self._下次检查 = time.monotonic() + 24 * 60 * 60

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._日志进程锁():
                if time.monotonic() >= self._下次检查:
                    self._检查保留期限()
                super().emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            self._已关闭 = True
            if self._每日定时器 is not None:
                self._每日定时器.cancel()
                self._每日定时器 = None
        finally:
            self.release()
        super().close()
