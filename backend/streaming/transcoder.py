"""
FFmpeg-based on-the-fly transcoder, async edition.

We run ffmpeg as a subprocess that reads the source file (-i path) and writes
the transcoded stream to stdout. The HTTP layer iterates the stdout pipe in
chunks and yields them to the client.

Critical things to get right
----------------------------
1. Don't load the file into memory. We pass a path to ffmpeg, not bytes.
2. Don't leak processes. If the client disconnects mid-stream we MUST reap
   ffmpeg, otherwise dropped streams leak fds until the host falls over. The
   teardown path is cancellation-safe: SIGTERM is sent synchronously and the
   wait/SIGKILL fallback runs as a fire-and-forget task so a cancelled caller
   can't strand the process. See `_terminate` and `_reap`.
3. Don't block on stderr. ffmpeg writes status to stderr; if we don't drain
   it, it eventually fills its pipe and ffmpeg blocks. We drain it concurrently
   into a small ring buffer so non-zero exits show up in the log with context.
4. Support seek (-ss). Passing `start_seconds` before `-i` is input-seek —
   fast, and accurate enough for audio (decoders we transcode from seek to a
   sample boundary, not a keyframe-snapped position).
"""

from __future__ import annotations

import asyncio
import collections
import logging
from pathlib import Path
from typing import AsyncIterator, Deque, Optional

from backend.config import get_settings
from .presets import TranscodePreset

log = logging.getLogger(__name__)

# How much stderr to keep around for diagnostics. Enough for an error line,
# bounded so a misbehaving ffmpeg can't grow our memory unboundedly.
_STDERR_CAP_BYTES = 4 * 1024


class TranscodeStream:
    """
    Manages one ffmpeg subprocess and async iteration over its stdout.

    Usage:
        async with TranscodeStream(path, preset, start_seconds=12.0) as ts:
            async for chunk in ts.iter_chunks():
                yield chunk
    """

    def __init__(
        self,
        source_path: str,
        preset: TranscodePreset,
        chunk_size: int = 64 * 1024,
        start_seconds: float = 0.0,
    ):
        self.source_path = source_path
        self.preset = preset
        self.chunk_size = chunk_size
        self.start_seconds = max(0.0, float(start_seconds))
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_tail: Deque[int] = collections.deque(maxlen=_STDERR_CAP_BYTES)
        self._stderr_task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "TranscodeStream":
        settings = get_settings()
        # Resolve to an absolute path: a filename starting with "-" could be
        # misread as a flag. Library paths are absolute already; this hardens
        # the boundary.
        safe_path = str(Path(self.source_path).resolve())

        cmd = [
            settings.ffmpeg_binary,
            "-loglevel", "error",
            "-nostdin",
        ]
        # Input seek goes BEFORE -i so ffmpeg can fast-seek the demuxer.
        if self.start_seconds > 0:
            cmd += ["-ss", f"{self.start_seconds:.3f}"]
        cmd += ["-i", safe_path, *self.preset.ffmpeg_args, "pipe:1"]

        log.debug("ffmpeg: %s", " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Drain stderr concurrently into the ring buffer.
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        return self

    async def _drain_stderr(self) -> None:
        """Continuously read stderr so the pipe never fills. Keep last N bytes."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                chunk = await self._proc.stderr.read(1024)
                if not chunk:
                    return
                self._stderr_tail.extend(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("stderr drain stopped", exc_info=True)

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        """Yield transcoded bytes until ffmpeg's stdout closes."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("TranscodeStream used before __aenter__")
        try:
            while True:
                chunk = await self._proc.stdout.read(self.chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            self._terminate()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._terminate()

    def _terminate(self) -> None:
        """
        Signal ffmpeg synchronously, then hand off the wait/kill to a
        fire-and-forget reaper.

        Why synchronous: `_terminate` runs from a `finally` block on the
        cancellation path. Any `await` here can itself raise CancelledError,
        which would unwind before SIGTERM is sent and strand the process.
        Sending the signal with `proc.terminate()` is a non-awaiting syscall,
        so it always lands. The follow-up wait/SIGKILL runs in `_reap`, which
        is a top-level coroutine immune to our caller's cancellation.
        """
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        stderr_task = self._stderr_task
        self._stderr_task = None

        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

        # Hand off the rest. ensure_future schedules on the running loop.
        try:
            asyncio.ensure_future(
                _reap(proc, stderr_task, bytes(self._stderr_tail), self.source_path)
            )
        except RuntimeError:
            # No running loop (extremely unlikely from an async context). Best
            # effort: kill outright so we at least don't leave a runaway process.
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def _reap(
    proc: asyncio.subprocess.Process,
    stderr_task: Optional[asyncio.Task],
    stderr_tail: bytes,
    source_path: str,
) -> None:
    """Wait for ffmpeg to exit. SIGKILL if it refuses. Log non-zero exits."""
    try:
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    log.error("ffmpeg refused to die for %s", source_path)
                    return

        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if proc.returncode not in (0, None) and stderr_tail:
            tail = stderr_tail.decode("utf-8", errors="replace").strip()
            log.warning(
                "ffmpeg exited %s for %s — stderr: %s",
                proc.returncode, source_path, tail,
            )
    except Exception:
        log.exception("error reaping ffmpeg")
