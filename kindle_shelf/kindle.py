"""Finding a Kindle and copying books onto it.

Three kinds, all handled:
  * USB drive (mass storage) — older Kindles such as the 2012 Paperwhite. udiskie
    mounts it at /run/media/$USER/Kindle; books go in documents/.
  * MTP — 2024-and-newer Kindles. Reached through gvfs (`gio`), books go in
    <storage>/documents/.
  * SSH — a jailbroken Kindle on Wi-Fi with an SSH server (KOReader's), reached through
    the ssh hosts named in its config entry (home network first, then Tailscale). It's
    recognised by the same serial as over USB (/proc/usid), so the same entry applies.
Any kind can be given another folder in the config (the jailbroken Paperwhite
takes EPUBs in documents/Books/ for KOReader).
"""
from __future__ import annotations

import filecmp
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

AMAZON_VENDOR = "1949"


class KindleError(RuntimeError):
    pass


class KindleUnreachable(KindleError):
    """The Kindle on Wi-Fi stopped answering (asleep, out of range, SSH server off): try again later."""


@dataclass
class Kindle:
    serial: str
    product: str
    kind: str              # "msc" | "mtp" | "ssh"
    sysfs: str
    block: str = ""         # /dev/sdX for msc
    mount: str = ""         # mount point (msc), gio root (mtp) or ssh host (ssh)
    storage: str = ""       # mtp: e.g. "mtp://Amazon_Kindle_G0001/Internal Storage/"; ssh: "/mnt/us/"
    free: int = 0
    total: int = 0
    parked: bool = False    # msc: unmounted by us after copying, so pulling the cable leaves the FAT clean

    @property
    def ready(self) -> bool:
        return bool((self.mount or self.parked) if self.kind == "msc" else self.storage)

    @property
    def wifi(self) -> bool:
        return self.kind == "ssh"


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def _read(p: str) -> str:
    try:
        return Path(p).read_text().strip()
    except OSError:
        return ""


def scan() -> list[Kindle]:
    """Every Amazon e-reader on the USB bus right now (cheap: sysfs only)."""
    out = []
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        if _read(f"{dev}/idVendor") != AMAZON_VENDOR or not os.path.exists(f"{dev}/serial"):
            continue
        blocks = glob.glob(f"{dev}/*/host*/target*/*/block/sd*")
        out.append(Kindle(
            serial=_read(f"{dev}/serial"),
            product=_read(f"{dev}/product") or "Kindle",
            kind="msc" if blocks else "mtp",
            sysfs=dev,
            block="/dev/" + os.path.basename(blocks[0]) if blocks else "",
        ))
    return out


# ---- USB drive -------------------------------------------------------------------

def _msc_mountpoint(block: str) -> tuple[str, str]:
    """(mountpoint, device) of the Kindle's filesystem, which may be the whole disk or a partition."""
    r = _run(["lsblk", "-J", "-o", "PATH,MOUNTPOINT,FSTYPE", block])
    try:
        devs = json.loads(r.stdout).get("blockdevices", [])
    except ValueError:
        return "", ""
    flat = []
    def walk(ds):
        for d in ds:
            flat.append(d)
            walk(d.get("children") or [])
    walk(devs)
    for d in flat:
        if d.get("mountpoint"):
            return d["mountpoint"], d["path"]
    fs = next((d for d in flat if d.get("fstype")), None)
    return "", fs["path"] if fs else ""


def prepare(k: Kindle) -> Kindle:
    """Make sure the Kindle is mounted/reachable and fill in free space."""
    if k.kind == "ssh":
        r = _ssh(k.mount, _only_on(k.serial) + "df -k /mnt/us | tail -n 1", timeout=15)
        if r.returncode == 0:
            k.free, k.total = _space(_out(r).strip())
        return k
    if k.kind == "msc":
        k.parked = False
        mount, dev = _msc_mountpoint(k.block)
        if not mount and dev:
            for _ in range(5):  # give udiskie a moment before mounting it ourselves
                time.sleep(1)
                mount, dev = _msc_mountpoint(k.block)
                if mount:
                    break
            if not mount:
                _run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"])
                mount, dev = _msc_mountpoint(k.block)
        k.mount = mount
        if mount:
            (Path(mount) / "documents").mkdir(exist_ok=True)
            st = os.statvfs(mount)
            k.free, k.total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    else:
        root = _mtp_root(k.serial)
        if root:
            _run(["gio", "mount", root], timeout=30)  # "already mounted" is fine
            k.mount = root
            r = _run(["gio", "list", root], timeout=30)
            stores = [s for s in r.stdout.splitlines() if s.strip()]
            if stores:
                k.storage = root + stores[0] + "/"
                info = _run(["gio", "info", "-f", k.storage], timeout=30).stdout
                for line in info.splitlines():
                    line = line.strip()
                    if line.startswith("filesystem::free:"):
                        k.free = int(line.split(":")[-1].strip() or 0)
                    elif line.startswith("filesystem::size:"):
                        k.total = int(line.split(":")[-1].strip() or 0)
                _run(["gio", "mkdir", k.storage + "documents"], timeout=30)
    return k


