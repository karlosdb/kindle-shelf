"""Book covers: keep a real one, replace a stand-in, find one online, or make a plain one.

Downloaded books often carry something in the cover slot that isn't a cover: nothing at all, a blank page, a
title page with a few lines of text, or the publisher's logo. The Kindle shows that page (or a grey box) on its
home screen and in the library. ensure() settles it before the book is converted:

1. the file's own cover, when it looks like a cover;
2. otherwise Open Library's cover for the book (the cover id from the search, or a title + author search).
   A black-and-white image (a logo, a text-only page) is swapped too, when Open Library's cover is other art;
3. otherwise a plain cover: title and author in serif on paper white.

An edition named in brackets ("Frankenstein (1818 Edition)") gets the book's cover with a band
naming the edition, so two editions of one book don't look the same.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

from . import text
from .sources import UA

COVER_W, COVER_H = 1200, 1800
SERIF_BOLD = "/usr/share/fonts/noto/NotoSerif-Bold.ttf"
SERIF = "/usr/share/fonts/noto/NotoSerif-Regular.ttf"
PAPER, INK, INK2 = (244, 241, 234), (28, 28, 28), (90, 90, 88)

EDITION = re.compile(r"\s*[(\[]([^)\]]*\bedition\b[^)\]]*)[)\]]\s*$", re.I)


def edition(title: str) -> tuple[str, str]:
    """('Frankenstein', '1818 Edition') from a title ending in a bracketed edition."""
    m = EDITION.search(title or "")
    if not m:
        return title or "", ""
    return title[:m.start()].strip(), m.group(1).strip()


def _gray(src, size: tuple[int, int]) -> Image.Image:
    with Image.open(src) as im:
        im.load()
        return ImageOps.grayscale(im).resize(size)


def problem(path: Path) -> str:
    """Why this image can't be a book's cover, or "" when it can."""
    try:
        with Image.open(path) as im:
            im.load()
            w, h = im.size
            g = ImageOps.grayscale(im).resize((120, 180))
    except (OSError, ValueError):
        return "unreadable"
    if w < 200 or h < 280:
        return "too small"
    if not 1.15 <= h / w <= 1.9:
        return "not cover-shaped"
    if ImageStat.Stat(g).stddev[0] < 6:
        return "blank"
    box = g.point(lambda v: 255 if v < 215 else 0).getbbox()
    if not box:
        return "blank"
    if (box[2] - box[0]) * (box[3] - box[1]) < 0.3 * 120 * 180:
        return "mostly empty, a title page"
    return ""


def is_plain(path: Path) -> bool:
    """Black and white with hardly any greys in between: a logo or a text-only page (or a very spare cover)."""
    g = _gray(path, (300, 450))
    hist = g.histogram()
    return sum(hist[41:215]) / (300 * 450) < 0.10


def same_art(a: Path, b: Path) -> bool:
    """Whether two images show the same artwork (contrast-normalised thumbnails, mean difference)."""
    ga = ImageOps.autocontrast(_gray(a, (24, 36)))
    gb = ImageOps.autocontrast(_gray(b, (24, 36)))
    diff = sum(abs(x - y) for x, y in zip(ga.getdata(), gb.getdata())) / (24 * 36)
    return diff < 40


NO_AUTHOR = {"unknown", "anonymous", "n/a", "various", ""}


def _search_cover_ids(title: str, authors: list[str]) -> list[int]:
    """Cover ids of the matching works, best match first."""
    params = {"title": text.main_title(title), "fields": "title,cover_i", "limit": 10}
    named = [a for a in authors if a.strip().lower() not in NO_AUTHOR]
    if named:
        params["author"] = named[0]
    r = requests.get("https://openlibrary.org/search.json", params=params, headers=UA, timeout=20)
    r.raise_for_status()
    ids = []
    for d in r.json().get("docs", []):
        if d.get("cover_i") and text.title_coverage(title, d.get("title", "")) >= 0.75 and d["cover_i"] not in ids:
            ids.append(int(d["cover_i"]))
    return ids


def online(title: str, authors: list[str], cover_id: int = 0, workdir: Path | None = None) -> bytes | None:
    """Open Library's large cover, by id or by a title + author search. A cover that's too small or isn't
    cover-like is skipped for the next match (up to four)."""
    try:
        ids = [cover_id] if cover_id else []
        ids += [i for i in _search_cover_ids(title, authors) if i not in ids]
    except (requests.RequestException, ValueError):
        ids = [cover_id] if cover_id else []
    probe = (workdir or Path("/tmp")) / ".kindle-shelf-cover-probe.jpg"
    for cid in ids[:4]:
        try:
            r = requests.get(f"https://covers.openlibrary.org/b/id/{cid}-L.jpg", params={"default": "false"},
                             headers=UA, timeout=45)
        except requests.RequestException:
            continue
        if not r.ok or len(r.content) < 5000:
            continue
        probe.write_bytes(r.content)
        bad = problem(probe)
        probe.unlink(missing_ok=True)
        if not bad:
            return r.content
    return None


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, words: list[str], font, width: int) -> list[str]:
    lines, line = [], ""
    for word in words:
        trial = (line + " " + word).strip()
        if line and draw.textlength(trial, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def with_band(src: Path, label: str, out: Path) -> None:
    """The cover with a paper-white band at the bottom naming the edition."""
    with Image.open(src) as im:
        im = im.convert("RGB")
    im = ImageOps.contain(im, (COVER_W, COVER_H))
    w, h = im.size
    band = int(h * 0.11)
    draw = ImageDraw.Draw(im)
    draw.rectangle([0, h - band, w, h], fill=PAPER)
    draw.line([0, h - band, w, h - band], fill=INK, width=max(2, w // 300))
    size = int(band * 0.42)
    font = _font(SERIF_BOLD, size)
    while draw.textlength(label, font=font) > w * 0.9 and size > 12:
        size -= 2
        font = _font(SERIF_BOLD, size)
    tw = draw.textlength(label, font=font)
    draw.text(((w - tw) / 2, h - band / 2), label, font=font, fill=INK, anchor="lm")
    im.save(out, "JPEG", quality=90)


def has_band(path: Path) -> bool:
    """Whether the cover already carries with_band's band: paper-white margins at the bottom under a dark rule. Keeps
    ensure() from banding a cover twice (fix-covers run again, or a cover that came back from a banded book)."""
    try:
        with Image.open(path) as im:
            im.load()
            g = ImageOps.grayscale(im)
    except (OSError, ValueError):
        return False
    w, h = g.size
    band = int(h * 0.11)
    if band < 8 or w < 50:
        return False
    rule = max(2, w // 300)
    top = h - band
    margin = max(4, int(w * 0.04))   # with_band centres its label: the outer 4% is always bare paper
    strips = [g.crop((0, top + rule + 2, margin, h)), g.crop((w - margin, top + rule + 2, w, h))]
    paper = all(ImageStat.Stat(st).mean[0] > 220 and ImageStat.Stat(st).stddev[0] < 12 for st in strips)
    line = ImageStat.Stat(g.crop((0, top, w, top + rule))).mean[0] < 110
    return paper and line


def _keep(cover: Path, label: str) -> str:
    """The book's own cover stays; a bracketed edition still gets its band (once)."""
    if label and not has_band(cover):
        with_band(cover, label, cover)
        return "kept the book's own cover, with the edition band"
    return "kept the book's own cover"


