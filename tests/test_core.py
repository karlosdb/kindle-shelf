import tempfile
import unittest
from pathlib import Path

from kindle_shelf import convert, covers, kindle, text


class TextMatching(unittest.TestCase):
    def test_title_coverage(self):
        self.assertEqual(text.title_coverage("Project Hail Mary", "Project Hail Mary by Andy Weir EPUB"), 1.0)
        self.assertLess(text.title_coverage("Project Hail Mary", "The Martian"), 0.5)
        self.assertEqual(text.title_coverage("Dune: Deluxe Edition", "Frank Herbert - Dune.epub"), 1.0)

    def test_authors(self):
        self.assertTrue(text.author_matches(["Ursula K. Le Guin"], "The Dispossessed — le Guin, Ursula K"))
        self.assertTrue(text.author_matches(["Andy Weir"], "Weir, Andy - Project Hail Mary"))
        self.assertFalse(text.author_matches(["Andy Weir"], "Alison Weird"))

    def test_name_parts(self):
        self.assertEqual(text.name_parts("Frankenstein -- Mary Shelley; -- Penguin Classics, 2003 -- isbn13 9780141439471 -- 0123456789abcdef0123456789abcdef -- Anna’s Archive"),
                         ("Frankenstein", ["Mary Shelley"]))
        self.assertEqual(text.name_parts("Good Omens -- Pratchett, Terry; Gaiman, Neil -- 2006"), ("Good Omens", ["Pratchett, Terry", "Gaiman, Neil"]))
        self.assertEqual(text.name_parts("Some Book - A. Writer"), ("Some Book", ["A. Writer"]))
        self.assertEqual(text.name_parts("Just a name"), ("Just a name", []))

    def test_not_the_book(self):
        self.assertEqual(text.not_the_book("Summary of Project Hail Mary"), "summary")
        self.assertEqual(text.not_the_book("Project Hail Mary: Study Guide"), "study guide")
        self.assertEqual(text.not_the_book("Project Hail Mary"), "")


class UsbDriveCopy(unittest.TestCase):
    def test_copy_with_thumbnail(self):
        with tempfile.TemporaryDirectory() as d:
            mount = Path(d)
            (mount / "documents").mkdir()
            (mount / "system" / "thumbnails").mkdir(parents=True)
            src = mount / "book.azw3"
            src.write_bytes(b"x" * 5000)
            thumb = mount / "t.jpg"
            thumb.write_bytes(b"jpg")
            k = kindle.Kindle(serial="S1", product="Kindle", kind="msc", sysfs="", mount=str(mount))
            self.assertFalse(kindle.exists(k, "Book - Author.azw3"))
            kindle.copy(k, src, "Book - Author.azw3", ("ASIN-1", thumb))
            self.assertTrue(kindle.exists(k, "Book - Author.azw3"))
            self.assertTrue((mount / "system/thumbnails/thumbnail_ASIN-1_EBOK_portrait.jpg").exists())
            self.assertFalse(list((mount / "documents").glob(".part-*")))

    def test_copy_into_configured_folder(self):
        with tempfile.TemporaryDirectory() as d:
            mount = Path(d)
            (mount / "documents").mkdir()
            src = mount / "book.epub"
            src.write_bytes(b"x" * 5000)
            k = kindle.Kindle(serial="S1", product="Kindle", kind="msc", sysfs="", mount=str(mount))
            self.assertFalse(kindle.exists(k, "Book - Author.epub", "documents/Books"))
            kindle.copy(k, src, "Book - Author.epub", folder="documents/Books")
            self.assertTrue(kindle.exists(k, "Book - Author.epub", "documents/Books"))
            self.assertFalse(kindle.exists(k, "Book - Author.epub"))

    def test_parked_kindle_still_counts_as_ready(self):
        k = kindle.Kindle(serial="S1", product="Kindle", kind="msc", sysfs="", mount="", parked=True)
        self.assertTrue(k.ready)
        k.parked = False
        self.assertFalse(k.ready)


def _copy(**kw):
    base = {"source": "torrent", "source_label": "Pirate Bay", "format": "epub", "size": 1_000_000, "seeders": 10,
            "cached": True, "status": "member", "torrent_name": "Book by Author EPUB", "file_name": "Book.epub",
            "pack": False, "pack_count": 0, "flag": "", "lang": ""}
    base.update(kw)
    return base


class Rating(unittest.TestCase):
    def setUp(self):
        from kindle_shelf import config, rating
        self.cfg = config.Config()
        self.rating = rating
        rating.load_stats = lambda: {}

    def test_identical_copies_fold_and_survive_rerating(self):
        copies = [_copy(id="a", hash="a", seeders=300), _copy(id="b", hash="b", size=1_005_000, seeders=20),
                  _copy(id="c", source="libgen", source_label="LibGen", md5="c", cached=False, size=400_000)]
        rated = self.rating.rate(copies, {"pages": 300}, self.cfg)
        self.assertEqual(len(rated), 2)
        top = rated[0]
        self.assertEqual(top["id"], "a")
        self.assertEqual(top["dupes"], 1)
        again = self.rating.rate(rated, {"pages": 300}, self.cfg)   # what `kindle-shelf rate` does
        self.assertEqual([c["id"] for c in again], [c["id"] for c in rated])
        self.assertEqual(again[0]["dupes"], 1)

    def test_signals(self):
        good = _copy(id="g", hash="g", status="vip", torrent_name="Book by Author (retail) EPUB")
        sample = _copy(id="s", hash="s", size=2_000_000, torrent_name="Book - free sample")
        tiny = _copy(id="t", hash="t", size=40_000, cached=False, seeders=2)
        pdf = _copy(id="p", hash="p", format="pdf", size=9_000_000, seeders=500)
        rated = {c["id"]: c for c in self.rating.rate([good, sample, tiny, pdf], {"pages": 400}, self.cfg)}
        self.assertEqual(rated["g"]["grade"], "A")
        self.assertTrue(rated["g"]["recommended"])
        self.assertLess(rated["s"]["score"], rated["g"]["score"] - 30)
        self.assertIn("may be incomplete", rated["t"]["warn"])
        self.assertFalse(rated["p"]["recommended"])

    def test_best_pick_leads_even_if_outscored(self):
        pdf = _copy(id="p", hash="p", format="pdf", size=9_000_000, seeders=5000, status="vip")
        mobi = _copy(id="m", hash="m", format="mobi", size=3_000_000)
        rated = self.rating.rate([pdf, mobi], {}, self.cfg)
        self.assertEqual(rated[0]["id"], "m")


if __name__ == "__main__":
    unittest.main()


