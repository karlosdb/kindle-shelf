"""Paths and settings.

Settings live in ~/.config/kindle-shelf/config.toml (0600). The TorBox key is `torbox_key`
there, or else read from torbox-flow's config (~/.config/torbox/config.toml, `api_key`)
so there is one place to change it.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "kindle-shelf"
CONFIG_PATH = CONFIG_DIR / "config.toml"
TORBOX_CONFIG = HOME / ".config" / "torbox" / "config.toml"
STATE_DIR = HOME / ".local" / "state" / "kindle-shelf"
JOBS_PATH = STATE_DIR / "jobs.json"
CACHE_DIR = HOME / ".cache" / "kindle-shelf"
WORK_DIR = CACHE_DIR / "work"
COVER_DIR = CACHE_DIR / "covers"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "kindle-shelf"
STATE_PATH = RUNTIME_DIR / "state.json"
SOCKET_PATH = RUNTIME_DIR / "ctl.sock"

DEFAULT_CONFIG = """\
# Kindle Shelf settings. Edited by hand or by `kindle-shelf name-kindle`.

books_dir = "~/Books"        # Outbox/, Sent/ and kept books live here
keep_days = 7                # laptop copy is deleted this long after it reaches a Kindle
max_parallel = 3             # books fetched at once
libgen_url = "https://libgen.li"
torbox_key = ""              # or api_key in ~/.config/torbox/config.toml

# What a toast's click runs to open your Kindle Shelf card, if you have one (empty: no click), e.g.
# card_command = ["omarchy-shell", "my.books", "open"]

# Anna's Archive: only used if you have a membership key (their fast-download API).
annas_url = "https://annas-archive.gl"
annas_key = ""

# Kindles connected over USB. `serial` is filled in the first time each one is
# plugged in and named from the card. Optional per Kindle: `format = "epub"` sends
# EPUB instead of AZW3 (for KOReader), `folder` is where books go (default "documents"),
# `ssh = ["kindle", "kindle-ts"]` also sends over Wi-Fi to a jailbroken Kindle running an
# SSH server (hosts from ~/.ssh/config, tried in order: home network first, then Tailscale).
[[kindle]]
id = "mine"
name = "My Paperwhite"
serial = ""

[[kindle]]
id = "second"
name = "Second Kindle"
serial = ""

# Send-to-Kindle by email. Add destinations and SMTP details to enable it, e.g.
# [[email]]
# id = "mine-email"
# name = "My Paperwhite (email)"
# address = "you_xxxx@kindle.com"
#
# [smtp]
# host = "smtp.gmail.com"
# port = 587
# user = "you@gmail.com"
# password = "gmail app password"
"""


@dataclass
class KindleDest:
    id: str
    name: str
    serial: str = ""
    format: str = "azw3"        # "azw3" for Amazon's reader, "epub" for KOReader
    folder: str = "documents"   # where books go, relative to the Kindle's storage root
    ssh: list[str] = field(default_factory=list)  # ssh hosts that reach it over Wi-Fi, in order


@dataclass
class EmailDest:
    id: str
    name: str
    address: str


@dataclass
class Config:
    torbox_key: str = ""
    books_dir: Path = HOME / "Books"
    keep_days: int = 7
    max_parallel: int = 3
    libgen_url: str = "https://libgen.li"
    annas_url: str = "https://annas-archive.gl"
    annas_key: str = ""
    kindles: list[KindleDest] = field(default_factory=list)
    emails: list[EmailDest] = field(default_factory=list)
    smtp: dict = field(default_factory=dict)
    card_command: list[str] = field(default_factory=list)   # opens the card from a toast; [] = no click

    @property
    def outbox_dir(self) -> Path:
        return self.books_dir / "Outbox"

    @property
    def sent_dir(self) -> Path:
        return self.books_dir / "Sent"

    def kindle_by_id(self, dest_id: str) -> KindleDest | None:
        return next((k for k in self.kindles if k.id == dest_id), None)

    def kindle_by_serial(self, serial: str) -> KindleDest | None:
        return next((k for k in self.kindles if serial and k.serial == serial), None)

    def email_by_id(self, dest_id: str) -> EmailDest | None:
        return next((e for e in self.emails if e.id == dest_id), None)

    @property
    def email_ready(self) -> bool:
        return bool(self.smtp.get("host") and self.smtp.get("user") and self.smtp.get("password"))


def _hosts(value) -> list[str]:
    """`ssh = "kindle"` or `ssh = ["kindle", "kindle-ts"]` → ["kindle", ...]; anything else → []."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [h.strip() for h in value if isinstance(h, str) and h.strip()]


def ensure_config() -> None:
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
        CONFIG_PATH.chmod(0o600)


def load() -> Config:
    ensure_config()
    raw = tomllib.loads(CONFIG_PATH.read_text())
    cfg = Config()
    try:
        cfg.torbox_key = tomllib.loads(TORBOX_CONFIG.read_text()).get("api_key", "")
    except (OSError, tomllib.TOMLDecodeError):
        pass
    cfg.torbox_key = raw.get("torbox_key") or cfg.torbox_key
    cfg.books_dir = Path(os.path.expanduser(raw.get("books_dir", "~/Books")))
    cfg.keep_days = int(raw.get("keep_days", 7))
    cfg.max_parallel = max(1, int(raw.get("max_parallel", 3)))
    cfg.libgen_url = str(raw.get("libgen_url", cfg.libgen_url)).rstrip("/")
    cfg.annas_url = str(raw.get("annas_url", cfg.annas_url)).rstrip("/")
    cfg.annas_key = str(raw.get("annas_key", ""))
    cfg.kindles = [KindleDest(str(k["id"]), str(k.get("name") or k["id"]), str(k.get("serial", "")),
                              str(k.get("format", "azw3")).lower(), str(k.get("folder", "documents")).strip("/") or "documents",
                              _hosts(k.get("ssh")))
                   for k in raw.get("kindle", [])]
    cfg.emails = [EmailDest(str(e["id"]), str(e.get("name") or e["id"]), str(e["address"])) for e in raw.get("email", []) if e.get("address")]
    cfg.smtp = dict(raw.get("smtp", {}))
    card = raw.get("card_command") or []
    cfg.card_command = [str(a) for a in card] if isinstance(card, list) else []
    return cfg


def set_kindle_serial(dest_id: str, serial: str) -> None:
    """Bind a USB serial to a Kindle destination, editing the TOML in place so
    comments survive. Any other destination holding that serial is cleared."""
    ensure_config()
    lines = CONFIG_PATH.read_text().splitlines()
    out: list[str] = []
    current_id: str | None = None
    in_kindle = False
    for line in lines:
        s = line.strip()
        if s.startswith("[["):
            in_kindle = s == "[[kindle]]"
            current_id = None
        elif s.startswith("["):
            in_kindle = False
        if in_kindle and s.startswith("id") and "=" in s:
            current_id = s.split("=", 1)[1].strip().strip('"')
        if in_kindle and s.startswith("serial") and "=" in s:
            value = serial if current_id == dest_id else None
            if value is None:
                existing = s.split("=", 1)[1].strip().strip('"')
                value = "" if existing == serial else existing
            line = f'serial = "{value}"'
        out.append(line)
    CONFIG_PATH.write_text("\n".join(out) + "\n")
    CONFIG_PATH.chmod(0o600)
