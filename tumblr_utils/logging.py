from typing import Optional

import sys
import threading

from enum import Enum
from functools import total_ordering


@total_ordering
class LogLevel(Enum):
    INFO = 0
    WARN = 1
    ERROR = 2

    def __lt__(self, other):
        if type(self) is type(other):
            return self.value < other.value
        return NotImplemented


class Logger:
    def __init__(self):
        self.lock = threading.Lock()
        self.backup_account: Optional[str] = None
        self.status_msg: Optional[str] = None

    def log(self, level: LogLevel, msg: str, account: bool = False) -> None:
        if options.quiet and level < LogLevel.WARN:
            return
        with self.lock:
            for line in msg.splitlines(True):
                self._print(line, account)
            if self.status_msg:
                self._print(self.status_msg, account=True)
            sys.stdout.flush()

    def info(self, msg, account=False):
        self.log(LogLevel.INFO, msg, account)

    def warn(self, msg, account=False):
        self.log(LogLevel.WARN, msg, account)

    def error(self, msg, account=False):
        self.log(LogLevel.ERROR, msg, account)

    def status(self, msg):
        self.status_msg = msg
        self.log(LogLevel.INFO, '')

    def _print(self, msg, account=False):
        if account:  # Optional account prefix
            msg = '{}: {}'.format(self.backup_account, msg)

        # Separate terminator
        it = (i for i, c in enumerate(reversed(msg)) if c not in '\r\n')
        try:
            idx = len(msg) - next(it)
        except StopIteration:
            idx = 0
        msg, term = msg[:idx], msg[idx:]

        pad = ' ' * (80 - len(msg))  # Pad to 80 chars
        print(msg + pad + term, end='', file=sys.stderr if options.json_info else sys.stdout)
