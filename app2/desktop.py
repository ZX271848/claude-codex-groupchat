"""Desktop lifecycle helpers. No network listener is needed to focus an existing window."""
import os
from pathlib import Path
import threading


class SingleInstance:
    """POSIX advisory lock, released by the kernel even after a crash.

    Keep the lock file in place: unlinking it would allow two clients to lock
    different inodes and write the same room concurrently.
    """
    def __init__(self, state):
        self.state = Path(state)
        self.file = None
        self.request = self.state / "show-window"
        self.stopped = threading.Event()

    def acquire(self):
        import fcntl
        self.state.mkdir(parents=True, exist_ok=True)
        self.file = (self.state / "client.lock").open("a+")
        self.seen = self._stamp()
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            self.request.touch()
            return False
        return True

    def _stamp(self):
        try:
            return self.request.stat().st_mtime_ns
        except FileNotFoundError:
            return 0

    def watch(self, show):
        def poll():
            while not self.stopped.wait(0.3):
                stamp = self._stamp()
                if stamp != self.seen:
                    self.seen = stamp
                    show()
        threading.Thread(target=poll, daemon=True).start()

    def close(self):
        self.stopped.set()
        if self.file:
            self.file.close()
            self.file = None


def stop_process(process):
    if process is None:
        return
    import subprocess
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            stream.close()
