"""Resume-safe account store.

Accounts (and failed attempts) are appended as one JSON object per
line to a JSONL file. Appends are atomic enough for a single writer:
each record is written with one write() + flush() + fsync().

Resume safety:
  * The store is append-only; restarting the tool never deletes or
    rewrites existing records.
  * Usernames already in the file are skipped on the next run, so a
    restart never retries (or duplicates) an account that was already
    attempted or created.
  * Every record carries a UTC timestamp, the stage reached, and the
    credentials (when created) — so the file alone is the full audit
    trail of a run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .identities import Identity


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AccountStore:
    """Append-only JSONL store for attempt records."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._seen_usernames: set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        """Index usernames already recorded (for resume/dedupe)."""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and rec.get("username"):
                        self._seen_usernames.add(str(rec["username"]))
        except OSError:
            pass

    @property
    def seen_usernames(self) -> set[str]:
        return set(self._seen_usernames)

    def already_attempted(self, username: str) -> bool:
        return username in self._seen_usernames

    def record(self, identity: Identity, stage: str, detail: str = "",
               http_status: int = 0) -> dict:
        """Append one attempt record and return it."""
        rec = {
            "ts": _utcnow(),
            "email": identity.email,
            "username": identity.username,
            "first_name": identity.first_name,
            "last_name": identity.last_name,
            # Password is stored so a created account stays usable.
            # Keep this file private (mode 600) — see README.
            "password": identity.password,
            "birth_year": identity.birth_year,
            "birth_month": identity.birth_month,
            "birth_day": identity.birth_day,
            "gender": identity.gender,
            "stage": stage,
            "detail": detail,
            "http_status": http_status,
            "created": stage == "created",
        }
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        # Append + flush + fsync so a crash cannot lose the record.
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self._seen_usernames.add(identity.username)
        # Restrict the file to the owner: it contains passwords.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return rec
