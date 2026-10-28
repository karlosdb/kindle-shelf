"""The few TorBox endpoints books need (same calls as torbox-flow's client)."""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import requests

BASE = "https://api.torbox.app/v1/api"
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
]


class TorboxError(RuntimeError):
    pass


def magnet_for(info_hash: str, name: str = "") -> str:
    m = f"magnet:?xt=urn:btih:{info_hash.lower()}"
    if name:
        m += f"&dn={quote(name)}"
    return m + "".join(f"&tr={quote(t)}" for t in TRACKERS)


class Torbox:
    def __init__(self, api_key: str, timeout: float = 30.0):
        if not api_key:
            raise TorboxError("no TorBox API key (expected in ~/.config/torbox/config.toml)")
        self.key = api_key
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key}", "User-Agent": "kindle-shelf/1.0"})

    def _call(self, method: str, path: str, **kw) -> Any:
        kw.setdefault("timeout", self.timeout)
        for attempt in range(3):
            try:
                r = self.s.request(method, f"{BASE}{path}", **kw)
            except requests.RequestException as e:
                if attempt == 2:
                    raise TorboxError(f"can't reach TorBox: {e}") from e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            try:
                body = r.json()
            except ValueError:
                raise TorboxError(f"TorBox answered HTTP {r.status_code}")
            if not body.get("success", False):
                raise TorboxError(str(body.get("detail") or body.get("error") or f"HTTP {r.status_code}"))
            return body.get("data")
        raise TorboxError("TorBox is rate limiting, try again in a minute")

    def cached(self, hashes: list[str]) -> set[str]:
        hashes = [h.lower() for h in hashes if h]
        out: set[str] = set()
        for i in range(0, len(hashes), 80):
            data = self._call("GET", "/torrents/checkcached", params={"hash": ",".join(hashes[i:i + 80]), "format": "list"})
            for d in data or []:
                if d and d.get("hash"):
                    out.add(str(d["hash"]).lower())
        return out

    def add(self, info_hash: str, name: str) -> int:
        form = {"magnet": (None, magnet_for(info_hash, name)), "seed": (None, "1"), "allow_zip": (None, "false")}
        try:
            data = self._call("POST", "/torrents/createtorrent", files=form) or {}
            tid = data.get("torrent_id") or data.get("id")
            if tid:
                return int(tid)
        except TorboxError:
            # Already on the account counts as success: find it below.
            pass
        for t in self._call("GET", "/torrents/mylist", params={"bypass_cache": "true", "limit": 1000}) or []:
            if str(t.get("hash", "")).lower() == info_hash.lower():
                return int(t["id"])
        raise TorboxError("TorBox didn't accept the torrent")

    def get(self, torrent_id: int) -> dict:
        data = self._call("GET", "/torrents/mylist", params={"id": torrent_id, "bypass_cache": "true"})
        if isinstance(data, list):
            data = next((d for d in data if int(d.get("id", -1)) == torrent_id), None)
        if not data:
            raise TorboxError("torrent disappeared from TorBox")
        return data

    def link(self, torrent_id: int, file_id: int) -> str:
        url = self._call("GET", "/torrents/requestdl", params={"token": self.key, "torrent_id": torrent_id, "file_id": file_id, "redirect": "false"})
        if not isinstance(url, str) or not url.startswith("http"):
            raise TorboxError("TorBox didn't return a download link")
        return url