def _mtp_root(serial: str) -> str:
    r = _run(["gio", "mount", "-li"], timeout=20)
    roots = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("activation_root=mtp://"):
            roots.append(line.split("=", 1)[1])
    for root in roots:
        if serial and serial in root:
            return root if root.endswith("/") else root + "/"
    kindles = [r for r in roots if "kindle" in r.lower() or "amazon" in r.lower()]
    return kindles[0].rstrip("/") + "/" if kindles else ""


# ---- over Wi-Fi (SSH) --------------------------------------------------------------

STORAGE = "/mnt/us/"   # the Kindle's user storage, the same files USB shows
USID = "/proc/usid"    # the Kindle's serial, then a NUL (tests point this at a file of their own)
WRONG_KINDLE = 7       # a remote command's exit status when the device answering isn't the Kindle it was meant for
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
SERIAL = re.compile(r"[A-Z0-9]{10,32}")


def _ssh_argv(host: str) -> list[str]:
    """The command that runs one shell command on `host` (tests swap in a local shell)."""
    return ["ssh", *SSH_OPTS, host]


def _ssh(host: str, remote: str, timeout: int = 30, stdin=None, input: str | None = None) -> subprocess.CompletedProcess:
    """Run `remote` (a POSIX shell command) on the Kindle. Exit 255 is ssh's own failure, 124 our timeout."""
    try:
        return subprocess.run(_ssh_argv(host) + [remote], stdin=stdin, input=input, capture_output=True,
                              text=stdin is None, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(remote, 124, "", "timed out")


def _out(r: subprocess.CompletedProcess) -> str:
    return r.stdout.decode(errors="replace") if isinstance(r.stdout, bytes) else (r.stdout or "")


def _err(r: subprocess.CompletedProcess) -> str:
    return r.stderr.decode(errors="replace") if isinstance(r.stderr, bytes) else (r.stderr or "")


def _lost(r: subprocess.CompletedProcess) -> bool:
    return r.returncode in (124, 255, WRONG_KINDLE)


def _only_on(serial: str) -> str:
    """The start of every command that looks at or writes books over SSH: it goes on only on the Kindle with `serial`.
    The probe saw that serial, but the next connection to the same host name can reach another device that SSH also
    trusts (review C1), so each connection checks for itself, before anything else runs."""
    n = len(serial)
    return (f'[ "$(head -c {n} {shlex.quote(USID)} 2>/dev/null)" = {shlex.quote(serial)} ] && '
            f'[ $(wc -c < {shlex.quote(USID)}) -le {n + 1} ] || exit {WRONG_KINDLE}; ')


def _space(line: str) -> tuple[int, int]:
    """(free, total) bytes from one `df -k` line: filesystem, 1K-blocks, used, available, …"""
    f = line.split()
    try:
        return int(f[3]) * 1024, int(f[1]) * 1024
    except (IndexError, ValueError):
        return 0, 0


def probe(hosts: list[str]) -> Kindle | None:
    """The Kindle answering on the first of `hosts` that does right now (its serial and free space),
    or None when none answers: asleep, off Wi-Fi, or its SSH server isn't running."""
    for host in hosts:
        # /proc/usid ends in a NUL, not a newline: read words, not lines (serial, then df's six columns)
        r = _ssh(host, "cat /proc/usid; echo; df -k /mnt/us | tail -n 1", timeout=15)
        if r.returncode != 0:
            continue
        words = _out(r).replace("\x00", " ").split()
        if not words or not SERIAL.fullmatch(words[0]):
            continue
        free, total = _space(" ".join(words[1:7]))
        return Kindle(serial=words[0], product="Kindle", kind="ssh", sysfs="", mount=host, storage=STORAGE,
                      free=free, total=total)
    return None


# Asks KOReader, through the Quiet bridge of Quiet for Kindle (github.com/karlosdb/quiet-kindle,
# kindle/patches/2-quiet-bridge.lua, which reads one line from /tmp/quiet-cmd), to re-read its Library so a new
# book shows without a restart. In a book it does nothing.
REFRESH_LIBRARY = ('lua local R = package.loaded["apps/reader/readerui"] if R and R.instance then return "in a book" end '
                   'local FM = package.loaded["apps/filemanager/filemanager"] local fm = FM and FM.instance '
                   'if not fm then return "no library" end fm:onRefresh() return "library refreshed"')


def show_new_books(k: Kindle) -> None:
    """Best effort: never waits for KOReader and never overwrites a command already waiting for the bridge."""
    if k.kind == "ssh":
        _ssh(k.mount, _only_on(k.serial) + "[ -e /tmp/quiet-cmd ] || cat > /tmp/quiet-cmd", timeout=15,
             input=REFRESH_LIBRARY + "\n")


# ---- copying -----------------------------------------------------------------------

def exists(k: Kindle, name: str, folder: str = "documents") -> bool:
    if k.kind == "msc":
        return (Path(k.mount) / folder / name).exists()
    if k.kind == "ssh":
        r = _ssh(k.mount, _only_on(k.serial) + "test -e " + shlex.quote(k.storage + folder + "/" + name), timeout=20)
        if _lost(r):
            raise KindleUnreachable(_why(r))
        return r.returncode == 0
    return _run(["gio", "info", k.storage + folder + "/" + name], timeout=30).returncode == 0


def same_book(k: Kindle, name: str, folder: str, src: Path) -> bool | None:
    """Whether <folder>/<name> on the Kindle holds exactly the bytes of `src`: only the bytes prove it is this book (a
    file of the same size can be another one, review C5). None when it can't tell: MTP can't read a file in place."""
    if k.kind == "msc":
        there = Path(k.mount) / folder / name
        try:
            return there.is_file() and filecmp.cmp(src, there, shallow=False)
        except OSError:
            return False
    if k.kind == "ssh":
        r = _ssh(k.mount, _only_on(k.serial) + "md5sum < " + shlex.quote(k.storage + folder + "/" + name),
                 timeout=60 + src.stat().st_size // (1024 * 1024))
        if _lost(r):
            raise KindleUnreachable(_why(r))
        words = _out(r).split()
        if r.returncode != 0 or not words:
            return False
        with open(src, "rb") as f:
            return words[0] == hashlib.file_digest(f, "md5").hexdigest()
    return None


def _why(r: subprocess.CompletedProcess) -> str:
    if r.returncode == WRONG_KINDLE:
        return "another device answers there, not this Kindle"
    return "the Kindle stopped answering over Wi-Fi"


def copy(k: Kindle, src: Path, name: str, thumb: tuple[str, Path] | None = None, folder: str = "documents",
         tag: str = "") -> None:
    """Copy `src` onto the Kindle as <folder>/<name>. `thumb` = (asin, jpg) writes the
    cover thumbnail older Kindles need for sideloaded books. The book is written under a hidden temp name that
    carries `tag` (the job's id, so a retry reuses its own; review C2: a temp name made from the book's name alone
    could be a file that's already there)."""
    tag = tag or uuid.uuid4().hex[:10]
    if k.kind == "ssh":
        _copy_ssh(k, src, name, folder, tag)
        return
    if k.kind == "msc":
        docs = Path(k.mount) / folder
        docs.mkdir(parents=True, exist_ok=True)
        tmp = docs / f".part-{tag}-{name}"
        size = src.stat().st_size
        shutil.copyfile(src, tmp)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        got = tmp.stat().st_size
        if got != size:   # never publish a short copy (the source changed under us)
            tmp.unlink(missing_ok=True)
            raise KindleError(f"the copy came out at {got} of {size} bytes; try again")
        os.replace(tmp, docs / name)
        if thumb:
            tdir = Path(k.mount) / "system" / "thumbnails"
            if tdir.is_dir():
                shutil.copyfile(thumb[1], tdir / f"thumbnail_{thumb[0]}_EBOK_portrait.jpg")
        fd = os.open(k.mount, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _run(["sync", "-f", k.mount], timeout=60)
    else:
        if folder != "documents":
            _run(["gio", "mkdir", "-p", k.storage + folder], timeout=30)  # "exists" is fine
        target = k.storage + folder + "/" + name
        size = src.stat().st_size
        state, before = _mtp_stat(target)   # a book being replaced, a free name, or (a failed look) unknown
        if state == "unknown":
            # without knowing what's there, a failed copy couldn't tell its own half-written file from an untouched
            # book (review F18): don't start
            raise KindleError("couldn't check the Kindle over USB; try again")
        r = _run(["gio", "copy", str(src), target], timeout=600)
        if r.returncode != 0:
            # MTP can't rename, so a copy that began writing leaves half a book under the real name: remove what
            # this copy made or changed (size or time), never a book it didn't touch (it can fail before writing)
            after_state, after = _mtp_stat(target)
            if after_state == "ok" and (state == "absent" or after != before):
                _run(["gio", "remove", target], timeout=30)
            raise KindleError((r.stderr.strip() or "copy over MTP failed")[:200])
        got_state, got = _mtp_stat(target)
        if got_state != "ok" or got[0] != size:   # gio said it copied: what's there is ours, and incomplete
            if got_state == "ok":
                _run(["gio", "remove", target], timeout=30)
            raise KindleError("the Kindle didn't keep the file" if got_state != "ok" else f"the Kindle kept {got[0]} of {size} bytes")


# gio's own words for "nothing there" (the last ": "-separated part of its error; the part before is the path, which
# can contain anything, a title like "Not Found" included: review F22)
GONE = {"no such file or directory", "file does not exist", "file not found", "the file does not exist"}


def _mtp_stat(target: str) -> tuple[str, tuple[int, int] | None]:
    """What's at `target` over MTP: ("ok", (size, modified)), ("absent", None) when gio says there's nothing, or
    ("unknown", None) when it couldn't look (a timeout or another error: that is not "absent")."""
    r = _run(["gio", "info", "-a", "standard::size,time::modified", target], timeout=30)
    if r.returncode == 0:
        size = re.search(r"standard::size:\s*(\d+)", r.stdout or "")
        when = re.search(r"time::modified:\s*(\d+)", r.stdout or "")
        if size:
            return "ok", (int(size.group(1)), int(when.group(1)) if when else 0)
        return "unknown", None
    lines = (r.stderr or "").strip().splitlines()
    reason = lines[-1].rsplit(": ", 1)[-1].strip().rstrip(".").lower() if lines else ""
    if r.returncode != 124 and reason in GONE:
        return "absent", None
    return "unknown", None


SHORT = 3   # the remote copy's exit status when fewer bytes arrived than were sent


def remote_copy(folder_path: str, name: str, size: int, serial: str, tag: str) -> str:
    """The shell command the Kindle runs for one book: only on the Kindle with `serial` (_only_on), receive it under a
    hidden temp name of this job's (KOReader doesn't list dot files), and only when exactly `size` bytes arrived
    rename it into place; a short stream is removed, never shown."""
    q = shlex.quote
    part, final = f"{folder_path}/.part-{tag}-{name}", f"{folder_path}/{name}"
    return (_only_on(serial) + f"mkdir -p {q(folder_path)} && cat > {q(part)} && n=$(wc -c < {q(part)} | tr -d ' ') && "
            f"if [ \"$n\" -eq {int(size)} ]; then mv -f {q(part)} {q(final)} && sync && echo \"$n\"; "
            f"else rm -f {q(part)}; echo \"got $n of {int(size)} bytes\" >&2; exit {SHORT}; fi")


def _copy_ssh(k: Kindle, src: Path, name: str, folder: str, tag: str) -> None:
    """Stream the book over ssh (see remote_copy: nothing is renamed into place before the Kindle has it all) and
    check the size it reports. Amazon's thumbnails don't matter here: a jailbroken Kindle gets EPUBs for KOReader."""
    q = shlex.quote
    folder_path = k.storage + folder
    part = f"{folder_path}/.part-{tag}-{name}"
    size = src.stat().st_size
    with open(src, "rb") as f:
        r = _ssh(k.mount, remote_copy(folder_path, name, size, k.serial, tag), timeout=60 + size // (100 * 1024),
                 stdin=f)   # >= 100 KB/s
    if r.returncode != 0:
        _ssh(k.mount, _only_on(k.serial) + "rm -f " + q(part), timeout=15)   # on another device it removes nothing
        if _lost(r):
            raise KindleUnreachable(_why(r))
        if r.returncode == SHORT:
            raise KindleError(("the copy arrived incomplete (" + _err(r).strip() + "); try again")[:200])
        raise KindleError(("copy over Wi-Fi failed: " + _err(r).strip())[:200])
    try:
        got = int(_out(r).strip().split()[-1])
    except (IndexError, ValueError):
        got = -1
    if got != size:
        raise KindleError(f"the Kindle kept {got} of {size} bytes")


def park(k: Kindle) -> bool:
    """Unmount a USB-drive Kindle but leave it connected: it stays in USB mode and is
    remounted by prepare() if another book comes, and unplugging can't leave the volume dirty."""
    if k.kind != "msc":
        return False
    mount, dev = _msc_mountpoint(k.block)
    if mount and dev:
        r = _run(["udisksctl", "unmount", "-b", dev, "--no-user-interaction"])
        if r.returncode != 0:
            return False
    k.mount, k.parked = "", True
    return True


def eject(k: Kindle) -> str:
    if k.kind == "ssh":
        return ""
    if k.kind == "msc":
        mount, dev = _msc_mountpoint(k.block)
        if dev:
            r = _run(["udisksctl", "unmount", "-b", dev, "--no-user-interaction"])
            if r.returncode != 0 and mount:
                return (r.stderr.strip() or "unmount failed")[:200]
        _run(["udisksctl", "power-off", "-b", k.block, "--no-user-interaction"])
        return ""
    if k.mount:
        _run(["gio", "mount", "-u", k.mount], timeout=30)
    return ""
