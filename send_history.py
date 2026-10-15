"""Durable forwarding receipts and a lock shared by cron and manual runs."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def mailbox_scope(config: dict, mode: str) -> str:
    return json.dumps(
        [config["host"].rstrip("/").lower(), config["email"].strip().lower(), mode],
        separators=(",", ":"),
    )


@contextmanager
def forwarding_lock(path: Path):
    """Skip overlapping runs rather than letting two processes send the same mail."""
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class SendHistory:
    def __init__(self, path: Path, scope: str):
        self.path = path
        self.scope = scope
        self.connection = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.connection = sqlite3.connect(self.path)
        try:
            self.connection.execute("PRAGMA synchronous = FULL")
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sends (
                    scope TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'sent')),
                    sent_id TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (scope, source_id)
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    scope TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (scope, key)
                );
                """
            )
        except BaseException:
            self.connection.close()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.connection.close()

    def status(self, source_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT state FROM sends WHERE scope = ? AND source_id = ?",
            (self.scope, str(source_id)),
        ).fetchone()
        return row[0] if row else None

    def begin_send(self, source_id: str) -> None:
        # Commit before the network call: an interrupted or ambiguous send must
        # be checked in Sent, rather than automatically sent a second time.
        with self.connection:
            self.connection.execute(
                "INSERT INTO sends (scope, source_id, state) VALUES (?, ?, 'pending')",
                (self.scope, str(source_id)),
            )

    def confirm_sent(self, source_id: str, sent_id: str) -> None:
        if not sent_id:
            raise RuntimeError("Send returned no receipt; check Sent before retrying")
        with self.connection:
            result = self.connection.execute(
                """UPDATE sends SET state = 'sent', sent_id = ?,
                   updated_at = CURRENT_TIMESTAMP
                   WHERE scope = ? AND source_id = ? AND state = 'pending'""",
                (str(sent_id), self.scope, str(source_id)),
            )
            if result.rowcount != 1:
                raise RuntimeError("Pending send record missing; check Sent before retrying")

    def import_legacy_successes(self, log_path: Path) -> int:
        key = "legacy_success_log_v1"
        if self.connection.execute(
            "SELECT 1 FROM metadata WHERE scope = ? AND key = ?",
            (self.scope, key),
        ).fetchone():
            return 0
        ids = set()
        if log_path.is_file():
            with log_path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    match = re.match(r"^OK\s+id=(\S+)(?:\s|$)", line)
                    if match:
                        ids.add(match.group(1))
        with self.connection:
            before = self.connection.total_changes
            self.connection.executemany(
                "INSERT OR IGNORE INTO sends (scope, source_id, state) VALUES (?, ?, 'sent')",
                [(self.scope, source_id) for source_id in ids],
            )
            imported = self.connection.total_changes - before
            self.connection.execute(
                "INSERT INTO metadata (scope, key, value) VALUES (?, ?, ?)",
                (self.scope, key, str(imported)),
            )
        return imported
