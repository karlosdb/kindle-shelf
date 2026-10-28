"""Who may change a job, and when: the jobs' lease.

A job is changed by two kinds of actor: the worker running it (Shelf._run: fetch, check, cover, convert, copy) and
everything else: the card's and the CLI's commands (cancel, retry, confirm, deliver, remove) and the USB and Wi-Fi
watchers. Without one rule they overwrote each other (Codex review, 2026-10-19): a watcher re-queued a book cancelled
during its probe (F2), a worker turned a job cancelled during a slow metadata read into "confirm" (F10), and a guard
against two workers dropped the continuation the user had just confirmed while the first worker was finishing (F9).

The rule, in one place:
  - Every (re)queue gives the job a new generation, and the worker submitted for it carries that number.
  - claim(): a worker runs only its own generation, and one worker per generation.
  - advance(): every change a worker makes is checked against its generation. Once the job was cancelled, removed or
    re-queued, the change is refused and the worker stops without touching the job again.
  - requeue() / cancel() / forget(): everyone else changes (or drops) a job only from the phases those changes are
    defined for, checked in the same step as the change.
  - overwrite_answer() / mismatch_accepted(): a user's answer counts only for what it was asked about.
All of them are called under the shelf's lock; none of them saves (save() takes save_lock, then the lock).
"""
from __future__ import annotations

ACTIVE = frozenset({"queued", "fetching", "checking", "converting", "copying", "emailing"})
CANCELLABLE = ACTIVE | {"waiting", "confirm", "error"}
FORGET_SENT = 60 * 86400        # a sent book whose laptop copy is gone leaves the list this long after its last change
FORGET_CANCELLED = 86400        # a cancelled one this long after


def listed(jobs: list[dict], j: dict) -> bool:
    return any(x is j for x in jobs)


def requeue(jobs: list[dict], j: dict, from_phases, **changes) -> int | None:
    """Back to the queue from one of `from_phases` with a new generation for its worker to carry; None when the job is
    gone or no longer in those phases (someone changed it first: it stays as they left it)."""
    if not listed(jobs, j) or j.get("phase") not in from_phases:
        return None
    j.update(changes)
    j["phase"] = "queued"
    j["gen"] = j.get("gen", 0) + 1
    return j["gen"]


def claim(jobs: list[dict], j: dict, gen: int) -> bool:
    """The worker submitted for generation `gen` may run the job: still listed and queued, that generation, and no
    other worker has it."""
    if not listed(jobs, j) or j.get("phase") != "queued" or j.get("gen", 0) != gen or j.get("claimed") == gen:
        return False
    j["claimed"] = gen
    return True


def owns(jobs: list[dict], j: dict, gen: int) -> bool:
    """The worker for `gen` still holds the job: nothing cancelled, removed or re-queued it since its claim."""
    return listed(jobs, j) and j.get("gen", 0) == gen and j.get("claimed") == gen


def advance(jobs: list[dict], j: dict, gen: int, **changes) -> bool:
    """A worker's change, applied only while it still holds the job."""
    if not owns(jobs, j, gen):
        return False
    j.update(changes)
    return True


def cancel(jobs: list[dict], j: dict) -> bool:
    """Cancelled from any phase that can be; the new generation nobody carries ends the worker's hold."""
    if not listed(jobs, j) or j.get("phase") not in CANCELLABLE:
        return False
    j.update(phase="cancelled", detail="Cancelled", progress=0)
    j["gen"] = j.get("gen", 0) + 1
    return True


def removable(j: dict) -> bool:
    """Only a job no worker is running can be removed from the list."""
    return j.get("phase") not in ACTIVE


def overwrite_answer(j: dict, serial: str, folder: str, name: str) -> str | None:
    """The user's "replace" or "both" for this name in this folder on this Kindle, when that is what it was said about
    (review F28: after a rebinding or a folder change, the Kindle that gets the book is asked about first)."""
    return j.get("overwrite") if j.get("overwrite_for") == [serial, folder, name] else None


def mismatch_accepted(j: dict, src: str) -> bool:
    """Whether the file at `src` may go although it doesn't look like the book picked: a book from this computer always
    (the user picked that file); a download only when "send anyway" was said about that very file (review F28: a
    re-fetch can bring another; F30: an answer that names no file, given before answers named one, is about none)."""
    if (j.get("copy") or {}).get("source") == "file":
        return True
    return bool(j.get("accept_mismatch")) and j.get("accept_mismatch_for") == src


def forget(jobs: list[dict], j: dict, now: float) -> bool:
    """Old history leaves the list: a sent book whose laptop copy is gone, or a cancelled one, long enough after its last
    change. Checked in the same step as the removal (review F26: a Retry between the janitor's look and its removal
    made the book queued again, and the janitor removed it anyway)."""
    if not listed(jobs, j):
        return False
    age = now - j.get("updated", now)
    if not ((j.get("phase") == "sent" and j.get("files_deleted") and age > FORGET_SENT)
            or (j.get("phase") == "cancelled" and age > FORGET_CANCELLED)):
        return False
    jobs.remove(j)
    return True
