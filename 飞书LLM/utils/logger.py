import sys
from pathlib import Path

from loguru import logger

from config import settings

_CONFIGURED = False


def _configure_logger() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    try:
        logger.remove()
    except Exception:
        pass

    level = (settings.ws_log_level or "INFO").upper()
    logger.add(sys.stderr, level=level, backtrace=False, diagnose=False)

    try:
        base_dir = Path(__file__).resolve().parents[1]
        log_dir = base_dir / ".logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_dir / "app.log"),
            level=level,
            rotation="5 MB",
            retention="7 days",
            encoding="utf-8",
            backtrace=False,
            diagnose=False,
        )
    except Exception:
        pass


def get_logger():
    _configure_logger()
    return logger
