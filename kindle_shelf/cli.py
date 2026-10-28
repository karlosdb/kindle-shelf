"""kindle-shelf command line. The notch card calls these; they work from a terminal too.

  kindle-shelf search "project hail mary"            which book? (JSON)
  kindle-shelf covers <cover id>…                    cache Open Library covers locally (JSON)
  kindle-shelf fix-covers <book>…                    give books with no real cover one (JSON)
  kindle-shelf copies '<work json>' [torrents|libgen] copies of it, ranked (JSON)
  kindle-shelf rate '{"work":…,"copies":[…]}'        re-rate copies from several sources together
  kindle-shelf send '<work json>' '<copy json>' <dest> [--keep]
                                                     <dest> "computer": just keep it, in ~/Books/<Author>/ (EPUB)
  kindle-shelf send <file>… [--to <kindle>|computer] [--keep] [--work '<work json>']
                                                     books on this computer → a Kindle (USB or Wi-Fi); --work names it
                                                     after the book picked in the card
  kindle-shelf files                                 newest book files in ~/Downloads (JSON)
  kindle-shelf cancel|retry|remove <job>
  kindle-shelf confirm <job> send|replace|both|cancel
  kindle-shelf keep <job> on|off
  kindle-shelf deliver [dest]                        send waiting books now: plugged in, or over Wi-Fi
  kindle-shelf name-kindle <serial> <dest>
  kindle-shelf eject | status | open | reload
  kindle-shelf daemon                                run the queue (systemd does this)
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys

from . import config as C


def call(cmd: str, **args) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    try:
        s.connect(str(C.SOCKET_PATH))
    except OSError:
        return {"ok": False, "error": "kindle-shelfd isn't running (systemctl --user start kindle-shelf)"}
    with s:
        s.sendall((json.dumps({"cmd": cmd, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    try:
        return json.loads(buf or b"{}")
    except ValueError:
        return {"ok": False, "error": "bad reply from kindle-shelfd"}


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cmd, rest = a[0], a[1:]

    if cmd == "daemon":
        from .daemon import main as run
        run()
        return
    if cmd == "search":
        from .sources import search_works
        try:
            out({"ok": True, "works": search_works(" ".join(rest))})
        except Exception as e:
            out({"ok": False, "error": f"Open Library didn't answer ({e.__class__.__name__}). Try again."})
        return
    if cmd == "covers":
        from .sources import fetch_covers
        out({"ok": True, "covers": fetch_covers([int(x) for x in rest if x.isdigit()])})
        return
    if cmd == "fix-covers":
        from pathlib import Path
        from .covers import fix_file
        books = [fix_file(Path(p)) for p in rest]
        out({"ok": not any(b.get("error") for b in books), "books": books})
        return
    if cmd == "copies":
        from .sources import find_copies
        work = json.loads(rest[0])
        which = rest[1] if len(rest) > 1 else "all"
        res = find_copies(work, C.load(), which)
        out({"ok": True, "source": which, **res})
        return
    if cmd == "files":
        from pathlib import Path
        from . import text
        from .sources import EBOOK_EXT, ext_of, human
        found = []
        for f in (Path.home() / "Downloads").glob("*"):
            try:
                if f.is_file() and ext_of(f.name) in EBOOK_EXT:
                    found.append((f.stat().st_mtime, f))
            except OSError:
                continue
        files = []
        for mtime, f in sorted(found, reverse=True)[:8]:
            title, authors = text.name_parts(f.stem)
            files.append({"path": str(f), "name": f.name, "title": title, "author": ", ".join(authors[:2]),
                          "format": ext_of(f.name), "size": f.stat().st_size, "size_h": human(f.stat().st_size), "mtime": mtime})
        out({"ok": True, "files": files})
        return
    if cmd == "rate":
        # Re-rate copies gathered from several `copies` calls together, so the
        # agreement signal sees every source. Reads {"work":…, "copies":[…]} from argv or stdin.
        from .sources import rank
        data = json.loads(rest[0] if rest else sys.stdin.read())
        out({"ok": True, "copies": rank(data.get("copies", []), C.load(), data.get("work") or {})})
        return
    if cmd == "send":
        keep = "--keep" in rest
        rest = [r for r in rest if r != "--keep"]
        if rest and not rest[0].lstrip().startswith("{"):
            # files: the daemon runs elsewhere, so it gets absolute paths
            import os
            opts = {"--to": "", "--work": ""}
            for flag in opts:
                if flag in rest:
                    i = rest.index(flag)
                    opts[flag] = rest[i + 1] if i + 1 < len(rest) else ""
                    rest = rest[:i] + rest[i + 2:]
            r = call("send-files", paths=[os.path.abspath(os.path.expanduser(f)) for f in rest], dest=opts["--to"], keep=keep,
                     work=json.loads(opts["--work"]) if opts["--work"] else None)
        else:
            r = call("send", work=json.loads(rest[0]), copy=json.loads(rest[1]), dest=rest[2], keep=keep)
    elif cmd in ("cancel", "retry", "remove"):
        r = call(cmd, id=rest[0])
    elif cmd == "confirm":
        r = call("confirm", id=rest[0], choice=rest[1] if len(rest) > 1 else "send")
    elif cmd == "keep":
        r = call("keep", id=rest[0], on=(rest[1] if len(rest) > 1 else "on") == "on")
    elif cmd == "deliver":
        r = call("deliver", dest=rest[0] if rest else "")
    elif cmd == "name-kindle":
        r = call("name-kindle", serial=rest[0], dest=rest[1])
    elif cmd in ("eject", "reload"):
        r = call(cmd)
    elif cmd == "status":
        r = call("state")
        out(r.get("state", r))
        return
    elif cmd == "open":
        cfg = C.load()
        cfg.books_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(cfg.books_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    else:
        print(f"unknown command: {cmd}\n\n{__doc__.strip()}", file=sys.stderr)
        sys.exit(2)
    out(r)
    if not r.get("ok", False):
        sys.exit(1)
