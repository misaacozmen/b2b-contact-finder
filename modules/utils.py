import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar

import config


T = TypeVar("T")


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "headers", {}).get("Retry-After", "") if response is not None else ""
    if not value:
        return None
    try:
        seconds = max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return min(seconds, max(float(config.MAX_RETRY_AFTER_SEC), 0.0))


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


def retry_with_backoff(
    max_retries: int | None = None,
    backoff_base: float | None = None,
    retry_if: Callable[[Exception], bool] | None = None,
):
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
                    if attempt >= retries or (retry_if is not None and not retry_if(exc)):
                        break
                    retry_after = _retry_after_seconds(exc)
                    sleep_for = (
                        retry_after
                        if retry_after is not None
                        else (base ** attempt) + random.uniform(0.25, 1.0)
                    )
                    time.sleep(sleep_for)
            raise last_error  # type: ignore[misc]

        return wrapper

    return decorator