class Covers(unittest.TestCase):
    """Telling a cover from a stand-in (blank page, title page, logo) and the plain fallback cover."""

    def _img(self, d, name, size, draw=None, fill=(255, 255, 255)):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", size, fill)
        if draw:
            draw(ImageDraw.Draw(im), im)
        p = Path(d) / name
        im.save(p, "JPEG")
        return p

    def _art(self, dr, im):
        # a busy, cover-like picture: bands of greys and a dark block
        w, h = im.size
        for y in range(0, h, 6):
            dr.rectangle([0, y, w, y + 5], fill=((y * 7) % 256, (y * 3) % 256, (y * 5) % 256))
        dr.rectangle([w // 5, h // 3, 4 * w // 5, 2 * h // 3], fill=(20, 20, 20))

    def test_problem(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(covers.problem(self._img(d, "blank.jpg", (600, 900))), "blank")
            title_page = self._img(d, "title.jpg", (1240, 1752),
                                   lambda dr, im: dr.rectangle([500, 300, 740, 380], fill=(0, 0, 0)))
            self.assertEqual(covers.problem(title_page), "mostly empty, a title page")
            self.assertEqual(covers.problem(self._img(d, "wide.jpg", (900, 600), self._art)), "not cover-shaped")
            self.assertEqual(covers.problem(self._img(d, "tiny.jpg", (100, 150), self._art)), "too small")
            self.assertEqual(covers.problem(self._img(d, "art.jpg", (600, 900), self._art)), "")

    def test_edition(self):
        self.assertEqual(covers.edition("Frankenstein (1818 Edition)"),
                         ("Frankenstein", "1818 Edition"))
        self.assertEqual(covers.edition("Dune: Deluxe Edition"), ("Dune: Deluxe Edition", ""))

    def test_ensure_offline(self):
        orig = covers.online
        covers.online = lambda *a, **k: None
        try:
            with tempfile.TemporaryDirectory() as d:
                real = self._img(d, "cover.jpg", (600, 900), self._art)
                self.assertEqual(covers.ensure(real, "Some Book", ["A. Writer"]), "kept the book's own cover")
                blank = self._img(d, "blank.jpg", (600, 900))
                self.assertEqual(covers.ensure(blank, "A Rather Long Title That Needs Wrapping", ["A. Writer"]),
                                 "made a plain cover (blank)")
                self.assertEqual(covers.problem(blank), "")
                missing = Path(d) / "none.jpg"
                self.assertEqual(covers.ensure(missing, "Short", []), "made a plain cover (none in the file)")
                self.assertTrue(missing.exists())
        finally:
            covers.online = orig



class WifiKindle(unittest.TestCase):
    """The SSH transport: which host, which Kindle, and how a book lands."""

    def setUp(self):
        self._ssh, self._argv, self._usid = kindle._ssh, kindle._ssh_argv, kindle.USID

    def tearDown(self):
        kindle._ssh, kindle._ssh_argv, kindle.USID = self._ssh, self._argv, self._usid

    @staticmethod
    def _done(code, out=""):
        import subprocess
        return subprocess.CompletedProcess("", code, out, "")

    def test_probe_tries_hosts_in_order(self):
        calls = []

        def fake(host, remote, timeout=30, stdin=None, input=None):
            calls.append(host)
            if host == "kindle":
                return self._done(255)  # not on the home network
            return self._done(0, "G000TEST0000ABCD\nfsp 26696848 607644 25714420 2% /mnt/us\n")
        kindle._ssh = fake
        k = kindle.probe(["kindle", "kindle-ts"])
        self.assertEqual(calls, ["kindle", "kindle-ts"])
        self.assertEqual((k.serial, k.kind, k.mount, k.storage), ("G000TEST0000ABCD", "ssh", "kindle-ts", "/mnt/us/"))
        self.assertEqual((k.free, k.total), (25714420 * 1024, 26696848 * 1024))
        self.assertTrue(k.ready and k.wifi)

    def test_probe_reads_the_serial_as_the_paperwhite_sends_it(self):
        # byte for byte what the Paperwhite answers: /proc/usid ends in a NUL, then the echo's newline
        kindle._ssh = lambda host, remote, **kw: self._done(0, "G000TEST0000ABCD\x00\nfsp  26696848  607960  25714104   2% /mnt/us\n")
        k = kindle.probe(["kindle"])
        self.assertEqual((k.serial, k.free), ("G000TEST0000ABCD", 25714104 * 1024))

    def test_probe_wants_a_kindle_serial(self):
        kindle._ssh = lambda host, remote, **kw: self._done(0, "cat: can't open '/proc/usid'\n")
        self.assertIsNone(kindle.probe(["somebox"]))
        kindle._ssh = lambda host, remote, **kw: self._done(255)
        self.assertIsNone(kindle.probe(["kindle", "kindle-ts"]))

    def _local_kindle(self, root, answers="S1"):
        """A "Kindle" right here: its commands run in a local shell, and its /proc/usid says `answers`."""
        kindle._ssh_argv = lambda host: ["sh", "-c"]  # run the "remote" commands right here
        usid = Path(root).parent / "usid"
        usid.write_bytes(answers.encode() + b"\x00")
        kindle.USID = str(usid)
        return kindle.Kindle(serial="S1", product="Kindle", kind="ssh", sysfs="", mount="local", storage=str(root) + "/")

    def test_copy_lands_whole_under_a_temp_name_first(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.epub"
            src.write_bytes(b"epub" * 3000)
            k = self._local_kindle(root / "us")
            name = "Gulliver's Travels (2) - Jonathan Swift.epub"   # quote, brackets, spaces
            seen = []
            real = kindle._ssh
            kindle._ssh = lambda host, remote, **kw: (seen.append(remote), real(host, remote, **kw))[1]
            self.assertFalse(kindle.exists(k, name, "documents/Books"))
            kindle.copy(k, src, name, folder="documents/Books")
            self.assertTrue(kindle.exists(k, name, "documents/Books"))
            books = root / "us" / "documents" / "Books"
            self.assertEqual((books / name).read_bytes(), src.read_bytes())
            self.assertEqual([p.name for p in books.iterdir()], [name])   # no .part- left behind
            copy_cmd = next(c for c in seen if "cat >" in c)
            self.assertLess(copy_cmd.index("cat > "), copy_cmd.index("mv -f "))
            self.assertIn(".part-", copy_cmd.split("mv -f ")[0])

    def test_a_connection_reaching_another_device_writes_nothing(self):      # C1
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.epub"
            src.write_bytes(b"epub" * 3000)
            for answers in ("S2", "S1X"):                     # another Kindle; one whose serial only starts the same
                k = self._local_kindle(root / "us", answers=answers)
                for look in (lambda: kindle.exists(k, "Book.epub", "documents/Books"),
                             lambda: kindle.same_book(k, "Book.epub", "documents/Books", src),
                             lambda: kindle.copy(k, src, "Book.epub", folder="documents/Books", tag="j1")):
                    with self.assertRaises(kindle.KindleUnreachable):
                        look()
                self.assertFalse((root / "us").exists())        # nothing written, not even the folder
            k = self._local_kindle(root / "us", answers="S1")    # the bound one
            kindle.copy(k, src, "Book.epub", folder="documents/Books", tag="j1")
            self.assertTrue(kindle.same_book(k, "Book.epub", "documents/Books", src))

    def test_only_the_same_bytes_are_the_same_book(self):                      # C5, the Kindle side
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.epub"
            src.write_bytes(b"NEW BOOK" * 500)
            for k in (self._local_kindle(root / "us"),
                      kindle.Kindle(serial="S1", product="Kindle", kind="msc", sysfs="", mount=str(root / "us"))):
                books = root / "us" / "documents" / "Books"
                books.mkdir(parents=True, exist_ok=True)
                self.assertFalse(kindle.same_book(k, "Book.epub", "documents/Books", src), k.kind)     # nothing there
                (books / "Book.epub").write_bytes(b"OLD BOOK" * 500)                                  # same size
                self.assertFalse(kindle.same_book(k, "Book.epub", "documents/Books", src), k.kind)
                (books / "Book.epub").write_bytes(src.read_bytes())
                self.assertTrue(kindle.same_book(k, "Book.epub", "documents/Books", src), k.kind)
                (books / "Book.epub").unlink()
            mtp = kindle.Kindle(serial="S1", product="Kindle", kind="mtp", sysfs="", storage="mtp://K/IS/")
            self.assertIsNone(kindle.same_book(mtp, "Book.epub", "documents", src))               # can't tell

    def test_a_file_named_like_a_temp_is_left_alone(self):                   # C2, the Kindle side
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.epub"
            src.write_bytes(b"epub" * 3000)
            for k in (self._local_kindle(root / "us"),
                      kindle.Kindle(serial="S1", product="Kindle", kind="msc", sysfs="", mount=str(root / "us"))):
                books = root / "us" / "documents" / "Books"
                books.mkdir(parents=True, exist_ok=True)
                (books / ".part-Book.epub").write_bytes(b"someone's file")   # the name the temp used to have
                (books / ".part-j1-Book.epub").write_bytes(b"x")              # this job's own, from a stop
                kindle.copy(k, src, "Book.epub", folder="documents/Books", tag="j1")
                self.assertTrue((books / ".part-Book.epub").is_file(), k.kind)
                self.assertEqual((books / ".part-Book.epub").read_bytes(), b"someone's file", k.kind)
                self.assertEqual(sorted(p.name for p in books.iterdir()), [".part-Book.epub", "Book.epub"], k.kind)
                for p in books.iterdir():
                    p.unlink()

    def test_short_copy_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.epub"
            src.write_bytes(b"x" * 5000)
            k = kindle.Kindle(serial="S1", product="Kindle", kind="ssh", sysfs="", mount="kindle", storage="/mnt/us/")
            kindle._ssh = lambda host, remote, **kw: self._done(0, "  12\n")
            with self.assertRaises(kindle.KindleError) as e:
                kindle.copy(k, src, "Book.epub", folder="documents/Books")
            self.assertNotIsInstance(e.exception, kindle.KindleUnreachable)

    def test_dropping_off_wifi_means_try_later(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.epub"
            src.write_bytes(b"x" * 5000)
            k = kindle.Kindle(serial="S1", product="Kindle", kind="ssh", sysfs="", mount="kindle", storage="/mnt/us/")
            seen = []
            kindle._ssh = lambda host, remote, **kw: (seen.append(remote), self._done(255))[1]
            with self.assertRaises(kindle.KindleUnreachable):
                kindle.copy(k, src, "Book.epub", folder="documents/Books", tag="j1")
            self.assertEqual(seen[-1], kindle._only_on("S1") + "rm -f /mnt/us/documents/Books/.part-j1-Book.epub")
            with self.assertRaises(kindle.KindleUnreachable):
                kindle.exists(k, "Book.epub", "documents/Books")

    def test_library_refresh_never_overwrites_a_waiting_command(self):
        seen = {}

        def fake(host, remote, timeout=30, stdin=None, input=None):
            seen.update(remote=remote, input=input)
            return self._done(0)
        kindle._ssh = fake
        kindle.show_new_books(kindle.Kindle(serial="S1", product="Kindle", kind="ssh", sysfs="", mount="kindle", storage="/mnt/us/"))
        self.assertEqual(seen["remote"], kindle._only_on("S1") + "[ -e /tmp/quiet-cmd ] || cat > /tmp/quiet-cmd")
        self.assertTrue(seen["input"].startswith("lua ") and seen["input"].count("\n") == 1)
        seen.clear()
        kindle.show_new_books(kindle.Kindle(serial="S2", product="Kindle", kind="mtp", sysfs=""))
        self.assertEqual(seen, {})

    def test_retry_cadence(self):
        from kindle_shelf.daemon import NET_EVERY, net_due
        self.assertTrue(net_due(0, 1000))
        self.assertFalse(net_due(1000, 1000 + NET_EVERY - 1))
        self.assertTrue(net_due(1000, 1000 + NET_EVERY))
        self.assertTrue(net_due(2000, 1000))   # clock went backwards

    def test_config_hosts(self):
        from kindle_shelf.config import _hosts
        self.assertEqual(_hosts(["kindle", " kindle-ts ", ""]), ["kindle", "kindle-ts"])
        self.assertEqual(_hosts("kindle"), ["kindle"])
        self.assertEqual(_hosts(None), [])
        self.assertEqual(_hosts(3), [])


# ---- review round 1 (Codex, 2026-10-19): each case fails on the code before its fix -------------------------------

import os
import subprocess
import threading
import time

from kindle_shelf import config as C


class NothingIncompleteIsPublished(unittest.TestCase):
    """F1: a book that arrives short never replaces anything on the Kindle, over Wi-Fi or USB."""

    def test_remote_copy_publishes_only_a_whole_book(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "Books"
            (Path(d) / "usid").write_bytes(b"S1\x00")
            usid, kindle.USID = kindle.USID, str(Path(d) / "usid")
            try:
                cmd = kindle.remote_copy(str(folder), "Book - A.epub", 5000, "S1", "j1")
            finally:
                kindle.USID = usid
            r = subprocess.run(["sh", "-c", cmd], input=b"x" * 1200, capture_output=True)   # the stream ended early
            self.assertEqual(r.returncode, kindle.SHORT)
            self.assertEqual(list(folder.iterdir()), [])                                     # no book, no temp file
            r = subprocess.run(["sh", "-c", cmd], input=b"x" * 5000, capture_output=True)
            self.assertEqual((r.returncode, r.stdout.decode().strip()), (0, "5000"))
            self.assertEqual([p.name for p in folder.iterdir()], ["Book - A.epub"])

    def test_a_source_rewritten_mid_copy_leaves_the_old_book_alone(self):
        # the order Codex found: the size is read, then another job rewrites the shared file, then the stream ends early
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.epub"
            src.write_bytes(b"new!" * 1250)
            books = root / "us" / "documents" / "Books"
            books.mkdir(parents=True)
            (books / "Book - A.epub").write_bytes(b"the good copy already there")
            saved = kindle._ssh, kindle._ssh_argv, kindle.USID
            kindle._ssh_argv = lambda host: ["sh", "-c"]
            (root / "usid").write_bytes(b"S1\x00")
            kindle.USID = str(root / "usid")
            real = kindle._ssh

            def racing(host, remote, **kw):
                if "cat >" in remote:
                    os.truncate(src, 1200)
                return real(host, remote, **kw)
            kindle._ssh = racing
            try:
                k = kindle.Kindle(serial="S1", product="Kindle", kind="ssh", sysfs="", mount="local", storage=str(root / "us") + "/")
                with self.assertRaises(kindle.KindleError) as e:
                    kindle.copy(k, src, "Book - A.epub", folder="documents/Books")
                self.assertNotIsInstance(e.exception, kindle.KindleUnreachable)
            finally:
                kindle._ssh, kindle._ssh_argv, kindle.USID = saved
            self.assertEqual((books / "Book - A.epub").read_bytes(), b"the good copy already there")
            self.assertEqual([p.name for p in books.iterdir()], ["Book - A.epub"])

    def test_usb_copy_checks_the_size_before_renaming(self):
        import shutil
        with tempfile.TemporaryDirectory() as d:
            mount = Path(d) / "kindle"
            (mount / "documents").mkdir(parents=True)
            (mount / "documents" / "Book.azw3").write_bytes(b"the good copy")
            src = Path(d) / "Book.azw3"
            src.write_bytes(b"y" * 4000)
            real = shutil.copyfile

            def shrinking(a, b, *args, **kw):
                os.truncate(src, 100)
                return real(a, b, *args, **kw)
            shutil.copyfile = shrinking
            try:
                k = kindle.Kindle(serial="S", product="Kindle", kind="msc", sysfs="", mount=str(mount))
                with self.assertRaises(kindle.KindleError):
                    kindle.copy(k, src, "Book.azw3")
            finally:
                shutil.copyfile = real
            self.assertEqual((mount / "documents" / "Book.azw3").read_bytes(), b"the good copy")
            self.assertEqual(sorted(p.name for p in (mount / "documents").iterdir()), ["Book.azw3"])


def _shelf(books_dir: Path, kindles=None):
    """A Shelf without threads, sockets or the user's config: submissions are recorded, not run."""
    from kindle_shelf import daemon
    sh = daemon.Shelf.__new__(daemon.Shelf)
    sh.cfg = C.Config(books_dir=books_dir, kindles=list(kindles or []))
    sh.lock, sh.save_lock, sh.dirty = threading.RLock(), threading.Lock(), threading.Event()
    sh.device_lock, sh.files_lock, sh.worker = threading.Lock(), threading.RLock(), threading.local()
    sh.work_dir = Path(books_dir) / "work"
    sh.jobs = []
    sh.copying = False
    sh.net, sh.net_tried = {}, {}
    sh.kindles = {}
    sh.submitted = []

    class Pool:
        def submit(self, fn, *a):
            sh.submitted.append(a)   # (jid, gen)
    sh.pool = Pool()
    sh.save = lambda: None
    return sh


class JobsChangeHandsInOneStep(unittest.TestCase):
    """F2: a book cancelled or removed while a watcher probes the Kindle stays so; one worker per job."""

    def test_cancelled_during_the_probe_stays_cancelled(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            j = {"id": "j1", "phase": "waiting", "dest": "mine", "files": {}, "gen": 3}
            sh.jobs.append(j)
            listed = [j]                         # the watcher's list, taken before its probe
            sh.cmd_cancel("j1")                  # the user cancels while the probe runs
            self.assertFalse(sh._requeue_waiting(listed[0], detail="Sending over Wi-Fi", auto_copy=True))
            self.assertEqual((j["phase"], sh.submitted), ("cancelled", []))

    def test_removed_during_the_probe_is_not_resubmitted(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            j = {"id": "j1", "phase": "waiting", "dest": "mine", "files": {}}
            sh.jobs.append(j)
            sh.cmd_remove("j1")
            self.assertFalse(sh._requeue_waiting(j, detail="Sending"))
            self.assertEqual(sh.submitted, [])

    def test_a_generation_is_run_once(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            j = {"id": "j1", "phase": "queued", "dest": "mine", "files": {}, "copy": {}, "gen": 2, "claimed": 2}
            sh.jobs.append(j)                    # a worker holds generation 2
            sh._run("j1", 2)                     # a second submission of that generation must not touch it
            sh._run("j1", 1)                     # nor a stale one
            self.assertEqual(j["phase"], "queued")
            self.assertFalse((Path(d) / "work" / "j1").exists())


class OnlyTheBoundKindle(unittest.TestCase):
    """F3: a Kindle found on Wi-Fi counts only while it's the one bound to the destination."""

    def _kd(self, serial):
        return C.KindleDest("mine", "My Paperwhite", serial, "epub", "documents/Books", ["kindle"])

    def test_rebinding_during_the_probe_wins(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d), [self._kd("SERIALAAAAAA")])
            probe = kindle.probe

            def rebinding(hosts):
                sh.cfg = C.Config(books_dir=Path(d), kindles=[self._kd("SERIALBBBBBB")])   # reload during the probe
                return kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle", storage="/mnt/us/")
            kindle.probe = rebinding
            try:
                self.assertIsNone(sh.reach("mine"))
            finally:
                kindle.probe = probe
            self.assertEqual(sh.net, {})

    def test_a_cached_kindle_must_still_be_the_bound_one(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d), [self._kd("SERIALBBBBBB")])
            sh.net["mine"] = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle")
            sh.net_tried["mine"] = time.time()
            self.assertIsNone(sh._cached_net("mine"))
            sh.net["mine"].serial = "SERIALBBBBBB"
            self.assertIs(sh._cached_net("mine"), sh.net["mine"])

    def test_reload_forgets_what_was_found(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d), [self._kd("SERIALAAAAAA")])
            sh.net["mine"] = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle")
            sh.net_tried["mine"] = time.time()
            load = C.load
            C.load = lambda: C.Config(books_dir=Path(d), kindles=[self._kd("SERIALBBBBBB")])
            try:
                sh.cmd_reload()
            finally:
                C.load = load
            self.assertEqual((sh.net, sh.net_tried), ({}, {}))


class NeverOverwriteTheUsersFiles(unittest.TestCase):
    """F4: keeping a sent book never replaces a file that's already in ~/Books/<Author>/ (or in Sent)."""

    def test_keep_beside_the_users_own_copy(self):
        with tempfile.TemporaryDirectory() as d:
            books = Path(d)
            sh = _shelf(books)
            mine = books / "Writer" / "Book - Writer.epub"
            mine.parent.mkdir(parents=True)
            mine.write_bytes(b"the user's own file")
            final = books / "Outbox" / "mine" / "j1" / "Book - Writer.epub"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"the converted copy")
            j = {"id": "j1", "phase": "sent", "author": "Writer", "keep": False, "files": {"final": str(final)}}
            sh.jobs.append(j)
            sh.cmd_keep("j1", True)
            self.assertEqual(mine.read_bytes(), b"the user's own file")
            self.assertEqual(Path(j["files"]["final"]).name, "Book - Writer (2).epub")
            self.assertEqual(Path(j["files"]["final"]).read_bytes(), b"the converted copy")
            self.assertFalse(final.parent.exists())          # the job's own outbox folder is gone with its file

    def test_free_path(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "A.epub"
            self.assertEqual(archive.free_name(p), p)
            p.write_bytes(b"1")
            (Path(d) / "A (2).epub").write_bytes(b"2")
            self.assertEqual(archive.free_name(p).name, "A (3).epub")
            self.assertEqual(archive.free_name(p, {Path(d) / "A (3).epub"}).name, "A (4).epub")


class BooksFromThisComputer(unittest.TestCase):
    """F5 + F8: `send <file>` answers at once; the job copies the file, and can again after a cancel."""

    def test_send_files_answers_without_reading_or_copying(self):
        from kindle_shelf import convert as conv
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d), [C.KindleDest("mine", "My Paperwhite", "S", "epub", "documents/Books", ["kindle"])])
            book = Path(d) / "Some Book - A. Writer.epub"
            book.write_bytes(b"epub")
            read = conv.read_meta
            conv.read_meta = lambda p: (_ for _ in ()).throw(AssertionError("the command must not read the book"))
            try:
                r = sh.cmd_send_files([str(book)], "mine")
            finally:
                conv.read_meta = read
            self.assertTrue(r["ok"])
            j = sh.jobs[0]
            self.assertEqual((j["title"], j["authors"], j["files"]), ("Some Book", ["A. Writer"], {}))
            self.assertEqual(j["copy"]["path"], str(book.resolve()))
            self.assertEqual(len(sh.submitted), 1)

    def test_the_job_fetches_from_the_path_again_after_a_cancel(self):
        from kindle_shelf import convert as conv
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            book = Path(d) / "Some Book - A. Writer.epub"
            book.write_bytes(b"the book")
            j = {"id": "j1", "phase": "fetching", "title": "Some Book", "authors": [], "author": "",
                 "copy": {"source": "file", "path": str(book)}, "files": {}}
            sh.jobs.append(j)
            read = conv.read_meta
            conv.read_meta = lambda p: {"title": "Some Book: The Real Title", "authors": ["Ann Writer"]}
            try:
                work = Path(d) / "work1"
                work.mkdir()
                got = sh._fetch(j, work)
                self.assertEqual(got.read_bytes(), b"the book")
                self.assertEqual((j["title"], j["author"]), ("Some Book: The Real Title", "Ann Writer"))
                import shutil
                shutil.rmtree(work)                       # a cancel discards the job's files
                work.mkdir()
                self.assertEqual(sh._fetch(j, work).read_bytes(), b"the book")   # retry: from the same path
            finally:
                conv.read_meta = read
            self.assertTrue(book.exists())                # the original never moves

    def test_a_file_for_the_picked_book_keeps_that_books_name(self):
        from kindle_shelf import convert as conv
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d), [C.KindleDest("mine", "My Paperwhite", "S", "epub", "documents/Books", ["kindle"])])
            book = Path(d) / "frankenstein -- shelley -- 0123.epub"
            book.write_bytes(b"epub")
            work = {"key": "/works/OL1W", "title": "Frankenstein", "authors": ["Mary Shelley"], "year": 1818,
                    "cover_url": "/c/1.jpg", "cover_id": 1}
            r = sh.cmd_send_files([str(book)], "mine", work=work)
            self.assertTrue(r["ok"])
            j = sh.jobs[0]
            self.assertEqual((j["title"], j["author"], j["work_key"], j["cover_id"]), ("Frankenstein", "Mary Shelley", "/works/OL1W", 1))
            read = conv.read_meta
            conv.read_meta = lambda p: {"title": "frankenstein (retail)", "authors": ["Unknown Uploader"]}
            try:
                work_dir = Path(d) / "work1"
                work_dir.mkdir()
                sh._fetch(j, work_dir)
            finally:
                conv.read_meta = read
            self.assertEqual((j["title"], j["author"]), ("Frankenstein", "Mary Shelley"))
            sh.cmd_send_files([str(book)], "mine")             # without a picked book: keyed by its path
            self.assertEqual(sh.jobs[0]["work_key"], "file:" + str(book.resolve()))

    def test_a_vanished_file_is_a_plain_error(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            j = {"id": "j1", "phase": "fetching", "copy": {"source": "file", "path": str(Path(d) / "gone.epub")}, "files": {}}
            with self.assertRaises(ValueError):
                sh._fetch(j, Path(d))


class KeptOnThisComputer(unittest.TestCase):
    """"This computer": fetched, checked and covered like any book, made an EPUB, kept in ~/Books/<Author>/, sent nowhere."""

    def _job(self, sh, d, fmt="epub"):
        orig = Path(d) / "work" / "j1" / "g1" / f"some download.{fmt}"
        orig.parent.mkdir(parents=True)
        orig.write_bytes(b"the book")
        j = {"id": "j1", "phase": "converting", "gen": 1, "claimed": 1, "dest": "computer", "dest_type": "local",
             "keep": True, "title": "Book", "authors": ["Writer"], "author": "Writer", "detail": "",
             "files": {"original": str(orig), "cover": ""}}
        sh.jobs.append(j)
        return j, orig

    def test_its_a_destination_and_always_kept(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d), [C.KindleDest("mine", "My Paperwhite", "S", "epub")])
            r = sh.cmd_send({"key": "/works/OL1W", "title": "Book", "authors": ["Writer"]}, {"source": "libgen", "md5": "x"}, "computer")
            self.assertTrue(r["ok"])
            self.assertEqual((sh.jobs[0]["dest_type"], sh.jobs[0]["keep"], sh.jobs[0]["auto_copy"]), ("local", True, False))
            book = Path(d) / "Book - Writer.mobi"
            book.write_bytes(b"mobi")
            self.assertTrue(sh.cmd_send_files([str(book)], "computer")["ok"])
            self.assertEqual((sh.jobs[0]["dest"], sh.jobs[0]["keep"]), ("computer", True))
            self.assertFalse(sh.cmd_send({}, {}, "nowhere")["ok"])
            self.assertEqual(sh.dest_name("computer"), "This computer")
            sh.preparing, sh.last_error = set(), ""
            dests = sh.build_state()["dests"]
            self.assertEqual([(x["id"], x["type"]) for x in dests], [("mine", "usb"), ("computer", "local")])   # last

    def test_epub_for_reflowable_books_fixed_formats_as_they_are(self):
        from kindle_shelf import convert as conv
        calls = []
        real = conv.convert, conv.set_meta

        def fake_convert(src, dst, title, authors, cover=None):
            calls.append(("convert", dst.suffix))
            dst.write_bytes(b"converted")
            return dst
        conv.convert, conv.set_meta = fake_convert, lambda path, title, authors, cover=None: calls.append(("meta", path.suffix))
        try:
            for fmt, want_ext, want_calls in (("mobi", ".epub", [("convert", ".epub")]), ("azw3", ".epub", [("convert", ".epub")]),
                                              ("epub", ".epub", [("meta", ".epub")]), ("pdf", ".pdf", [])):
                with tempfile.TemporaryDirectory() as d:
                    sh = _shelf(Path(d))
                    j, orig = self._job(sh, d, fmt)
                    calls.clear()
                    out = sh._prepare_file(j, orig, orig.parent)
                    self.assertEqual((out.suffix, out.name, calls), (want_ext, "Book - Writer" + want_ext, want_calls), fmt)
                    self.assertTrue(orig.exists())
        finally:
            conv.convert, conv.set_meta = real

    def test_only_the_book_is_kept_in_the_authors_folder(self):
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            j, orig = self._job(sh, d)
            final = Path(d) / "Outbox" / "computer" / "j1-g1" / "Book - Writer.epub"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"the book, named")
            j["files"]["final"] = str(final)
            toasts = []
            real = daemon.notify
            daemon.notify = lambda *a, **k: toasts.append((a, k))
            sh.worker.gen = 1
            try:
                sh._deliver(j)
            finally:
                sh.worker.gen = None
                daemon.notify = real
            kept = Path(d) / "Writer" / "Book - Writer.epub"
            self.assertEqual((j["phase"], j["files"].get("final"), "original" in j["files"]), ("sent", str(kept), False))
            self.assertEqual(kept.read_bytes(), b"the book, named")
            self.assertEqual(sorted(x.name for x in (Path(d) / "Writer").iterdir()), ["Book - Writer.epub"])
            self.assertFalse(orig.exists())                                   # the download it was made from
            self.assertFalse(final.parent.exists())                           # the outbox folder
            self.assertEqual(toasts[0][1]["exec_cmd"], ["xdg-open", str(kept.parent)])
            self.assertFalse(sh.cmd_keep("j1", False)["ok"])                  # never to Sent/ and its countdown
            self.assertTrue(j["keep"] and kept.exists())

    def test_a_stop_after_the_plan_still_removes_the_download(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            j, orig = self._job(sh, d)
            final = Path(d) / "Outbox" / "computer" / "j1-g1" / "Book - Writer.epub"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"the book, named")
            j["files"]["final"] = str(final)

            class Stop(Exception):
                pass
            real = sh._finish_archive
            sh._finish_archive = lambda job: (_ for _ in ()).throw(Stop())     # stopped once the plan was saved
            sh.worker.gen = 1
            try:
                with self.assertRaises(Stop):
                    sh._mark_sent(j)
            finally:
                sh.worker.gen = None
                sh._finish_archive = real
            self.assertNotIn("original", j["files"])
            sh._finish_archive(j)                                             # what the next start does
            self.assertEqual(j["files"]["final"], str(Path(d) / "Writer" / "Book - Writer.epub"))
            self.assertFalse(orig.exists())


class EditionBands(unittest.TestCase):
    """F6: a bracketed edition gets its band on its own cover too, and only once."""
    _img, _art = Covers._img, Covers._art

    def test_kept_cover_of_an_edition_gets_its_band_once(self):
        orig = covers.online
        covers.online = lambda *a, **k: None
        try:
            with tempfile.TemporaryDirectory() as d:
                art = self._img(d, "cover.jpg", (600, 900), self._art)
                self.assertFalse(covers.has_band(art))
                self.assertEqual(covers.ensure(art, "A Book (Special Edition)", ["A. Writer"]),
                                 "kept the book's own cover, with the edition band")
                self.assertTrue(covers.has_band(art))
                once = art.read_bytes()
                self.assertEqual(covers.ensure(art, "A Book (Special Edition)", ["A. Writer"]), "kept the book's own cover")
                self.assertEqual(art.read_bytes(), once)
                plain = self._img(d, "plain.jpg", (600, 900), self._art)
                self.assertEqual(covers.ensure(plain, "A Book", ["A. Writer"]), "kept the book's own cover")
                self.assertFalse(covers.has_band(plain))
        finally:
            covers.online = orig


class CalibreFailuresAreSaid(unittest.TestCase):
    """F7: when Calibre refuses to write the cover, fix-covers says so instead of claiming a change."""

    def test_set_cover_raises(self):
        run = convert._run
        convert._run = lambda cmd, timeout=600: subprocess.CompletedProcess(cmd, 1, "", "permission denied\n")
        try:
            with self.assertRaises(convert.ConvertError):
                convert.set_cover(Path("/x.epub"), Path("/c.jpg"))
            with self.assertRaises(convert.ConvertError):
                convert.set_meta(Path("/x.epub"), "T", ["A"])
        finally:
            convert._run = run

    def test_fix_file_reports_the_failure(self):
        saved = convert.read_meta, convert.extract_cover, convert.set_cover, covers.online
        convert.read_meta = lambda p: {"title": "Blank Cover Book", "authors": ["A. Writer"]}
        convert.extract_cover = lambda src, out: False            # no cover in the file: a plain one gets made
        covers.online = lambda *a, **k: None

        def refuse(path, cover):
            raise convert.ConvertError("setting the cover failed: read-only file")
        convert.set_cover = refuse
        try:
            r = covers.fix_file(Path("/books/Blank Cover Book.epub"))
        finally:
            convert.read_meta, convert.extract_cover, convert.set_cover, covers.online = saved
        self.assertFalse(r["changed"])
        self.assertIn("read-only", r["error"])


class CancelWinsOverSlowKindleCalls(unittest.TestCase):
    """F2's class in the job thread: a cancel that lands while delivery waits on the Kindle (probe, exists) wins."""

    def _job(self, sh, final):
        j = {"id": "j1", "phase": "converting", "dest": "mine", "dest_type": "usb", "auto_copy": True, "title": "T",
             "author": "A", "overwrite": None, "files": {"final": str(final)}, "gen": 5, "claimed": 5}
        sh.jobs.append(j)
        sh.worker.gen = 5          # this test thread is the worker holding generation 5
        return j

    def _sh(self, d):
        return _shelf(Path(d), [C.KindleDest("mine", "My Paperwhite", "SERIALAAAAAA", "epub", "documents/Books", ["kindle"])])

    def test_cancel_during_the_probe(self):
        from kindle_shelf.daemon import Cancelled
        with tempfile.TemporaryDirectory() as d:
            sh = self._sh(d)
            final = Path(d) / "Outbox" / "mine" / "j1" / "T - A.epub"
            final.parent.mkdir(parents=True)
            final.write_bytes(b"book")
            j = self._job(sh, final)

            def probe_while_cancelled(dest):
                sh.cmd_cancel("j1")
                return None
            sh.reach = probe_while_cancelled
            with self.assertRaises(Cancelled):
                sh._deliver(j)
            self.assertEqual(j["phase"], "cancelled")
            sh.worker.gen = None

    def test_cancel_during_the_exists_check(self):
        from kindle_shelf.daemon import Cancelled
        with tempfile.TemporaryDirectory() as d:
            sh = self._sh(d)
            final = Path(d) / "T - A.epub"
            final.write_bytes(b"book")
            j = self._job(sh, final)
            k = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle", storage="/mnt/us/")
            saved = kindle.exists, kindle.copy
            copied = []

            def exists_while_cancelled(*a, **kw):
                sh.cmd_cancel("j1")
                return False
            kindle.exists, kindle.copy = exists_while_cancelled, lambda *a, **kw: copied.append(a)
            sh.net["mine"] = k
            try:
                with self.assertRaises(Cancelled):
                    sh._copy_to(j, k, final)
            finally:
                kindle.exists, kindle.copy = saved
                sh.worker.gen = None
            self.assertEqual((copied, j["phase"]), ([], "cancelled"))


class MtpCopiesAreAllOrNothing(unittest.TestCase):
    """F1's class over MTP: gio can't rename, so a failed or short copy is removed instead of left as a book."""

    def _kindle(self):
        return kindle.Kindle(serial="S", product="Kindle", kind="mtp", sysfs="", storage="mtp://K/Internal Storage/")

    def test_short_copy_is_removed(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "B.epub"
            src.write_bytes(b"z" * 3000)
            calls = []

            def fake(cmd, timeout=60):
                calls.append(cmd)
                if cmd[:2] == ["gio", "info"]:
                    return subprocess.CompletedProcess(cmd, 0, "attributes:\n  standard::size: 1000\n", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            run = kindle._run
            kindle._run = fake
            try:
                with self.assertRaises(kindle.KindleError):
                    kindle.copy(self._kindle(), src, "B.epub", folder="documents/Books")
            finally:
                kindle._run = run
            self.assertIn(["gio", "remove", "mtp://K/Internal Storage/documents/Books/B.epub"], calls)

    def _copy_with(self, sizes, copy_code):
        """Run one MTP copy against a fake gio: `sizes` = what `gio info` reports before and after the copy (-1: not
        there), `copy_code` = gio copy's exit status. Returns the gio commands run."""
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "B.epub"
            src.write_bytes(b"z" * 3000)
            calls, infos = [], list(sizes)

            def fake(cmd, timeout=60):
                calls.append(cmd)
                if cmd[:2] == ["gio", "copy"]:
                    return subprocess.CompletedProcess(cmd, copy_code, "", "Operation failed" if copy_code else "")
                if cmd[:2] == ["gio", "info"]:
                    size = infos.pop(0) if infos else -1
                    return (subprocess.CompletedProcess(cmd, 1, "", f"gio: {cmd[-1]}: No such file or directory") if size < 0 else
                            subprocess.CompletedProcess(cmd, 0, f"attributes:\n  standard::size: {size}\n", ""))
                return subprocess.CompletedProcess(cmd, 0, "", "")
            run = kindle._run
            kindle._run = fake
            try:
                try:
                    kindle.copy(self._kindle(), src, "B.epub")
                except kindle.KindleError:
                    pass
            finally:
                kindle._run = run
            return calls

    def test_failed_copy_removes_only_what_it_wrote(self):
        def removed(calls):
            return any(c[:2] == ["gio", "remove"] for c in calls)
        self.assertTrue(removed(self._copy_with([-1, 1500], 1)))          # a new name, half written: removed
        self.assertFalse(removed(self._copy_with([4000, 4000], 1)))       # F13: failed before writing: the old book stays
        self.assertTrue(removed(self._copy_with([4000, 1200], 1)))        # the old book half overwritten: removed
        self.assertFalse(removed(self._copy_with([-1, -1], 1)))           # nothing there to remove
        self.assertFalse(removed(self._copy_with([4000, 3000], 0)))       # a whole replacement is kept


# ---- review round 2 (Codex F9-F15): the jobs' lease, and the Kindle checked and copied in one step -----------------

from kindle_shelf import jobstate as JS


class TheJobsLease(unittest.TestCase):
    """jobstate.py on its own: the orders Codex found, as schedules (each a sequence of calls)."""

    def _job(self, phase="queued", gen=1):
        j = {"id": "j", "phase": phase, "gen": gen}
        return [j], j

    def test_cancel_during_the_watchers_probe(self):
        jobs, j = self._job("waiting", 4)
        self.assertTrue(JS.cancel(jobs, j))                 # while the watcher probes
        self.assertIsNone(JS.requeue(jobs, j, ("waiting",)))
        self.assertEqual(j["phase"], "cancelled")

    def test_confirm_while_the_first_worker_is_still_notifying(self):      # F9
        jobs, j = self._job()
        self.assertTrue(JS.claim(jobs, j, 1))
        self.assertTrue(JS.advance(jobs, j, 1, phase="confirm"))            # worker A asks, still in notify()
        gen = JS.requeue(jobs, j, ("confirm",))                             # the user confirms
        self.assertTrue(JS.claim(jobs, j, gen))                             # worker B runs it, A isn't in the way
        self.assertFalse(JS.advance(jobs, j, 1, phase="error"))             # and A can't touch it any more

    def test_cancel_during_the_metadata_read(self):                         # F10
        jobs, j = self._job()
        JS.claim(jobs, j, 1)
        JS.advance(jobs, j, 1, phase="checking")
        self.assertTrue(JS.cancel(jobs, j))
        self.assertFalse(JS.advance(jobs, j, 1, phase="confirm"))
        self.assertEqual(j["phase"], "cancelled")

    def test_two_submissions_of_one_generation(self):
        jobs, j = self._job()
        self.assertTrue(JS.claim(jobs, j, 1))
        self.assertFalse(JS.claim(jobs, j, 1))
        self.assertFalse(JS.claim(jobs, j, 0))              # nor a stale one

    def test_an_error_after_a_cancel_is_refused(self):
        jobs, j = self._job()
        JS.claim(jobs, j, 1)
        JS.cancel(jobs, j)
        self.assertFalse(JS.advance(jobs, j, 1, phase="error", error="boom"))
        self.assertNotIn("error", j)

    def test_removed_then_a_late_worker(self):
        jobs, j = self._job("queued")
        JS.claim(jobs, j, 1)
        jobs.remove(j)
        self.assertFalse(JS.advance(jobs, j, 1, phase="sent"))
        self.assertIsNone(JS.requeue(jobs, j, ("queued",)))

    def test_retry_after_cancel_gets_a_new_worker_and_the_old_one_stays_out(self):
        jobs, j = self._job()
        JS.claim(jobs, j, 1)
        JS.cancel(jobs, j)
        gen = JS.requeue(jobs, j, ("error", "cancelled"))
        self.assertTrue(JS.claim(jobs, j, gen))
        self.assertFalse(JS.advance(jobs, j, 1, phase="copying"))
        self.assertTrue(JS.advance(jobs, j, gen, phase="copying"))

    def test_only_the_defined_phases(self):
        jobs, j = self._job("sent")
        self.assertIsNone(JS.requeue(jobs, j, ("waiting",)))
        self.assertFalse(JS.cancel(jobs, j))
        self.assertTrue(JS.removable(j))
        jobs, j = self._job("copying")
        self.assertFalse(JS.removable(j))

    def test_an_answer_counts_only_for_what_it_was_about(self):            # F28, F30
        j = {"copy": {"source": "torrent"}, "overwrite": "replace", "overwrite_for": ["A", "documents", "B.epub"],
             "accept_mismatch": True, "accept_mismatch_for": "/w/g1/B.epub"}
        self.assertEqual(JS.overwrite_answer(j, "A", "documents", "B.epub"), "replace")
        for other in (("B", "documents", "B.epub"), ("A", "documents/Books", "B.epub"), ("A", "documents", "C.epub")):
            self.assertIsNone(JS.overwrite_answer(j, *other), other)
        self.assertTrue(JS.mismatch_accepted(j, "/w/g1/B.epub"))
        self.assertFalse(JS.mismatch_accepted(j, "/w/g2/B.epub"))              # what a re-fetch brought
        j["accept_mismatch_for"] = None                                          # F30: an answer that named no file
        self.assertFalse(JS.mismatch_accepted(j, "/w/g1/B.epub"))
        self.assertTrue(JS.mismatch_accepted({"copy": {"source": "file"}}, "/anything.epub"))   # the user's own file

    def test_a_retry_before_the_janitor_forgets_the_row(self):             # F26
        jobs, j = self._job("cancelled", 2)
        j["updated"] = 0
        gen = JS.requeue(jobs, j, ("error", "cancelled"))   # Retry first (the daemon also stamps "updated")
        self.assertFalse(JS.forget(jobs, j, now=3 * 86400))
        self.assertEqual((jobs, j["gen"]), ([j], gen))
        old = {"id": "k", "phase": "cancelled", "updated": 0}
        sent = {"id": "s", "phase": "sent", "updated": 0}
        jobs += [old, sent]
        self.assertTrue(JS.forget(jobs, old, now=3 * 86400))
        self.assertFalse(JS.forget(jobs, sent, now=61 * 86400))         # its laptop copy isn't gone yet
        sent["files_deleted"] = True
        self.assertTrue(JS.forget(jobs, sent, now=61 * 86400))
        self.assertEqual(jobs, [j])


class TheLeaseInTheDaemon(unittest.TestCase):
    """The lease through the daemon's real code: _run, cmd_confirm, cmd_cancel."""

    def test_a_cancel_during_the_metadata_read_stays_cancelled(self):      # F10 through _run
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            orig = Path(d) / "orig.epub"
            orig.write_bytes(b"book")
            j = {"id": "j1", "phase": "queued", "gen": 1, "dest": "mine", "dest_type": "usb", "title": "Picked Title",
                 "authors": ["A"], "author": "A", "copy": {"torrent_name": ""}, "accept_mismatch": False,
                 "files": {"original": str(orig)}}
            sh.jobs.append(j)
            saved = daemon.convert.read_meta, daemon.convert.unpack, daemon.notify
            daemon.notify = lambda *a, **k: None

            def read_while_cancelled(p):
                sh.cmd_cancel("j1")
                return {"title": "Some Other Book", "authors": ["Someone Else"]}
            daemon.convert.read_meta = read_while_cancelled
            try:
                sh._run("j1", 1)
            finally:
                daemon.convert.read_meta, daemon.convert.unpack, daemon.notify = saved
            self.assertEqual(j["phase"], "cancelled")
            self.assertFalse(j.get("confirm"))

    def test_a_confirmed_job_runs_while_its_first_worker_is_finishing(self):   # F9 through cmd_confirm
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            orig, final = Path(d) / "o.epub", Path(d) / "f.epub"
            orig.write_bytes(b"o")
            final.write_bytes(b"f")
            j = {"id": "j1", "phase": "confirm", "gen": 1, "claimed": 1, "dest": "mine", "dest_type": "usb",
                 "confirm": {"kind": "mismatch"}, "files": {"original": str(orig), "final": str(final)}}
            sh.jobs.append(j)                            # worker 1 published "confirm" and is still inside notify()
            self.assertTrue(sh.cmd_confirm("j1", "send")["ok"])
            (jid, gen), = sh.submitted
            delivered = []
            sh._deliver = lambda job: delivered.append(job["id"])
            sh._run(jid, gen)
            self.assertEqual(delivered, ["j1"])


class TheKindleCheckedAndCopiedInOneStep(unittest.TestCase):
    """F12, F14, F15: what _copy_to checks on the Kindle still holds when the copy starts."""

    def _setup(self, d, jobs=1, kd_serial="SERIALAAAAAA"):
        sh = _shelf(Path(d), [C.KindleDest("mine", "My Paperwhite", kd_serial, "epub", "documents/Books", ["kindle"])])
        out = []
        for n in range(jobs):
            final = Path(d) / f"job{n}" / "Book - Writer.epub"
            final.parent.mkdir()
            final.write_bytes(b"book %d" % n)
            j = {"id": f"j{n}", "phase": "converting", "gen": 1, "claimed": 1, "dest": "mine", "dest_type": "usb",
                 "auto_copy": True, "title": "Book", "author": "Writer", "overwrite": None, "keep": False,
                 "files": {"final": str(final)}}
            sh.jobs.append(j)
            out.append((j, final))
        k = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle", storage="/mnt/us/")
        return sh, k, out

    def test_two_jobs_with_one_name_never_overwrite_each_other(self):     # F12
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            sh, k, jobs = self._setup(d, jobs=2)
            on_kindle = set()
            saved = kindle.exists, kindle.copy, kindle.prepare, kindle.show_new_books, daemon.notify
            def slow_exists(kk, name, folder="documents"):
                there = name in on_kindle   # the Kindle answers now; the answer arrives a moment later (Wi-Fi)
                time.sleep(0.05)
                return there
            kindle.exists = slow_exists
            kindle.copy = lambda kk, src, name, thumb=None, folder="documents", **kw: on_kindle.add(name)
            kindle.prepare = lambda kk: kk
            kindle.show_new_books = lambda kk: None
            daemon.notify = lambda *a, **kw: None
            outcomes = {}

            def run(j, final):
                sh.worker.gen = 1
                try:
                    sh._copy_to(j, k, final)
                    outcomes[j["id"]] = j["phase"]
                except daemon.NeedsConfirm:
                    outcomes[j["id"]] = "confirm"
            try:
                ts = [threading.Thread(target=run, args=jf) for jf in jobs]
                [t.start() for t in ts]
                [t.join() for t in ts]
            finally:
                kindle.exists, kindle.copy, kindle.prepare, kindle.show_new_books, daemon.notify = saved
            self.assertEqual(sorted(outcomes.values()), ["confirm", "sent"])

    def test_a_rebinding_during_the_exists_check_stops_the_copy(self):    # F14
        with tempfile.TemporaryDirectory() as d:
            sh, k, [(j, final)] = self._setup(d)
            copied = []

            def exists_while_rebound(kk, name, folder="documents"):
                sh.cfg = C.Config(books_dir=Path(d), kindles=[C.KindleDest("mine", "My Paperwhite", "SERIALBBBBBB",
                                                                          "epub", "documents/Books", ["kindle"])])
                sh._forget_network()
                return False
            saved = kindle.exists, kindle.copy, kindle.prepare, kindle.show_new_books
            kindle.exists, kindle.copy = exists_while_rebound, lambda *a, **kw: copied.append(a)
            kindle.prepare, kindle.show_new_books = (lambda kk: kk), (lambda kk: None)   # never the real Kindle
            sh.worker.gen = 1
            try:
                # the rebinding lands during exists(), after the first bound-check: the one right before copying catches it
                with self.assertRaises(kindle.KindleUnreachable):
                    sh._copy_to(j, k, final)
            finally:
                kindle.exists, kindle.copy, kindle.prepare, kindle.show_new_books = saved
                sh.worker.gen = None
            self.assertEqual(copied, [])

    def test_already_there_on_a_kindle_unbound_meanwhile_asks_nothing(self):   # F27
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            sh, k, [(j, final)] = self._setup(d)
            told = []

            def there_while_rebound(kk, name, folder="documents"):
                sh.cfg = C.Config(books_dir=Path(d), kindles=[C.KindleDest("mine", "My Paperwhite", "SERIALBBBBBB",
                                                                          "epub", "documents/Books", ["kindle"])])
                sh._forget_network()
                return True                     # Kindle A has it; the destination is Kindle B's by now
            saved = kindle.exists, kindle.prepare, daemon.notify
            kindle.exists, kindle.prepare = there_while_rebound, (lambda kk: kk)
            daemon.notify = lambda *a, **kw: told.append(a)
            sh.worker.gen = 1
            outcome = None
            try:
                sh._copy_to(j, k, final)
            except (kindle.KindleUnreachable, daemon.NeedsConfirm) as e:
                outcome = type(e).__name__
            finally:
                kindle.exists, kindle.prepare, daemon.notify = saved
                sh.worker.gen = None
            # back to waiting (_deliver), for Kindle B: no question about Kindle A asked in its name
            self.assertEqual((outcome, j["phase"], j.get("confirm"), told), ("KindleUnreachable", "converting", None, []))

    def _answer_then(self, d, second):
        """Kindle A has the book: asked, "Replace" said about A; then the copy goes to `second` (a Kindle object, after
        whatever rebinding the caller set up). Returns (job, Kindles looked at, Kindles copied to)."""
        from kindle_shelf import daemon
        sh, a, [(j, final)] = self._setup(d)
        looks, copies = [], []
        saved = kindle.exists, kindle.copy, kindle.prepare, kindle.show_new_books, daemon.notify
        kindle.exists = lambda kk, name, folder="documents": (looks.append(kk.serial), True)[1]   # both have it
        kindle.copy = lambda kk, *a, **kw: copies.append(kk.serial)
        kindle.prepare, kindle.show_new_books = (lambda kk: kk), (lambda kk: None)
        daemon.notify = lambda *a, **kw: None
        try:
            sh.worker.gen = 1
            with self.assertRaises(daemon.NeedsConfirm):
                sh._copy_to(j, a, final)
            sh.worker.gen = None
            self.assertFalse(sh.cmd_confirm("j0", "send")["ok"])        # "already there" takes replace or both
            self.assertTrue(sh.cmd_confirm("j0", "replace")["ok"])      # said about A
            k = second(sh, a)
            (jid, gen), = sh.submitted
            with sh.lock:
                self.assertTrue(JS.claim(sh.jobs, j, gen))
            sh.worker.gen = gen
            try:
                sh._copy_to(j, k, final)
            except daemon.NeedsConfirm:
                pass
        finally:
            kindle.exists, kindle.copy, kindle.prepare, kindle.show_new_books, daemon.notify = saved
            sh.worker.gen = None
        return j, looks, copies

    def test_replace_said_about_one_kindle_doesnt_overwrite_the_next(self):   # F28
        with tempfile.TemporaryDirectory() as d:
            def rebound_to_b(sh, a):
                sh.cfg = C.Config(books_dir=Path(d), kindles=[C.KindleDest("mine", "My Paperwhite", "SERIALBBBBBB",
                                                                          "epub", "documents/Books", ["kindle"])])
                sh._forget_network()
                return kindle.Kindle(serial="SERIALBBBBBB", product="Kindle", kind="ssh", sysfs="", mount="kindle-b",
                                     storage="/mnt/us/")
            j, looks, copies = self._answer_then(d, rebound_to_b)
            self.assertEqual((looks, copies), (["SERIALAAAAAA", "SERIALBBBBBB"], []))   # B is asked about first
            self.assertEqual((j["phase"], j["confirm"]["serial"]), ("confirm", "SERIALBBBBBB"))

    def test_replace_still_counts_on_the_kindle_it_was_said_about(self):     # F28, the other side
        with tempfile.TemporaryDirectory() as d:
            j, looks, copies = self._answer_then(d, lambda sh, a: a)
            self.assertEqual((looks, copies, j["phase"]), (["SERIALAAAAAA"], ["SERIALAAAAAA"], "sent"))

    def test_a_parked_usb_kindle_is_mounted_before_it_is_looked_at(self):  # F15
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            sh, _, [(j, final)] = self._setup(d)
            k = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="msc", sysfs="", mount="", parked=True)
            sh.kindles["SERIALAAAAAA"] = k
            order = []
            saved = kindle.exists, kindle.copy, kindle.prepare, daemon.notify
            kindle.prepare = lambda kk: (order.append("mount"), setattr(kk, "mount", d), kk)[2]
            kindle.exists = lambda kk, name, folder="documents": (order.append("exists:" + kk.mount), True)[1]
            kindle.copy = lambda *a, **kw: order.append("copy")
            daemon.notify = lambda *a, **kw: None
            sh.worker.gen = 1
            mounted = os.path.ismount
            os.path.ismount = lambda p: bool(p) and p == d
            try:
                with self.assertRaises(daemon.NeedsConfirm):
                    sh._copy_to(j, k, final)
            finally:
                kindle.exists, kindle.copy, kindle.prepare, daemon.notify = saved
                os.path.ismount = mounted
                sh.worker.gen = None
            self.assertEqual(order[:2], ["mount", "exists:" + d])
            self.assertNotIn("copy", order)


class SentAndKeptNamesAreTakenOnce(unittest.TestCase):
    """F11: two jobs archived at once with one name both keep their file (files_lock spans the plan and the moves)."""

    def test_two_archives_with_one_name(self):
        from kindle_shelf import archive as A
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            jobs = []
            for n, data in ((1, b"A"), (2, b"B")):
                f = Path(d) / "Outbox" / "mine" / f"j{n}-g1" / "Book.epub"
                f.parent.mkdir(parents=True)
                f.write_bytes(data)
                j = {"id": f"j{n}", "phase": "copying", "gen": 1, "claimed": 1, "dest": "mine", "author": "W",
                     "keep": False, "files": {"final": str(f)}}
                sh.jobs.append(j)
                jobs.append(j)
            chose, go = threading.Event(), threading.Event()
            real_move = A.move_whole

            def slow_move(src, dst, owner):
                if "j1-g1" in str(src):
                    chose.set()               # A has its plan and is moving...
                    go.wait(0.5)              # ...while B tries (it must wait, or it plans the same free name)
                return real_move(src, dst, owner)

            def archive(j, then=None):
                sh.worker.gen = 1
                sh._mark_sent(j)
                if then:
                    then.set()
            A.move_whole = slow_move
            try:
                ta = threading.Thread(target=archive, args=(jobs[0],))
                ta.start()
                chose.wait(1)
                tb = threading.Thread(target=archive, args=(jobs[1], go))
                tb.start()
                ta.join(3)
                tb.join(3)
            finally:
                A.move_whole = real_move
            sent = Path(d) / "Sent"
            self.assertEqual(sorted(p.read_bytes() for p in sent.iterdir()), [b"A", b"B"])
            self.assertNotEqual(jobs[0]["files"]["final"], jobs[1]["files"]["final"])

class GenerationsKeepToTheirOwnFolders(unittest.TestCase):
    """The lease's last site: two generations of one job (cancel + retry during a conversion) never share files."""

    def _sh(self, d):
        sh = _shelf(Path(d))
        j = {"id": "j1", "phase": "converting", "gen": 2, "claimed": 2, "dest": "mine", "files": {}}
        sh.jobs.append(j)
        return sh, j

    def test_each_generation_has_its_own_folders(self):
        with tempfile.TemporaryDirectory() as d:
            sh, j = self._sh(d)
            self.assertNotEqual(sh._gen_dirs(j, 1), sh._gen_dirs(j, 2))
            self.assertTrue(all(a != b and not str(b).startswith(str(a) + "/")
                                for a, b in zip(sh._gen_dirs(j, 1), sh._gen_dirs(j, 2))))

    def test_a_superseded_worker_cannot_record_files(self):
        with tempfile.TemporaryDirectory() as d:
            sh, j = self._sh(d)
            from kindle_shelf.daemon import Superseded
            sh.worker.gen = 1                       # an older generation, still converting
            try:
                with self.assertRaises(Superseded):
                    sh._record(j, "final", "/old/output.epub")
            finally:
                sh.worker.gen = None
            self.assertEqual(j["files"], {})

    def test_a_superseded_worker_removes_only_what_nobody_uses(self):
        with tempfile.TemporaryDirectory() as d:
            sh, j = self._sh(d)
            old_work, old_out = sh._gen_dirs(j, 1)
            for p in (old_work / "big.download", old_out / "Book.epub"):
                p.parent.mkdir(parents=True)
                p.write_bytes(b"x")
            kept_work, _ = sh._gen_dirs(j, 3)
            (kept_work / "orig.epub").parent.mkdir(parents=True)
            (kept_work / "orig.epub").write_bytes(b"o")
            j["files"]["original"] = str(kept_work / "orig.epub")   # a newer generation uses this one
            sh._clean_superseded(j, 1)
            sh._clean_superseded(j, 3)
            self.assertFalse(old_work.exists() or old_out.exists())
            self.assertTrue((kept_work / "orig.epub").exists())


class DeliverAnswersAtOnce(unittest.TestCase):
    """F8's class: `deliver` never probes the Wi-Fi Kindle inside the command (the CLI waits only 30 s)."""

    def test_deliver_over_wifi_runs_in_the_background(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d), [C.KindleDest("mine", "My Paperwhite", "SERIALAAAAAA", "epub", "documents/Books", ["kindle"])])
            j = {"id": "j1", "phase": "waiting", "gen": 1, "dest": "mine", "files": {}}
            sh.jobs.append(j)
            probed = threading.Event()

            def slow_reach(dest):
                time.sleep(0.5)               # two hosts timing out take far longer; 0.5 s shows the point
                probed.set()
                return kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle")
            sh.reach = slow_reach
            t0 = time.time()
            r = sh.cmd_deliver("mine")
            self.assertLess(time.time() - t0, 0.2)
            self.assertEqual((r["count"], r["looking"]), (0, ["mine"]))
            self.assertTrue(probed.wait(2))
            for _ in range(50):
                if sh.submitted:
                    break
                time.sleep(0.02)
            self.assertEqual(sh.submitted, [("j1", 2)])
            self.assertEqual(j["phase"], "queued")


# ---- review round 3 (Codex F16-F18) ---------------------------------------------------------------------------------

class ArchivingASentJob(unittest.TestCase):
    """F16: "sent" and its files' new places are one write-ahead step: Keep waits for it, a restart finishes it."""

    def _job(self, d, keep=False):
        sh = _shelf(Path(d))
        final = Path(d) / "Outbox" / "mine" / "j1-g1" / "Book - Writer.epub"
        orig = Path(d) / "work" / "j1" / "g1" / "Book.epub"
        for p, data in ((final, b"final"), (orig, b"orig")):
            p.parent.mkdir(parents=True)
            p.write_bytes(data)
        j = {"id": "j1", "phase": "copying", "gen": 1, "claimed": 1, "dest": "mine", "author": "Writer", "keep": keep,
             "files": {"final": str(final), "original": str(orig)}}
        sh.jobs.append(j)
        return sh, j, final, orig

    def test_keep_while_the_files_are_moving_waits_for_them(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            sh, j, final, orig = self._job(d)
            moving, go = threading.Event(), threading.Event()
            real = archive.move_whole

            def paused_move(src, dst, owner):
                moving.set()
                go.wait(1)
                return real(src, dst, owner)
            archive.move_whole = paused_move
            errors = []

            def worker():
                sh.worker.gen = 1
                try:
                    sh._mark_sent(j, kindle_name="Book - Writer.epub")
                except Exception as e:
                    errors.append(e)
            try:
                t = threading.Thread(target=worker)
                t.start()
                self.assertTrue(moving.wait(1))
                keeper = threading.Thread(target=lambda: sh.cmd_keep("j1", True))
                keeper.start()
                time.sleep(0.1)
                self.assertTrue(keeper.is_alive())       # Keep waits for the archive (it used to move the same files)
                go.set()
                t.join(2)
                keeper.join(2)
            finally:
                archive.move_whole = real
            self.assertEqual((errors, j["phase"]), ([], "sent"))
            self.assertNotIn("archive", j)
            self.assertTrue(j["keep"])
            for key in ("final", "original"):
                self.assertTrue(Path(j["files"][key]).is_file(), key)
                self.assertIn(Path(d) / "Writer", Path(j["files"][key]).parents)   # kept after the archive

    def test_keep_right_after_the_plan_is_saved_waits_for_the_moves(self):
        with tempfile.TemporaryDirectory() as d:
            sh, j, final, orig = self._job(d)
            keeper = []

            def save():
                if j.get("archive") and not keeper:           # the plan is on disk, nothing has moved yet
                    t = threading.Thread(target=lambda: sh.cmd_keep("j1", True))
                    keeper.append(t)
                    t.start()
                    t.join(0.2)                               # a Keep that doesn't wait is done by now
            sh.save = save
            sh.worker.gen = 1
            try:
                sh._mark_sent(j)
            finally:
                sh.worker.gen = None
            keeper[0].join(2)
            for key in ("final", "original"):
                self.assertIn(Path(d) / "Writer", Path(j["files"][key]).parents, key)   # moved by Keep, after

    def test_a_restart_in_the_middle_finishes_the_archive(self):
        import json
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            T = Path(d)
            moved = T / "Books" / "Sent" / "Book - Writer.epub"
            moved.parent.mkdir(parents=True)
            moved.write_bytes(b"final")                  # the final had moved when the daemon stopped
            orig = T / "cache" / "work" / "j1" / "g1" / "Book.epub"
            orig.parent.mkdir(parents=True)
            orig.write_bytes(b"orig")                    # the original hadn't
            job = {"id": "j1", "phase": "sent", "dest": "mine", "gen": 1, "claimed": 1, "author": "Writer",
                   "files": {"final": str(T / "Books" / "Outbox" / "mine" / "j1-g1" / "Book - Writer.epub"),
                             "original": str(orig)},
                   "archive": {"final": [str(T / "Books" / "Outbox" / "mine" / "j1-g1" / "Book - Writer.epub"), str(moved)],
                               "original": [str(orig), str(T / "Books" / "Sent" / "original - Book.epub")]}}
            (T / "state").mkdir()
            (T / "state" / "jobs.json").write_text(json.dumps({"jobs": [job]}))
            saved = {n: getattr(C, n) for n in ("STATE_DIR", "CACHE_DIR", "RUNTIME_DIR", "JOBS_PATH", "WORK_DIR",
                                                "COVER_DIR", "STATE_PATH", "SOCKET_PATH", "load")}
            C.STATE_DIR, C.CACHE_DIR, C.RUNTIME_DIR = T / "state", T / "cache", T / "run"
            C.JOBS_PATH, C.WORK_DIR, C.COVER_DIR = T / "state" / "jobs.json", T / "cache" / "work", T / "cache" / "covers"
            C.STATE_PATH, C.SOCKET_PATH = T / "run" / "state.json", T / "run" / "ctl.sock"
            C.load = lambda: C.Config(books_dir=T / "Books")
            try:
                sh = daemon.Shelf()
                sh.pool.shutdown(wait=False)
            finally:
                for n, v in saved.items():
                    setattr(C, n, v)
            j = sh.jobs[0]
            self.assertNotIn("archive", j)
            self.assertEqual(Path(j["files"]["final"]), moved)
            self.assertEqual(Path(j["files"]["original"]).name, "original - Book.epub")
            self.assertTrue(Path(j["files"]["original"]).is_file())
            self.assertFalse(orig.exists())


class ARebindingCantSlipBetweenTheLastCheckAndTheCopy(unittest.TestCase):
    """F17: reload swaps the config under the same lock as _start_copy's last check and "copying"."""

    def test_reload_waits_for_the_copy_decision(self):
        with tempfile.TemporaryDirectory() as d:
            kd = lambda serial: C.KindleDest("mine", "My Paperwhite", serial, "epub", "documents/Books", ["kindle"])
            sh = _shelf(Path(d), [kd("SERIALAAAAAA")])
            j = {"id": "j1", "phase": "converting", "gen": 1, "claimed": 1, "dest": "mine", "files": {}}
            sh.jobs.append(j)
            k = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle")
            finished = {}
            load = C.load
            C.load = lambda: C.Config(books_dir=Path(d), kindles=[kd("SERIALBBBBBB")])
            real_bound = sh._still_bound

            def bound_then_reload(dest, kk):
                ok = real_bound(dest, kk)
                t = threading.Thread(target=lambda: (sh.cmd_reload(), finished.__setitem__("reload", time.time())))
                t.start()
                time.sleep(0.15)                          # a reload that doesn't wait would finish now
                finished["thread"] = t
                return ok
            sh._still_bound = bound_then_reload
            sh.worker.gen = 1
            try:
                sh._start_copy(j, k, "documents/Books", "Book.epub")
                copying_at = j["updated"]
                finished["thread"].join(2)
            finally:
                C.load = load
                sh.worker.gen = None
            self.assertEqual(j["phase"], "copying")
            self.assertGreater(finished["reload"], copying_at)   # the rebinding landed after the decision
            self.assertEqual(sh.cfg.kindles[0].serial, "SERIALBBBBBB")


class MtpLooksBeforeItCopies(unittest.TestCase):
    """F18: a failed look at the Kindle isn't "nothing there"; an overwrite of the same size is still seen."""

    def _run(self, infos, copy_code=1):
        calls = []
        seq = list(infos)

        def fake(cmd, timeout=60):
            calls.append(cmd)
            if cmd[:2] == ["gio", "copy"]:
                return subprocess.CompletedProcess(cmd, copy_code, "", "Operation failed" if copy_code else "")
            if cmd[:2] == ["gio", "info"]:
                kind, val = seq.pop(0) if seq else ("absent", None)
                if kind == "ok":
                    return subprocess.CompletedProcess(cmd, 0, f"attributes:\n  standard::size: {val[0]}\n  time::modified: {val[1]}\n", "")
                if kind == "absent":
                    return subprocess.CompletedProcess(cmd, 1, "", "gio: …: No such file or directory")
                return subprocess.CompletedProcess(cmd, 124 if kind == "timeout" else 1, "", "Operation not supported")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "B.epub"
            src.write_bytes(b"z" * 3000)
            run = kindle._run
            kindle._run = fake
            try:
                try:
                    kindle.copy(kindle.Kindle(serial="S", product="Kindle", kind="mtp", sysfs="", storage="mtp://K/IS/"), src, "B.epub")
                    outcome = "copied"
                except kindle.KindleError as e:
                    outcome = str(e)
            finally:
                kindle._run = run
        return outcome, [c[1] for c in calls]

    def test_a_blind_look_means_no_copy(self):
        for kind in ("timeout", "other"):
            outcome, ops = self._run([(kind, None)])
            self.assertNotIn("copy", ops, kind)
            self.assertNotIn("remove", ops, kind)
            self.assertIn("couldn't check", outcome)

    def test_an_overwrite_of_the_same_size_is_still_removed(self):
        outcome, ops = self._run([("ok", (4000, 100)), ("ok", (4000, 200))])   # same size, newer: it was written
        self.assertIn("remove", ops)

    def test_an_untouched_book_stays(self):
        outcome, ops = self._run([("ok", (4000, 100)), ("ok", (4000, 100))])
        self.assertNotIn("remove", ops)


# ---- from round 4's cut-off run (Codex's own reproductions, before its usage limit): F19-F23 ------------------------

class ArchivesAndKeepAreJournaled(unittest.TestCase):
    """F19, F20, F23: pending archive names are taken, an archive that can't move never breaks a delivered job or a
    start, and Keep's moves are journaled like archiving."""

    def test_a_plan_avoids_another_jobs_pending_names(self):                       # F19
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            f = Path(d) / "Outbox" / "mine" / "j2-g1" / "Book.epub"
            f.parent.mkdir(parents=True)
            f.write_bytes(b"B")
            other = {"id": "j1", "phase": "sent", "archive": {"final": ["/gone/Book.epub", str(Path(d) / "Sent" / "Book.epub")]}}
            j = {"id": "j2", "phase": "copying", "author": "W", "keep": False, "files": {"final": str(f)}}
            sh.jobs += [other, j]
            plan = sh._archive_plan(j)
            self.assertEqual(plan["final"][1].name, "Book (2).epub")

    def test_startup_finishes_archives_before_any_worker(self):                      # F19
        import json
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            T = Path(d)
            src = T / "Books" / "Outbox" / "mine" / "j1-g1" / "Book.epub"
            src.parent.mkdir(parents=True)
            src.write_bytes(b"A")
            dst = T / "Books" / "Sent" / "Book.epub"
            jobs = [{"id": "j1", "phase": "sent", "dest": "mine", "author": "W", "files": {"final": str(src)},
                     "archive": {"final": [str(src), str(dst)]}},
                    {"id": "j2", "phase": "queued", "dest": "mine", "gen": 1, "files": {}}]
            (T / "state").mkdir()
            (T / "state" / "jobs.json").write_text(json.dumps({"jobs": jobs}))
            saved = {n: getattr(C, n) for n in ("STATE_DIR", "CACHE_DIR", "RUNTIME_DIR", "JOBS_PATH", "WORK_DIR",
                                                "COVER_DIR", "STATE_PATH", "SOCKET_PATH", "load")}
            C.STATE_DIR, C.CACHE_DIR, C.RUNTIME_DIR = T / "state", T / "cache", T / "run"
            C.JOBS_PATH, C.WORK_DIR, C.COVER_DIR = T / "state" / "jobs.json", T / "cache" / "work", T / "cache" / "covers"
            C.STATE_PATH, C.SOCKET_PATH = T / "run" / "state.json", T / "run" / "ctl.sock"
            C.load = lambda: C.Config(books_dir=T / "Books")
            seen = []
            submit = daemon.Shelf._submit
            daemon.Shelf._submit = lambda self, jid, gen: seen.append((jid, dst.exists()))
            try:
                daemon.Shelf().pool.shutdown(wait=False)
            finally:
                daemon.Shelf._submit = submit
                for n, v in saved.items():
                    setattr(C, n, v)
            self.assertEqual(seen, [("j2", True)])          # the archive was done when the first worker was submitted

    def test_an_archive_that_cant_move_leaves_the_job_sent(self):                   # F20
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            (Path(d) / "Writer").write_bytes(b"a file where the author's folder would go")
            f = Path(d) / "Outbox" / "mine" / "j1-g1" / "Book.epub"
            f.parent.mkdir(parents=True)
            f.write_bytes(b"book")
            j = {"id": "j1", "phase": "copying", "gen": 1, "claimed": 1, "dest": "mine", "author": "Writer", "keep": True,
                 "files": {"final": str(f)}}
            sh.jobs.append(j)
            sh.worker.gen = 1
            try:
                sh._mark_sent(j)
            finally:
                sh.worker.gen = None
            self.assertEqual(j["phase"], "sent")
            self.assertNotIn("archive", j)
            self.assertTrue(Path(j["files"]["final"]).is_file())

    def test_a_failing_move_never_raises(self):                                     # F20, the start
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            f = Path(d) / "Book.epub"
            f.write_bytes(b"book")
            j = {"id": "j1", "phase": "sent", "files": {"final": str(f)},
                 "archive": {"final": [str(f), str(Path(d) / "Sent" / "Book.epub")]}}
            sh.jobs.append(j)
            real = archive.move_whole

            def full_disk(a, b, owner):
                raise OSError(28, "No space left on device")
            archive.move_whole = full_disk
            try:
                sh._finish_archive(j)
            finally:
                archive.move_whole = real
            self.assertNotIn("archive", j)
            self.assertEqual(j["files"]["final"], str(f))   # recorded where it actually is

    def test_keep_saves_where_files_go_before_moving_them(self):                    # F23
        import json
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            f = Path(d) / "Sent" / "Book.epub"
            f.parent.mkdir(parents=True)
            f.write_bytes(b"book")
            j = {"id": "j1", "phase": "sent", "author": "Writer", "keep": False, "files": {"final": str(f)}}
            sh.jobs.append(j)
            snapshots = []
            sh.save = lambda: snapshots.append(json.loads(json.dumps(j)))

            class Crash(BaseException):
                pass
            real = archive.move_whole

            def move_then_crash(a, b, owner):
                real(a, b, owner)
                raise Crash()
            archive.move_whole = move_then_crash
            try:
                with self.assertRaises(Crash):
                    sh.cmd_keep("j1", True)
            finally:
                archive.move_whole = real
            self.assertTrue(snapshots and snapshots[-1].get("archive"))   # on disk before the move
            persisted = snapshots[-1]
            sh.jobs[0] = persisted                                         # the next start, from that state
            sh._finish_archive(persisted)
            self.assertEqual(Path(persisted["files"]["final"]), Path(d) / "Writer" / "Book.epub")
            self.assertTrue(Path(persisted["files"]["final"]).is_file())


class JanitorSeesAKeepThatWonTheRace(unittest.TestCase):
    """F21: the janitor's decision is made again under files_lock."""

    def test_keep_between_the_look_and_the_lock(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            kept = Path(d) / "Writer" / "Book.epub"
            kept.parent.mkdir(parents=True)
            kept.write_bytes(b"book")
            j = {"id": "j1", "phase": "sent", "keep": False, "delete_after": 1, "files": {"final": str(kept)}}
            sh.jobs.append(j)

            class KeepWinsTheRace:
                def __enter__(self_):
                    j["keep"] = True          # Keep took the lock first and moved the book to ~/Books
                def __exit__(self_, *a):
                    return False
            sh.files_lock = KeepWinsTheRace()
            sh._janitor_pass(now=1000)
            self.assertFalse(j.get("files_deleted"))
            self.assertTrue(kept.exists())


class MtpErrorsAreReadByTheirReason(unittest.TestCase):
    """F22: gio's error names the path first; only its reason says whether the file is there."""

    def _stat(self, code, stderr):
        run = kindle._run
        kindle._run = lambda cmd, timeout=60: subprocess.CompletedProcess(cmd, code, "", stderr)
        try:
            return kindle._mtp_stat("mtp://K/Internal Storage/documents/Not Found - Writer.epub")
        finally:
            kindle._run = run

    def test_a_title_saying_not_found_is_not_a_reason(self):
        t = "mtp://K/Internal Storage/documents/Not Found - Writer.epub"
        self.assertEqual(self._stat(1, f"gio: {t}: Operation failed")[0], "unknown")
        self.assertEqual(self._stat(1, f"gio: {t}: No such file or directory")[0], "absent")
        self.assertEqual(self._stat(124, "timed out")[0], "unknown")


class ArchivedBooksLandWhole(unittest.TestCase):
    """archive.py on its own: F25 and the stops around it. A book lands whole at its destination or stays where it is,
    and what's at the destination counts as the book only when it is this book."""

    def _src(self, d, data=b"x" * 1000):
        src = Path(d) / "work" / "j1" / "g1" / "Book.epub"
        src.parent.mkdir(parents=True)
        src.write_bytes(data)
        return src, Path(d) / "Sent" / "original - Book.epub"

    def _other_filesystem(self, copy=None):
        """os.rename refuses as it does across filesystems (EXDEV); `copy` stands in for shutil.copyfile."""
        import contextlib, errno, shutil

        @contextlib.contextmanager
        def patched():
            saved = os.rename, shutil.copyfile

            def exdev(a, b):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            os.rename, shutil.copyfile = exdev, copy or shutil.copyfile
            try:
                yield
            finally:
                os.rename, shutil.copyfile = saved
        return patched()

    def test_a_users_file_named_like_a_temp_is_never_touched(self):           # C2
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            for across in (False, True):
                src, dst = self._src(Path(d) / str(across))
                dst.parent.mkdir()
                users = dst.with_name(f".{dst.name}.part")               # the name the temp used to have
                users.write_bytes(b"someone's file")
                own = archive.part_path(dst, "j1")
                own.write_bytes(b"x" * 400)                               # this job's own, from a stop
                if across:
                    with self._other_filesystem():
                        self.assertEqual(archive.settle(src, dst, "j1"), dst)
                else:
                    self.assertEqual(archive.settle(src, dst, "j1"), dst)
                self.assertTrue(users.is_file(), across)
                self.assertEqual(users.read_bytes(), b"someone's file", across)
                self.assertEqual(sorted(p.name for p in dst.parent.iterdir()), sorted([users.name, dst.name]), across)

    def test_a_full_disk_half_way_across_filesystems_leaves_the_book_where_it_was(self):   # F25
        import errno
        from kindle_shelf import archive

        def half_then_full(a, b, **kw):          # shutil.copy2 passes follow_symlinks
            Path(b).write_bytes(Path(a).read_bytes()[:400])
            raise OSError(errno.ENOSPC, "No space left on device")
        with tempfile.TemporaryDirectory() as d:
            src, dst = self._src(d)
            with self._other_filesystem(half_then_full):
                self.assertEqual(archive.settle(src, dst, "j1"), src)
            self.assertEqual(src.read_bytes(), b"x" * 1000)
            self.assertEqual(list(dst.parent.iterdir()), [])          # no half book, no .part

    def test_across_filesystems_the_book_lands_whole(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            src, dst = self._src(d)
            with self._other_filesystem():
                self.assertEqual(archive.settle(src, dst, "j1"), dst)
            self.assertEqual((dst.read_bytes(), src.exists()), (b"x" * 1000, False))
            self.assertEqual(list(dst.parent.iterdir()), [dst])

    def test_a_short_copy_is_never_published(self):
        from kindle_shelf import archive

        def short(a, b, **kw):
            Path(b).write_bytes(Path(a).read_bytes()[:400])          # no error, just short
        with tempfile.TemporaryDirectory() as d:
            src, dst = self._src(d)
            with self._other_filesystem(short):
                self.assertEqual(archive.settle(src, dst, "j1"), src)
            self.assertEqual((src.read_bytes(), list(dst.parent.iterdir())), (b"x" * 1000, []))

    def test_a_stop_after_the_book_landed_before_its_source_went(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            src, dst = self._src(d)
            dst.parent.mkdir()
            dst.write_bytes(src.read_bytes())
            self.assertEqual(archive.settle(src, dst, "j1"), dst)
            self.assertFalse(src.exists())

    def test_someone_elses_file_at_the_destination_is_never_taken_for_the_book(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            src, dst = self._src(d)
            dst.parent.mkdir()
            dst.write_bytes(b"x" * 400)                               # half a book an older version left, say
            self.assertEqual(archive.settle(src, dst, "j1"), src)
            self.assertEqual((src.read_bytes(), dst.read_bytes()), (b"x" * 1000, b"x" * 400))

    def test_a_stop_in_the_middle_of_a_copy(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            src, dst = self._src(d)
            dst.parent.mkdir()
            archive.part_path(dst, "j1").write_bytes(b"x" * 400)            # what the stop left
            with self._other_filesystem():
                self.assertEqual(archive.settle(src, dst, "j1"), dst)
            self.assertEqual((list(dst.parent.iterdir()), dst.read_bytes()), ([dst], b"x" * 1000))

    def test_after_a_stop_only_the_destination_or_neither(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            src, dst = self._src(d)
            dst.parent.mkdir()
            os.replace(src, dst)
            self.assertEqual(archive.settle(src, dst, "j1"), dst)
            dst.unlink()
            archive.part_path(dst, "j1").write_bytes(b"x" * 400)            # a copy stopped half way, and its source is gone
            self.assertIsNone(archive.settle(src, dst, "j1"))
            self.assertEqual(list(dst.parent.iterdir()), [])

    def test_prune_leaves_only_the_recorded_files(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "j1"
            keep = root / "g2" / "x_unpacked" / "Book.epub"
            for p in (keep, root / "g2" / "x_unpacked" / "notes.txt", root / "g2" / "cover.jpg", root / "g1" / "big.part"):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            archive.prune(root, {keep})
            self.assertEqual([p for p in root.rglob("*") if p.is_file()], [keep])
            archive.prune(root, set())
            self.assertFalse(root.exists())


class ALeftBehindCopyIsKeptTrackOf(unittest.TestCase):
    """F24 (and F25 through the daemon): a book that couldn't be archived stays where it is, recorded there. No cleanup
    removes it, the janitor deletes it there when its day comes, Keep moves it from there, removing the row takes it."""

    def _job(self, d):
        sh = _shelf(Path(d))
        final = Path(d) / "Outbox" / "mine" / "j1-g2" / "Book - Writer.epub"
        orig = Path(d) / "work" / "j1" / "g2" / "Book.epub"
        cover = Path(d) / "work" / "j1" / "g2" / "cover.jpg"
        stale = Path(d) / "Outbox" / "mine" / "j1-g1" / "Book - Writer.epub"    # a generation a restart left behind
        for p, data in ((final, b"final"), (orig, b"original"), (cover, b"jpg"), (stale, b"fin")):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        j = {"id": "j1", "phase": "copying", "gen": 2, "claimed": 2, "dest": "mine", "author": "Writer", "keep": False,
             "files": {"final": str(final), "original": str(orig), "cover": str(cover)}}
        sh.jobs.append(j)
        return sh, j, orig

    def _sent_with_the_original_left_behind(self, d):
        from kindle_shelf import archive
        sh, j, orig = self._job(d)
        real = archive.move_whole

        def full_for_the_original(src, dst, owner):
            if src == orig:
                raise OSError(28, "No space left on device")
            return real(src, dst, owner)
        archive.move_whole = full_for_the_original
        sh.worker.gen = 2
        try:
            sh._mark_sent(j)
        finally:
            archive.move_whole = real
            sh.worker.gen = None
        return sh, j, orig

    def test_an_original_that_couldnt_move_survives_the_cleanup(self):          # F24, its trace
        with tempfile.TemporaryDirectory() as d:
            sh, j, orig = self._sent_with_the_original_left_behind(d)
            self.assertEqual((j["phase"], j["files"]["original"]), ("sent", str(orig)))
            self.assertTrue(orig.is_file())                                     # recorded there, and there
            self.assertEqual(orig.read_bytes(), b"original")
            self.assertEqual(Path(j["files"]["final"]), Path(d) / "Sent" / "Book - Writer.epub")
            self.assertNotIn("archive", j)
            # the rest of the temporary folders went: the cover, the old generation's file, the outbox folders
            self.assertEqual([p for p in (Path(d) / "work").rglob("*") if p.is_file()], [orig])
            self.assertEqual(list((Path(d) / "Outbox" / "mine").iterdir()), [])
            self.assertEqual(j["files"]["cover"], "")

    def test_a_full_disk_across_filesystems_keeps_the_whole_books(self):        # F25, through _mark_sent
        import errno, shutil
        with tempfile.TemporaryDirectory() as d:
            sh, j, orig = self._job(d)
            final = Path(j["files"]["final"])
            saved = os.rename, shutil.copyfile

            def exdev(a, b):
                raise OSError(errno.EXDEV, "Invalid cross-device link")

            def half_then_full(a, b, **kw):          # shutil.copy2 passes follow_symlinks
                Path(b).write_bytes(Path(a).read_bytes()[:3])
                raise OSError(errno.ENOSPC, "No space left on device")
            os.rename, shutil.copyfile = exdev, half_then_full
            sh.worker.gen = 2
            try:
                sh._mark_sent(j)
            finally:
                os.rename, shutil.copyfile = saved
                sh.worker.gen = None
            self.assertEqual((j["files"]["original"], j["files"]["final"]), (str(orig), str(final)))
            self.assertEqual((orig.read_bytes(), final.read_bytes()), (b"original", b"final"))
            self.assertEqual(list((Path(d) / "Sent").iterdir()), [])            # no half copies

    def test_the_janitor_deletes_a_left_behind_copy_where_it_is(self):           # F24, the janitor
        with tempfile.TemporaryDirectory() as d:
            sh, j, orig = self._sent_with_the_original_left_behind(d)
            sh._janitor_pass(now=j["delete_after"] + 1)
            self.assertTrue(j.get("files_deleted"))
            self.assertFalse(orig.exists() or Path(j["files"]["final"]).exists())
            self.assertFalse((Path(d) / "work" / "j1").exists())

    def test_deleted_is_said_only_once_nothing_is_left(self):                    # F24, what the janitor says
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            mine = Path(d) / "Writer" / "Book.epub"           # Keep turned off, and the move back to Sent failed
            mine.parent.mkdir()
            mine.write_bytes(b"book")
            j = {"id": "j1", "phase": "sent", "dest": "mine", "keep": False, "delete_after": 1,
                 "files": {"final": str(mine)}}
            sh.jobs.append(j)
            sh._janitor_pass(now=1000)
            self.assertTrue(mine.exists())                    # never deleted from ~/Books/<Author>/
            self.assertFalse(j.get("files_deleted"))          # and not said to be

    def test_keep_moves_a_left_behind_copy_into_books(self):                     # F24, Keep
        with tempfile.TemporaryDirectory() as d:
            sh, j, orig = self._sent_with_the_original_left_behind(d)
            sh.cmd_keep("j1", True)
            for key in ("final", "original"):
                self.assertIn(Path(d) / "Writer", Path(j["files"][key]).parents, key)
                self.assertTrue(Path(j["files"][key]).is_file(), key)
            self.assertFalse((Path(d) / "work" / "j1").exists())

    def test_removing_the_row_takes_a_left_behind_copy_and_leaves_sent(self):    # F24, remove
        with tempfile.TemporaryDirectory() as d:
            sh, j, orig = self._sent_with_the_original_left_behind(d)
            in_sent = Path(j["files"]["final"])
            self.assertTrue(sh.cmd_remove("j1")["ok"])
            self.assertFalse((Path(d) / "work" / "j1").exists())
            self.assertTrue(in_sent.is_file())


class TheJanitorForgetsOnlyWhatIsStillOld(unittest.TestCase):
    """F26: the janitor's history removal decides under the lock it removes under."""

    def test_a_retry_between_the_janitors_look_and_its_removal_keeps_the_job(self):
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            j = {"id": "j1", "phase": "cancelled", "gen": 2, "dest": "mine", "updated": 0, "files": {}}
            sh.jobs.append(j)
            real, test = sh.lock, self

            class RetryTakesTheLockFirst:
                fired = False

                def __enter__(self_):
                    if not self_.fired:
                        self_.fired = True
                        test.assertTrue(sh.cmd_retry("j1")["ok"])
                    return real.__enter__()

                def __exit__(self_, *a):
                    return real.__exit__(*a)
            sh.lock = RetryTakesTheLockFirst()
            sh._janitor_pass(now=3 * 86400)
            self.assertIn(j, sh.jobs)
            self.assertEqual((j["phase"], sh.submitted), ("queued", [("j1", 3)]))


class SendAnywayIsAboutOneFile(unittest.TestCase):
    """F28's class: "send anyway" was said about the file that was checked; a re-fetch can bring another."""

    def test_a_yes_to_one_file_is_not_a_yes_to_the_next(self):
        self._yes_then_another_file(names_the_file=True)

    def test_a_yes_from_before_answers_named_a_file_is_about_none(self):      # F30
        self._yes_then_another_file(names_the_file=False)

    def _yes_then_another_file(self, names_the_file):
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            first, second = Path(d) / "first.epub", Path(d) / "second.epub"
            first.write_bytes(b"1")
            second.write_bytes(b"2")
            j = {"id": "j1", "phase": "confirm", "gen": 1, "claimed": 1, "dest": "mine", "dest_type": "usb",
                 "title": "Picked Title", "authors": ["A"], "author": "A", "copy": {"torrent_name": ""},
                 "accept_mismatch": False, "files": {"original": str(first)},
                 "confirm": {"kind": "mismatch", "found_title": "Other", "found_author": "B"}}
            if names_the_file:
                j["confirm"]["file"] = str(first)
            sh.jobs.append(j)
            self.assertTrue(sh.cmd_confirm("j1", "send")["ok"])        # yes, that file
            first.unlink()                                              # gone (a cancel discarded it, say)
            (jid, gen), = sh.submitted
            sh._fetch = lambda job, work: second                        # the next fetch brings another file
            saved = daemon.convert.read_meta, daemon.notify
            daemon.convert.read_meta = lambda p: {"title": "Yet Another Book", "authors": ["C"]}
            daemon.notify = lambda *a, **kw: None
            try:
                sh._run(jid, gen)
            finally:
                daemon.convert.read_meta, daemon.notify = saved
            self.assertEqual(j["phase"], "confirm")                          # asked again,
            self.assertEqual(j["confirm"]["file"], str(second))              # about the file it has now


class ArchivesHandOverOnlyTheirOwnFiles(unittest.TestCase):
    """F29 and its class: a book the shelf records is a real file of its own, never a link to bytes elsewhere."""

    def test_an_archive_never_hands_over_a_link(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            T = Path(d)
            (T / "secret.txt").write_text("a file on this computer")
            src = T / "src"
            (src / "sub").mkdir(parents=True)
            (src / "Book.epub").symlink_to("../../secret.txt")     # out of the extracted folder (bsdtar keeps links)
            (src / "Inside.epub").symlink_to("sub/payload")         # a link inside it is still a link
            (src / "sub" / "payload").write_bytes(b"x" * 30)
            (src / "Real.txt").write_text("a real book")
            arc = T / "work" / "book.zip"
            arc.parent.mkdir()
            subprocess.run(["bsdtar", "-a", "-cf", str(arc), "-C", str(src), "."], check=True)
            self.assertEqual(convert.unpack(arc).name, "Real.txt")     # though EPUB ranks above TXT
            (src / "Real.txt").unlink()
            only_links = T / "work" / "links.zip"
            subprocess.run(["bsdtar", "-a", "-cf", str(only_links), "-C", str(src), "."], check=True)
            with self.assertRaises(convert.ConvertError):
                convert.unpack(only_links)

    def test_a_linked_book_moves_as_its_bytes(self):
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            unpacked = Path(d) / "work" / "x_unpacked"
            (unpacked / "payload").mkdir(parents=True)
            (unpacked / "payload" / "content").write_bytes(b"the book")
            link = unpacked / "Book.epub"
            link.symlink_to("payload/content")
            dst = Path(d) / "Sent" / "original - Book.epub"
            self.assertEqual(archive.settle(link, dst, "j1"), dst)
            self.assertFalse(dst.is_symlink())
            self.assertEqual((dst.read_bytes(), os.path.lexists(link)), (b"the book", False))

    def test_a_linked_original_that_cant_move_keeps_its_book(self):          # F29, its trace
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            unpacked = Path(d) / "work" / "j1" / "g1" / "x_unpacked"
            (unpacked / "payload").mkdir(parents=True)
            (unpacked / "payload" / "content").write_bytes(b"the book")
            link = unpacked / "Book.epub"
            link.symlink_to("payload/content")
            j = {"id": "j1", "phase": "sent", "dest": "mine", "files": {"original": str(link)},
                 "archive": {"original": [str(link), str(Path(d) / "Sent" / "original - Book.epub")]}}
            sh.jobs.append(j)
            real = archive.move_whole

            def full_disk(a, b, owner):
                raise OSError(28, "No space left on device")
            archive.move_whole = full_disk
            try:
                sh._finish_archive(j)
            finally:
                archive.move_whole = real
            kept = Path(j["files"]["original"])
            self.assertTrue(kept.is_file())                             # the link, and the bytes it points to
            self.assertEqual(kept.read_bytes(), b"the book")

    def test_a_chain_of_links_keeps_everything_until_the_book_goes(self):      # F31
        from kindle_shelf import archive
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            unpacked = Path(d) / "work" / "j1" / "g1" / "x_unpacked"
            (unpacked / "payload").mkdir(parents=True)
            (unpacked / "payload" / "content").write_bytes(b"the book")
            (unpacked / "alias").symlink_to("payload/content")
            link = unpacked / "Book.epub"
            link.symlink_to("alias")                                    # Book.epub -> alias -> payload/content
            j = {"id": "j1", "phase": "sent", "dest": "mine", "keep": False, "delete_after": 1,
                 "files": {"original": str(link)},
                 "archive": {"original": [str(link), str(Path(d) / "Sent" / "original - Book.epub")]}}
            sh.jobs.append(j)
            real = archive.move_whole

            def full_disk(a, b, owner):
                raise OSError(28, "No space left on device")
            archive.move_whole = full_disk
            try:
                sh._finish_archive(j)
                sh._prune_temp(j, j["files"])                            # and any later cleanup
            finally:
                archive.move_whole = real
            kept = Path(j["files"]["original"])
            self.assertTrue(kept.is_file())
            self.assertEqual(kept.read_bytes(), b"the book")
            sh._janitor_pass(now=1000)                                  # its day: the book goes, and the rest with it
            self.assertTrue(j.get("files_deleted"))
            self.assertFalse((Path(d) / "work" / "j1").exists())

    def test_a_link_with_nothing_behind_it_protects_nothing(self):             # F31's sibling
        with tempfile.TemporaryDirectory() as d:
            sh = _shelf(Path(d))
            unpacked = Path(d) / "work" / "j1" / "g1" / "x_unpacked"
            unpacked.mkdir(parents=True)
            (unpacked / "cover.jpg").write_bytes(b"x")
            link = unpacked / "Book.epub"
            link.symlink_to("payload/gone")                             # its bytes are gone already
            j = {"id": "j1", "phase": "sent", "dest": "mine", "keep": False, "delete_after": 1,
                 "files": {"original": str(link)}}
            sh.jobs.append(j)
            sh._janitor_pass(now=1000)
            self.assertTrue(j.get("files_deleted"))
            self.assertFalse((Path(d) / "work" / "j1").exists())        # and nothing is left behind


class AWakeUpIsNeverMissed(unittest.TestCase):
    """C3: whoever comes second sees the other. A USB Kindle that became ready while its book was on its way to
    "waiting" (its attach found the book still working) takes the book."""

    def test_a_usb_kindle_ready_during_the_wifi_probe_takes_the_book(self):
        from kindle_shelf import daemon
        with tempfile.TemporaryDirectory() as d:
            kd = C.KindleDest("mine", "My Paperwhite", "SERIALAAAAAA", "epub", "documents/Books", ["kindle"])
            sh = _shelf(Path(d), [kd])
            final = Path(d) / "Book.epub"
            final.write_bytes(b"b")
            j = {"id": "j1", "phase": "converting", "gen": 1, "claimed": 1, "dest": "mine", "dest_type": "usb",
                 "auto_copy": True, "title": "Book", "author": "W", "overwrite": None, "files": {"final": str(final)}}
            sh.jobs.append(j)
            usb = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="msc", sysfs="", mount=d)

            def probe_while_plugged_in(dest):
                sh.kindles[usb.serial] = usb             # USB became ready meanwhile,
                sh._on_known_kindle(usb.serial)          # and its attach found the book still working
                return None                              # Wi-Fi didn't answer (USB turns it off)
            sh.reach = probe_while_plugged_in
            notify, daemon.notify = daemon.notify, lambda *a, **kw: None
            sh.worker.gen = 1
            try:
                sh._deliver(j)
            finally:
                sh.worker.gen = None
                daemon.notify = notify
            self.assertEqual((j["phase"], sh.submitted), ("queued", [("j1", 2)]))     # woken, not left waiting

    def test_a_reload_that_binds_a_plugged_in_kindle_wakes_its_books(self):     # C3's sibling
        with tempfile.TemporaryDirectory() as d:
            kd = lambda serial: C.KindleDest("second", "Second Kindle", serial, "azw3", "documents", [])
            sh = _shelf(Path(d), [kd("")])                       # not bound to any Kindle yet
            usb = kindle.Kindle(serial="SERIALCCCCCC", product="Kindle", kind="msc", sysfs="", mount=d)
            sh.kindles[usb.serial] = usb                         # plugged in already
            j = {"id": "j1", "phase": "waiting", "gen": 1, "dest": "second", "auto_copy": True, "files": {}}
            sh.jobs.append(j)
            load = C.load
            C.load = lambda: C.Config(books_dir=Path(d), kindles=[kd("SERIALCCCCCC")])
            try:
                sh.cmd_reload()
            finally:
                C.load = load
            self.assertEqual((j["phase"], sh.submitted), ("queued", [("j1", 2)]))


class AnUnknownOutcomeIsLookedAt(unittest.TestCase):
    """C4: an attempt whose outcome was lost (Wi-Fi dropped after the Kindle had renamed the book into place) is settled
    by looking at the Kindle, not by sending again: no question about our own copy, no second numbered copy."""

    def _sent_twice(self, d, overwrite=None, there=None, lands=True, meanwhile=None):
        from kindle_shelf import daemon
        kd = C.KindleDest("mine", "My Paperwhite", "SERIALAAAAAA", "epub", "documents/Books", ["kindle"])
        sh = _shelf(Path(d), [kd])
        final = Path(d) / "out" / "Book.epub"
        final.parent.mkdir()
        final.write_bytes(b"the book")
        j = {"id": "j1", "phase": "converting", "gen": 1, "claimed": 1, "dest": "mine", "dest_type": "usb",
             "auto_copy": True, "title": "Book", "author": "W", "keep": False, "files": {"final": str(final)}}
        if overwrite:
            j["overwrite"], j["overwrite_for"] = overwrite, ["SERIALAAAAAA", "documents/Books", "Book.epub"]
        sh.jobs.append(j)
        k = kindle.Kindle(serial="SERIALAAAAAA", product="Kindle", kind="ssh", sysfs="", mount="kindle", storage="/mnt/us/")
        on_kindle = dict(there or {})                       # name -> bytes
        copies = []

        def copy_then_lose_the_answer(kk, src, name, thumb=None, folder="documents", **kw):
            copies.append(name)
            if lands or len(copies) > 1:
                on_kindle[name] = src.read_bytes()          # the Kindle has it, renamed into place...
            if len(copies) == 1:
                raise kindle.KindleUnreachable("the Kindle stopped answering over Wi-Fi")   # ...but the answer is lost
        saved = kindle.exists, kindle.same_book, kindle.copy, kindle.prepare, kindle.show_new_books, daemon.notify
        kindle.exists = lambda kk, name, folder="documents": name in on_kindle
        kindle.same_book = lambda kk, name, folder, src: on_kindle.get(name) == src.read_bytes()
        kindle.copy = copy_then_lose_the_answer
        kindle.prepare, kindle.show_new_books = (lambda kk: kk), (lambda kk: None)
        daemon.notify = lambda *a, **kw: None
        sh.reach = lambda dest: k
        try:
            sh.worker.gen = 1
            sh._deliver(j)                                      # copied; its outcome lost: back to waiting
            self.assertEqual(j["phase"], "waiting")
            on_kindle.update(meanwhile or {})
            with sh.lock:
                gen = JS.requeue(sh.jobs, j, ("waiting",))      # the Wi-Fi watcher finds the Kindle again
                self.assertTrue(JS.claim(sh.jobs, j, gen))
            sh.worker.gen = gen
            try:
                sh._deliver(j)
            except daemon.NeedsConfirm:
                pass
        finally:
            kindle.exists, kindle.same_book, kindle.copy, kindle.prepare, kindle.show_new_books, daemon.notify = saved
            sh.worker.gen = None
        return j, copies, on_kindle

    def test_the_copy_that_made_it_is_not_asked_about(self):
        with tempfile.TemporaryDirectory() as d:
            j, copies, _ = self._sent_twice(d)
            self.assertEqual((j["phase"], j.get("confirm"), copies), ("sent", None, ["Book.epub"]))

    def test_keep_both_makes_one_copy_not_two(self):
        with tempfile.TemporaryDirectory() as d:
            j, copies, _ = self._sent_twice(d, overwrite="both", there={"Book.epub": b"another book, 99 bytes"})
            self.assertEqual((j["phase"], copies, j["kindle_name"]), ("sent", ["Book (2).epub"], "Book (2).epub"))

    def test_another_file_at_that_name_is_not_taken_for_the_copy(self):
        with tempfile.TemporaryDirectory() as d:
            # the first copy never landed, and meanwhile something else of that name is on the Kindle: asked about it
            j, copies, _ = self._sent_twice(d, lands=False, meanwhile={"Book.epub": b"another book, 99 bytes"})
            self.assertEqual((j["phase"], copies), ("confirm", ["Book.epub"]))
            self.assertEqual(j["confirm"]["name"], "Book.epub")

    def test_a_replace_that_never_landed_is_sent_again(self):                  # C5
        with tempfile.TemporaryDirectory() as d:
            # the Kindle has an old book of the same size; "replace"; the first copy fails before writing anything
            j, copies, on_kindle = self._sent_twice(d, overwrite="replace", there={"Book.epub": b"old book"}, lands=False)
            self.assertEqual((j["phase"], copies), ("sent", ["Book.epub", "Book.epub"]))
            self.assertEqual(on_kindle["Book.epub"], b"the book")
