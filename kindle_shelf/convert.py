"""Calibre wrappers: unpack, read metadata, convert, and Kindle cover thumbnails."""
from __future__ import annotations

import re
import shutil
import struct
import subprocess
from pathlib import Path

from .sources import EBOOK_EXT, FORMAT_RANK, ext_of

ARCHIVES = (".zip", ".rar", ".7z")


class ConvertError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ConvertError(f"{Path(cmd[0]).name} took too long")


def unpack(path: Path) -> Path:
    """If `path` is an archive, extract it and return the best ebook inside: a real file in the extracted folder. An
    archive can carry links (bsdtar recreates them), and a link named like a book can point at any file on this
    computer, which would then be converted and sent, or emailed (review F29's class); links are never taken."""
    if path.suffix.lower() not in ARCHIVES:
        return path
    out = path.parent / (path.stem + "_unpacked")
    out.mkdir(exist_ok=True)
    r = _run(["bsdtar", "-xf", str(path), "-C", str(out)], timeout=300)
    if r.returncode != 0:
        raise ConvertError("couldn't unpack the archive")
    inside = out.resolve()
    books = [p for p in out.rglob("*") if ext_of(p.name) in EBOOK_EXT and not p.is_symlink() and p.is_file()
             and p.resolve().is_relative_to(inside)]
    if not books:
        raise ConvertError("the archive has no ebook inside")
    return max(books, key=lambda p: (FORMAT_RANK.get(ext_of(p.name), 0), p.stat().st_size))


def read_meta(path: Path) -> dict:
    r = _run(["ebook-meta", str(path)], timeout=120)
    meta = {"title": "", "authors": []}
    for line in r.stdout.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "title":
            meta["title"] = v
        elif k.startswith("author(s)"):
            v = re.sub(r"\s*\[[^\]]*\]$", "", v)  # drop "[sort, name]"
            meta["authors"] = [a.strip() for a in v.split("&") if a.strip()]
        elif k == "languages":
            meta["language"] = v
    return meta


def extract_cover(path: Path, out: Path) -> bool:
    out.unlink(missing_ok=True)
    _run(["ebook-meta", str(path), f"--get-cover={out}"], timeout=120)
    return out.exists() and out.stat().st_size > 1000


def convert(src: Path, dst: Path, title: str, authors: list[str], cover: Path | None = None) -> Path:
    """ebook-convert src -> dst (format from dst's extension) with clean metadata.
    AZW3 output carries cdetype EBOK and an ASIN, which is what lets a Kindle show
    the cover of a sideloaded book."""
    dst.unlink(missing_ok=True)
    cmd = ["ebook-convert", str(src), str(dst), "--title", title, "--authors", " & ".join(authors) or "Unknown"]
    if cover and cover.exists():
        cmd += ["--cover", str(cover)]
    if dst.suffix.lower() == ".azw3":
        cmd += ["--no-inline-toc"]
    r = _run(cmd, timeout=900)
    if r.returncode != 0 or not dst.exists():
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["unknown error"]
        raise ConvertError(f"conversion failed: {tail[0][:160]}")
    return dst


def _checked(r: subprocess.CompletedProcess, what: str) -> None:
    """Calibre said no: say so, instead of carrying on as if the book had changed."""
    if r.returncode != 0:
        raise ConvertError(f"{what} failed: " + ((r.stderr or r.stdout or "").strip().splitlines() or ["no reason given"])[-1][:160])


def set_meta(path: Path, title: str, authors: list[str], cover: Path | None = None) -> None:
    cmd = ["ebook-meta", str(path), "--title", title, "--authors", " & ".join(authors) or "Unknown"]
    if cover and cover.exists():
        cmd += ["--cover", str(cover)]
    _checked(_run(cmd, timeout=120), "setting the title, author and cover")


def set_cover(path: Path, cover: Path) -> None:
    _checked(_run(["ebook-meta", str(path), "--cover", str(cover)], timeout=120), "setting the cover")


def mobi_asin(path: Path) -> tuple[str, str]:
    """(ASIN, cdetype) from a MOBI/AZW3 EXTH header, or ("", "")."""
    try:
        data = path.read_bytes()[:400_000]
        rec0 = struct.unpack(">I", data[78:82])[0]
        if data[rec0 + 16:rec0 + 20] != b"MOBI":
            return "", ""
        mobi_len = struct.unpack(">I", data[rec0 + 20:rec0 + 24])[0]
        exth = rec0 + 16 + mobi_len
        if data[exth:exth + 4] != b"EXTH":
            return "", ""
        count = struct.unpack(">I", data[exth + 8:exth + 12])[0]
        pos, asin, cdetype = exth + 12, "", ""
        for _ in range(count):
            rtype, rlen = struct.unpack(">II", data[pos:pos + 8])
            val = data[pos + 8:pos + rlen]
            if rtype == 113:
                asin = val.decode("utf-8", "replace")
            elif rtype == 501:
                cdetype = val.decode("ascii", "replace")
            pos += rlen
        return asin, cdetype
    except (struct.error, OSError, IndexError):
        return "", ""


def kindle_thumbnail(cover: Path, out: Path) -> bool:
    """Cover thumbnail in the size older Kindles read from system/thumbnails."""
    if not shutil.which("magick") or not cover.exists():
        return False
    r = _run(["magick", str(cover), "-resize", "x330", "-quality", "90", str(out)], timeout=60)
    return r.returncode == 0 and out.exists()
