"""Rate every copy of a book and pick the one to download.

A copy's score is the sum of a few independent signals, each of which also leaves a
human-readable reason (shown in the card's tooltip, best ones as the "Best pick" line):

  format      how well it reads on a Kindle (EPUB/AZW3 best, PDF worst)
  speed       cached on TorBox (seconds) > LibGen (~a minute) > uncached torrent (depends on seeders)
  agreement   several independent copies of the same size are almost always the clean retail
              file; a size nothing else shares is more likely a scan, a sample or a bad convert
  trust       Pirate Bay VIP/trusted uploaders, seeders
  name clues  "retail", versioned releases (v5.0) vs OCR / scan / sample / excerpt / abridged
  size sanity tiny files for long books are usually incomplete; huge ones are image-heavy
  history     sources that have been failing on this machine lately are pushed down

The total maps to a letter (A best … F), and the top non-flagged copy is the Best pick.
"""
from __future__ import annotations

import json
import math
import re
import statistics

from .config import STATE_DIR, Config
from .sources import human
from .text import ADAPTATION, BILINGUAL, wanted_title

STATS_PATH = STATE_DIR / "source-stats.json"

FORMAT_FIT = {"epub": 30, "azw3": 30, "archive": 20, "mobi": 18, "fb2": 15, "docx": 8, "rtf": 6, "kfx": 5, "azw": 5, "txt": 4, "pdf": 2, "djvu": 0}
GOOD_NAME = [(re.compile(r"\bretail\b", re.I), 6, "retail release"),
             (re.compile(r"\bv\d+(\.\d+)?\b", re.I), 2, "versioned release")]
BAD_NAME = [(re.compile(r"\b(sample|excerpt|preview|teaser|first \d+ chapters)\b", re.I), -40, "looks like a sample"),
            (re.compile(r"\b(?<!un)abridged\b", re.I), -25, "abridged"),
            (re.compile(r"\b(ocr|scan(ned)?|pdf ?to ?epub|converted from pdf)\b", re.I), -12, "scanned / OCR text"),
            (re.compile(r"\b(fan ?translation|machine translat)", re.I), -20, "unofficial translation")]
TRUSTED = {"vip": 8, "trusted": 8, "helper": 6, "moderator": 6, "supermod": 6, "admin": 6}


def source_key(c: dict) -> str:
    if c.get("source") == "torrent":
        return "torrent-cached" if c.get("cached") else "torrent-uncached"
    return "libgen"


def load_stats() -> dict:
    try:
        return json.loads(STATS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def record_outcome(key: str, ok: bool, seconds: float) -> None:
    """Called by the daemon when a fetch finishes or fails; keeps the last 20 per source."""
    stats = load_stats()
    hist = stats.get(key, [])[-19:]
    hist.append({"ok": ok, "s": round(seconds, 1)})
    stats[key] = hist
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(stats))
    tmp.replace(STATS_PATH)


def _eta(c: dict, stats: dict, fast_libgen: bool) -> tuple[str, int]:
    """(label, speed points)."""
    hist = [h for h in stats.get(source_key(c), []) if h.get("ok")]
    typical = statistics.median([h["s"] for h in hist]) if len(hist) >= 3 else None
    if c["source"] == "torrent":
        if c.get("cached"):
            return "seconds", 25
        s = c.get("seeders", 0)
        if s >= 20:
            return "a few minutes", 12
        if s >= 5:
            return "minutes, maybe longer", 7
        if s >= 1:
            return "could take hours", 2
        return "may never finish", -10
    if fast_libgen:
        return "seconds", 20
    if typical is not None:
        return ("about " + (f"{int(typical)} s" if typical < 90 else f"{round(typical / 60)} min")), 12 if typical < 120 else 6
    return "about a minute", 12


DERIVED = ("alternates", "dupes", "reasons", "why", "grade", "score", "eta", "note", "warn", "recommended")


