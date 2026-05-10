"""
Filesystem walker.

Yields candidate audio files from a music folder, fast and robustly.

WHY this is more complicated than `os.walk`:

  * On normal local filesystems, `os.scandir` + `entry.is_dir()`/`is_file()`
    is the fastest possible recipe — we get the file type straight from the
    `getdents64` syscall without a separate `stat()` per entry.

  * BUT on FUSE-based filesystems — virtiofs, sshfs, some Docker/podman
    bind mounts, certain SMB/CIFS configurations, fuse-overlayfs — the
    kernel often returns `DT_UNKNOWN` for the `d_type` field. When that
    happens, Python's `is_dir()`/`is_file()` are supposed to fall back to
    `stat()` automatically, but on some filesystem implementations that
    fallback is silent: both `is_dir()` and `is_file()` return `False`,
    everything is treated as "neither", and the walk silently yields zero
    results. This is the classic "scanner found nothing on my virtiofs
    mount" symptom.

  * Additionally, `entry.is_dir(follow_symlinks=False)` returns False for
    anything reached via a symlink — including shared NAS mounts that are
    themselves symlinked, or music libraries that use symlinks to keep
    one canonical artist directory linked to many compilations.

So this implementation is layered:

  1. Try the fast path: `entry.is_dir()` / `entry.is_file()` with
     `follow_symlinks=True`. We follow symlinks because on shared mounts
     you usually do want to descend through them; the loop guard below
     prevents infinite recursion.

  2. If the fast path returns `False` for both, fall back to a fresh
     `os.stat(entry.path)` and inspect the mode bits directly. This
     handles the DT_UNKNOWN case explicitly.

  3. We track inodes we've already visited, so symlink loops can't hang
     us. (NAS shares with circular symlinks are not unheard of.)

A debug logger is included — set `MUSE_LOG_LEVEL=DEBUG` to see one line
per directory visited. INFO-level summary lines are emitted at start and
end of every walk so users can verify a scan actually happened.
"""

from __future__ import annotations

import logging
import os
import stat as stat_mod
from pathlib import Path
from typing import Iterator, Set, Tuple

log = logging.getLogger("muse.scanner.walker")


def walk_audio_files(
    root: str,
    extensions: Set[str],
    follow_symlinks: bool = True,
) -> Iterator[Tuple[str, os.stat_result]]:
    """
    Yield (absolute_path, stat_result) for every audio file under root.

    Parameters
    ----------
    root : str
        Directory to walk recursively.
    extensions : Set[str]
        Lowercase extensions including the dot, e.g. {'.mp3', '.flac'}.
    follow_symlinks : bool
        Default True. We follow symlinks because on FUSE/virtiofs/SMB
        the file you actually want to read is often only reachable that
        way. Symlink loops are guarded against by tracking visited
        inodes — not by refusing to follow at all.
    """
    root_path = Path(root).expanduser()
    # We don't gate on Path.is_dir() — on a freshly-mounted virtiofs the
    # first stat() can briefly race the mount and return False. Open the
    # directory and let `os.scandir` fail loudly if it isn't readable.
    root_str = str(root_path)

    log.info(
        "walker: starting walk of %s (follow_symlinks=%s, %d extensions)",
        root_str, follow_symlinks, len(extensions),
    )

    yielded = 0
    dirs_visited = 0
    visited_inodes: set[tuple[int, int]] = set()

    stack: list[str] = [root_str]
    while stack:
        current = stack.pop()
        dirs_visited += 1

        # Loop guard: if a symlink leads us back to a directory we've
        # already walked, skip it. Cheap on normal filesystems and a
        # safety net on weird ones.
        try:
            d_st = os.stat(current, follow_symlinks=True)
            inode_key = (d_st.st_dev, d_st.st_ino)
            if inode_key in visited_inodes:
                log.debug("walker: skipping already-visited inode at %s", current)
                continue
            visited_inodes.add(inode_key)
        except OSError as e:
            log.debug("walker: cannot stat directory %s: %s", current, e)
            # Don't `continue` here — try scandir anyway. Some filesystems
            # let you list a directory you can't stat directly.

        try:
            it = os.scandir(current)
        except (OSError, PermissionError) as e:
            log.warning("walker: cannot open directory %s: %s", current, e)
            continue

        entries_seen = 0
        with it:
            for entry in it:
                entries_seen += 1
                try:
                    is_dir, is_file = _classify(entry, follow_symlinks)
                except OSError as e:
                    log.debug("walker: classify failed for %s: %s", entry.path, e)
                    continue

                if is_dir:
                    stack.append(entry.path)
                    continue

                if not is_file:
                    continue

                # Cheap extension check before we commit to yielding.
                name_lower = entry.name.lower()
                dot = name_lower.rfind(".")
                if dot < 0:
                    continue
                if name_lower[dot:] not in extensions:
                    continue

                # We need stat for size + mtime. Prefer entry.stat() —
                # it's allowed to reuse cached dirent info on most FSes —
                # but fall back to a fresh os.stat() if it fails on
                # virtiofs/FUSE.
                try:
                    st = entry.stat(follow_symlinks=follow_symlinks)
                except OSError:
                    try:
                        st = os.stat(entry.path, follow_symlinks=follow_symlinks)
                    except OSError as e:
                        log.debug("walker: cannot stat %s: %s", entry.path, e)
                        continue

                yielded += 1
                yield (entry.path, st)

        log.debug("walker: %s — %d entries seen", current, entries_seen)

    log.info(
        "walker: finished %s — visited %d director%s, yielded %d audio file%s",
        root_str,
        dirs_visited, "y" if dirs_visited == 1 else "ies",
        yielded, "" if yielded == 1 else "s",
    )


def _classify(entry: os.DirEntry, follow_symlinks: bool) -> tuple[bool, bool]:
    """
    Decide whether a directory entry is a file, a directory, or neither.

    Returns
    -------
    (is_dir, is_file)

    The fast path is `entry.is_dir()` / `entry.is_file()`. On filesystems
    that report `DT_UNKNOWN` from `getdents64` (notably virtiofs, sshfs,
    and several SMB configurations) Python's automatic stat fallback can
    silently return `False` for both predicates, which would silently
    drop every file. We therefore re-stat the entry explicitly when the
    fast path can't make up its mind.
    """
    # Step 1: cheap dirent-based check.
    try:
        if entry.is_dir(follow_symlinks=follow_symlinks):
            return (True, False)
    except OSError:
        pass
    try:
        if entry.is_file(follow_symlinks=follow_symlinks):
            return (False, True)
    except OSError:
        pass

    # Step 2: explicit stat fallback for the DT_UNKNOWN case.
    try:
        st = os.stat(entry.path, follow_symlinks=follow_symlinks)
    except OSError:
        # If the user asked us not to follow symlinks but this entry IS
        # a symlink, the previous predicates would have returned False
        # already; try lstat as a last resort to see what it points at.
        try:
            st = os.stat(entry.path, follow_symlinks=False)
        except OSError:
            return (False, False)

    mode = st.st_mode
    return (stat_mod.S_ISDIR(mode), stat_mod.S_ISREG(mode))
