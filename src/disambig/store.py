"""SQLite store for the review UI: items, proposals, labels, timing.

Labels are append-only. Undo marks the previous row superseded rather than
deleting it, so the full adjudication history survives for audit.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from disambig.models import Item, Label, Proposal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    stratum TEXT NOT NULL,
    queue_key TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    item_id TEXT PRIMARY KEY REFERENCES items(item_id),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
    label_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    annotator TEXT NOT NULL,
    decision TEXT NOT NULL,
    ror_ids TEXT NOT NULL,
    chosen_ranks TEXT NOT NULL,
    justification_viewed INTEGER NOT NULL,
    note TEXT,
    elapsed_ms INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_labels_item ON labels(item_id, annotator, superseded);
CREATE INDEX IF NOT EXISTS idx_items_queue ON items(queue_key);
"""


def queue_key(item_id: str) -> str:
    """Deterministic shuffle so strata interleave during labelling."""
    return hashlib.sha256(item_id.encode()).hexdigest()


class ReviewStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def load_items(self, items: list[Item], proposals: list[Proposal]) -> int:
        """Idempotent load: existing item_ids are left untouched."""
        inserted = 0
        proposal_map = {proposal.item_id: proposal for proposal in proposals}
        for item in items:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO items (item_id, stratum, queue_key, payload)"
                " VALUES (?, ?, ?, ?)",
                (item.item_id, item.stratum, queue_key(item.item_id), item.model_dump_json()),
            )
            if cursor.rowcount:
                inserted += 1
            proposal = proposal_map.get(item.item_id)
            if proposal is not None:
                self._db.execute(
                    "INSERT OR REPLACE INTO proposals (item_id, payload) VALUES (?, ?)",
                    (item.item_id, proposal.model_dump_json()),
                )
        self._db.commit()
        return inserted

    def next_item(self, annotator: str) -> tuple[Item, Proposal | None] | None:
        row = self._db.execute(
            """
            SELECT i.payload, p.payload FROM items i
            LEFT JOIN proposals p ON p.item_id = i.item_id
            WHERE NOT EXISTS (
                SELECT 1 FROM labels l
                WHERE l.item_id = i.item_id AND l.annotator = ?
                  AND l.superseded = 0 AND l.decision != 'skipped'
            )
            ORDER BY i.queue_key
            LIMIT 1
            """,
            (annotator,),
        ).fetchone()
        if row is None:
            return None
        item = Item.model_validate_json(row[0])
        proposal = Proposal.model_validate_json(row[1]) if row[1] is not None else None
        return item, proposal

    def save_label(self, label: Label) -> int:
        self._db.execute(
            "UPDATE labels SET superseded = 1 WHERE item_id = ? AND annotator = ?"
            " AND superseded = 0",
            (label.item_id, label.annotator),
        )
        cursor = self._db.execute(
            "INSERT INTO labels (item_id, annotator, decision, ror_ids, chosen_ranks,"
            " justification_viewed, note, elapsed_ms, submitted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                label.item_id,
                label.annotator,
                label.decision.value,
                json.dumps(label.ror_ids),
                json.dumps(label.chosen_ranks),
                int(label.justification_viewed),
                label.note,
                label.elapsed_ms,
                label.submitted_at,
            ),
        )
        self._db.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def undo_last(self, annotator: str) -> str | None:
        """Supersede the annotator's most recent live label; returns its item_id."""
        row = self._db.execute(
            "SELECT label_id, item_id FROM labels WHERE annotator = ? AND superseded = 0"
            " ORDER BY label_id DESC LIMIT 1",
            (annotator,),
        ).fetchone()
        if row is None:
            return None
        self._db.execute("UPDATE labels SET superseded = 1 WHERE label_id = ?", (row[0],))
        self._db.commit()
        return str(row[1])

    def progress(self, annotator: str) -> dict[str, Any]:
        totals = dict(
            self._db.execute("SELECT stratum, COUNT(*) FROM items GROUP BY stratum").fetchall()
        )
        done_rows = self._db.execute(
            """
            SELECT i.stratum, l.decision, COUNT(*), AVG(l.elapsed_ms)
            FROM labels l JOIN items i ON i.item_id = l.item_id
            WHERE l.annotator = ? AND l.superseded = 0 AND l.decision != 'skipped'
            GROUP BY i.stratum, l.decision
            """,
            (annotator,),
        ).fetchall()
        strata: dict[str, Any] = {
            stratum: {"total": total, "done": 0, "decisions": {}, "mean_elapsed_ms": None}
            for stratum, total in totals.items()
        }
        for stratum, decision, count, mean_ms in done_rows:
            entry = strata[stratum]
            entry["done"] += count
            entry["decisions"][decision] = count
            entry["mean_elapsed_ms"] = mean_ms
        overall_done = sum(entry["done"] for entry in strata.values())
        overall_total = sum(entry["total"] for entry in strata.values())
        return {"strata": strata, "done": overall_done, "total": overall_total}

    def export_labels(self) -> list[dict[str, Any]]:
        """Live (non-superseded) labels joined to their items, for freeze/export."""
        rows = self._db.execute(
            """
            SELECT i.payload, l.annotator, l.decision, l.ror_ids, l.chosen_ranks,
                   l.justification_viewed, l.note, l.elapsed_ms, l.submitted_at
            FROM labels l JOIN items i ON i.item_id = l.item_id
            WHERE l.superseded = 0 AND l.decision != 'skipped'
            ORDER BY l.label_id
            """
        ).fetchall()
        exported = []
        for payload, annotator, decision, ror_ids, ranks, viewed, note, ms, at in rows:
            item = json.loads(payload)
            exported.append(
                {
                    "item": item,
                    "annotator": annotator,
                    "decision": decision,
                    "ror_ids": json.loads(ror_ids),
                    "chosen_ranks": json.loads(ranks),
                    "justification_viewed": bool(viewed),
                    "note": note,
                    "elapsed_ms": ms,
                    "submitted_at": at,
                }
            )
        return exported

    def close(self) -> None:
        self._db.close()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
