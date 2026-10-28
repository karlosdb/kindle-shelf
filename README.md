# Kindle Shelf

Search for a book, pick the right one, pick a copy, choose a Kindle, press **Send**.
The book is fetched (TorBox or LibGen), turned into the right format with Calibre, and
copied onto the Kindle (over USB, or over Wi-Fi for a jailbroken Kindle) or left in
its outbox until that Kindle is reachable. Books you already have go the same way:
`kindle-shelf send <file>…`. Or choose **This computer** to just download a book and keep it
in `~/Books/<Author>/`, for reading elsewhere or sending to a Kindle later.

Kindle Shelf is a background service and a command line, made for an [Omarchy](https://omarchy.org)
laptop (Arch Linux, Hyprland). It was built with a card in Omarchy's notch (search box, copies, queue,
Send it where?), a QML shell plugin that isn't part of this repo. The card only runs `kindle-shelf`
commands and reads `kindle-shelf status`, so everything below works from a terminal too; the README
still names the card's buttons where they help.

## Requirements

- Python 3.11+ with `requests` and `Pillow`; Calibre (`ebook-convert`, `ebook-meta`); `bsdtar`;
  Noto Serif for plain covers (a default font otherwise).
- MTP Kindles: gvfs with MTP (`gio`). USB-drive Kindles: udisks2 (`udisksctl`). Wi-Fi: `ssh`.
- Toasts: `omarchy-notification-send` (without it there are none, everything else works).
- A TorBox account for torrents (`torbox_key`); LibGen needs nothing.

## Pieces

| Piece | Where |
|---|---|
| Queue service `kindle-shelfd` | `systemctl --user status kindle-shelf` → `kindle-shelf daemon` |
| CLI | `~/.local/bin/kindle-shelf` → `bin/kindle-shelf` (`kindle-shelf --help`) |
| Card (not in this repo) | any UI that runs the CLI; `card_command` in the config is what a toast's click runs to open it |
| Settings | `~/.config/kindle-shelf/config.toml` (0600, written on first start). TorBox key: `torbox_key` there, or `api_key` in `~/.config/torbox/config.toml` |
| Card state | `$XDG_RUNTIME_DIR/kindle-shelf/state.json` (socket `ctl.sock` next to it) |
| Jobs | `~/.local/state/kindle-shelf/jobs.json` |
| Books | `~/Books/Outbox/<kindle>/<job>-g<n>/` waiting (one folder per job and try, so two sends of one book never share a file) · `~/Books/Sent/` deleted 7 days after sending · `~/Books/<Author>/` kept, and books saved to This computer (never over a file already there: "… (2).epub"). A file lands in Sent or Books whole or not at all; one that can't move (disk full) stays where it was and is deleted from there on its day |

Install / reinstall: `./install.sh`. Tests: `python3 -m unittest tests.test_core`.

## Flow

