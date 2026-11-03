"""
统一日志模块。
"""

import logging
import sys

_CONFIGURED = False


def get_logger(name: str = "vl_eval") -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        if not root.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.INFO)
            handler.flush = sys.stdout.flush
            fmt = logging.Formatter(
                "[%(asctime)s] %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
            handler.setFormatter(fmt)
            root.addHandler(handler)
        _CONFIGURED = True

    return logging.getLogger(name)
