"""Keep terminal output visible while saving UTF-8 logs for each run."""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sys
import threading
import traceback


class _Tee:
    def __init__(self, stream, log, lock):
        self.stream = stream
        self.log = log
        self.lock = lock

    def write(self, text):
        with self.lock:
            self.log.write(text)
            self.log.flush()
            try:
                self.stream.write(text)
            except UnicodeEncodeError:
                # Legacy Windows consoles may not support emoji; the log does.
                self.stream.write(text.encode(self.stream.encoding, errors="backslashreplace")
                                  .decode(self.stream.encoding))
            return len(text)

    def flush(self):
        with self.lock:
            self.log.flush()
            self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextmanager
def terminal_log(name, directory=None):
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parent / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}_{datetime.now():%Y%m%d_%H%M%S_%f}.log"
    stdout, stderr = sys.stdout, sys.stderr
    lock = threading.RLock()
    with path.open("x", encoding="utf-8") as log:
        sys.stdout = _Tee(stdout, log, lock)
        sys.stderr = _Tee(stderr, log, lock)
        try:
            print(f"📝 로그 저장: {path}")
            yield path
        except BaseException:
            # The interpreter prints to the restored stderr; save a copy here.
            traceback.print_exc(file=log)
            raise
        finally:
            sys.stdout, sys.stderr = stdout, stderr

