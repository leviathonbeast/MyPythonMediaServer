"""
FFmpeg-based on-the-fly transcoder.

We run ffmpeg as a subprocess that reads the source file (-i path) and writes
the transcoded stream to stdout. The HTTP layer then iterates over stdout in
chunks and yields them to the client.

Critical things to get right
----------------------------
1. Don't load the file into memory. We pass a path to ffmpeg, not bytes.
2. Don't leak processes. If the client disconnects mid-stream we MUST kill
   ffmpeg, otherwise a few dropped streams leak processes until the host runs
   out of file descriptors.
3. Don't block on stderr. ffmpeg writes status to stderr; if we don't drain
   it, it eventually fills its pipe and ffmpeg blocks. We redirect stderr to
   DEVNULL because we don't need it (we'd lose error detail, but the cost of
   a dedicated drain thread isn't worth it for a personal music server).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterator, Optional

from backend.config import get_settings
from .presets import TranscodePreset

log = logging.getLogger(__name__)


class TranscodeStream:
    """
    Manages one ffmpeg subprocess + iteration over its stdout.

    Use as a context manager:
        with TranscodeStream(path, preset) as ts:
            for chunk in ts.iter_chunks():
                yield chunk
    """

    def __init__(
        self, source_path: str, preset: TranscodePreset, chunk_size: int = 64 * 1024
    ):
        self.source_path = source_path
        self.preset = preset
        self.chunk_size = chunk_size
        self._proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "TranscodeStream":
        settings = get_settings()
        # Resolve to an absolute path: a filename starting with "-" could
        # otherwise be misread as a flag by ffmpeg's arg parser. Library paths
        # are configured absolute already; this just hardens the boundary.
        safe_path = str(Path(self.source_path).resolve())
        cmd = [
            settings.ffmpeg_binary,
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            safe_path,
            *self.preset.ffmpeg_args,
            "pipe:1",
        ]
        log.debug("ffmpeg: %s", " ".join(cmd))
        # bufsize=0 means unbuffered — chunks reach us as fast as ffmpeg writes.
        # close_fds=True (default on POSIX) — don't inherit our other FDs.
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
        return self

    def iter_chunks(self) -> Iterator[bytes]:
        """Yield transcoded bytes until ffmpeg's stdout closes."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = self._proc.stdout.read(self.chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            self._terminate()

    def __exit__(self, exc_type, exc, tb) -> None:
        self._terminate()

    def _terminate(self) -> None:
        """
        Reap the subprocess. Idempotent — safe to call multiple times.

        Ordering matters:
            * close stdout first to unblock any pending writes
            * .terminate() is SIGTERM (graceful), then .kill() (SIGKILL) if
              ffmpeg ignores us. We give it 1 second to clean up.
        """
        if self._proc is None:
            return
        try:
            if self._proc.stdout:
                try:
                    self._proc.stdout.close()
                except Exception:
                    pass
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
        except Exception:
            log.exception("error tearing down ffmpeg subprocess")
        finally:
            self._proc = None
