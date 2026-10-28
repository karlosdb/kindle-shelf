"""kindle-shelfd: the background queue.

Jobs move   queued → fetching → checking → converting → (waiting) → copying|emailing → sent
with side exits  confirm (needs a click: wrong-looking book / name already on the Kindle),
                 error, cancelled.

Besides the Kindles and email there is one more destination, LOCAL ("This computer"): the book is fetched, checked
and given its cover the same way, made an EPUB (PDFs and other fixed formats stay as they are), and kept in
~/Books/<Author>/ by the same archive step as Keep. Nothing is copied anywhere.

A Kindle whose config entry names ssh hosts also gets books over Wi-Fi (home network or Tailscale): straight
away when it answers, otherwise the book waits and is tried again every NET_EVERY seconds while anything is
waiting for it (nothing is tried while nothing waits). Plugged in over USB, USB wins (its Wi-Fi is off then).

State for the card goes to $XDG_RUNTIME_DIR/kindle-shelf/state.json; commands come in on
a unix socket next to it (see cli.py). Jobs persist in ~/.local/state/kindle-shelf/jobs.json.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import smtplib
import socket
import socketserver
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from pathlib import Path

import requests

from . import config as C
from . import archive, convert, covers, jobstate, kindle, rating, text
from .sources import UA, ext_of, human
from .torbox import Torbox, TorboxError

log = logging.getLogger("kindle-shelf")

ACTIVE = jobstate.ACTIVE
GLYPH = "󰗚"
DAY = 86400
NET_EVERY = 90   # seconds between looks for a Kindle on Wi-Fi while a book waits for it
LOCAL = "computer"   # the destination that keeps the book on this computer
LOCAL_AS_IS = {"pdf", "djvu", "txt", "docx", "kfx", "azw"}   # kept in their own format; anything else becomes EPUB


def net_due(last_try: float, now: float, every: float = NET_EVERY) -> bool:
    """Time to look for the Kindle on Wi-Fi again (also when the clock went backwards)."""
    return now - last_try >= every or now < last_try


class Superseded(Exception):
    """This worker no longer holds its job (cancelled, removed or re-queued meanwhile: jobstate.py)."""


Cancelled = Superseded   # the fetchers' name for it


class NeedsConfirm(Exception):
    pass


def notify(title: str, body: str = "", image: str = "", exec_cmd: list[str] | None = None, timeout_ms: int = 6000) -> None:
    cmd = ["omarchy-notification-send", "--app-name", "Kindle Shelf", "-g", GLYPH, "-u", "low", "-t", str(timeout_ms)]
    if image and os.path.exists(image):
        cmd += ["--image", image]
    cmd += [title, body]
    if exec_cmd:
        cmd += ["--exec", *exec_cmd]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class Shelf:
    def __init__(self) -> None:
        self.cfg = C.load()
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.dirty = threading.Event()
        self.jobs: list[dict] = self._load_jobs()
        self.worker = threading.local()                # .gen: the generation the current worker thread holds
        self.files_lock = threading.RLock()             # archiving a sent job, Keep, remove, the janitor: one at a time
        self.work_dir = C.WORK_DIR
        self.pool = ThreadPoolExecutor(self.cfg.max_parallel, thread_name_prefix="job")
        self.device_lock = threading.Lock()
        self.kindles: dict[str, kindle.Kindle] = {}     # serial -> prepared Kindle (USB)
        self.net: dict[str, kindle.Kindle] = {}         # dest -> Kindle answering over Wi-Fi
        self.net_tried: dict[str, float] = {}           # dest -> when we last looked for it
        self.preparing: set[str] = set()
        self.attach_at: dict[str, float] = {}
        self.attach_notified: dict[str, bool] = {}
        self.copying = False
        self.last_error = ""
        for d in (C.WORK_DIR, C.COVER_DIR, C.RUNTIME_DIR, C.STATE_DIR):
            d.mkdir(parents=True, exist_ok=True)
        # Anything that was mid-flight when the daemon stopped starts over (with a new generation each).
        for j in self.jobs:
            if j["phase"] in ACTIVE:
                j["phase"], j["detail"] = "queued", "Restarted"
        # a stop in the middle of archiving a sent job: finish it (where each file goes was saved before anything
        # moved), before any worker starts and could choose one of those names (review F19)
        for j in self.jobs:
            if j.get("archive"):
                self._finish_archive(j)
        for j in self.jobs:
            if j["phase"] == "queued":
                self._submit(j["id"], jobstate.requeue(self.jobs, j, ("queued",)))

    # ---- persistence ---------------------------------------------------------------
    def _load_jobs(self) -> list[dict]:
        try:
            return json.loads(C.JOBS_PATH.read_text()).get("jobs", [])
        except (OSError, ValueError):
            return []

    def save(self) -> None:
        # Jobs run in parallel and all save here; one lock and a per-thread temp name keep
        # two saves from racing on the same temp file (that crashed a 3-book Send).
        with self.save_lock:
            with self.lock:
                data = json.dumps({"jobs": self.jobs}, indent=1)
            tmp = C.JOBS_PATH.with_name(f".jobs.{threading.get_ident()}.tmp")
            tmp.write_text(data)
            os.replace(tmp, C.JOBS_PATH)
        self.dirty.set()

    def job(self, jid: str) -> dict | None:
        return next((j for j in self.jobs if j["id"] == jid), None)

    def update(self, j: dict, **kw) -> None:
        """Change a job and save. From a worker (it carries its generation, see jobstate.py) the change lands only while
        the worker still holds the job; otherwise the worker stops right here (Superseded)."""
        gen = getattr(self.worker, "gen", None)
        with self.lock:
            if gen is None:
                j.update(kw)
            elif not jobstate.advance(self.jobs, j, gen, **kw):
                raise Superseded()
            j["updated"] = time.time()
        self.save()

    def _submit(self, jid: str, gen: int | None) -> None:
        if gen is not None:
            self.pool.submit(self._run, jid, gen)

    def _requeue(self, j: dict, from_phases, **kw) -> bool:
        """Back to the queue from `from_phases` with a new generation and a worker for it, in one checked step: a book
        cancelled or removed meanwhile stays cancelled or removed. (save() runs after the lock: it takes save_lock.)"""
        with self.lock:
            gen = jobstate.requeue(self.jobs, j, from_phases, **kw)
            if gen is not None:
                j["updated"] = time.time()
        if gen is None:
            return False
        self.save()
        self._submit(j["id"], gen)
        return True

    def _requeue_waiting(self, j: dict, **kw) -> bool:
        return self._requeue(j, ("waiting",), **kw)

    # ---- state for the card ------------------------------------------------------------
    def dest_name(self, dest: str) -> str:
        if dest == LOCAL:
            return "This computer"
        k = self.cfg.kindle_by_id(dest)
        if k:
            return k.name
        e = self.cfg.email_by_id(dest)
        return e.name if e else dest

    def dest_type(self, dest: str) -> str:
        """"email", "local" (this computer) or "usb" (a Kindle, over USB or Wi-Fi); "" for a destination not set up."""
        if dest == LOCAL:
            return "local"
        if self.cfg.email_by_id(dest):
            return "email"
        return "usb" if self.cfg.kindle_by_id(dest) else ""

    def connected_dest(self, serial: str) -> str:
        k = self.cfg.kindle_by_serial(serial)
        return k.id if k else ""

    def kindle_for_dest(self, dest: str) -> kindle.Kindle | None:
        """The Kindle for `dest` plugged in over USB and ready, if it is."""
        kd = self.cfg.kindle_by_id(dest)
        if not kd or not kd.serial:
            return None
        k = self.kindles.get(kd.serial)
        return k if k and k.ready else None

    def network_dest(self, dest: str) -> C.KindleDest | None:
        """`dest` if it can get books over Wi-Fi: ssh hosts in its config entry and its serial known."""
        kd = self.cfg.kindle_by_id(dest)
        return kd if kd and kd.ssh and kd.serial else None

    def reach(self, dest: str) -> kindle.Kindle | None:
        """Look for `dest` on Wi-Fi now. Only the Kindle with the serial bound to it counts."""
        kd = self.network_dest(dest)
        if not kd:
            return None
        self.net_tried[dest] = time.time()
        k = kindle.probe(kd.ssh)
        now_kd = self.network_dest(dest)   # the binding as it is NOW (a reload may have changed it during the probe)
        if k and (not now_kd or k.serial != now_kd.serial):
            log.warning("%s answers with serial %s, not %s's %s: not sending there", k.mount, k.serial, dest,
                        now_kd.serial if now_kd else "(none)")
            k = None
        if k:
            self.net[dest] = k
        else:
            self.net.pop(dest, None)
        self.dirty.set()
        return k

    def _cached_net(self, dest: str) -> kindle.Kindle | None:
        """The Kindle found on Wi-Fi in the last 30 s, if it's still the one bound to `dest`."""
        kd = self.network_dest(dest)
        k = self.net.get(dest)
        if not k or not kd or k.serial != kd.serial or time.time() - self.net_tried.get(dest, 0) >= 30:
            return None
        return k

    def _forget_network(self) -> None:
        """Bindings or hosts may have changed: nothing found on Wi-Fi before counts any more."""
        self.net.clear()
        self.net_tried.clear()

    def _waiting_detail(self, dest: str) -> str:
        if self.network_dest(dest):
            return f"In the outbox · waiting for {self.dest_name(dest)} (asleep or off Wi-Fi)"
        return f"In the outbox · waiting for {self.dest_name(dest)}"

    def build_state(self) -> dict:
        now = time.time()
        with self.lock:
            jobs = [dict(j) for j in self.jobs]
        connected = []
        for serial, k in list(self.kindles.items()):
            dest = self.connected_dest(serial)
            connected.append({
                "serial": serial, "dest": dest, "name": self.dest_name(dest) if dest else "Unknown Kindle",
                "kind": k.kind, "ready": k.ready, "parked": k.parked, "free": k.free, "free_h": human(k.free), "total": k.total,
                "known": bool(dest), "guess": self._guess_dest(k) if not dest else "",
            })
        for serial in self.preparing:
            if serial not in self.kindles:
                connected.append({"serial": serial, "dest": self.connected_dest(serial), "name": "Kindle", "kind": "", "ready": False,
                                  "free": 0, "free_h": "", "total": 0, "known": True, "guess": ""})
        dests = []
        for k in self.cfg.kindles:
            waiting = [j for j in jobs if j["dest"] == k.id and j["phase"] == "waiting"]
            dests.append({"id": k.id, "name": k.name, "type": "usb", "bound": bool(k.serial), "format": k.format,
                          "connected": self.kindle_for_dest(k.id) is not None, "waiting": len(waiting),
                          "network": self.network_dest(k.id) is not None, "reachable": k.id in self.net})
        if self.cfg.email_ready:
            for e in self.cfg.emails:
                dests.append({"id": e.id, "name": e.name, "type": "email", "bound": True, "connected": True, "waiting": 0})
        # last, so the card's default stays a Kindle
        dests.append({"id": LOCAL, "name": self.dest_name(LOCAL), "type": "local", "bound": True, "connected": True, "waiting": 0})
        for j in jobs:
            j["dest_name"] = self.dest_name(j["dest"])
            if j.get("delete_after") and not j.get("keep") and not j.get("files_deleted"):
                j["delete_in_days"] = max(0, round((j["delete_after"] - now) / DAY, 1))
        active = [j for j in jobs if j["phase"] in ACTIVE]
        return {
            "running": True,
            "updated": now,
            "kindles": connected,
            "dests": dests,
            "jobs": jobs,
            "active_count": len(active),
            "attention_count": len([j for j in jobs if j["phase"] in ("confirm", "error")]),
            "waiting_ready": sum(d["waiting"] for d in dests if d["connected"]),
            "copying": self.copying,
            "email_ready": self.cfg.email_ready,
            "books_dir": str(self.cfg.books_dir),
            "error": self.last_error,
        }

    def _guess_dest(self, k: kindle.Kindle) -> str:
        # Older Kindles such as the 2012 Paperwhite are USB drives, newer ones MTP; the default config's "mine" is the
        # newer one.
        unbound = [d for d in self.cfg.kindles if not d.serial]
        want = "second" if k.kind == "msc" else "mine"
        if any(d.id == want for d in unbound):
            return want
        return unbound[0].id if unbound else ""

    def state_writer(self) -> None:
        while True:
            self.dirty.wait(timeout=5)
            self.dirty.clear()
            try:
                data = json.dumps(self.build_state())
                tmp = C.STATE_PATH.with_name(".state.writer.tmp")
                tmp.write_text(data)
                os.replace(tmp, C.STATE_PATH)
            except Exception:
                log.exception("state write failed")
            time.sleep(0.25)

    # ---- commands ------------------------------------------------------------------------
    def cmd_send(self, work: dict, copy: dict, dest: str, keep: bool = False) -> dict:
        if not self.dest_type(dest):
            return {"ok": False, "error": f"unknown destination {dest}"}
        authors = work.get("authors") or ([work["author"]] if work.get("author") else [])
        j = {
            "id": uuid.uuid4().hex[:10],
            "created": time.time(), "updated": time.time(),
            "title": text.main_title(work.get("title", "")) if len(work.get("title", "")) > 80 else work.get("title", ""),
            "authors": authors, "author": ", ".join(authors[:2]),
            "year": work.get("year") or 0, "cover_url": work.get("cover_url", ""), "cover_id": work.get("cover_id") or 0,
            "work_key": work.get("key", ""),
            "copy": copy, "dest": dest, "dest_type": self.dest_type(dest),
            "keep": bool(keep) or dest == LOCAL, "auto_copy": self._sends_now(dest),
            "phase": "queued", "progress": 0.0, "detail": "Waiting for a free slot", "error": "",
            "files": {}, "confirm": None, "overwrite": None, "accept_mismatch": False, "gen": 1,
        }
        with self.lock:
            self.jobs.insert(0, j)
        self.save()
        self._submit(j["id"], 1)
        return {"ok": True, "id": j["id"]}

    def _sends_now(self, dest: str) -> bool:
        """Send copies as soon as the book is ready: when the Kindle is plugged in, or always for one on Wi-Fi."""
        return self.kindle_for_dest(dest) is not None or self.network_dest(dest) is not None

    def cmd_send_files(self, paths: list[str], dest: str = "", keep: bool = False, work: dict | None = None) -> dict:
        """Books already on this computer: each goes through the same steps as a download (cover check, the
        Kindle's format, its folder) and is copied the same way. The files themselves are left where they are.
        With `work` (the book picked in the card, its own copy fetched by hand) the job keeps that book's title,
        author and cover instead of the file's."""
        dest = dest or self._default_dest()
        if not self.dest_type(dest):
            return {"ok": False, "error": f"unknown destination {dest or '(no Kindle set up)'}"}
        ids, errors = [], []
        for raw in paths:
            p = Path(raw).expanduser().resolve()
            ext = ext_of(p.name)
            if not p.is_file():
                errors.append(f"{raw}: no such file")
                continue
            if ext not in convert.EBOOK_EXT:
                errors.append(f"{p.name}: not a book file")
                continue
            # The job reads the file's own title and author when it starts (that and the copy can take a while; the
            # command answers straight away). Until then: what the name suggests.
            title, authors = text.name_parts(p.stem)
            w = work or {}
            if w.get("key"):
                title = w.get("title") or title
                authors = w.get("authors") or ([w["author"]] if w.get("author") else authors)
            jid = uuid.uuid4().hex[:10]
            j = {
                "id": jid, "created": time.time(), "updated": time.time(),
                "title": title, "authors": authors, "author": ", ".join(authors[:2]),
                "year": w.get("year") or 0, "cover_url": w.get("cover_url", ""), "cover_id": w.get("cover_id") or 0,
                "work_key": w.get("key") or "file:" + str(p),
                "copy": {"source": "file", "path": str(p), "format": ext, "file_name": p.name, "size": p.stat().st_size},
                "dest": dest, "dest_type": self.dest_type(dest),
                "keep": bool(keep) or dest == LOCAL, "auto_copy": self._sends_now(dest),
                "phase": "queued", "progress": 0.0, "detail": "Waiting for a free slot", "error": "",
                "files": {}, "confirm": None, "overwrite": None, "accept_mismatch": True, "gen": 1,
            }
            with self.lock:
                self.jobs.insert(0, j)
            self.save()
            self._submit(jid, 1)
            ids.append(jid)
        return {"ok": bool(ids), "ids": ids, "dest": dest, "errors": errors, **({"error": "; ".join(errors)} if errors and not ids else {})}

    def _default_dest(self) -> str:
        """The Kindle a file goes to when none is named: one plugged in, else one on Wi-Fi, else the first."""
        for kinds in (self.kindle_for_dest, self.network_dest):
            for d in self.cfg.kindles:
                if kinds(d.id):
                    return d.id
        return self.cfg.kindles[0].id if self.cfg.kindles else ""

    def cmd_cancel(self, jid: str) -> dict:
        j = self.job(jid)
        if not j:
            return {"ok": False, "error": "no such job"}
        with self.lock:   # one step: nobody can requeue it between the check and the change (jobstate.cancel)
            if jobstate.cancel(self.jobs, j):
                j["updated"] = time.time()
                self._discard_files(j)
            else:
                return {"ok": True}
        self.save()
        return {"ok": True}

    def cmd_retry(self, jid: str) -> dict:
        j = self.job(jid)
        if not j or not self._requeue(j, ("error", "cancelled"), detail="Waiting for a free slot", error="", progress=0,
                                      auto_copy=self._sends_now(j["dest"])):
            return {"ok": False, "error": "nothing to retry"}
        return {"ok": True}

    def cmd_confirm(self, jid: str, choice: str) -> dict:
        """An answer counts only for what it was asked about (review F28): "send anyway" for the file that was checked,
        "replace" / "keep both" for that name in that folder on the Kindle that had it."""
        j = self.job(jid)
        if not j or j["phase"] != "confirm":
            return {"ok": False, "error": "nothing to confirm"}
        ask = j.get("confirm") or {}
        kind = ask.get("kind")
        if choice == "cancel":
            return self.cmd_cancel(jid)
        if kind == "mismatch":
            moved = self._requeue(j, ("confirm",), accept_mismatch=True, accept_mismatch_for=ask.get("file"), confirm=None,
                                  detail="Continuing")
        elif kind == "exists":
            if choice not in ("replace", "both"):
                return {"ok": False, "error": "it's already on the Kindle: replace or both?"}
            moved = self._requeue(j, ("confirm",), overwrite=choice,
                                  overwrite_for=[ask.get("serial"), ask.get("folder"), ask.get("name")], confirm=None,
                                  detail="Continuing", auto_copy=True)
        else:
            moved = False
        if not moved:
            return {"ok": False, "error": "nothing to confirm"}
        return {"ok": True}

    def cmd_remove(self, jid: str) -> dict:
        j = self.job(jid)
        # checked and done in one step: a watcher may be requeueing it right now; and never while Keep or the janitor
        # moves its files (files_lock, taken before the lock as everywhere)
        with self.files_lock, self.lock:
            if not j or not jobstate.removable(j) or not jobstate.listed(self.jobs, j):
                return {"ok": False, "error": "can't remove a book that's still working"}
            self.jobs.remove(j)
            # its temporary folders go; for a sent book that's only a copy that couldn't be archived (review F24), which
            # nothing would find again. Its copies in Sent/ and ~/Books/ stay.
            self._discard_files(j)
        self.save()
        return {"ok": True}

    def cmd_keep(self, jid: str, on: bool) -> dict:
        j = self.job(jid)
        if not j:
            return {"ok": False, "error": "no such job"}
        if j.get("dest") == LOCAL and not on:   # kept is what it was for; Sent/ would delete it in a week
            return {"ok": False, "error": "a book saved on this computer stays in ~/Books; delete it there if you don't want it"}
        with self.files_lock:   # never while its archiving moves the same files (review F16)
            return self._keep(j, on)

    def _keep(self, j: dict, on: bool) -> dict:
        """Keep on: a sent book's files go to ~/Books/<Author>/; off: back to Sent/, where the countdown applies again.
        The same write-ahead journal as archiving (review F23): where each file goes is saved before anything moves."""
        if j["phase"] == "sent" and not j.get("files_deleted") and not j.get("archive"):
            with self.lock:
                j["keep"] = on
                plan = self._archive_plan(j)
                j["archive"] = {key: [str(a), str(b)] for key, (a, b) in plan.items()}
                if not on:
                    j["delete_after"] = max(j.get("delete_after") or 0, time.time() + DAY)
                j["updated"] = time.time()
            self.save()
            self._finish_archive(j)
        self.update(j, keep=on)
        return {"ok": True}

    def cmd_deliver(self, dest: str = "") -> dict:
        """Send what's waiting: to a plugged-in Kindle now, and to one on Wi-Fi from a background thread. Probing its
        hosts can take as long as the CLI waits for an answer (review F8's class: a command never does the slow part
        itself); the card shows the books moving as they go."""
        dests = [dest] if dest else [d.id for d in self.cfg.kindles]
        n, looking = 0, []
        for d in dests:
            if self.kindle_for_dest(d):
                n += self._requeue_dest(d)
            elif self.network_dest(d):
                looking.append(d)
                threading.Thread(target=self._deliver_over_wifi, args=(d,), daemon=True, name=f"deliver-{d}").start()
        return {"ok": True, "count": n, "looking": looking}

    def _requeue_dest(self, d: str) -> int:
        with self.lock:
            waiting = [j for j in self.jobs if j["dest"] == d and j["phase"] == "waiting"]
        return sum(self._requeue_waiting(j, auto_copy=True, detail="Sending") for j in waiting)

    def _deliver_over_wifi(self, d: str) -> None:
        try:
            if self.reach(d):
                self._requeue_dest(d)
        except Exception:
            log.exception("deliver over Wi-Fi")

    def cmd_name_kindle(self, serial: str, dest: str) -> dict:
        if not self.cfg.kindle_by_id(dest):
            return {"ok": False, "error": f"unknown destination {dest}"}
        C.set_kindle_serial(dest, serial)
        cfg = C.load()
        with self.lock:   # swapped in one step with _start_copy's last check (review F17)
            self.cfg = cfg
            self._forget_network()
        self.dirty.set()
        self._on_known_kindle(serial)
        return {"ok": True}

    def cmd_eject(self) -> dict:
        # the device lock, not just the copying flag: an eject between a copy's last check and its start would pull
        # the drive from under it (review F17's lead); a long copy keeps it, so don't wait for it here
        if not self.device_lock.acquire(timeout=1):
            return {"ok": False, "error": "still copying"}
        try:
            errs = []
            for k in list(self.kindles.values()):
                e = kindle.eject(k)
                if e:
                    errs.append(e)
        finally:
            self.device_lock.release()
        return {"ok": not errs, "error": "; ".join(errs)}

    def cmd_reload(self) -> dict:
        cfg = C.load()
        with self.lock:   # swapped in one step with _start_copy's last check (review F17)
            before = {d.id: d.serial for d in self.cfg.kindles}
            self.cfg = cfg
            self._forget_network()
        self.dirty.set()
        # a destination now bound to a Kindle that's already plugged in: its waiting books are woken as when it's
        # plugged in or named (review C3's sibling)
        for d in cfg.kindles:
            k = self.kindles.get(d.serial) if d.serial and before.get(d.id) != d.serial else None
            if k and k.ready:
                self._on_known_kindle(d.serial)
        return {"ok": True}

    def handle(self, req: dict) -> dict:
        cmd, a = req.get("cmd"), req.get("args") or {}
        try:
            if cmd == "send":
                return self.cmd_send(a["work"], a["copy"], a["dest"], a.get("keep", False))
            if cmd == "send-files":
                return self.cmd_send_files(list(a["paths"]), a.get("dest", ""), a.get("keep", False), a.get("work"))
            if cmd == "cancel":
                return self.cmd_cancel(a["id"])
            if cmd == "retry":
                return self.cmd_retry(a["id"])
            if cmd == "confirm":
                return self.cmd_confirm(a["id"], a.get("choice", "send"))
            if cmd == "remove":
                return self.cmd_remove(a["id"])
            if cmd == "keep":
                return self.cmd_keep(a["id"], bool(a.get("on", True)))
            if cmd == "deliver":
                return self.cmd_deliver(a.get("dest", ""))
            if cmd == "name-kindle":
                return self.cmd_name_kindle(a["serial"], a["dest"])
            if cmd == "eject":
                return self.cmd_eject()
            if cmd == "reload":
                return self.cmd_reload()
            if cmd == "state":
                return {"ok": True, "state": self.build_state()}
            return {"ok": False, "error": f"unknown command {cmd}"}
        except KeyError as e:
            return {"ok": False, "error": f"missing {e}"}
        except Exception as e:
            log.exception("command %s failed", cmd)
            return {"ok": False, "error": str(e)}

    # ---- the job pipeline ------------------------------------------------------------------
    def _check(self, j: dict) -> None:
        """Between steps: stop when this worker no longer holds its job (cancelled, removed or re-queued)."""
        gen = getattr(self.worker, "gen", None)
        if gen is not None:
            with self.lock:
                if not jobstate.owns(self.jobs, j, gen):
                    raise Superseded()

    def _record(self, j: dict, key: str, value: str) -> None:
        """A worker notes one of its files on the job (checked like every worker change)."""
        gen = getattr(self.worker, "gen", None)
        with self.lock:
            if gen is not None and not jobstate.owns(self.jobs, j, gen):
                raise Superseded()
            j.setdefault("files", {})[key] = value
            j["updated"] = time.time()
        self.save()

    def _gen_dirs(self, j: dict, gen: int) -> tuple[Path, Path]:
        """A generation's own folders: its work folder and its outbox folder. Two generations of one job (a cancel
        and a retry while the first is still converting) never write into the same place."""
        return self.work_dir / j["id"] / f"g{gen}", self.cfg.outbox_dir / j["dest"] / f"{j['id']}-g{gen}"

    def _clean_superseded(self, j: dict, gen: int) -> None:
        """What a superseded worker made and nobody uses (the job's files don't point into it) goes."""
        with self.lock:
            used = [Path(v) for v in (j.get("files") or {}).values() if v]
        for d in self._gen_dirs(j, gen):
            if d.exists() and not any(d == u or d in u.parents for u in used):
                shutil.rmtree(d, ignore_errors=True)

    def _clean_if_gone(self, j: dict) -> None:
        """A superseded worker may have written files after a cancel discarded the job's: remove them, unless the job
        was re-queued and a newer worker is using that folder now."""
        with self.lock:
            if not jobstate.listed(self.jobs, j) or j.get("phase") == "cancelled":
                self._discard_files(j)

    def _run(self, jid: str, gen: int) -> None:
        with self.lock:   # claim generation `gen` (jobstate.claim): one worker per generation, never a stale one
            j = self.job(jid)
            if not j or not jobstate.claim(self.jobs, j, gen):
                return
        self.worker.gen = gen
        try:
            work = self._gen_dirs(j, gen)[0]
            work.mkdir(parents=True, exist_ok=True)
            files = j.setdefault("files", {})
            if not files.get("original") or not Path(files["original"]).exists():
                self.update(j, phase="fetching", detail="Starting", progress=0)
                original = self._fetch(j, work)
                self._record(j, "original", str(convert.unpack(original)))
            self._check(j)
            src = Path(files["original"])

            if not files.get("final") or not Path(files["final"]).exists():
                self.update(j, phase="checking", detail="Checking it's the right book", progress=0)
                meta = convert.read_meta(src)
                if not jobstate.mismatch_accepted(j, str(src)) and not self._looks_right(j, meta, src):
                    shown = meta.get("title") or src.name
                    by = ", ".join(meta.get("authors") or []) or "unknown author"
                    self.update(j, phase="confirm", detail="This file doesn't look like the book you picked",
                                confirm={"kind": "mismatch", "found_title": shown, "found_author": by, "file": str(src)})
                    notify("Check this book", f"You picked {j['title']}, but the file says “{shown}” by {by}. Open Kindle Shelf to send it anyway or cancel.")
                    return
                self._check(j)
                cover = work / "cover.jpg"
                convert.extract_cover(src, cover)
                self.update(j, detail="Checking the cover")
                log.info("cover for %s: %s", j["title"], covers.ensure(cover, j["title"], j["authors"] or [], j.get("cover_id") or 0))
                self._record(j, "cover", str(cover) if cover.exists() else "")
                self.update(j, phase="converting", detail="Preparing the file" if j["dest_type"] == "local" else "Preparing the Kindle file")
                self._record(j, "final", str(self._prepare_file(j, src, work)))
            self._check(j)
            self._deliver(j)
        except Superseded:
            self._clean_if_gone(j)
            self._clean_superseded(j, gen)
        except NeedsConfirm:
            pass
        except (TorboxError, convert.ConvertError, kindle.KindleError, requests.RequestException, OSError, smtplib.SMTPException, ValueError) as e:
            log.warning("job %s failed: %s", jid, e)
            try:
                self.update(j, phase="error", error=self._friendly(e), detail="")
                notify("Couldn't get " + j["title"], self._friendly(e))
            except Superseded:   # cancelled meanwhile: that's the outcome, not this error
                self._clean_if_gone(j)
                self._clean_superseded(j, gen)
        except Exception as e:  # never kill the worker silently
            log.error("job %s crashed: %s", jid, traceback.format_exc())
            try:
                self.update(j, phase="error", error=f"unexpected error: {e}", detail="")
            except Superseded:
                self._clean_if_gone(j)
                self._clean_superseded(j, gen)
        finally:
            self.worker.gen = None

    @staticmethod
    def _friendly(e: Exception) -> str:
        if isinstance(e, requests.RequestException):
            return "download failed: " + e.__class__.__name__.replace("Error", " error").strip()
        return str(e)[:220]

    def _looks_right(self, j: dict, meta: dict, path: Path) -> bool:
        name = path.stem + " " + (j["copy"].get("torrent_name") or "")
        t_ok = text.title_coverage(j["title"], meta.get("title", "")) >= 0.6 or text.title_coverage(j["title"], name) >= 0.75
        if not j["authors"]:
            return t_ok
        a_ok = text.author_matches(j["authors"], " ".join(meta.get("authors") or [])) or text.author_matches(j["authors"], name)
        return t_ok and a_ok

    # -- fetching
    def _fetch_file(self, j: dict, work: Path) -> Path:
        """A book from this computer: a copy of it into the job's folder (the original stays where it is), and the
        title and author it carries, when it carries them."""
        src = Path(j["copy"].get("path") or "")
        if not src.is_file():
            raise ValueError(f"{src.name or 'the file'} is no longer where it was sent from")
        self.update(j, phase="fetching", detail="Copying the file")
        local = work / (text.safe_filename(src.stem) + "." + ext_of(src.name))
        shutil.copyfile(src, local)
        if j.get("work_key") and not j["work_key"].startswith("file:"):
            return local                                   # named after the book picked in the card
        meta = convert.read_meta(local)
        title = meta.get("title", "").strip()
        authors = [a.strip(" ;") for a in meta.get("authors") or [] if a.strip(" ;") and a.lower() != "unknown"]   # Anna's: "Name;"
        if title and title.lower() != "unknown":
            self.update(j, title=title)
        if authors:
            self.update(j, authors=authors, author=", ".join(authors[:2]))
        return local

    def _fetch(self, j: dict, work: Path) -> Path:
        """Fetch the file, and remember how the source did so future ratings learn from it."""
        if j["copy"].get("source") == "file":
            return self._fetch_file(j, work)
        tries = [j["copy"]] + list(j["copy"].get("alternates") or [])[:4]
        for n, c in enumerate(tries):
            key = rating.source_key(c)
            started = time.time()
            try:
                if n:
                    self.update(j, copy=dict(c, alternates=j["copy"].get("alternates")), detail=f"First copy failed, trying an identical one ({n}/{len(tries) - 1})")
                path = self._fetch_torrent(j, work) if c["source"] == "torrent" else self._fetch_libgen(j, work)
            except Cancelled:
                raise
            except Exception as e:
                rating.record_outcome(key, False, time.time() - started)
                log.info("job %s: copy %d failed: %s", j["id"], n, e)
                if n == len(tries) - 1:
                    raise
                continue
            rating.record_outcome(key, True, time.time() - started)
            return path
        raise ValueError("no copy could be downloaded")

    def _fetch_torrent(self, j: dict, work: Path) -> Path:
        tb = Torbox(self.cfg.torbox_key)
        c = j["copy"]
        self.update(j, detail="Adding to TorBox")
        tid = tb.add(c["hash"], c.get("torrent_name", ""))
        self.update(j, torbox_id=tid)
        started = time.time()
        while True:
            self._check(j)
            t = tb.get(tid)
            if t.get("download_finished") and t.get("download_present"):
                break
            if time.time() - started > 3 * 3600:
                raise TorboxError("TorBox couldn't fetch this torrent in 3 hours; try another copy")
            prog = float(t.get("progress") or 0)
            state = str(t.get("download_state") or "").replace("_", " ")
            self.update(j, detail=f"TorBox is fetching it · {state} · {prog * 100:.0f}%", progress=prog * 0.5)
            time.sleep(5)
        files = t.get("files") or []
        want = c.get("file_name") or ""
        pick = next((f for f in files if (f.get("short_name") or f.get("name", "").rsplit("/", 1)[-1]) == want), None) if want else None
        if not pick:
            books = [f for f in files if ext_of(f.get("name", "")) in convert.EBOOK_EXT or f.get("name", "").lower().endswith(convert.ARCHIVES)]
            matching = [f for f in books if text.title_coverage(j["title"], f.get("name", "").rsplit("/", 1)[-1]) >= 0.75] or books
            if not matching:
                raise TorboxError("no ebook file in that torrent")
            pick = max(matching, key=lambda f: (convert.FORMAT_RANK.get(ext_of(f.get("name", "")), 0), int(f.get("size") or 0)))
        url = tb.link(tid, int(pick["id"]))
        name = (pick.get("short_name") or pick.get("name", "book").rsplit("/", 1)[-1])
        return self._download(j, url, work / text.safe_filename(name), base=0.5)

    def _fetch_libgen(self, j: dict, work: Path) -> Path:
        c = j["copy"]
        md5 = c["md5"]
        url = ""
        if self.cfg.annas_key:
            self.update(j, detail="Asking Anna's Archive")
            r = requests.get(f"{self.cfg.annas_url}/dyn/api/fast_download.json", params={"md5": md5, "key": self.cfg.annas_key}, headers=UA, timeout=30)
            try:
                url = r.json().get("download_url") or ""
            except ValueError:
                url = ""
        if not url:
            self.update(j, detail="Finding the LibGen download")
            r = requests.get(f"{self.cfg.libgen_url}/ads.php", params={"md5": md5}, headers=UA, timeout=45)
            r.raise_for_status()
            m = re.search(r'href="(get\.php\?md5=[0-9a-fA-F]{32}&(?:amp;)?key=[A-Za-z0-9]+)"', r.text)
            if not m:
                raise ValueError("LibGen has no download for this file right now; try another copy")
            url = f"{self.cfg.libgen_url}/" + m.group(1).replace("&amp;", "&")
        name = text.safe_filename(f"{j['title']} - {j['author'] or 'Unknown'}") + "." + (c.get("format") or "epub")
        return self._download(j, url, work / name, base=0.0)

    def _download(self, j: dict, url: str, dest: Path, base: float) -> Path:
        self.update(j, detail="Downloading", progress=base)
        tmp = dest.with_name(dest.name + ".part")
        with requests.get(url, headers=UA, stream=True, timeout=(20, 120)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0) or int(j["copy"].get("size") or 0)
            got, last = 0, 0.0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    self._check(j)
                    f.write(chunk)
                    got += len(chunk)
                    if time.time() - last > 0.7:
                        last = time.time()
                        frac = min(1.0, got / total) if total else 0
                        self.update(j, detail=f"Downloading {human(got)}" + (f" of {human(total)}" if total else ""), progress=base + (1 - base) * frac * 0.95)
        if got < 1024:
            tmp.unlink(missing_ok=True)
            raise ValueError("the download came back empty")
        if not dest.suffix or ext_of(dest.name) not in convert.EBOOK_EXT | {"zip", "rar", "7z"}:
            dest = dest.with_name(dest.name + "." + (j["copy"].get("format") or "epub"))
        os.replace(tmp, dest)
        return dest


    # -- converting
    def _final_name(self, j: dict, ext: str) -> str:
        return text.safe_filename(f"{j['title']} - {j['author'] or 'Unknown'}") + "." + ext

    def _prepare_file(self, j: dict, src: Path, work: Path) -> Path:
        fmt = ext_of(src.name)
        cover = Path(j["files"]["cover"]) if j["files"].get("cover") else None
        authors = j["authors"] or ["Unknown"]
        # its own folder (per job and generation): two jobs for the same book, or two generations of one job, would
        # otherwise write one "Title - Author" file while the other still copies it (the Kindle's name is final.name)
        out_dir = self._gen_dirs(j, getattr(self.worker, "gen", None) or j.get("gen", 0))[1]
        out_dir.mkdir(parents=True, exist_ok=True)
        if j["dest_type"] == "email":
            # Amazon's email service takes EPUB/PDF/DOCX, not AZW3/MOBI.
            if fmt in ("epub", "pdf", "docx"):
                dst = out_dir / self._final_name(j, fmt)
                shutil.copyfile(src, dst)
                if fmt == "epub":
                    convert.set_meta(dst, j["title"], authors, cover)
                return dst
            return convert.convert(src, out_dir / self._final_name(j, "epub"), j["title"], authors, cover)
        if j["dest_type"] == "local":
            # This computer: EPUB, which every reader outside Amazon's opens; PDFs and other fixed formats as they are.
            if fmt in LOCAL_AS_IS:
                dst = out_dir / self._final_name(j, fmt)
                shutil.copyfile(src, dst)
                return dst
            if fmt == "epub":
                self.update(j, detail="Setting title, author and cover")
                dst = out_dir / self._final_name(j, "epub")
                shutil.copyfile(src, dst)
                convert.set_meta(dst, j["title"], authors, cover)
                return dst
            self.update(j, detail="Converting to EPUB")
            return convert.convert(src, out_dir / self._final_name(j, "epub"), j["title"], authors, cover)
        # USB: the Kindle's format (AZW3, or EPUB for KOReader) for anything reflowable;
        # PDF and locked formats go as they are.
        if fmt in ("pdf", "azw", "kfx", "txt"):
            dst = out_dir / self._final_name(j, fmt)
            shutil.copyfile(src, dst)
            return dst
        kd = self.cfg.kindle_by_id(j["dest"])
        want = kd.format if kd and kd.format in ("azw3", "epub") else "azw3"
        if want == "epub" and fmt == "epub":
            self.update(j, detail="Setting title, author and cover")
            dst = out_dir / self._final_name(j, "epub")
            shutil.copyfile(src, dst)
            convert.set_meta(dst, j["title"], authors, cover)
            return dst
        self.update(j, detail=f"Converting to {want.upper()}" if fmt != want else "Setting title, author and cover")
        return convert.convert(src, out_dir / self._final_name(j, want), j["title"], authors, cover)

    # -- delivering
    def _deliver(self, j: dict) -> None:
        final = Path(j["files"]["final"])
        if j["dest_type"] == "local":
            self._mark_sent(j)   # Keep's archive step: into ~/Books/<Author>/
            at = Path(j["files"].get("final") or final)
            notify("Saved on this computer", f"{j['title']} · {j['author']}. In {str(at.parent).replace(str(Path.home()), '~', 1)}/",
                   j["files"].get("cover", ""), exec_cmd=["xdg-open", str(at.parent)])
            return
        if j["dest_type"] == "email":
            self._email(j, final)
            self._mark_sent(j)
            notify(f"Emailed to {self.dest_name(j['dest'])}", f"{j['title']}. Amazon usually delivers in a few minutes once the Kindle is on Wi-Fi.", j["files"].get("cover", ""))
            return
        dest = j["dest"]
        k = self.kindle_for_dest(dest)
        if not k and j.get("auto_copy") and self.network_dest(dest):
            k = self._cached_net(dest)
            if not k:
                self.update(j, detail=f"Looking for {self.dest_name(dest)} on Wi-Fi")
                k = self.reach(dest)
                self._check(j)   # cancelled during the probe: stays cancelled, never back to "waiting"
        if not k or not j.get("auto_copy"):
            self.update(j, phase="waiting", progress=1.0, detail=self._waiting_detail(dest))
            self._wake_if_plugged_in(dest)
            return
        try:
            self._copy_to(j, k, final)
        except kindle.KindleUnreachable:
            # It went to sleep or out of range mid-way: back to the outbox, the Wi-Fi watcher tries again.
            self.net.pop(dest, None)
            self._check(j)
            self.update(j, phase="waiting", progress=1.0, detail=self._waiting_detail(dest))
            self._wake_if_plugged_in(dest)

    def _wake_if_plugged_in(self, dest: str) -> None:
        """The worker's last step once its book is "waiting": a USB Kindle for `dest` that became ready meanwhile found
        the book still working and woke nothing (review C3). Whichever comes second sees the other: the attach sees
        "waiting", or this sees the Kindle. Books sent while it was plugged in go now; the others are offered."""
        k = self.kindle_for_dest(dest)
        if k:
            self._on_known_kindle(k.serial)

    def _start_copy(self, j: dict, k: kindle.Kindle, folder: str, name: str) -> None:
        """"copying", with where the book goes saved alongside (sending_to), for the next attempt to look at first
        should this one's outcome be lost (review C4)."""
        how = " over Wi-Fi" if k.wifi else ""
        self._publish_look(j, k, phase="copying", detail=f"Copying to {self.dest_name(j['dest'])}{how}", progress=1.0,
                           sending_to=[k.serial, folder, name])

    def _landed_earlier(self, j: dict, k: kindle.Kindle, folder: str, final: Path) -> str:
        """The name an earlier attempt sent this book to on `k`, when exactly this book's bytes are there: that attempt's
        copy made it, but its outcome was lost (Wi-Fi dropped after the Kindle renamed it into place, or a stop during
        the copy). Sending it again would ask about our own copy, or with "keep both" make another (review C4). Only
        the bytes prove it: an old book of the same size that a failed "replace" left is not this one (C5); MTP can't
        tell, so it goes the usual way."""
        earlier = j.get("sending_to") or []
        if len(earlier) != 3 or earlier[:2] != [k.serial, folder]:
            return ""
        return earlier[2] if kindle.same_book(k, earlier[2], folder, final) else ""

    def _publish_look(self, j: dict, k: kindle.Kindle, **changes) -> None:
        """What looking at `k` decided ("copying", or "confirm": already on it), published only while it still holds:
        the worker still holds the job and `k` is still the Kindle its destination sends to. One step under the lock
        that reload and name-kindle take to swap the config (review F17, F27): a rebinding lands before (nothing is
        published, the book goes back to waiting for the newly bound Kindle) or after (the copy that started finishes)."""
        gen = getattr(self.worker, "gen", None)
        with self.lock:
            if gen is not None and not jobstate.owns(self.jobs, j, gen):
                raise Superseded()
            if not self._still_bound(j["dest"], k):
                raise kindle.KindleUnreachable("that Kindle isn't the one this destination sends to any more")
            j.update(changes, updated=time.time())
        self.save()

    def _still_bound(self, dest: str, k: kindle.Kindle) -> bool:
        """`k` is still the Kindle `dest` sends to (a reload or naming may have rebound it meanwhile)."""
        if k.wifi:
            kd = self.network_dest(dest)
            return bool(kd and kd.serial == k.serial)
        return self.kindle_for_dest(dest) is k

    def _copy_to(self, j: dict, k: kindle.Kindle, final: Path) -> None:
        kd = self.cfg.kindle_by_id(j["dest"])
        folder = kd.folder if kd else "documents"
        thumb = None
        if k.kind == "msc" and final.suffix.lower() == ".azw3" and j["files"].get("cover"):
            asin, cdetype = convert.mobi_asin(final)
            tjpg = Path(j["files"]["final"]).with_suffix(".thumb.jpg")
            if asin and cdetype == "EBOK" and convert.kindle_thumbnail(Path(j["files"]["cover"]), tjpg):
                thumb = (asin, tjpg)
        try:
            # Looking at the Kindle and copying are one step under the device lock (review F12, F14, F15): no other
            # job can take the name between the check and the copy, the Kindle is still the one bound to this
            # destination when the copy starts, and a parked USB Kindle is mounted before anything is looked up on it.
            with self.device_lock:
                self._check(j)
                if not self._still_bound(j["dest"], k):
                    raise kindle.KindleUnreachable("that Kindle isn't the one this destination sends to any more")
                if k.kind == "msc" and not (k.mount and os.path.ismount(k.mount)):
                    kindle.prepare(k)   # we unmounted it after the last batch; mount it again
                    if not k.mount:
                        raise kindle.KindleError("couldn't mount the Kindle; unplug it and plug it back in")
                name = self._landed_earlier(j, k, folder, final)
                if name:   # there already: not copied, asked about or numbered again; published only while k is bound
                    self._start_copy(j, k, folder, name)
                else:
                    name = final.name
                    answer = jobstate.overwrite_answer(j, k.serial, folder, name)
                    if answer is None and kindle.exists(k, name, folder):
                        # an answer from `k`: asked about only while `k` is still this destination's Kindle (review F27)
                        self._publish_look(j, k, phase="confirm", detail=f"{name} is already on {self.dest_name(j['dest'])}",
                                           confirm={"kind": "exists", "name": name, "serial": k.serial, "folder": folder})
                        notify("Already on " + self.dest_name(j["dest"]), f"{j['title']} is already on it. Open Kindle Shelf to replace it or keep both.")
                        raise NeedsConfirm()
                    if answer == "both":
                        stem, ext = final.stem, final.suffix
                        n = 2
                        while kindle.exists(k, f"{stem} ({n}){ext}", folder):
                            n += 1
                        name = f"{stem} ({n}){ext}"
                    # the looks at the Kindle took time (seconds over Wi-Fi): a reload or a cancel meanwhile still wins
                    self._start_copy(j, k, folder, name)
                    self.copying = True
                    self.dirty.set()
                    try:
                        kindle.copy(k, final, name, thumb, folder, tag=j["id"])
                    finally:
                        self.copying = False
        finally:
            if thumb:
                thumb[1].unlink(missing_ok=True)
        kindle.prepare(k)  # refresh free space
        self._mark_sent(j, kindle_name=name)
        if k.wifi:
            kindle.show_new_books(k)
            notify(f"On {self.dest_name(j['dest'])}", f"{j['title']} · {j['author']}. Sent over Wi-Fi, it's in the Library.",
                   j["files"].get("cover", ""))
            return
        self._park_when_idle(k, j["dest"])
        notify(f"On {self.dest_name(j['dest'])}", f"{j['title']} · {j['author']}. Safe to unplug.", j["files"].get("cover", ""))

    def _park_when_idle(self, k: kindle.Kindle, dest: str) -> None:
        """Once nothing else is headed for this Kindle, unmount it so pulling the cable
        can't leave its FAT volume marked dirty (it stays in USB mode until unplugged)."""
        with self.lock:
            pending = [x for x in self.jobs if x["dest"] == dest and (x["phase"] in ACTIVE or (x["phase"] == "waiting" and x.get("auto_copy")))]
        if pending or k.kind != "msc":
            return
        with self.device_lock:
            if kindle.park(k):
                log.info("kindle %s unmounted after sending", k.serial)
        self.dirty.set()

    def _email(self, j: dict, path: Path) -> None:
        e = self.cfg.email_by_id(j["dest"])
        if not e or not self.cfg.email_ready:
            raise ValueError("email isn't set up (see ~/.config/kindle-shelf/config.toml)")
        if path.stat().st_size > 48 * 1024 * 1024:
            raise ValueError(f"{human(path.stat().st_size)} is over Amazon's 50 MB email limit; send it over USB instead")
        self.update(j, phase="emailing", detail=f"Emailing to {e.name}")
        s = self.cfg.smtp
        msg = EmailMessage()
        msg["From"] = s.get("from") or s["user"]
        msg["To"] = e.address
        msg["Subject"] = j["title"]
        msg.set_content(f"{j['title']} — {j['author']}")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        if path.suffix.lower() == ".epub":
            maintype, subtype = "application", "epub+zip"
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        with smtplib.SMTP(s["host"], int(s.get("port", 587)), timeout=60) as smtp:
            smtp.starttls()
            smtp.login(s["user"], s["password"])
            smtp.send_message(msg)

    def _mark_sent(self, j: dict, kindle_name: str = "") -> None:
        """Sent, and its files archived (Sent/, or ~/Books/<Author>/ with Keep), as one write-ahead step (review F16):
        "sent" and where each file goes are saved first, the files move, then their new places are saved and the
        job's temporary folders emptied of what it no longer records. Keep, remove and the janitor wait for it
        (files_lock); a stop in the middle is finished at the next start (_finish_archive)."""
        now = time.time()
        gen = getattr(self.worker, "gen", None)
        with self.files_lock:
            with self.lock:
                if gen is not None and not jobstate.owns(self.jobs, j, gen):
                    raise Superseded()
                if j.get("dest_type") == "local" and (j.get("files") or {}).get("final"):
                    # the kept book is the final file; the download it was made from is the same book again, so it
                    # isn't recorded any more and the temp clean-up removes it (now, or after a stop, at the next start)
                    j["files"] = {k: v for k, v in j["files"].items() if k != "original"}
                plan = self._archive_plan(j)
                j.update(phase="sent", progress=1.0, detail="", sent_at=now, kindle_name=kindle_name,
                         delete_after=now + self.cfg.keep_days * DAY, cover_url=j.get("cover_url", ""),
                         archive={key: [str(a), str(b)] for key, (a, b) in plan.items()}, updated=now)
            self.save()
            self._finish_archive(j)

    def _archive_plan(self, j: dict) -> dict:
        """Where a sent job's files go (archive.plan): ~/Books/<Author>/ with Keep, else Sent/ (files_lock is held)."""
        dst_dir = self.cfg.sent_dir
        if j.get("keep"):
            dst_dir = self.cfg.books_dir / text.safe_filename(j["author"] or "Unknown")
            if dst_dir.exists() and not dst_dir.is_dir():   # a file of that name is in the way: Sent/ instead
                log.warning("%s isn't a folder; keeping %s in Sent", dst_dir, j.get("title", j["id"]))
                dst_dir = self.cfg.sent_dir
        # names other jobs' unfinished archives will take (review F19): not free, though nothing is there yet
        taken = {Path(b) for x in self.jobs if x is not j and x.get("archive") for _, b in x["archive"].values()}
        return archive.plan(j.get("files") or {}, dst_dir, bool(j.get("keep")), taken)

    def _finish_archive(self, j: dict) -> None:
        """Carry out the saved plan file by file (archive.settle: a book lands whole or stays where it is), record where
        each file is now, drop the plan, and empty the job's temporary folders of everything it doesn't record. Safe
        to run again after a stop. Never raises (review F20): a file that can't move (a full disk, its folder in the
        way) stays where it is and is recorded there, and from then on that's its laptop copy: the janitor deletes it
        there, Keep moves it from there, removing the row takes it, and no other cleanup touches it (review F24, F25)."""
        with self.files_lock:
            files = dict(j.get("files") or {})
            for key, (src, dst) in (j.get("archive") or {}).items():
                try:
                    at = archive.settle(Path(src), Path(dst), j["id"])
                except Exception:   # settle doesn't raise; a start must never stop here either way
                    log.exception("archiving %s", src)
                    at = Path(src)
                files[key] = str(at) if at else ""
                self._drop_outbox_dir(Path(src))
            try:
                self._prune_temp(j, files)
            except Exception:
                log.exception("cleaning up after %s", j.get("title", j["id"]))
            if files.get("cover") and not Path(files["cover"]).exists():
                files["cover"] = ""
            with self.lock:
                j["files"] = files
                j.pop("archive", None)
                j["updated"] = time.time()
            self.save()

    def _temp_dirs(self, j: dict) -> list[Path]:
        """The job's temporary folders: its work folder and its outbox folders (one per generation)."""
        if not j.get("id"):
            return []
        outs = sorted((self.cfg.outbox_dir / j["dest"]).glob(f"{j['id']}-g*")) if j.get("dest") else []
        return [self.work_dir / j["id"], *outs]

    def _prune_temp(self, j: dict, files: dict) -> None:
        """Empty the job's temporary folders of everything but the book files it records (archive.prune_job)."""
        archive.prune_job(self._temp_dirs(j), [Path(files[k]) for k in archive.BOOK_KEYS if files.get(k)])

    def _drop_outbox_dir(self, p: Path) -> None:
        """Remove a job's own outbox folder once its file has left it."""
        d = p.parent
        if self.cfg.outbox_dir in d.parents and d.parent.parent == self.cfg.outbox_dir:
            try:
                d.rmdir()
            except OSError:
                pass

    def _discard_files(self, j: dict) -> None:
        for d in self._temp_dirs(j):
            archive.prune(d, set())
        final = j.get("files", {}).get("final")
        if final and self.cfg.outbox_dir in Path(final).parents:
            Path(final).unlink(missing_ok=True)
            self._drop_outbox_dir(Path(final))

    # ---- Kindle watching -----------------------------------------------------------------
    def watcher(self) -> None:
        while True:
            try:
                present = {k.serial: k for k in kindle.scan()}
                for serial in list(self.kindles):
                    if serial not in present:
                        log.info("kindle %s unplugged", serial)
                        self.kindles.pop(serial, None)
                        self.attach_notified.pop(serial, None)
                        self.dirty.set()
                for serial, k in present.items():
                    known = self.kindles.get(serial)
                    # New, or attached but never became readable (e.g. it was scanned before its
                    # drive appeared): (re)attach, at most every 15 s for the stuck case.
                    stuck = known is not None and not known.ready and time.time() - self.attach_at.get(serial, 0) > 15
                    if (known is None or stuck) and serial not in self.preparing:
                        self.preparing.add(serial)
                        self.attach_at[serial] = time.time()
                        self.dirty.set()
                        threading.Thread(target=self._attach, args=(k,), daemon=True).start()
            except Exception:
                log.exception("watcher")
            time.sleep(2)

    def _attach(self, k: kindle.Kindle) -> None:
        was_ready = bool(self.kindles.get(k.serial) and self.kindles[k.serial].ready)
        try:
            for _ in range(15):
                # A USB-drive Kindle shows up on the bus a moment before its disk does, so
                # re-read the bus each try: the first scan can mistake it for an MTP Kindle.
                fresh = next((x for x in kindle.scan() if x.serial == k.serial), None)
                if fresh is None:
                    return
                if (fresh.kind, fresh.block) != (k.kind, k.block):
                    k = fresh
                kindle.prepare(k)
                if k.ready:
                    break
                time.sleep(2)
            log.info("kindle %s (%s) ready=%s mount=%s", k.serial, k.kind, k.ready, k.mount or k.storage)
            self.kindles[k.serial] = k
        finally:
            self.preparing.discard(k.serial)
            self.dirty.set()
        if not k.ready:
            if not self.attach_notified.get(k.serial):
                self.attach_notified[k.serial] = True
                notify("Kindle found but not readable yet", "Unlock it so it shows USB drive mode. Kindle Shelf keeps retrying.")
            return
        self.attach_notified.pop(k.serial, None)
        if was_ready:
            return
        if self.cfg.kindle_by_serial(k.serial):
            self._on_known_kindle(k.serial)
        else:
            card = self.cfg.card_command
            notify("New Kindle connected", "Open Kindle Shelf to say whose it is." if card else
                   f"Say whose it is: kindle-shelf name-kindle {k.serial} <kindle id>", exec_cmd=card or None, timeout_ms=10000)

    def _on_known_kindle(self, serial: str) -> None:
        dest = self.connected_dest(serial)
        if not dest:
            return
        # Books queued with Send while it was plugged in (then unplugged) still go automatically.
        with self.lock:
            waiting = [j for j in self.jobs if j["dest"] == dest and j["phase"] == "waiting" and j.get("auto_copy")]
        for j in waiting:
            self._requeue_waiting(j, detail="Sending")
        self._offer_waiting(dest)

    def _offer_waiting(self, dest: str) -> None:
        waiting = [j for j in self.jobs if j["dest"] == dest and j["phase"] == "waiting" and not j.get("auto_copy")]
        if not waiting:
            return
        n = len(waiting)
        body = f"Click to send {waiting[0]['title']}." if n == 1 else f"Click to send {n} books."
        notify(f"{self.dest_name(dest)} connected · {n} book{'s' if n > 1 else ''} waiting", body,
               exec_cmd=[str(Path(__file__).resolve().parent.parent / "bin" / "kindle-shelf"), "deliver", dest], timeout_ms=12000)

    def net_watcher(self) -> None:
        """Books waiting for a Kindle that takes them over Wi-Fi: look for it every NET_EVERY seconds while any
        wait (it answers when it's awake on Wi-Fi with its SSH server running) and send them when it does.
        Nothing is tried while nothing waits; a plugged-in Kindle is left to the USB watcher."""
        while True:
            try:
                for kd in self.cfg.kindles:
                    if not self.network_dest(kd.id) or self.kindle_for_dest(kd.id):
                        continue
                    with self.lock:
                        waiting = [j for j in self.jobs if j["dest"] == kd.id and j["phase"] == "waiting"]
                    if not waiting:
                        if self.net.pop(kd.id, None):
                            self.dirty.set()
                        continue
                    if not net_due(self.net_tried.get(kd.id, 0), time.time()):
                        continue
                    if self.reach(kd.id):
                        # the list is from before the probe: only books still waiting now go (one step each)
                        n = sum(self._requeue_waiting(j, detail="Sending over Wi-Fi", auto_copy=True) for j in waiting)
                        log.info("%s answers over Wi-Fi: sending %d book(s)", kd.id, n)
            except Exception:
                log.exception("net watcher")
            time.sleep(5)

    # ---- cleanup -------------------------------------------------------------------------
    def janitor(self) -> None:
        while True:
            try:
                if self._janitor_pass(time.time()):
                    self.save()
            except Exception:
                log.exception("janitor")
            time.sleep(3600)

    def _janitor_pass(self, now: float) -> bool:
        """Delete laptop copies past their day, forget old rows. Whether a job's files go is decided under files_lock,
        the lock Keep and archiving hold (review F21): a Keep that wins the race is seen, and nothing kept is deleted.
        Only the shelf's own folders are emptied: Sent/, and the outbox and work folders, where a book that couldn't
        be archived stays (review F24); never ~/Books/<Author>/. "Deleted" is said only once none of its files is left."""
        changed = False
        managed = (self.cfg.sent_dir, self.cfg.outbox_dir, self.work_dir)
        for j in list(self.jobs):
            def expired() -> bool:
                return (j["phase"] == "sent" and not j.get("keep") and not j.get("files_deleted")
                        and not j.get("archive") and j.get("delete_after", now + 1) < now)
            if expired():
                with self.files_lock:
                    if expired():
                        files = j.get("files") or {}
                        books = [Path(files[k]) for k in archive.BOOK_KEYS if files.get(k)]
                        for p in books:
                            if p.is_file() and any(m in p.parents for m in managed):
                                try:
                                    p.unlink()
                                except OSError as e:
                                    log.warning("couldn't delete %s: %s", p, e)
                        self._prune_temp(j, files)
                        if not any(p.is_file() for p in books):
                            with self.lock:
                                j["files_deleted"] = True
                            changed = True
            with self.lock:   # eligibility and removal in one step (review F26): a Retry that lands first keeps it
                if jobstate.forget(self.jobs, j, now):
                    changed = True
        return changed


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline()
        try:
            req = json.loads(line)
        except ValueError:
            resp = {"ok": False, "error": "bad request"}
        else:
            resp = self.server.shelf.handle(req)
        self.wfile.write((json.dumps(resp) + "\n").encode())


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    shelf = Shelf()
    C.SOCKET_PATH.unlink(missing_ok=True)
    server = _Server(str(C.SOCKET_PATH), _Handler)
    server.shelf = shelf
    os.chmod(C.SOCKET_PATH, 0o600)
    for target in (shelf.state_writer, shelf.watcher, shelf.net_watcher, shelf.janitor):
        threading.Thread(target=target, daemon=True).start()
    shelf.dirty.set()

    def stop(*_):
        try:
            state = json.loads(C.STATE_PATH.read_text())
            state["running"] = False
            C.STATE_PATH.write_text(json.dumps(state))
        except (OSError, ValueError):
            pass
        C.SOCKET_PATH.unlink(missing_ok=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("kindle-shelfd listening on %s", C.SOCKET_PATH)
    server.serve_forever()
