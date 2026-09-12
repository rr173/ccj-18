"""业务逻辑：发票 / 核销 / 作废 / 过期清扫 / 离线补齐 / 管理视图。"""

from __future__ import annotations

import os
import secrets
import sqlite3
from typing import Optional

from .db import (
    add_event,
    event_dict,
    iso,
    ticket_dict,
    utcnow,
    write_tx,
)

# Crockford base32，去掉了易混淆的 I/L/O/U
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 8

SWEEP_INTERVAL = float(os.getenv("PASSPORT_SWEEP_INTERVAL", "10"))
SYNC_BATCH = 200

REASON_TEXT = {
    "ok": "核销成功",
    "already_redeemed": "该票已核销",
    "revoked": "该票已作废",
    "expired": "该票已过期",
    "not_yet_valid": "未到生效时间",
    "not_found": "票面不存在",
    "invalid_code": "票面编号格式无效",
}


def normalize_code(code: str) -> str:
    """票号格式固定为 T-XXXXXXXX；仅允许空白差异与大小写差异。"""
    return "".join(str(code).split()).upper()


def _gen_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LEN))
    return f"T-{raw}"


def _lazy_expire(conn: sqlite3.Connection, ticket: sqlite3.Row, now: str) -> bool:
    """ACTIVE 但已到/过截止时间 -> 当场置 EXPIRED 并记事件。"""
    if ticket["status"] != "ACTIVE" or now < ticket["valid_until"]:
        return False
    add_event(
        conn,
        "TICKET_EXPIRED",
        ts=now,
        ticket_code=ticket["code"],
        person_id=ticket["person_id"],
        reason="valid_until_reached",
        payload={"valid_until": ticket["valid_until"]},
    )
    conn.execute(
        "UPDATE tickets SET status='EXPIRED', expired_at=? WHERE code=?",
        (now, ticket["code"]),
    )
    return True


# ---------------- 门点 ----------------

def create_gate(conn: sqlite3.Connection, gate_id: str, name: str) -> dict:
    now = iso(utcnow())
    conn.execute(
        "INSERT INTO gates (id,name,created_at,revoked) VALUES (?,?,?,0)",
        (gate_id, name, now),
    )
    return {"id": gate_id, "name": name, "created_at": now, "revoked": False}


def get_gate(conn: sqlite3.Connection, gate_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()


def revoke_gate(conn: sqlite3.Connection, gate_id: str) -> None:
    conn.execute("UPDATE gates SET revoked=1 WHERE id=?", (gate_id,))


def list_gates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT g.*, c.last_version, c.updated_at AS cursor_updated_at
             FROM gates g LEFT JOIN gate_cursors c ON c.gate_id=g.id
            ORDER BY g.created_at"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["revoked"] = bool(d["revoked"])
        out.append(d)
    return out


# ---------------- 发票 / 作废 ----------------

def issue_ticket(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    valid_from: str,
    valid_until: str,
    note: Optional[str] = None,
) -> dict:
    now = iso(utcnow())
    with write_tx(conn):
        for _ in range(5):
            code = _gen_code()
            exists = conn.execute(
                "SELECT 1 FROM tickets WHERE code=?", (code,)
            ).fetchone()
            if not exists:
                break
        else:  # pragma: no cover - 极小概率
            raise RuntimeError("无法生成唯一票面编号")

        conn.execute(
            """INSERT INTO tickets
                   (code,person_id,valid_from,valid_until,issued_at,status,note)
               VALUES (?,?,?,?,?,'ACTIVE',?)""",
            (code, person_id, valid_from, valid_until, now, note),
        )
        version = add_event(
            conn,
            "TICKET_ISSUED",
            ts=now,
            ticket_code=code,
            person_id=person_id,
            payload={
                "valid_from": valid_from,
                "valid_until": valid_until,
                "note": note,
            },
        )
        row = conn.execute("SELECT * FROM tickets WHERE code=?", (code,)).fetchone()
    result = ticket_dict(row)
    result["version"] = version
    return result


def revoke_ticket(
    conn: sqlite3.Connection, *, code: str, reason: str
) -> dict:
    now = iso(utcnow())
    with write_tx(conn):
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (code,)
        ).fetchone()
        if ticket is None:
            return {"http_status": 404, "ok": False, "reason": "not_found"}

        _lazy_expire(conn, ticket, now)
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (code,)
        ).fetchone()

        if ticket["status"] != "ACTIVE":
            return {
                "http_status": 409,
                "ok": False,
                "reason": "already_terminal",
                "status": ticket["status"],
            }

        add_event(
            conn,
            "TICKET_REVOKED",
            ts=now,
            ticket_code=code,
            person_id=ticket["person_id"],
            reason=reason or "admin_revoke",
            payload={"reason": reason},
        )
        conn.execute(
            """UPDATE tickets
                  SET status='REVOKED', revoked_at=?, revoked_reason=?
                WHERE code=?""",
            (now, reason, code),
        )
        version = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()["m"]
        row = conn.execute("SELECT * FROM tickets WHERE code=?", (code,)).fetchone()
    result = ticket_dict(row)
    result["version"] = version
    return {"http_status": 200, "ok": True, "ticket": result}


