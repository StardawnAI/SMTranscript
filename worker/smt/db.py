"""The job database: one SQLite file in the data volume.

SQLite because one worker process owns it. Several threads write to it, so
the file runs in WAL mode and every thread has its own connection; a claim is
one UPDATE … RETURNING, which SQLite runs atomically.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
create table if not exists job (
    id              text primary key,
    tenant          text not null,
    kind            text not null,              -- video | profile
    idem_key        text not null,
    input_url       text not null,
    platform        text not null,
    platform_id     text,
    canonical_url   text,
    options         text not null default '{}',
    status          text not null,              -- queued resolving fetching processing retry_wait blocked
                                                -- completed partial failed cancelled
    stage           text,                       -- fetch | asr | profile | null: nothing left to do
    attempts        integer not null default 0,
    next_at         real not null default 0,
    lease_owner     text,
    lease_until     real,
    route           text not null,
    error_class     text,
    error           text,
    warnings        text not null default '[]',
    parent_id       text,
    media_id        integer,
    platform_requests integer not null default 0,
    priority        integer not null default 0, -- 1: someone is waiting for it (an MCP call)
    member          text,                       -- the caller's own user it ran for (a hub user), or null
    created_at      real not null,
    updated_at      real not null,
    finished_at     real,
    unique (tenant, idem_key)
);
create index if not exists job_claim on job (stage, next_at);
create index if not exists job_parent on job (parent_id);

-- One row per video on a platform; a carousel post has one row per video in it.
create table if not exists media (
    id              integer primary key,
    platform        text not null,
    platform_id     text not null,
    part            integer not null default 0,
    canonical_url   text not null,
    info            text,                       -- the extractor's answer, without signed URLs
    info_at         real,
    meta            text not null default '{}',
    unique (platform, platform_id, part)
);

create table if not exists artifact (
    id              text primary key,           -- random: no path, no platform id in it
    media_id        integer not null references media (id),
    kind            text not null,              -- video | captions
    path            text not null,              -- relative to the media directory
    mime            text not null,
    size            integer not null,
    sha256          text not null,
    language        text,
    created_at      real not null
);

-- Text tracks are kept apart by where they came from. A new model makes a new
-- row next to the old one; nothing is overwritten.
create table if not exists track (
    id              integer primary key,
    media_id        integer not null references media (id),
    source          text not null,              -- native_captions | asr
    language        text,
    origin          text not null default 'unknown',
    model           text,
    model_revision  text,
    params          text not null default '{}',
    status          text not null,              -- available | no_speech | no_audio | failed
    segments        text not null default '[]',
    text            text not null default '',
    quality         text not null default '{}',
    created_at      real not null
);
create index if not exists track_media on track (media_id, source);

create table if not exists route_state (
    key             text primary key,           -- platform:route
    cooldown_until  real not null default 0,
    blocked         integer not null default 0,
    failures        integer not null default 0,
    last_error      text,
    updated_at      real not null
);

create table if not exists profile_cursor (
    job_id          text primary key,
    cursor          text,
    seen            text not null default '[]'
);

create table if not exists audit (
    id              integer primary key,
    at              real not null,
    tenant          text,
    action          text not null,
    job_id          text,
    detail          text
);
"""

TERMINAL = ("completed", "partial", "failed", "cancelled")


class Db:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn.executescript(SCHEMA)
        # Databases from before 1.2.0 lack the column; create table if not exists does not add it.
        columns = {r[1] for r in self.conn.execute("pragma table_info(job)")}
        if "priority" not in columns:
            self.conn.execute("alter table job add column priority integer not null default 0")
        # Databases from before 1.6.0 lack the member column.
        if "member" not in columns:
            self.conn.execute("alter table job add column member text")

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma journal_mode = wal")
            conn.execute("pragma foreign_keys = on")
            conn.execute("pragma busy_timeout = 30000")
            self._local.conn = conn
        return conn

    def one(self, sql: str, params=()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def run(self, sql: str, params=()) -> int:
        return self.conn.execute(sql, params).rowcount

    def insert(self, sql: str, params=()) -> int:
        return self.conn.execute(sql, params).lastrowid

    def audit(self, action: str, tenant: str | None = None, job_id: str | None = None, **detail):
        self.run(
            "insert into audit (at, tenant, action, job_id, detail) values (?, ?, ?, ?, ?)",
            (time.time(), tenant, action, job_id, json.dumps(detail) if detail else None),
        )