def make(title: str, authors: list[str], out: Path) -> None:
    """A plain typographic cover: the title large, the author below, a thin frame, paper white."""
    base, label = edition(title)
    im = Image.new("RGB", (COVER_W, COVER_H), PAPER)
    draw = ImageDraw.Draw(im)
    m = 70
    draw.rectangle([m, m, COVER_W - m, COVER_H - m], outline=INK, width=4)
    width = COVER_W - 2 * m - 160
    size = 132
    while True:
        font = _font(SERIF_BOLD, size)
        lines = _wrap(draw, base.split(), font, width)
        if len(lines) <= 5 or size <= 60:
            break
        size -= 8
    line_h = int(size * 1.22)
    y = int(COVER_H * 0.36) - len(lines) * line_h // 2
    for line in lines:
        draw.text((COVER_W / 2, y), line, font=font, fill=INK, anchor="ma")
        y += line_h
    y += 50
    draw.line([COVER_W / 2 - 70, y, COVER_W / 2 + 70, y], fill=INK, width=4)
    if label:
        small = _font(SERIF, 52)
        draw.text((COVER_W / 2, y + 50), label, font=small, fill=INK2, anchor="ma")
    author = " & ".join(a for a in authors if a.strip().lower() not in NO_AUTHOR)
    if author:
        font_a = _font(SERIF, 72)
        for i, line in enumerate(_wrap(draw, author.split(), font_a, width)[:2]):
            draw.text((COVER_W / 2, int(COVER_H * 0.74) + i * 90), line, font=font_a, fill=INK, anchor="ma")
    im.save(out, "JPEG", quality=92)


def ensure(cover: Path, title: str, authors: list[str], cover_id: int = 0) -> str:
    """Leave a usable cover at `cover` (it may already hold the file's own). Returns what was done."""
    have = cover.exists() and cover.stat().st_size > 1000
    why = problem(cover) if have else "none in the file"
    base, label = edition(title)
    if have and not why and not is_plain(cover):
        return _keep(cover, label)
    data = online(base, authors, 0 if label else cover_id, cover.parent)
    if data:
        found = cover.with_name(cover.stem + "-online.jpg")
        found.write_bytes(data)
        if not problem(found):
            if have and not why and same_art(cover, found):
                found.unlink()
                return _keep(cover, label)
            if label:
                with_band(found, label, cover)
                found.unlink()
            else:
                os.replace(found, cover)
            return "cover from Open Library (" + (why or "the file had a logo or text page") + ")"
        found.unlink(missing_ok=True)
    if have and not why:
        return _keep(cover, label)
    make(title, authors, cover)
    return f"made a plain cover ({why})"


def fix_file(path: Path) -> dict:
    """Check an ebook's own cover and, when it isn't a real one, put a better one in the file."""
    import tempfile

    from . import convert
    meta = convert.read_meta(path)
    title = meta.get("title") or path.stem
    with tempfile.TemporaryDirectory() as d:
        cover = Path(d) / "cover.jpg"
        convert.extract_cover(path, cover)
        before = cover.read_bytes() if cover.exists() else b""
        result = ensure(cover, title, meta.get("authors") or [])
        changed = cover.exists() and cover.read_bytes() != before
        error = ""
        if changed:
            try:
                convert.set_cover(path, cover)
            except convert.ConvertError as e:   # the new cover never made it into the book
                changed, error = False, str(e)
    out = {"file": str(path), "title": title, "result": result, "changed": changed}
    if error:
        out["error"] = error
    return out
