"""Title / author matching helpers shared by search ranking and the post-download check."""
from __future__ import annotations

import re
import unicodedata

STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "to", "for", "by", "with", "at", "from", "de", "la", "el", "le", "les", "der", "die", "das"}

# Things that share a title with the book but are not the book.
NOT_THE_BOOK = [
    ("summary", re.compile(r"\b(summary|summarized|quickread|quick read|instaread|blinkist|key takeaways|abridged summary)\b", re.I)),
    ("study guide", re.compile(r"\b(study guide|sparknotes|cliffs ?notes|bookrags|supersummary|lit ?charts|analysis of|reading guide|discussion guide|conversation starters|trivia|workbook|teacher'?s? guide|lesson plans?|literature unit|novel units?)\b", re.I)),
    ("audiobook", re.compile(r"\b(audiobook|audio book|unabridged audio|m4b|mp3)\b", re.I)),
    ("comic", re.compile(r"\b(graphic novel|comic|manga adaptation)\b", re.I)),
]
SUMMARY_PUBLISHERS = re.compile(r"\b(quickread|supersummary|instaread|bookrags|sparknotes|cliffsnotes|readtrepreneur|worth books|bright summaries|book summary|summary station)\b", re.I)


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"['’`]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [t for t in fold(s).split() if t and t not in STOP]


def main_title(title: str) -> str:
    """'Dune: Deluxe Edition' -> 'Dune'; keeps short titles intact."""
    t = re.split(r"\s[:(\[]|:\s|\s-\s|\s/\s", title or "", maxsplit=1)[0].strip()
    return t or (title or "")


def title_coverage(wanted: str, found: str) -> float:
    """Share of the wanted title's words that appear in `found` (order-free)."""
    w = tokens(main_title(wanted)) or tokens(wanted)
    if not w:
        return 0.0
    f = set(tokens(found))
    return sum(1 for t in w if t in f) / len(w)


PARTICLES = {"le", "de", "van", "von", "del", "la", "di", "du", "st", "st.", "da", "der"}


def author_surnames(authors: list[str]) -> list[str]:
    """Folded surnames, with and without a leading particle: "Honoré de Balzac"
    gives "de balzac" and "balzac" (LibGen writes "Balzac, Honoré de")."""
    out = []
    for a in authors or []:
        a = a.strip()
        if "," in a:  # "Le Guin, Ursula K."
            last = a.split(",", 1)[0]
        else:
            parts = a.split()
            last = parts[-1] if parts else ""
            if len(parts) >= 2 and parts[-2].lower() in PARTICLES:
                last = parts[-2] + " " + parts[-1]
        f = fold(last)
        if not f:
            continue
        out.append(f)
        words = f.split()
        if len(words) > 1 and words[0] in PARTICLES and words[0] not in {"le", "la"}:
            out.append(" ".join(words[1:]))
    return out


def query_surname(authors: list[str]) -> str:
    """Surname to put in a search query ("le guin", "balzac")."""
    names = author_surnames(authors[:1])
    return names[-1] if names else ""


IGNORED_QUERY_WORDS = {"epub", "mobi", "azw3", "pdf", "ebook", "book", "novel", "english", "translation", "kindle", "version"}


def wanted_title(work: dict) -> tuple[str, list[str]]:
    """(title to look for, other titles that also identify the work).

    Open Library sometimes names a work by its original-language title ("L'Étranger"
    for The Stranger). If what the user typed, minus the author's name, doesn't match
    that title, the typed words are the title they want ("the stranger")."""
    title = work.get("title", "")
    query = work.get("query", "")
    author_words = set()
    for a in work.get("authors") or []:
        author_words |= set(fold(a).split())
    typed = [t for t in tokens(query) if t not in author_words and t not in IGNORED_QUERY_WORDS]
    if typed and title_coverage(title, " ".join(typed)) < 0.75 and title_coverage(" ".join(typed), title) < 0.75:
        return " ".join(typed), [title]
    return title, []


BILINGUAL = re.compile(r"\b(bilingu\w*|parallel text|dual[- ]language|side[- ]by[- ]side|zweisprachig)\b", re.I)
ADAPTATION = re.compile(r"\b(storybook|read[- ]aloud|for (young )?children|for kids|colou?ring|pop[- ]up|picture book|retold|adapted|easy reader|graded reader)\b", re.I)


def author_matches(authors: list[str], found: str) -> bool:
    words = fold(found).split()
    hay = " " + " ".join(words) + " "
    # Whole words only, but let "Le Guin" match "LeGuin" (and the reverse).
    joined = set(words) | {a + b for a, b in zip(words, words[1:])}
    for s in author_surnames(authors):
        if f" {s} " in hay or s.replace(" ", "") in joined:
            return True
    return False


def not_the_book(text: str) -> str:
    for label, rx in NOT_THE_BOOK:
        if rx.search(text or ""):
            return label
    return ""


def safe_filename(s: str, limit: int = 120) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit].rstrip(" .") or "Untitled"


def name_parts(stem: str) -> tuple[str, list[str]]:
    """Title and authors a file's name suggests, until the book's own metadata is read: Anna's Archive names
    ("Title -- Author; Author -- Publisher -- … -- md5 -- Anna's Archive"), else "Title - Author" as Kindle Shelf names
    books, else the whole name."""
    parts = [x.strip() for x in stem.split(" -- ")] if " -- " in stem else [x.strip() for x in stem.split(" - ", 1)]
    title = parts[0] or stem.strip()
    authors = [a.strip() for a in parts[1].split(";") if a.strip()] if len(parts) > 1 else []
    return title, authors