1. **Which book** — Open Library search. Open Library sometimes files a translated book under
   its original title (*L'Étranger* for *The Stranger*); if what you typed doesn't match that title, copies
   are searched under your words ("the stranger") and copies carrying only the original title are
   marked as probably another language. Summaries, study guides, audiobooks and other
   languages are labeled and sorted last.
2. **Which copy** — run in parallel:
   - Pirate Bay ebook category (`apibay.org`, and the same torrents through Knaben's index, `api.knaben.org`, since
     apibay's search can answer "No results" to everything while its file lists still work), file lists checked
     so packs only pull the matching file, then TorBox's cache check ("ready instantly").
   - LibGen (`libgen.li`), slower to answer.
   Every copy gets a letter grade and the ★ **Best pick** leads the list (see Rating).
3. **Where** — one of your Kindles (USB, or Wi-Fi for a jailbroken one), email destinations once set up, or
   **This computer** (see below). Nothing downloads before Send. Sending the same book to the same place twice warns.
4. **Queue** — up to 3 books at once; keep searching meanwhile. Each job:
   fetch → unpack → check the file's own title/author against what you picked (asks if they
   differ) → cover → convert → deliver.
5. **Cover** (`kindle_shelf/covers.py`) — the file's own cover when it looks like one. A blank page,
   a title page with a few lines of text, or a publisher's logo doesn't count: then Open Library's
   cover (the next edition's if the first is tiny), and failing that a plain cover with the title
   and author in serif. An edition named in brackets ("… (1818 Edition)") gets the book's
   cover with a band naming the edition, its own cover too (once: a banded cover isn't banded again).
   `kindle-shelf fix-covers <book>…` does the same for books you already have, and says so when Calibre
   refuses to write the new cover into the book.

### Rating (`kindle_shelf/rating.py`)
Each copy's score is a sum of signals, every one with a reason (hover the grade in the card):

| Signal | Points |
|---|---|
| Format | EPUB/AZW3 30, MOBI 18, FB2 15 … PDF 2 |
| Speed | cached on TorBox 25 · LibGen 12 (6 if it's been slow lately) · uncached torrent 12/7/2 by seeders, −10 with none |
| Agreement | +4 per other copy of the same format within 3% size, up to 12 (LibGen-to-LibGen +2, up to 6: its sizes are rounded); −6 for an odd size, only when most copies agree |
| Trust | VIP/trusted uploader +8, 50+ seeders +4, LibGen publisher listed +2 |
| Name clues | retail +6, versioned (v5.0) +2 · sample/excerpt −40, abridged −25, OCR/scan −12, fan translation −20 |
| Edition type | bilingual / parallel text −30, children's adaptation (storybook, read-aloud, retold…) −35, only matches the other-language title −40 |
| LibGen credits | publisher +5 (a re-upload group named as publisher −4), translator named +8 for translated books, other credits +3 |
| Size sanity | under 350 bytes per page (Open Library's count) −15, over 80 MB −10 |
| Not the book | summary/study guide −80, other language −50, KFX/AZW −10, inside a pack −6 |
| History | a source failing ≥30% of its last 10 fetches on this machine: down to −20 |

A ≥ 65 · B ≥ 50 · C ≥ 38 · D ≥ 25 · F below. The Best pick is the highest-rated copy
graded C or better that isn't flagged and isn't PDF/DjVu, and it always leads the list.

Copies that are the same file (same source and format, size within 1.5%) fold into one row,
"+N backups". If the chosen copy fails to download, the queue tries those before giving up.
Outcomes are recorded in `~/.local/state/kindle-shelf/source-stats.json` (last 20 per source)
and feed the speed and history signals. LibGen only reports rounded sizes, so its "same size"
matches are approximate.

### Formats
- USB: everything reflowable becomes **AZW3** (Calibre marks it EBOK with an ASIN, so the
  Kindle shows the cover). PDF, KFX, AZW and TXT are copied as they are.
- A Kindle with `format = "epub"` in its config entry gets **EPUB** instead (KOReader doesn't
  read AZW3), in the folder named by `folder` (default `documents`).
- Email: Amazon accepts EPUB/PDF/DOCX, not AZW3/MOBI, so books go as **EPUB**.

### This computer
The last choice under "Send it where?" (`--to computer` from a terminal). The book gets the same checks and cover as
any other, becomes an **EPUB** (EPUBs get their title, author and cover set; MOBI, AZW3, FB2, RTF are converted; PDF,
DjVu, TXT, DOCX, KFX and AZW stay as they are), and is kept in `~/Books/<Author>/`, never over a file already there.
Only that file is kept: the download it was made from is deleted, and nothing goes to a Kindle. The button says
**Download** (or **Save** for a file you picked), and a toast opens the folder when it's done. In the Sent tab its
row opens the folder (󰉋) or sends the book to a Kindle (󰗚: the usual "Send it where?" step, with that file). It is
always kept: Keep can't be turned off for it, and removing the row leaves the book where it is.

### Kindles
The default config has two: `mine` ("My Paperwhite") and `second` ("Second Kindle"). Rename them, add more.
- **Newer Kindles** (such as the 2024 Paperwhite) are MTP: reached through gvfs (`gio`). A jailbroken
  one running KOReader gets EPUBs in `<storage>/documents/Books/`, KOReader's home folder
  (`format = "epub"`, `folder = "documents/Books"`). While it's plugged in it's in USB mode and KOReader
  isn't running. It can also take books **over Wi-Fi** (see below).
- **Older Kindles** (such as the 2012 Paperwhite) are USB drives: udiskie mounts them, books go in
  `documents/`, the cover thumbnail goes in `system/thumbnails/`, then the drive is flushed
  so it's safe to unplug (Eject is optional).
- Each Kindle is recognized by its USB serial. The first time one is plugged in, the card
  asks whose it is (the guess: drive → `second`, MTP → `mine`); from a terminal,
  `kindle-shelf name-kindle <serial> <kindle id>` (`kindle-shelf status` lists what's plugged in).
- Books queued while a Kindle was plugged in copy straight away. Otherwise they wait, and
  plugging that Kindle in shows a toast: click to send. Nothing copies without a click.
- Same file name already on the Kindle → asks: Replace or Keep both.
- The 2012 Paperwhite lost Kindle Store downloads on 20 May 2026; emailed documents
  probably don't arrive on it either. Use USB for it.

### Over Wi-Fi (SSH)
A Kindle whose `[[kindle]]` entry has `ssh = ["kindle", "kindle-ts"]` gets books over SSH, tried in order:
for example `kindle` for its address at home and `kindle-ts` for its Tailscale address, both hosts in
`~/.ssh/config` (KOReader's SSH server: user root, port 2222, your key). The Kindle must be awake, on
Wi-Fi and in KOReader, and not plugged in (USB mode turns Wi-Fi off; then USB takes over as before).

- It's the same Kindle as over USB: its serial is read from `/proc/usid` and must match the entry's
  `serial`, so format and folder (EPUB, `documents/Books/`) are the same. Every command checks it again on its
  own connection, so another device answering on those hosts gets nothing, not even a temp file.
- If Wi-Fi drops just as a book lands, the next try finds exactly its bytes there and counts it as sent
  (no second copy, no "already on it" question about it).
- **Send** copies as soon as the book is ready; no click needed. The file arrives under a hidden temp name
  and the Kindle renames it only when every byte is there (KOReader never sees half a book, and a short copy
  never replaces a book already there). Over USB the same check comes before the rename, and over MTP (which
  can't rename) a failed or short copy is deleted.
- Then KOReader is asked, through the bridge of [Quiet for Kindle](https://github.com/karlosdb/quiet-kindle)
  (`/tmp/quiet-cmd`), to re-read its Library so the book shows without a restart (without Quiet the book
  still lands; KOReader lists it when its Library next refreshes). It does nothing while a book is open, and
  never replaces a command already waiting for the bridge (the bridge looks every 2 s, or every 20 s when idle).
- Asleep, off Wi-Fi, or KOReader not running: the book waits in the outbox ("waiting for My Paperwhite
  (asleep or off Wi-Fi)") and Kindle Shelf looks again every 90 s while anything waits. Nothing is tried
  while nothing waits. `kindle-shelf deliver mine` looks right away.
- Dropping off Wi-Fi in the middle puts the book back in the outbox for the next try. The half-copied temp
  file is hidden from KOReader and gets overwritten by that next try.
- A book cancelled or removed while Kindle Shelf is looking for the Kindle stays cancelled or removed, and
  each book has at most one worker (the Wi-Fi watcher and `deliver` can't both send it).

Books already on this computer: in the card, the newest book files in `~/Downloads` are listed under the search
box (FROM DOWNLOADS) and under a picked book's copies ("Got it yourself? Pick the file", that book's files first);
typing a path into the search box works too. Picked under a book, the file keeps that book's title, author and
cover. Same from a terminal: `kindle-shelf send ~/Downloads/Book.epub [more…] [--to mine] [--keep] [--work '<json>']`.
Each gets the same treatment as a download (cover check, the Kindle's format, its folder) and the files
themselves are left where they are. Without `--to` it goes to the plugged-in Kindle, else the Wi-Fi one.
The command answers at once; each job copies its file (and reads its title and author) when it starts, from
the path it was sent from, so Retry after a Cancel works as long as the file is still there.

## Anna's Archive
Its pages are behind CAPTCHAs and it has no search API; its `llms.txt` asks programs to use
the member fast-download API. LibGen md5s are the same ids, so with a membership key set as
`annas_key` in the config, LibGen hits are downloaded through
`/dyn/api/fast_download.json` instead of LibGen's own (slower) link.

## Email
Add to `config.toml`, then `kindle-shelf reload`:

```toml
[[email]]
id = "mine-email"
name = "My Paperwhite"
address = "you_xxxx@kindle.com"

[smtp]
host = "smtp.gmail.com"
port = 587
user = "you@gmail.com"
password = "16-character Gmail app password"
```

Each Kindle owner must add the sending address under Amazon → Manage Content and Devices →
Preferences → Personal Document Settings → Approved Personal Document E-mail List.
Amazon's limit is 50 MB per email.

## Debugging
```sh
journalctl --user -u kindle-shelf -f
kindle-shelf status | jq .
```

## License
MIT, see [LICENSE](LICENSE). Not affiliated with Amazon; Kindle and Paperwhite are Amazon's trademarks.
