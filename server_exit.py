"""Knowing when the game server has actually gone.

The restart path used to guess: `save`, 8s, `quit`, 8s, a force-kill, then a new
server 7s later. A world still saving at the 16s mark was killed mid-save, and on
a slow box the new JVM started while the old one was alive. Lives apart from
Jeeves.py so the wait can be tested without a Discord connection.
"""

import asyncio
import subprocess
import sys
import time
from typing import Callable

_IS_WINDOWS = sys.platform == 'win32'


def server_process_running(proc: str) -> bool:
    """Is a process with this name alive? A check that cannot run answers False,
    which falls back to the old behaviour rather than holding a restart forever."""
    try:
        if _IS_WINDOWS:
            output = subprocess.check_output(
                f'tasklist /FI "IMAGENAME eq {proc}" /NH', shell=True, text=True, timeout=10)
            return proc.lower() in output.lower()
        return subprocess.run(['pgrep', '-f', proc], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


async def wait_for_exit(is_running: Callable[[], bool], timeout: float,
                        interval: float = 2.0) -> bool:
    """Poll until is_running() answers False. True if it did within timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if not await asyncio.to_thread(is_running):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)
