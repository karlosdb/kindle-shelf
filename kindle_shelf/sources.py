"""Finding the book (Open Library) and copies of it (Pirate Bay ebooks + TorBox cache, LibGen).

Anna's Archive has no search API and CAPTCHAs its pages; its llms.txt asks programs to
use its member fast-download API instead. LibGen's md5s are the same ids Anna's Archive
uses, so with an `annas_key` a LibGen hit is fetched through that API.
"""
from __future__ import annotations

import html
import math
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import requests

from . import text
from .config import COVER_DIR, Config
from .torbox import Torbox, TorboxError

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) kindle-shelf/1.0"}
EBOOK_EXT = {"epub", "azw3", "mobi", "azw", "kfx", "pdf", "fb2", "txt", "docx", "djvu", "rtf"}
ARCHIVE_EXT = {"zip", "rar", "7z"}
FORMAT_RANK = {"epub": 102, "azw3": 100, "mobi": 70, "fb2": 60, "docx": 40, "rtf": 30, "azw": 35, "kfx": 35, "txt": 20, "pdf": 15, "djvu": 5}
LANGS = {"eng": "English", "spa": "Spanish", "fre": "French", "ger": "German", "ita": "Italian", "por": "Portuguese",
         "rus": "Russian", "jpn": "Japanese", "chi": "Chinese", "dut": "Dutch", "pol": "Polish", "kor": "Korean",
         "swe": "Swedish", "tur": "Turkish", "ara": "Arabic", "heb": "Hebrew", "hun": "Hungarian", "cze": "Czech",
         "dan": "Danish", "fin": "Finnish", "nor": "Norwegian", "gre": "Greek", "ukr": "Ukrainian", "vie": "Vietnamese",
         "ind": "Indonesian", "cat": "Catalan", "rum": "Romanian"}


