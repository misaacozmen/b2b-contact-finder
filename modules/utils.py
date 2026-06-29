import logging
import random
import time
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar

import config


T = TypeVar("T")


def ensure_directories() -> None:
    for path in (config.INPUT_DIR, config.OUTPUT_DIR, config.STATE_DIR):
        Path(path).mkdir(parents=True, exist_ok=True)


def setup_logging() -> logging.Logger:
    ensure_directories()
    logger = logging.getLogger("contact_finder")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def random_delay(min_sec: float | None = None, max_sec: float | None = None) -> None:
    time.sleep(random.uniform(min_sec or config.MIN_DELAY_SEC, max_sec or config.MAX_DELAY_SEC))


def retry_with_backoff(max_retries: int | None = None, backoff_base: float | None = None):
    retries = config.MAX_RETRIES if max_retries is None else max_retries
    base = config.RETRY_BACKOFF_BASE_SEC if backoff_base is None else backoff_base

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_error: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_error = exc
                    if attempt >= retries:
                        break
                    sleep_for = (base ** attempt) + random.uniform(0.25, 1.0)
                    time.sleep(sleep_for)
            raise last_error  # type: ignore[misc]

        return wrapper

    return decorator

