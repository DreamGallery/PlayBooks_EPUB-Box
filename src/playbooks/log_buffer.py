"""Batch GUI log rendering without delaying subprocess output reads."""
import asyncio

LOG_REFRESH_INTERVAL = 0.1


class LogBuffer:
    def __init__(self, render):
        self.render = render
        self.pending = []
        self.timer = None

    def append(self, line):
        self.pending.append(line)
        if self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(LOG_REFRESH_INTERVAL, self.flush)

    def clear(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.pending.clear()

    def flush(self):
        lines = self.pending[:]
        self.clear()
        if lines:
            self.render(lines)
