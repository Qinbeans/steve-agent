"""Conversations on disk, in SQLite. The server keeps working from memory and writes through to here."""

import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE conversations (
    thread_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workspace TEXT NOT NULL,
    head_response_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX conversations_by_workspace ON conversations (workspace, updated_at);
CREATE TABLE responses (
    response_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES conversations ON DELETE CASCADE,
    position INTEGER NOT NULL,
    checkpoint_id TEXT,
    parent_id TEXT,
    child_of TEXT,
    children TEXT NOT NULL,
    compaction INTEGER NOT NULL,
    input_items TEXT NOT NULL,
    response TEXT NOT NULL
);
CREATE INDEX responses_by_thread ON responses (thread_id, position);
CREATE TABLE checkpoints (
    thread_id TEXT NOT NULL REFERENCES conversations ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    checkpoint_type TEXT NOT NULL,
    checkpoint BLOB NOT NULL,
    metadata_type TEXT NOT NULL,
    metadata BLOB NOT NULL,
    parent_id TEXT,
    PRIMARY KEY (thread_id, namespace, checkpoint_id)
);
CREATE TABLE writes (
    thread_id TEXT NOT NULL REFERENCES conversations ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value BLOB NOT NULL,
    task_path TEXT NOT NULL,
    PRIMARY KEY (thread_id, namespace, checkpoint_id, task_id, idx)
);
CREATE TABLE blobs (
    thread_id TEXT NOT NULL REFERENCES conversations ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    value_type TEXT NOT NULL,
    value BLOB NOT NULL,
    PRIMARY KEY (thread_id, namespace, channel, version)
);
"""


@dataclass
class ThreadRows:
    """What a saved thread holds, in the shapes InMemorySaver and ResponseStore use."""

    responses: list[dict[str, Any]]
    checkpoints: list[tuple]
    writes: list[tuple]
    blobs: list[tuple]


class ConversationDb:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Created here so that it is private from the start. SQLite gives its side files the same mode.
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        # The event loop is the only user, but its thread is not the one that opens this.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        # Deleted means gone from the file, not only unlisted. delete() also empties the log.
        self._db.execute("PRAGMA secure_delete=ON")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > _SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"{path} is from a newer version of Steve.")
        if version == 0:
            with self._db:
                self._db.executescript(_SCHEMA + f"PRAGMA user_version={_SCHEMA_VERSION};")

    def close(self) -> None:
        self._db.close()

    def thread_of(self, response_id: str) -> str | None:
        row = self._db.execute("SELECT thread_id FROM responses WHERE response_id = ?", (response_id,)).fetchone()
        return row[0] if row else None

    def has_thread(self, thread_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM conversations WHERE thread_id = ?", (thread_id,)).fetchone() is not None

    def list_threads(self, workspace: str, limit: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """SELECT thread_id, title, updated_at, head_response_id FROM conversations
               WHERE workspace = ? ORDER BY updated_at DESC LIMIT ?""",
            (workspace, limit),
        )
        return [{"id": row[0], "title": row[1], "updated_at": row[2], "head_response_id": row[3]} for row in rows]

    def title(self, thread_id: str) -> str | None:
        row = self._db.execute("SELECT title FROM conversations WHERE thread_id = ?", (thread_id,)).fetchone()
        return row[0] if row else None

    def load(self, thread_id: str) -> ThreadRows:
        select = lambda sql: self._db.execute(sql, (thread_id,)).fetchall()  # noqa: E731
        responses = [
            {
                "response_id": row[0],
                "checkpoint_id": row[1],
                "parent_id": row[2],
                "child_of": row[3],
                "children": json.loads(row[4]),
                "compaction": bool(row[5]),
                "input_items": json.loads(row[6]),
                "response": json.loads(row[7]),
            }
            for row in select(
                """SELECT response_id, checkpoint_id, parent_id, child_of, children, compaction, input_items, response
                   FROM responses WHERE thread_id = ? ORDER BY position"""
            )
        ]
        return ThreadRows(
            responses,
            select(
                """SELECT namespace, checkpoint_id, checkpoint_type, checkpoint, metadata_type, metadata, parent_id
                   FROM checkpoints WHERE thread_id = ?"""
            ),
            select(
                """SELECT namespace, checkpoint_id, task_id, idx, channel, value_type, value, task_path
                   FROM writes WHERE thread_id = ?"""
            ),
            select("SELECT namespace, channel, version, value_type, value FROM blobs WHERE thread_id = ?"),
        )

    def save(self, thread_id: str, title: str, workspace: str, head: str | None, rows: ThreadRows) -> None:
        """Makes the saved thread match these rows in one transaction, writing only what changed."""
        now = time.time()
        with self._db:
            self._db.execute(
                """INSERT INTO conversations (thread_id, title, workspace, head_response_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (thread_id) DO UPDATE SET head_response_id = excluded.head_response_id,
                                                         updated_at = excluded.updated_at""",
                (thread_id, title, workspace, head, now, now),
            )
            self._sync_responses(thread_id, rows.responses)
            self._sync(
                "checkpoints",
                ("namespace", "checkpoint_id"),
                "namespace, checkpoint_id, checkpoint_type, checkpoint, metadata_type, metadata, parent_id",
                thread_id,
                rows.checkpoints,
            )
            self._sync(
                "writes",
                ("namespace", "checkpoint_id", "task_id", "idx"),
                "namespace, checkpoint_id, task_id, idx, channel, value_type, value, task_path",
                thread_id,
                rows.writes,
            )
            self._sync("blobs", ("namespace", "channel", "version"), "namespace, channel, version, value_type, value", thread_id, rows.blobs)

    def _sync(self, table: str, key_columns: tuple[str, ...], columns: str, thread_id: str, rows: list[tuple]) -> None:
        """These rows never change once written, so comparing their keys is enough."""
        size = len(key_columns)
        keys = ", ".join(key_columns)
        saved = {tuple(row) for row in self._db.execute(f"SELECT {keys} FROM {table} WHERE thread_id = ?", (thread_id,))}
        wanted = {tuple(row[:size]) for row in rows}
        match = " AND ".join(f"{column} = ?" for column in key_columns)
        self._db.executemany(f"DELETE FROM {table} WHERE thread_id = ? AND {match}", [(thread_id, *key) for key in saved - wanted])
        marks = ", ".join("?" * (len(rows[0]) + 1)) if rows else ""
        self._db.executemany(
            f"INSERT INTO {table} (thread_id, {columns}) VALUES ({marks})",
            [(thread_id, *row) for row in rows if tuple(row[:size]) not in saved],
        )

    def _sync_responses(self, thread_id: str, responses: list[dict[str, Any]]) -> None:
        saved = {row[0] for row in self._db.execute("SELECT response_id FROM responses WHERE thread_id = ?", (thread_id,))}
        wanted = {response["response_id"] for response in responses}
        self._db.executemany("DELETE FROM responses WHERE response_id = ?", [(id,) for id in saved - wanted])
        position = self._db.execute("SELECT COALESCE(MAX(position), 0) FROM responses WHERE thread_id = ?", (thread_id,)).fetchone()[0]
        for response in responses:
            if response["response_id"] in saved:
                # Only a child list can change, and compacting never touches what is stored.
                self._db.execute(
                    "UPDATE responses SET children = ? WHERE response_id = ?",
                    (json.dumps(response["children"]), response["response_id"]),
                )
                continue
            position += 1
            self._db.execute(
                """INSERT INTO responses (response_id, thread_id, position, checkpoint_id, parent_id, child_of, children,
                                          compaction, input_items, response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    response["response_id"],
                    thread_id,
                    position,
                    response["checkpoint_id"],
                    response["parent_id"],
                    response["child_of"],
                    json.dumps(response["children"]),
                    int(response["compaction"]),
                    json.dumps(response["input_items"]),
                    json.dumps(response["response"]),
                ),
            )

    def delete(self, thread_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM conversations WHERE thread_id = ?", (thread_id,))
        # The write-ahead log still holds what was deleted until it is emptied.
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def open_conversations(home: Path) -> ConversationDb | None:
    """A database that cannot be opened costs saved conversations, not the agent."""
    path = home / "conversations.db"
    try:
        return ConversationDb(path)
    except (OSError, sqlite3.Error) as error:
        print(f"Conversations will not be saved: could not open {path}: {error}", file=sys.stderr)
        return None
