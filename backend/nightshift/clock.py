"""Elapsed time excluding system suspend on supported platforms."""
from __future__ import annotations

import ctypes
import sys
import time


def active_time() -> float:
    """Return a monotonic clock that stops while Windows is suspended."""
    if sys.platform == "win32":
        ticks = ctypes.c_ulonglong()
        if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(ticks)):
            return ticks.value / 10_000_000
        raise OSError("QueryUnbiasedInterruptTime failed")
    return time.monotonic()


class SleepClock:
    """Measure active elapsed time and detect wall-clock discontinuities."""
    def __init__(self, active=active_time, wall=time.time):
        self._active = active
        self._wall = wall
        self._last_active = active()
        self._last_wall = wall()
        self._start = self._last_active

    def elapsed(self) -> float:
        return max(0.0, self._active() - self._start)

    def jumped(self, tolerance: float = 5.0) -> bool:
        active = self._active()
        wall = self._wall()
        jumped = abs((wall - self._last_wall) - (active - self._last_active)) > tolerance
        self._last_active, self._last_wall = active, wall
        return jumped
