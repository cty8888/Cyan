"""日志配置。

终端观感由 rich Console 负责；logging 只负责落盘，默认不往 stderr 打，
避免和界面叠两套输出。``--verbose`` 时才额外挂 StreamHandler。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "cyan"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 5


def get_logger(name: str | None = None) -> logging.Logger:
    """取 ``cyan`` 或其子 logger；调用方不必自己拼名字。"""
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)


def setup_logging(log_dir: Path, level: str = "INFO", to_stderr: bool = False) -> Path:
    """配置根 logger，返回日志文件路径。重复调用会先清掉旧 handler。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "agent.log"

    numeric = _parse_level(level)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if to_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(numeric)
        stream_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(stream_handler)

    logger.debug("日志写入 %s，to_stderr=%s 级别 %s", log_path, to_stderr, level.upper())
    return log_path


def _parse_level(level: str) -> int:
    numeric = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric, int):
        return logging.INFO
    return numeric