# ---------------- 核销（核心） ----------------

def redeem(
    conn: sqlite3.Connection,
    *,
    raw_code: str,
    gate_id: str,
    attempt_id: str,
) -> dict:
    """核销一张票。

    返回 {"http_status":..., "response": {...给门点的报文...}}。
    整个判定+状态迁移+事件写入在一个 IMMEDIATE 事务中完成；
    条件 UPDATE 的 WHERE status='ACTIVE' 保证并发时仅一人成功。
    """
    now_dt = utcnow()
    now = iso(now_dt)
    code = normalize_code(raw_code)

    # 1) 幂等：同一门点同一 attempt_id 重放第一次的结果
    prior = conn.execute(
        "SELECT * FROM scan_attempts WHERE gate_id=? AND attempt_id=?",
        (gate_id, attempt_id),
    ).fetchone()
    if prior is not None:
        import json

        response = json.loads(prior["result_json"])
        response["replayed"] = True
        return {"http_status": prior["http_status"], "response": response}

    with write_tx(conn):
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (code,)
        ).fetchone()

        if ticket is None:
            response = {
                "ok": False,
                "status": "UNKNOWN",
                "code": code,
                "gate_id": gate_id,
                "ts": now,
                "reason": "not_found",
                "reason_text": REASON_TEXT["not_found"],
            }
            http_status = 404
        else:
            person_id = ticket["person_id"]
            # 到点未清扫的 ACTIVE 票：当场过期（仍然写事件，绝不复活）
            _lazy_expire(conn, ticket, now)
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE code=?", (code,)
            ).fetchone()
            status = ticket["status"]

            if status == "ACTIVE" and now < ticket["valid_from"]:
                response = {
                    "ok": False,
                    "status": "ACTIVE",
                    "code": code,
                    "person_id": person_id,
                    "gate_id": gate_id,
                    "ts": now,
                    "reason": "not_yet_valid",
                    "reason_text": REASON_TEXT["not_yet_valid"],
                    "valid_from": ticket["valid_from"],
                    "valid_until": ticket["valid_until"],
                }
                http_status = 403
            elif status == "ACTIVE":
                # 条件 UPDATE：并发核销时只有一个门点 rowcount=1
                cur = conn.execute(
                    """UPDATE tickets
                          SET status='REDEEMED', redeemed_at=?, redeemed_gate=?
                        WHERE code=? AND status='ACTIVE'""",
                    (now, gate_id, code),
                )
                if cur.rowcount != 1:  # 理论上被锁保护，不会走到
                    response = {
                        "ok": False,
                        "status": "REDEEMED",
                        "code": code,
                        "person_id": person_id,
                        "gate_id": gate_id,
                        "ts": now,
                        "reason": "already_redeemed",
                        "reason_text": REASON_TEXT["already_redeemed"],
                    }
                    http_status = 409
                else:
                    version = add_event(
                        conn,
                        "TICKET_REDEEMED",
                        ts=now,
                        ticket_code=code,
                        person_id=person_id,
                        gate_id=gate_id,
                        reason="gate_scan",
                        payload={"gate_id": gate_id, "attempt_id": attempt_id},
                    )
                    response = {
                        "ok": True,
                        "status": "REDEEMED",
                        "code": code,
                        "person_id": person_id,
                        "gate_id": gate_id,
                        "ts": now,
                        "version": version,
                        "valid_until": ticket["valid_until"],
                        "reason_text": REASON_TEXT["ok"],
                    }
                    http_status = 200
            elif status == "REDEEMED":
                response = {
                    "ok": False,
                    "status": "REDEEMED",
                    "code": code,
                    "person_id": person_id,
                    "gate_id": gate_id,
                    "ts": now,
                    "reason": "already_redeemed",
                    "reason_text": REASON_TEXT["already_redeemed"],
                    "redeemed_at": ticket["redeemed_at"],
                    "redeemed_gate": ticket["redeemed_gate"],
                }
                http_status = 409
            elif status == "REVOKED":
                response = {
                    "ok": False,
                    "status": "REVOKED",
                    "code": code,
                    "person_id": person_id,
                    "gate_id": gate_id,
                    "ts": now,
                    "reason": "revoked",
                    "reason_text": REASON_TEXT["revoked"],
                    "revoked_at": ticket["revoked_at"],
                    "revoked_reason": ticket["revoked_reason"],
                }
                http_status = 410
            else:  # EXPIRED
                response = {
                    "ok": False,
                    "status": "EXPIRED",
                    "code": code,
                    "person_id": person_id,
                    "gate_id": gate_id,
                    "ts": now,
                    "reason": "expired",
                    "reason_text": REASON_TEXT["expired"],
                    "valid_until": ticket["valid_until"],
                    "expired_at": ticket["expired_at"],
                }
                http_status = 410

        # 2) 记录扫码尝试（门点记录 + 幂等重放）
        import json

        conn.execute(
            """INSERT INTO scan_attempts
                   (gate_id,attempt_id,code,at,http_status,ok,status,result_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                gate_id,
                attempt_id,
                code,
                now,
                http_status,
                1 if response["ok"] else 0,
                response["status"],
                json.dumps(response, ensure_ascii=False, sort_keys=True),
            ),
        )

    return {"http_status": http_status, "response": response}


# ---------------- 过期清扫 ----------------

def sweep_expired(conn: sqlite3.Connection) -> list[str]:
    """后台周期任务：把所有到点的 ACTIVE 票一次性置为 EXPIRED。"""
    now = iso(utcnow())
    expired: list[str] = []
    with write_tx(conn):
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status='ACTIVE' AND valid_until<=?",
            (now,),
        ).fetchall()
        for t in rows:
            add_event(
                conn,
                "TICKET_EXPIRED",
                ts=now,
                ticket_code=t["code"],
                person_id=t["person_id"],
                reason="valid_until_reached",
                payload={"valid_until": t["valid_until"]},
            )
            conn.execute(
                "UPDATE tickets SET status='EXPIRED', expired_at=? WHERE code=?",
                (now, t["code"]),
            )
            expired.append(t["code"])
    return expired


# ---------------- 离线补齐 / 心跳 ----------------

def get_events_since(
    conn: sqlite3.Connection, *, since_version: int, gate_id: str
) -> dict:
    rows = conn.execute(
        "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?",
        (since_version, SYNC_BATCH),
    ).fetchall()
    events = [event_dict(r) for r in rows]
    has_more = len(events) >= SYNC_BATCH
    next_since = events[-1]["version"] if events else since_version

    if events:
        conn.execute(
            """INSERT INTO gate_cursors (gate_id,last_version,updated_at)
               VALUES (?,?,?)
               ON CONFLICT(gate_id) DO UPDATE SET
                 last_version=excluded.last_version,
                 updated_at=excluded.updated_at""",
            (gate_id, next_since, iso(utcnow())),
        )
        conn.commit()
    return {"events": events, "next_since": next_since, "has_more": has_more}


# ---------------- 管理视图 ----------------

def get_ticket(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM tickets WHERE code=?", (code,)).fetchone()
    if row is None:
        return None
    return ticket_dict(row)


def _effective_status(t: dict, now_str: str) -> str:
    if t["status"] == "ACTIVE" and now_str >= t["valid_until"]:
        return "EXPIRED"
    return t["status"]


def get_person_view(conn: sqlite3.Connection, person_id: str) -> Optional[dict]:
    now_str = iso(utcnow())
    rows = conn.execute(
        "SELECT * FROM tickets WHERE person_id=? ORDER BY issued_at DESC",
        (person_id,),
    ).fetchall()
    ticket_rows = conn.execute(
        "SELECT DISTINCT person_id FROM tickets WHERE person_id=?",
        (person_id,),
    ).fetchall()
    if not ticket_rows:
        return None

    gates = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM gates")}
    tickets = []
    for r in rows:
        t = ticket_dict(r)
        t["effective_status"] = _effective_status(t, now_str)
        tickets.append(t)

    event_rows = conn.execute(
        "SELECT * FROM events WHERE person_id=? ORDER BY id", (person_id,)
    ).fetchall()
    events = [event_dict(r) for r in event_rows]

    codes = [t["code"] for t in tickets]
    attempts: list[dict] = []
    if codes:
        placeholders = ",".join("?" for _ in codes)
        attempt_rows = conn.execute(
            f"""SELECT s.*, g.name AS gate_name
                  FROM scan_attempts s LEFT JOIN gates g ON g.id=s.gate_id
                 WHERE s.code IN ({placeholders})
                 ORDER BY s.at""",
            codes,
        ).fetchall()
        for a in attempt_rows:
            attempts.append(
                {
                    "at": a["at"],
                    "gate_id": a["gate_id"],
                    "gate_name": a["gate_name"],
                    "code": a["code"],
                    "ok": bool(a["ok"]),
                    "status": a["status"],
                    "attempt_id": a["attempt_id"],
                    "http_status": a["http_status"],
                }
            )

    return {
        "person_id": person_id,
        "now": now_str,
        "tickets": tickets,
        "valid_tickets": [
            t for t in tickets if t["effective_status"] == "ACTIVE"
            and now_str >= t["valid_from"]
        ],
        "events": events,
        "scan_attempts": attempts,
        "gates": gates,
    }


def list_people(conn: sqlite3.Connection, q: Optional[str] = None, limit: int = 100) -> list[dict]:
    now_str = iso(utcnow())
    sql = "SELECT * FROM tickets"
    params: list = []
    if q:
        sql += " WHERE person_id LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY issued_at DESC LIMIT ?"
    params.append(limit * 50)
    rows = conn.execute(sql, params).fetchall()
    people: dict[str, dict] = {}
    for r in rows:
        t = ticket_dict(r)
        p = people.setdefault(
            t["person_id"],
            {"person_id": t["person_id"], "total": 0, "valid": 0,
             "redeemed": 0, "revoked": 0, "expired": 0},
        )
        p["total"] += 1
        eff = _effective_status(t, now_str)
        if eff == "ACTIVE" and now_str >= t["valid_from"]:
            p["valid"] += 1
        elif eff in p:
            p[eff.lower()] += 1
    out = sorted(people.values(), key=lambda x: x["person_id"])
    return out[:limit]


def list_events(
    conn: sqlite3.Connection,
    *,
    since: Optional[int] = None,
    code: Optional[str] = None,
    person_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT * FROM events WHERE 1=1"
    params: list = []
    if since is not None:
        sql += " AND id>?"
        params.append(since)
    if code:
        sql += " AND ticket_code=?"
        params.append(normalize_code(code))
    if person_id:
        sql += " AND person_id=?"
        params.append(person_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 1000))
    return [event_dict(r) for r in conn.execute(sql, params).fetchall()]


def list_attempts(
    conn: sqlite3.Connection, *, gate_id: Optional[str] = None, limit: int = 200
) -> list[dict]:
    sql = (
        "SELECT s.*, g.name AS gate_name FROM scan_attempts s "
        "LEFT JOIN gates g ON g.id=s.gate_id"
    )
    params: list = []
    if gate_id:
        sql += " WHERE s.gate_id=?"
        params.append(gate_id)
    sql += " ORDER BY s.at DESC LIMIT ?"
    params.append(min(limit, 1000))
    out = []
    for a in conn.execute(sql, params).fetchall():
        out.append(
            {
                "at": a["at"],
                "gate_id": a["gate_id"],
                "gate_name": a["gate_name"],
                "code": a["code"],
                "ok": bool(a["ok"]),
                "status": a["status"],
                "attempt_id": a["attempt_id"],
                "http_status": a["http_status"],
            }
        )
    return out


def stats(conn: sqlite3.Connection) -> dict:
    now_str = iso(utcnow())
    counts = {
        s: conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE status=?", (s,)
        ).fetchone()["c"]
        for s in ("ACTIVE", "REDEEMED", "REVOKED", "EXPIRED")
    }
    due = conn.execute(
        "SELECT COUNT(*) c FROM tickets WHERE status='ACTIVE' AND valid_until<=?",
        (now_str,),
    ).fetchone()["c"]
    counts["ACTIVE"] -= due
    counts["EXPIRED"] += due
    return {
        "now": now_str,
        "tickets": counts,
        "gates": conn.execute("SELECT COUNT(*) c FROM gates").fetchone()["c"],
        "gates_revoked": conn.execute(
            "SELECT COUNT(*) c FROM gates WHERE revoked=1"
        ).fetchone()["c"],
        "events": conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
        "last_version": conn.execute(
            "SELECT COALESCE(MAX(id),0) m FROM events"
        ).fetchone()["m"],
    }
