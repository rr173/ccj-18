"""数据层：SQLite (WAL)。

核心设计
--------
1. ``tickets`` 是票的当前状态（每个票面编号一行，票之间互不顶替）。
2. ``events`` 是只追加（append-only）的状态变化流水，``id`` 由 SQLite
   AUTOINCREMENT 单调分配，作为门点离线补齐用的全局版本号。
3. 状态只允许单向迁移：ACTIVE -> REDEEMED / REVOKED / EXPIRED，
   终态不可回退，因此撤销、过期、服务器重启都不可能让票“复活”。
4. 核销走 ``BEGIN IMMEDIATE`` 串行写事务 + 条件 UPDATE
   (``WHERE status='ACTIVE'``)，并发扫同一张票时只有一个门点成功。
5. ``scan_attempts`` 对 (gate_id, attempt_id) 建唯一约束，门点网络抖动
   重发同一次扫码时按幂等处理，返回第一次的核销结果。
6. ``zones`` 是分区注册表；``gates.zone_id`` 指定门点所属分区，
   ``tickets.zones`` (JSON 数组) 指定票允许通行的分区。
7. ``policy_rules`` 是封锁/解除封锁规则表，``rule_id`` 主键去重
   （重复发布同一规则不重复生效）；每次发布同时写一条
   ``POLICY_LOCK`` / ``POLICY_UNLOCK`` 事件，版本号即 ``events.id``，
   门点离线重连后与票据事件一起按版本补齐。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

DB_PATH = os.getenv("PASSPORT_DB", os.path.join(os.getcwd(), "passport.db"))

TICKET_STATUSES = ("ACTIVE", "REDEEMED", "REVOKED", "EXPIRED")
EVENT_TYPES = (
    "TICKET_ISSUED",
    "TICKET_REDEEMED",
    "TICKET_REVOKED",
    "TICKET_EXPIRED",
    "POLICY_LOCK",
    "POLICY_UNLOCK",
)
RULE_ACTIONS = ("LOCK", "UNLOCK")

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    zone_id    TEXT               -- NULL = 未配置分区策略，核销默认拒绝
);

CREATE TABLE IF NOT EXISTS tickets (
    code           TEXT PRIMARY KEY,
    person_id      TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_until    TEXT NOT NULL,
    issued_at      TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('ACTIVE','REDEEMED','REVOKED','EXPIRED')),
    redeemed_at    TEXT,
    redeemed_gate  TEXT,
    revoked_at     TEXT,
    revoked_reason TEXT,
    expired_at     TEXT,
    note           TEXT,
    zones          TEXT NOT NULL DEFAULT '[]'  -- JSON 数组：允许通行的分区
);
CREATE INDEX IF NOT EXISTS idx_tickets_person ON tickets(person_id);

-- 只追加事件流水：id 即全局版本号（AUTOINCREMENT 保证单调、不复用）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    type        TEXT NOT NULL
                CHECK (type IN ('TICKET_ISSUED','TICKET_REDEEMED',
                                'TICKET_REVOKED','TICKET_EXPIRED',
                                'POLICY_LOCK','POLICY_UNLOCK')),
    ticket_code TEXT,
    person_id   TEXT,
    gate_id     TEXT,
    reason      TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_code);
CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id);

-- 封锁/解除封锁规则：rule_id 幂等去重；version 对应该规则的事件版本
CREATE TABLE IF NOT EXISTS policy_rules (
    rule_id    TEXT PRIMARY KEY,
    action     TEXT NOT NULL CHECK (action IN ('LOCK','UNLOCK')),
    zone_id    TEXT,                -- NULL = 全局规则，作用于所有分区
    reason     TEXT,
    created_at TEXT NOT NULL,
    version    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_zone ON policy_rules(zone_id);

-- 门点每次扫码尝试（含失败），用于幂等重放与门点记录
CREATE TABLE IF NOT EXISTS scan_attempts (
    gate_id     TEXT NOT NULL,
    attempt_id  TEXT NOT NULL,
    code        TEXT NOT NULL,
    at          TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    status      TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (gate_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_gate ON scan_attempts(gate_id);
CREATE INDEX IF NOT EXISTS idx_attempts_code ON scan_attempts(code);

-- 门点同步水位（客户端也自带游标，这里仅作管理侧观测）
CREATE TABLE IF NOT EXISTS gate_cursors (
    gate_id      TEXT PRIMARY KEY,
    last_version INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: Any) -> datetime:
    """解析 ISO8601；朴素时间按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.isolation_level = None  # 手工事务
    return conn


def init_db() -> None:
    conn = connect()
    try:
        # WAL 模式持久化在数据库文件上；配合 BEGIN IMMEDIATE 实现单写多读
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """老库就地升级：补列 + 重建 events 表以扩展事件类型 CHECK。"""
    gate_cols = {r["name"] for r in conn.execute("PRAGMA table_info(gates)")}
    if "zone_id" not in gate_cols:
        conn.execute("ALTER TABLE gates ADD COLUMN zone_id TEXT")
    ticket_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
    if "zones" not in ticket_cols:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN zones TEXT NOT NULL DEFAULT '[]'"
        )
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()
    if row and "POLICY_LOCK" not in row["sql"]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """CREATE TABLE events_new (
                       id          INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts          TEXT NOT NULL,
                       type        TEXT NOT NULL
                                   CHECK (type IN ('TICKET_ISSUED','TICKET_REDEEMED',
                                                   'TICKET_REVOKED','TICKET_EXPIRED',
                                                   'POLICY_LOCK','POLICY_UNLOCK')),
                       ticket_code TEXT,
                       person_id   TEXT,
                       gate_id     TEXT,
                       reason      TEXT,
                       payload     TEXT NOT NULL
                   )"""
            )
            conn.execute(
                """INSERT INTO events_new
                       (id,ts,type,ticket_code,person_id,gate_id,reason,payload)
                   SELECT id,ts,type,ticket_code,person_id,gate_id,reason,payload
                     FROM events"""
            )
            conn.execute("DROP TABLE events")
            conn.execute("ALTER TABLE events_new RENAME TO events")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_code)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_person ON events(person_id)"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """立即获取写锁的事务，把并发核销在数据库层串行化。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def add_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    ts: str,
    ticket_code: Optional[str] = None,
    person_id: Optional[str] = None,
    gate_id: Optional[str] = None,
    reason: Optional[str] = None,
    payload: Optional[dict] = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO events
               (ts, type, ticket_code, person_id, gate_id, reason, payload)
           VALUES (?,?,?,?,?,?,?)""",
        (
            ts,
            event_type,
            ticket_code,
            person_id,
            gate_id,
            reason,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    return int(cur.lastrowid)


def event_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["version"] = data.pop("id")
    try:
        data["payload"] = json.loads(data.get("payload") or "{}")
    except json.JSONDecodeError:
        pass
    return data


def ticket_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for col in ("revoked",):
        d.pop(col, None)
    try:
        d["zones"] = json.loads(d.get("zones") or "[]")
    except json.JSONDecodeError:
        d["zones"] = []
    return d