def human(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def ext_of(name: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", name or "")
    return m.group(1).lower() if m else ""


# ---- 1. which book ---------------------------------------------------------------

def search_works(query: str, limit: int = 10) -> list[dict]:
    fields = "key,title,subtitle,author_name,first_publish_year,number_of_pages_median,cover_i,language,edition_count"
    r = requests.get("https://openlibrary.org/search.json", params={"q": query, "fields": fields, "limit": 20}, headers=UA, timeout=25)
    r.raise_for_status()
    works = []
    q_tokens = text.tokens(query)
    for d in r.json().get("docs", []):
        title = d.get("title") or ""
        authors = d.get("author_name") or []
        # Open Library pads results with loose full-text hits (Othello for "hail mary");
        # keep works whose title + authors carry most of what was typed.
        hay = set(text.tokens(title + " " + (d.get("subtitle") or "") + " " + " ".join(authors)))
        if q_tokens and sum(1 for t in q_tokens if t in hay) / len(q_tokens) < 0.6:
            continue
        langs = d.get("language") or []
        flag = text.not_the_book(" ".join([title, d.get("subtitle") or ""]))
        if not flag and authors and all(text.SUMMARY_PUBLISHERS.search(a) for a in authors):
            flag = "summary"
        lang = ""
        if langs and "eng" not in langs:
            lang = LANGS.get(langs[0], langs[0])
        cover = d.get("cover_i")
        cached = COVER_DIR / f"{cover}-M.jpg" if cover else None
        works.append({
            "key": d.get("key", ""),
            "title": title,
            "subtitle": d.get("subtitle") or "",
            "authors": authors[:3],
            "author": ", ".join(authors[:2]),
            "year": d.get("first_publish_year") or 0,
            "pages": d.get("number_of_pages_median") or 0,
            "editions": d.get("edition_count") or 0,
            "cover_id": cover or 0,
            "cover_url": f"file://{cached}" if cached and cached.exists() else "",
            "lang": lang,
            "flag": flag,
            "query": query,
        })
    # Real books first (Open Library's own relevance order otherwise), then the rest.
    works.sort(key=lambda w: (bool(w["flag"]), bool(w["lang"])))
    return works[:limit]


def fetch_covers(ids: list[int]) -> dict[str, str]:
    """Open Library covers redirect to slow archive.org zips the card's image loader
    won't follow, so fetch them here once and serve them from disk."""
    COVER_DIR.mkdir(parents=True, exist_ok=True)

    def one(cid: int) -> tuple[str, str]:
        out = COVER_DIR / f"{cid}-M.jpg"
        if not out.exists():
            try:
                r = requests.get(f"https://covers.openlibrary.org/b/id/{cid}-M.jpg", headers=UA, timeout=20)
                if not (r.ok and len(r.content) > 1000):
                    return str(cid), ""
                tmp = out.with_suffix(".part")
                tmp.write_bytes(r.content)
                tmp.replace(out)
            except requests.RequestException:
                return str(cid), ""
        return str(cid), f"file://{out}"

    with ThreadPoolExecutor(8) as ex:
        return dict(ex.map(one, [int(i) for i in ids if int(i) > 0]))


# ---- 2. copies: torrents ---------------------------------------------------------

def _tpb_query(q: str) -> list[dict]:
    r = requests.get("https://apibay.org/q.php", params={"q": q, "cat": "601"}, headers=UA, timeout=20)
    r.raise_for_status()
    return [d for d in r.json() if d.get("id") not in (None, "0")]


def _knaben_query(q: str) -> list[dict]:
    """The same Pirate Bay ebooks through Knaben's index, shaped like apibay's rows. apibay's search sometimes answers
    "No results" to everything (October 2026) while its file lists still work, so Knaben's hits keep their TPB id."""
    r = requests.post("https://api.knaben.org/v1", headers=UA, timeout=20, json={
        "search_field": "title", "query": q, "categories": [9000000], "order_by": "seeders",
        "order_direction": "desc", "size": 50, "hide_unsafe": True, "hide_xxx": True})
    r.raise_for_status()
    out = []
    for h in r.json().get("hits") or []:
        tid = re.search(r"thepiratebay/description\.php\?id=(\d+)", h.get("details") or "")
        if not tid or not h.get("hash"):
            continue
        out.append({"id": tid.group(1), "name": h.get("title") or "", "info_hash": h["hash"].lower(),
                    "seeders": int(h.get("seeders") or 0), "size": int(h.get("bytes") or 0), "status": "", "num_files": None,
                    "added": 0})
    return out


def _tpb_files(tid: str) -> list[dict]:
    try:
        r = requests.get("https://apibay.org/f.php", params={"id": tid}, headers=UA, timeout=15)
        r.raise_for_status()
        out = []
        data = r.json()
        for f in data if isinstance(data, list) else []:
            name, size = f.get("name"), f.get("size")
            name = name[0] if isinstance(name, list) and name else name
            size = size[0] if isinstance(size, list) and size else size
            if isinstance(name, str) and name:
                out.append({"name": name, "size": int(size or 0)})
        return out
    except (requests.RequestException, ValueError):
        return []


def _title_match(wanted: str, others: list[str], name: str) -> tuple[bool, str]:
    """(matches?, other title it matched instead of the wanted one, or "")."""
    if text.title_coverage(wanted, name) >= 0.75:
        return True, ""
    for t in others:
        if text.title_coverage(t, name) >= 0.75:
            return True, t
    return False, ""


def torrent_copies(work: dict, cfg: Config) -> list[dict]:
    authors = work.get("authors") or []
    wanted, others = text.wanted_title(work)
    surname = text.query_surname(authors)
    queries = []
    for t in [wanted] + others:
        short = text.main_title(t)
        queries += [f"{short} {surname}".strip(), short]
    if surname:
        queries.append(f"{surname} collection")
    seen: dict[str, dict] = {}
    queries = list(dict.fromkeys(queries))
    with ThreadPoolExecutor(6) as ex:
        # apibay's rows first: they carry the uploader's status and file count, Knaben's don't.
        found = list(ex.map(lambda q: _safe(_tpb_query, q), queries)) + list(ex.map(lambda q: _safe(_knaben_query, q), queries))
        for res in found:
            for d in res or []:
                seen.setdefault(d["info_hash"].lower(), d)

    singles, packs = [], []
    other_title: dict[str, str] = {}
    for h, d in seen.items():
        name = html.unescape(d.get("name", ""))
        d["name"] = name
        if text.not_the_book(name) == "audiobook":
            continue
        ok, other = _title_match(wanted, others, name)
        if ok:
            singles.append(d)
            other_title[h] = other
        elif surname and text.author_matches(authors, name) and (d["num_files"] is None or int(d["num_files"] or 0) > 1):   # None: Knaben, count unknown
            packs.append(d)
    singles.sort(key=lambda d: -int(d.get("seeders") or 0))
    packs.sort(key=lambda d: -int(d.get("seeders") or 0))
    candidates = singles[:12] + packs[:5]

    with ThreadPoolExecutor(8) as ex:
        files = list(ex.map(lambda d: _tpb_files(d["id"]), candidates))

    copies = []
    for d, flist in zip(candidates, files):
        is_pack_candidate = d in packs
        ebooks = [f for f in flist if ext_of(f["name"]) in EBOOK_EXT or ext_of(f["name"]) in ARCHIVE_EXT]
        if not flist:  # file list unavailable: trust the name
            fmt = next((e for e in ("epub", "azw3", "mobi", "pdf") if re.search(rf"\b{e}\b", d["name"], re.I)), "")
            if is_pack_candidate or not fmt:
                continue
            ebooks = [{"name": d["name"] + "." + fmt, "size": int(d.get("size") or 0), "guessed": True}]
        if is_pack_candidate or len([f for f in ebooks if ext_of(f["name"]) in EBOOK_EXT]) > 6:
            matching = [f for f in ebooks if text.title_coverage(wanted, f["name"].rsplit("/", 1)[-1]) >= 0.75]
            pack = True
        else:
            matching, pack = ebooks, False
        if not matching:
            continue
        best = max(matching, key=lambda f: FORMAT_RANK.get(ext_of(f["name"]), 1 if ext_of(f["name"]) in ARCHIVE_EXT else 0))
        fmt = ext_of(best["name"])
        others = sorted({ext_of(f["name"]).upper() for f in matching if ext_of(f["name"]) != fmt and ext_of(f["name"]) in EBOOK_EXT})
        copies.append({
            "id": f"tpb:{d['info_hash'].lower()}",
            "source": "torrent",
            "source_label": "Pirate Bay",
            "hash": d["info_hash"].lower(),
            "torrent_name": d.get("name", ""),
            "file_name": "" if best.get("guessed") else best["name"].rsplit("/", 1)[-1],
            "format": fmt if fmt in EBOOK_EXT else "archive",
            "also": others,
            "size": int(best.get("size") or 0),
            "torrent_size": int(d.get("size") or 0),
            "seeders": int(d.get("seeders") or 0),
            "status": str(d.get("status") or ""),
            "added": int(d.get("added") or 0),
            "pack": pack,
            "pack_count": len([f for f in ebooks if ext_of(f["name"]) in EBOOK_EXT]) if pack else 0,
            "cached": False,
            "flag": text.not_the_book(d.get("name", "")),
            "lang": "",
            "other_title": other_title.get(d["info_hash"].lower(), ""),
        })

    if copies and cfg.torbox_key:
        try:
            cached = Torbox(cfg.torbox_key, timeout=15).cached([c["hash"] for c in copies])
            for c in copies:
                c["cached"] = c["hash"] in cached
        except TorboxError:
            pass
    return copies


# ---- 2b. copies: LibGen ----------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", s or ""))).strip()


def _size_bytes(s: str) -> int:
    m = re.match(r"([\d.,]+)\s*(B|kB|KB|MB|GB)", s or "", re.I)
    if not m:
        return 0
    n = float(m.group(1).replace(",", "."))
    return int(n * {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}[m.group(2).lower()])


def libgen_copies(work: dict, cfg: Config) -> list[dict]:
    authors = work.get("authors") or []
    wanted, others = text.wanted_title(work)
    surname = text.query_surname(authors)
    params = [("req", f"{text.main_title(wanted)} {surname}".strip()), ("res", "100"),
              ("columns[]", "t"), ("columns[]", "a"), ("objects[]", "f"), ("topics[]", "l"), ("topics[]", "f")]
    r = requests.get(f"{cfg.libgen_url}/index.php?{urlencode(params)}", headers=UA, timeout=45)
    r.raise_for_status()
    page = r.text
    start = page.find('id="tablelibgen"')
    if start < 0:
        return []
    copies = []
    # Tooltip attributes carry raw "<br>" markup, which would end tags early.
    body = re.sub(r'\s(?:title|data-original-title)="[^"]*"', "", page[start:])
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 9:
            continue
        md5 = re.search(r"md5=([0-9a-fA-F]{32})", row)
        if not md5:
            continue
        t_html = cells[0]
        anchors = re.findall(r"<a [^>]*href=\"(?:edition|book/index|index)\.php[^\"]*\"[^>]*>(.*?)</a>", t_html, re.S)
        row_title = _clean(anchors[0]) if anchors else _clean(re.sub(r"<b>.*?</b>", "", t_html, flags=re.S))
        row_authors = _clean(cells[1])
        ext = _clean(cells[7]).lower()
        if ext not in EBOOK_EXT:
            continue
        ok, other = _title_match(wanted, others, row_title)
        # LibGen titles are clean, so also require the copy's own main title to be the
        # book ("Reading Camus: … The Stranger" is a different book).
        if not ok or max(text.title_coverage(row_title, t) for t in [wanted] + others) < 0.5:
            continue
        if authors and row_authors and not text.author_matches(authors, row_authors):
            continue
        # LibGen lists translators/illustrators alongside the author.
        names = [n.strip() for n in re.split(r";|&|\band\b", row_authors) if n.strip()]
        credits = [n for n in names if not text.author_matches(authors, n)]
        lang = _clean(cells[4])
        copies.append({
            "id": f"libgen:{md5.group(1).lower()}",
            "source": "libgen",
            "source_label": "LibGen",
            "md5": md5.group(1).lower(),
            "torrent_name": f"{row_title} — {row_authors}",
            "title": row_title,
            "file_name": "",
            "format": ext,
            "also": [],
            "size": _size_bytes(_clean(cells[6])),
            "seeders": 0,
            "pack": False,
            "pack_count": 0,
            "cached": False,
            "year": _clean(cells[3]),
            "publisher": _clean(cells[2]),
            "flag": text.not_the_book(row_title),
            "lang": "" if lang.lower() in ("", "english") else lang,
            "other_title": other,
            "credits": ", ".join(credits[:2]),
        })
        if len(copies) >= 60:
            break
    return copies


# ---- ranking ----------------------------------------------------------------------

def rank(copies: list[dict], cfg: Config, work: dict | None = None) -> list[dict]:
    from .rating import rate
    return rate(copies, work or {}, cfg)


def _safe(fn, *a):
    try:
        return fn(*a)
    except (requests.RequestException, ValueError):
        return []


def find_copies(work: dict, cfg: Config, which: str = "all") -> dict:
    out, errors = [], []
    if which in ("all", "torrents"):
        try:
            out += torrent_copies(work, cfg)
        except (requests.RequestException, ValueError) as e:
            errors.append(f"Pirate Bay: {e.__class__.__name__}")
    if which in ("all", "libgen"):
        try:
            out += libgen_copies(work, cfg)
        except (requests.RequestException, ValueError) as e:
            down = isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code >= 500
            errors.append(f"LibGen is down right now ({e.response.status_code})" if down else f"LibGen didn't answer ({e.__class__.__name__})")
    wanted, others = text.wanted_title(work)
    return {"copies": rank(out, cfg, work), "errors": errors, "wanted": wanted if others else ""}
