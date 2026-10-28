"""Where a sent book's files go, and moving them there: the archive's rules in one place.

Archiving was patched five times in review (Codex, 2026-10-19/20: F16 write-ahead journal, F19 start order and pending
names, F20 never raises, F21 the janitor under files_lock, F23 Keep journaled) and still lost books: a file that
couldn't move was recorded in the work folder, which was then deleted (F24, F20's own fallback), and a move across
filesystems that ran out of space left half a book at the destination, which was then recorded as the book (F25).

The rules:
  - plan(): every file gets a name nobody has: nothing on disk there, and not a name another job's unfinished archive
    will use (F19). A file already in the destination folder stays.
  - move_whole(): a book appears at its destination whole or not at all, and never over another file: one rename on
    the same filesystem; across filesystems a copy beside it under a hidden ".part" name of the job's own (its id is
    in it: a name made from the book's alone could be a file of the user's, review C2), flushed and checked against
    the source's size, renamed into place, and only then the source removed.
  - settle(): one planned move, decided from what's on disk now, so a second try after a stop does the right thing.
    The destination counts as the book only when the source is gone or is the same book; anything else leaves the
    book where it is, and that's where it is recorded.
  - prune_job(): a job's temporary folders are emptied of everything but the files it records: a book that couldn't
    move lives on there, and is deleted from there when its day comes (Shelf._janitor_pass). A recorded link (only
    jobs from before convert.unpack refused links: F29, F31) keeps the folders whole until its book goes.
The shelf holds files_lock around all of it and saves the plan before anything moves (Shelf._mark_sent, Shelf._keep).
"""
from __future__ import annotations

import errno
import filecmp
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("kindle-shelf")

BOOK_KEYS = ("final", "original")


def plan(files: dict, dst_dir: Path, keep: bool, taken: set[Path]) -> dict[str, tuple[Path, Path]]:
    """{key: (file now, where it goes)} for a sent job's book files, each on a free name (free_name), `taken` being the
    names other jobs' unfinished archives will use."""
    out: dict[str, tuple[Path, Path]] = {}
    for key in BOOK_KEYS:
        p = Path(files.get(key) or "")
        if not files.get(key) or not p.is_file() or dst_dir in p.parents:
            continue
        name = p.name.removeprefix("original - ")
        mine = {b for _, b in out.values()}
        if keep:
            dst = dst_dir / name
            if key == "original" and (os.path.lexists(dst) or dst in mine | taken):   # the kept book itself has that name
                dst = dst_dir / ("original - " + name)
        else:
            dst = dst_dir / (name if key == "final" else "original - " + name)
        out[key] = (p, free_name(dst, mine | taken))
    return out


def free_name(dst: Path, taken: set[Path] = frozenset()) -> Path:
    """`dst`, or "name (2).ext", "(3)"…: nothing on disk there and not in `taken`. Never over a file we didn't make just
    now (a book sent from ~/Books/<Author>/ with Keep on would otherwise be replaced by its own copy)."""
    out, n = dst, 2
    while os.path.lexists(out) or out in taken:
        out = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        n += 1
    return out


def part_path(dst: Path, owner: str) -> Path:
    """Where job `owner`'s copy across filesystems is written before it is whole: hidden, beside its destination."""
    return dst.with_name(f".{dst.name}.{owner}.part")


def move_whole(src: Path, dst: Path, owner: str) -> None:
    """Move `src` to `dst`, which must be free. On failure `src` is as it was and nothing is at `dst`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "already there", str(dst))
    if not src.is_symlink():         # a link is copied as the book it points to: renamed, it would point elsewhere (F29)
        try:
            os.rename(src, dst)      # the same filesystem: one step
            return
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
    part = part_path(dst, owner)
    try:
        shutil.copyfile(src, part)
        fd = os.open(part, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if part.stat().st_size != src.stat().st_size:
            raise OSError(errno.EIO, "the copy came out short", str(part))
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "already there", str(dst))
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)
    src.unlink()


def same(a: Path, b: Path) -> bool:
    return os.path.samefile(a, b) or filecmp.cmp(a, b, shallow=False)


def settle(src: Path, dst: Path, owner: str) -> Path | None:
    """Carry out one planned move from what's on disk now and say where the book is: `dst` once it's there whole, `src`
    while it couldn't move, None when it's at neither. Never raises.
        only src                          → moved (move_whole); if that fails, src
        src and dst, the same book        → a stop between placing it and removing src: src goes, dst
        src and dst, not the same         → someone else's file at dst: src, both untouched
        only dst                          → moved before a stop: dst
    """
    try:
        part_path(dst, owner).unlink(missing_ok=True)      # what a stop in the middle of this job's copy left
        if src.is_file() and not os.path.lexists(dst):
            move_whole(src, dst, owner)
        elif src.is_file() and dst.is_file() and same(src, dst):
            src.unlink()
        elif src.is_file():
            log.warning("%s is already there and isn't this book; it stays at %s", dst, src)
    except OSError as e:
        log.warning("couldn't move %s to %s: %s", src, dst, e)
    try:
        if dst.is_file() and (not src.is_file() or same(src, dst)):
            return dst       # a source that couldn't be removed is a leftover now: unrecorded, pruned with the rest
        return src if src.is_file() else None
    except OSError:
        return src


def prune_job(dirs: list[Path], recorded: list[Path]) -> None:
    """Empty a job's temporary folders of everything but its recorded book files. If one of those is a link that still
    leads to a book, nothing goes: the way to its bytes can run through links anywhere in these folders (F31), and
    they all go with the book. A link that leads nowhere any more protects nothing."""
    if any(os.path.islink(p) and p.exists() for p in recorded):
        return
    keep = {p for p in recorded if p.is_file()}
    for d in dirs:
        prune(d, keep)


def prune(root: Path, keep: set[Path]) -> None:
    """Remove everything under `root` but the files in `keep` and the folders on their way; `root` itself goes once
    nothing in it is kept. Best effort, like rmtree(ignore_errors=True): what can't be removed stays."""
    try:
        if not os.path.lexists(root):
            return
        if root.is_symlink() or not root.is_dir():
            if root not in keep:
                root.unlink()
            return
        if not any(root in k.parents for k in keep):
            shutil.rmtree(root, ignore_errors=True)
            return
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        prune(child, keep)
