"""Desktop notifications (macOS Notification Center); falls back to a log line elsewhere."""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger("notify")


def notify(title: str, message: str) -> None:
    log.warning("%s: %s", title, message)
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       check=False, timeout=5, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
