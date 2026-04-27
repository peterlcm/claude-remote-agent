"""
日志配置
"""
import logging
import logging.handlers
from pathlib import Path

try:
    import colorlog
    HAS_COLORLOG = True
except ImportError:
    HAS_COLORLOG = False

from config import config


def setup_logging():
    """配置日志"""
    # 确保日志目录存在
    log_path = Path(config.log.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 设置日志级别
    level = getattr(logging, config.log.level.upper(), logging.INFO)

    # 根日志配置
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除默认处理器
    root_logger.handlers.clear()

    # 控制台格式
    if HAS_COLORLOG:
        console_formatter = colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s%(reset)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "red,bg_white",
            }
        )
    else:
        console_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

    # 文件格式
    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 文件处理器（轮转）
    file_handler = logging.handlers.RotatingFileHandler(
        config.log.file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # 降低第三方库日志级别
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
