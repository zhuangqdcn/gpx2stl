from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

ProgressCallback = Callable[[str], None]


def console_progress(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)

