"""Logging utilities."""

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logger():
    """Configure process logging once."""
    log_level = os.getenv("LOG_LEVEL", "INFO")
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format=log_format,
    )

    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.setLevel(logging.WARNING)

    kline_logger = logging.getLogger("app.routes.kline")
    kline_logger.setLevel(logging.WARNING)

    logging.getLogger("app.services.usdt_payment_service").setLevel(logging.INFO)
    logging.getLogger("app.routes.billing").setLevel(logging.INFO)

    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    app_log_path = os.path.abspath(os.path.join(log_dir, "app.log"))
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "") == app_log_path:
            return

    file_handler = RotatingFileHandler(
        app_log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)
