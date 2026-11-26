"""SQLite state: releases, the event log, and metrics from the shop.

The event log is the bot's own audit trail (Flagsmith's audit log is a paid
feature): who approved what, when, and every action taken. State lives here
so the bot can be restarted mid-rollout and carry on.

Several processes share this file (the running bot, and one-shot CLI commands
such as `approve`), so every write is its own short transaction.
"""

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    flag TEXT NOT NULL,
    feature_id INTEGER,
    segment_id INTEGER,
    policy TEXT NOT NULL,          -- JSON of the approved Policy
    state TEXT NOT NULL,
    stage_started_at REAL,
    approved_by TEXT,              -- approval to release to everyone
    rollback_requested_by TEXT,
    rollback_reason TEXT,          -- set while a rollback is pending or done
    baseline_old TEXT,             -- JSON PathStats of the old path at the last partial stage
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    release_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    flag TEXT NOT NULL,
    path TEXT NOT NULL,
    ok INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    error_type TEXT,
    error_message TEXT,
    error_where TEXT
);
CREATE INDEX IF NOT EXISTS metrics_flag_ts ON metrics (flag, ts);
CREATE TABLE IF NOT EXISTS github_events (
    id INTEGER PRIMARY KEY,
    delivery_id TEXT UNIQUE,       -- X-GitHub-Delivery; GitHub may redeliver
    received_at REAL NOT NULL,
    payload TEXT NOT NULL,         -- JSON of the parsed event
    attempts INTEGER NOT NULL DEFAULT 0,
    processed_at REAL,
    result TEXT
);
"""

MAX_EVENT_ATTEMPTS = 3

ACTIVE_STATES = ("stage_0", "stage_1", "stage_2", "awaiting_approval", "full")
TERMINAL_STATES = ("released", "rolled_back", "abandoned", "rejected")
# Before merge: a plan waiting for /approve, then approved and waiting for the merge.
PLAN_STATES = ("proposed", "pending_merge")

# Columns added after the first version; added to an existing autopilot.db on start.
ADDED_COLUMNS = {
    "pr_number": "INTEGER",        # the PR this release came from (NULL for CLI releases)
    "plan": "TEXT",                # JSON: triage, wording, preview and /set history
    "plan_comment_id": "INTEGER",  # the plan comment, edited as the plan changes
}


class Store:
    def __init__(self, path="autopilot.db", clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(releases)")}
        for name, kind in ADDED_COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE releases ADD COLUMN {name} {kind}")

    def close(self):
        self.db.close()

    # --- releases ----------------------------------------------------------

    def create_release(self, flag, feature_id, segment_id, policy_dict, state):
        now = self.clock()
        cur = self.db.execute(
            "INSERT INTO releases (flag, feature_id, segment_id, policy, state, stage_started_at,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (flag, feature_id, segment_id, json.dumps(policy_dict), state, now, now, now),
        )
        return cur.lastrowid

    def create_proposal(self, pr_number, flag, policy_dict, plan):
        """A release plan waiting for /approve. Nothing exists in Flagsmith yet."""
        now = self.clock()
        cur = self.db.execute(
            "INSERT INTO releases (flag, policy, state, pr_number, plan, created_at, updated_at)"
            " VALUES (?, ?, 'proposed', ?, ?, ?, ?)",
            (flag, json.dumps(policy_dict), pr_number, json.dumps(plan), now, now),
        )
        return cur.lastrowid

    def release_for_pr(self, pr_number):
        row = self.db.execute(
            "SELECT * FROM releases WHERE pr_number = ? ORDER BY id DESC LIMIT 1", (pr_number,)).fetchone()
        return dict(row) if row else None

    def flag_in_use(self, flag, except_pr=None):
        """True if another release still owns this flag name (anything but
        rejected/abandoned plans), so a new PR must pick a different one."""
        row = self.db.execute(
            "SELECT 1 FROM releases WHERE flag = ? AND state NOT IN ('rejected', 'abandoned')"
            " AND (pr_number IS NULL OR pr_number IS NOT ?) LIMIT 1", (flag, except_pr)).fetchone()
        return row is not None

    def get_release(self, release_id):
        row = self.db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        return dict(row) if row else None

    def latest_release(self, flag):
        row = self.db.execute(
            "SELECT * FROM releases WHERE flag = ? ORDER BY id DESC LIMIT 1", (flag,)).fetchone()
        return dict(row) if row else None

    def active_releases(self):
        marks = ",".join("?" * len(ACTIVE_STATES))
        rows = self.db.execute(
            f"SELECT * FROM releases WHERE state IN ({marks}) ORDER BY id", ACTIVE_STATES).fetchall()
        return [dict(r) for r in rows]

    def all_releases(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM releases ORDER BY id DESC")]

    def update_release(self, release_id, **fields):
        fields["updated_at"] = self.clock()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE releases SET {cols} WHERE id = ?", (*fields.values(), release_id))

    def request(self, release_id, column, value, only_states):
        """Set a request column (approval, rollback) only if the release is in
        one of `only_states`. Returns True if it was set. Atomic, so two
        approvals can't both win."""
        marks = ",".join("?" * len(only_states))
        cur = self.db.execute(
            f"UPDATE releases SET {column} = ?, updated_at = ? WHERE id = ? AND {column} IS NULL"
            f" AND state IN ({marks})",
            (value, self.clock(), release_id, *only_states),
        )
        return cur.rowcount == 1

    # --- events ------------------------------------------------------------

    def add_event(self, release_id, kind, message, data=None):
        self.db.execute(
            "INSERT INTO events (release_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
            (release_id, self.clock(), kind, message, json.dumps(data) if data is not None else None),
        )

    def events(self, release_id):
        rows = self.db.execute(
            "SELECT * FROM events WHERE release_id = ? ORDER BY id", (release_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- GitHub events (queued by the webhook, handled by the bot loop) ------

    def add_github_event(self, delivery_id, event):
        """Queue an event. Returns False if this delivery was already queued."""
        try:
            self.db.execute(
                "INSERT INTO github_events (delivery_id, received_at, payload) VALUES (?, ?, ?)",
                (delivery_id, self.clock(), json.dumps(event)))
        except sqlite3.IntegrityError:
            return False
        return True

    def pending_github_events(self):
        rows = self.db.execute(
            "SELECT * FROM github_events WHERE processed_at IS NULL AND attempts < ? ORDER BY id",
            (MAX_EVENT_ATTEMPTS,)).fetchall()
        return [{**dict(r), "event": json.loads(r["payload"])} for r in rows]

    def finish_github_event(self, event_id, result):
        self.db.execute("UPDATE github_events SET processed_at = ?, result = ? WHERE id = ?",
                        (self.clock(), result, event_id))

    def fail_github_event(self, event_id, error):
        """Count a failed attempt. Returns True if it will be retried, False if
        it has used up MAX_EVENT_ATTEMPTS."""
        self.db.execute("UPDATE github_events SET attempts = attempts + 1, result = ? WHERE id = ?",
                        (error[:500], event_id))
        attempts = self.db.execute("SELECT attempts FROM github_events WHERE id = ?", (event_id,)).fetchone()[0]
        return attempts < MAX_EVENT_ATTEMPTS

    # --- metrics -----------------------------------------------------------

    def add_metrics(self, events):
        """Insert well-formed metric events; returns how many were stored."""
        rows = []
        for e in events:
            try:
                rows.append((
                    float(e["ts"]), str(e["flag"]), str(e["path"]), int(bool(e["ok"])),
                    float(e["latency_ms"]), e.get("error_type"), e.get("error_message"),
                    e.get("error_where"),
                ))
            except (KeyError, TypeError, ValueError):
                continue  # skip malformed events rather than reject the batch
        rows = [r for r in rows if r[2] in ("new", "old")]
        if rows:
            self.db.executemany(
                "INSERT INTO metrics (ts, flag, path, ok, latency_ms, error_type, error_message,"
                " error_where) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def metrics_since(self, flag, since, path=None):
        sql = "SELECT * FROM metrics WHERE flag = ? AND ts >= ?"
        args = [flag, since]
        if path:
            sql += " AND path = ?"
            args.append(path)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY ts", args)]