def _flatten(copies: list[dict]) -> list[dict]:
    """Undo an earlier collapse so re-rating (after another source answers) sees every copy."""
    out = []
    for c in copies:
        for x in [c] + list(c.get("alternates") or []):
            out.append({k: v for k, v in x.items() if k not in DERIVED})
    return out


def rate(copies: list[dict], work: dict, cfg: Config) -> list[dict]:
    copies = _flatten(copies)
    stats = load_stats()
    fast_libgen = bool(cfg.annas_key)
    pages = int(work.get("pages") or 0)
    translated = bool(work.get("title")) and bool(wanted_title(work)[1])

    # Sizes per format, for the agreement and outlier checks. Outliers only count when
    # most copies really agree (illustrated vs text-only editions legitimately differ).
    by_fmt: dict[str, list[int]] = {}
    for c in copies:
        if c.get("size"):
            by_fmt.setdefault(c["format"], []).append(c["size"])
    consensus: dict[str, bool] = {}
    for fmt, sizes in by_fmt.items():
        biggest = max(sum(1 for s in sizes if abs(s - x) <= 0.03 * x) for x in sizes)
        consensus[fmt] = len(sizes) >= 4 and biggest / len(sizes) >= 0.4

    for c in copies:
        reasons: list[tuple[int, str]] = []   # (points, why)
        fmt, size = c["format"], int(c.get("size") or 0)

        # format
        fit = FORMAT_FIT.get(fmt, 4)
        reasons.append((fit, {"epub": "EPUB, ideal for Kindle", "azw3": "AZW3, Kindle's own format", "mobi": "MOBI, older Kindle format",
                              "pdf": "PDF, fixed pages", "archive": "zipped ebook"}.get(fmt, f"{fmt.upper()} format")))

        # speed
        eta, pts = _eta(c, stats, fast_libgen)
        c["eta"] = eta
        if c["source"] == "torrent" and c.get("cached"):
            reasons.append((pts, "instant from TorBox"))
        elif c["source"] == "torrent":
            reasons.append((pts, f"not cached on TorBox, {c.get('seeders', 0)} seeders"))
        else:
            reasons.append((pts, "LibGen direct download"))

        # agreement: other copies (any source) of this format within ±3% size
        same = by_fmt.get(fmt, [])
        if size and len(same) >= 2:
            twins = sum(1 for s in same if abs(s - size) <= 0.03 * size) - 1
            # LibGen only reports rounded sizes ("2 MB"), so LibGen-to-LibGen matches are weak evidence.
            exact = sum(1 for o in copies if o is not c and o["format"] == fmt and o.get("source") == "torrent"
                        and o.get("size") and abs(o["size"] - size) <= 0.03 * size)
            weak = c.get("source") == "libgen"
            pts = 4 * exact + (2 if weak else 4) * (twins - exact)
            if twins >= 1:
                reasons.append((min(12 if not weak else 6, pts), f"same size as {twins} other cop{'y' if twins == 1 else 'ies'}"))
            elif consensus.get(fmt):
                reasons.append((-6, "size differs from most copies"))

        # trust
        if c["source"] == "torrent":
            t = TRUSTED.get(str(c.get("status") or "").lower(), 0)
            if t:
                reasons.append((t, f"{c['status'].upper() if c['status'] == 'vip' else c['status']} uploader"))
            if c.get("seeders", 0) >= 50:
                reasons.append((4, f"popular ({c['seeders']} seeders)"))
        else:
            pub = c.get("publisher") or ""
            if re.search(r"[\u3000-\u9fff\uac00-\ud7af]", pub) and not re.search(r"[\u3000-\u9fff\uac00-\ud7af]", c.get("title") or ""):
                reasons.append((-4, "re-upload group listed as publisher"))
            elif pub:
                reasons.append((5, f"published by {pub.split(';')[0].split('(')[0].strip()[:30]}"))
            if c.get("credits"):
                reasons.append((8 if translated else 3, ("translator named: " if translated else "credits ") + c["credits"][:40]))

        # name clues
        name = " ".join([c.get("torrent_name") or "", c.get("file_name") or ""])
        for rx, p, why in GOOD_NAME + BAD_NAME:
            if rx.search(name):
                reasons.append((p, why))

        # edition type
        if c.get("other_title"):
            reasons.append((-40, f"titled “{c['other_title']}”, probably not English"))
        if BILINGUAL.search(name):
            reasons.append((-30, "bilingual edition (two languages)"))
        if ADAPTATION.search(name):
            reasons.append((-35, "adaptation, not the full text"))

        # size sanity
        if size and fmt in ("epub", "azw3", "mobi", "fb2"):
            if pages >= 150 and size < pages * 350:
                reasons.append((-15, f"only {human(size)} for {pages} pages, may be incomplete"))
            elif size > 80 * 1024 ** 2:
                reasons.append((-10, "very large, lots of images?"))

        if c.get("pack"):
            reasons.append((-6, f"inside a {c.get('pack_count', 0)}-book pack"))
        if c.get("flag"):
            reasons.append((-80, f"looks like a {c['flag']}"))
        if c.get("lang"):
            reasons.append((-50, f"in {c['lang']}"))
        if fmt in ("azw", "kfx"):
            reasons.append((-10, "may be locked to someone else's account"))

        # history on this machine
        hist = stats.get(source_key(c), [])[-10:]
        if len(hist) >= 3:
            fails = sum(1 for h in hist if not h.get("ok"))
            if fails / len(hist) >= 0.3:
                reasons.append((-int(20 * fails / len(hist)), f"this source failed {fails} of the last {len(hist)} times"))

        score = sum(p for p, _ in reasons)
        c["score"] = score
        c["grade"] = "A" if score >= 65 else "B" if score >= 50 else "C" if score >= 38 else "D" if score >= 25 else "F"
        c["reasons"] = [{"p": p, "why": why} for p, why in sorted(reasons, key=lambda r: -abs(r[0])) if p]
        good = [why for p, why in sorted(reasons, key=lambda r: -r[0])
                if p > 0 and not why.startswith(("EPUB", "AZW3", "MOBI", "LibGen direct"))]
        bad = [why for p, why in sorted(reasons, key=lambda r: r[0]) if p <= -6]
        c["why"] = ", ".join(good[:2])
        c["size_h"] = human(size)
        # Short lines for the row itself.
        c["note"] = {"seconds": "ready in seconds"}.get(eta, eta)
        c["warn"] = " · ".join(bad[:2])
        c["recommended"] = False

    copies.sort(key=lambda c: (-c["score"], -(c.get("seeders") or 0)))
    copies = _collapse(copies)
    copies.sort(key=lambda c: (-c["score"], -c["dupes"], -(c.get("seeders") or 0)))   # ties: more backups wins
    for c in copies:
        if c["grade"] in ("A", "B", "C") and not c.get("flag") and not c.get("lang") and c["format"] not in ("pdf", "djvu"):
            c["recommended"] = True
            copies.remove(c)
            copies.insert(0, c)   # the Best pick always leads
            break
    return copies


def _collapse(ranked: list[dict]) -> list[dict]:
    """Fold copies that are the same file (same source, format and size within 1.5%) into
    the best-rated one. The others ride along as `alternates`, which the queue falls back
    to if the first download fails."""
    kept: list[dict] = []
    for c in ranked:
        twin = next((k for k in kept if k["source"] == c["source"] and k["format"] == c["format"]
                     and k.get("size") and c.get("size") and abs(k["size"] - c["size"]) <= 0.015 * k["size"]), None)
        if twin is None:
            c["alternates"] = []
            kept.append(c)
        else:
            twin["alternates"].append({k: v for k, v in c.items() if k not in DERIVED})
    for k in kept:
        k["dupes"] = len(k["alternates"])
    return kept
