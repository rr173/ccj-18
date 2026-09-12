"""业务逻辑：发票 / 核销 / 作废 / 过期清扫 / 分区与封锁策略 / 离线补齐 / 管理视图。"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from typing import Optional

from .db import (
    CAPACITY_HOLDING_STATUSES,
    add_event,
    event_dict,
    iso,
    parse_dt,
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
    "zone_mismatch": "票据未授权本门点所属分区",
    "locked": "本分区处于紧急封锁中",
    "stale_policy": "门点策略版本过旧，请先同步最新封锁规则",
    "gate_no_policy": "门点未配置分区策略，默认拒绝",
    "unknown_zone": "门点所属分区未知，默认拒绝",
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
        batch_id=ticket["batch_id"],
        application_id=ticket["application_id"],
    )
    conn.execute(
        "UPDATE tickets SET status='EXPIRED', expired_at=? WHERE code=?",
        (now, ticket["code"]),
    )
    return True


def appointment_info(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    """核销时附加的批次/同行人数信息（门点据此核对整组访客）。"""
    row = conn.execute(
        """SELECT t.batch_id, t.application_id, t.party_size,
                  b.name AS batch_name, b.visit_date, b.zone_id,
                  a.name AS applicant_name, a.status AS application_status
             FROM tickets t
             LEFT JOIN batches b ON b.id = t.batch_id
             LEFT JOIN applications a ON a.id = t.application_id
            WHERE t.code=?""",
        (code,),
    ).fetchone()
    if row is None or row["batch_id"] is None:
        return None
    return {
        "batch_id": row["batch_id"],
        "batch_name": row["batch_name"],
        "visit_date": row["visit_date"],
        "zone_id": row["zone_id"],
        "application_id": row["application_id"],
        "applicant_name": row["applicant_name"],
        "application_status": row["application_status"],
        "party_size": row["party_size"],
    }


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
        """SELECT g.*, z.name AS zone_name,
                  c.last_version, c.updated_at AS cursor_updated_at
             FROM gates g
             LEFT JOIN zones z ON z.id = g.zone_id
             LEFT JOIN gate_cursors c ON c.gate_id=g.id
            ORDER BY g.created_at"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["revoked"] = bool(d["revoked"])
        out.append(d)
    return out


# ---------------- 分区 ----------------

def create_zone(conn: sqlite3.Connection, zone_id: str, name: str) -> dict:
    now = iso(utcnow())
    conn.execute(
        "INSERT INTO zones (id,name,created_at) VALUES (?,?,?)",
        (zone_id, name, now),
    )
    return {"id": zone_id, "name": name, "created_at": now}


def get_zone(conn: sqlite3.Connection, zone_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()


def delete_zone(conn: sqlite3.Connection, zone_id: str) -> None:
    """删除分区注册。引用它的门点变为“未知分区”，核销默认拒绝。"""
    conn.execute("DELETE FROM zones WHERE id=?", (zone_id,))


def set_gate_zone(
    conn: sqlite3.Connection, gate_id: str, zone_id: Optional[str]
) -> None:
    conn.execute("UPDATE gates SET zone_id=? WHERE id=?", (zone_id, gate_id))


# ---------------- 封锁 / 解除封锁规则 ----------------

def _rule_dict(row: sqlite3.Row) -> dict:
    return {
        "rule_id": row["rule_id"],
        "action": row["action"],
        "zone_id": row["zone_id"],  # None = 全局
        "reason": row["reason"],
        "created_at": row["created_at"],
        "version": row["version"],
    }


def current_policy_version(conn: sqlite3.Connection) -> int:
    """当前策略版本 = 最新一条封锁规则的版本；无规则时为 0。"""
    return conn.execute(
        "SELECT COALESCE(MAX(version),0) v FROM policy_rules"
    ).fetchone()["v"]


def zone_lock_state(conn: sqlite3.Connection, zone_id: str) -> dict:
    """分区当前封锁状态：按版本取最新一条适用规则（本区或全局）。"""
    row = conn.execute(
        """SELECT * FROM policy_rules
            WHERE zone_id IS NULL OR zone_id=?
            ORDER BY version DESC LIMIT 1""",
        (zone_id,),
    ).fetchone()
    if row is None:
        return {"locked": False, "rule": None}
    return {"locked": row["action"] == "LOCK", "rule": _rule_dict(row)}


def publish_rule(
    conn: sqlite3.Connection,
    *,
    rule_id: str,
    action: str,
    zone_id: Optional[str],
    reason: Optional[str],
) -> dict:
    """发布封锁/解除封锁规则。

    - 规则与核销共用 BEGIN IMMEDIATE 写事务，并发下有明确先后：
      版本小于规则版本的核销按旧策略判定，大于的按新策略判定。
    - rule_id 幂等：重复发布同一规则返回首次结果，不重复生效；
      同 rule_id 不同内容返回 409。
    """
    if action not in ("LOCK", "UNLOCK"):
        return {"http_status": 400, "error": "action 必须是 LOCK 或 UNLOCK"}
    now = iso(utcnow())
    with write_tx(conn):
        existing = conn.execute(
            "SELECT * FROM policy_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
        if existing is not None:
            if existing["action"] != action or (
                (existing["zone_id"] or None) != (zone_id or None)
            ):
                return {
                    "http_status": 409,
                    "error": "rule_id 已被内容不同的规则占用",
                    "rule": _rule_dict(existing),
                }
            return {
                "http_status": 200,
                "duplicated": True,
                "rule": _rule_dict(existing),
            }
        if zone_id is not None and get_zone(conn, zone_id) is None:
            return {"http_status": 400, "error": f"未知分区: {zone_id}"}

        version = add_event(
            conn,
            "POLICY_LOCK" if action == "LOCK" else "POLICY_UNLOCK",
            ts=now,
            reason=reason or ("emergency_lock" if action == "LOCK" else "lock_lifted"),
            payload={"rule_id": rule_id, "action": action, "zone_id": zone_id},
        )
        conn.execute(
            """INSERT INTO policy_rules
                   (rule_id,action,zone_id,reason,created_at,version)
               VALUES (?,?,?,?,?,?)""",
            (rule_id, action, zone_id, reason, now, version),
        )
        row = conn.execute(
            "SELECT * FROM policy_rules WHERE rule_id=?", (rule_id,)
        ).fetchone()
    return {"http_status": 201, "duplicated": False, "rule": _rule_dict(row)}


def list_rules(
    conn: sqlite3.Connection, zone_id: Optional[str] = None
) -> list[dict]:
    if zone_id is None:
        rows = conn.execute(
            "SELECT * FROM policy_rules ORDER BY version DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM policy_rules
                WHERE zone_id IS NULL OR zone_id=?
                ORDER BY version DESC""",
            (zone_id,),
        ).fetchall()
    return [_rule_dict(r) for r in rows]


def list_zones(conn: sqlite3.Connection) -> list[dict]:
    """分区总览：封锁状态、当前规则、门点数、受影响票数。"""
    out = []
    for z in conn.execute("SELECT * FROM zones ORDER BY created_at").fetchall():
        lock = zone_lock_state(conn, z["id"])
        out.append(
            {
                "id": z["id"],
                "name": z["name"],
                "created_at": z["created_at"],
                "locked": lock["locked"],
                "current_rule": lock["rule"],
                "gates": conn.execute(
                    "SELECT COUNT(*) c FROM gates WHERE zone_id=?", (z["id"],)
                ).fetchone()["c"],
                "affected_tickets": conn.execute(
                    """SELECT COUNT(*) c FROM tickets t
                        WHERE EXISTS (SELECT 1 FROM json_each(t.zones) je
                                      WHERE je.value=?)""",
                    (z["id"],),
                ).fetchone()["c"],
            }
        )
    return out


def zone_detail(conn: sqlite3.Connection, zone_id: str) -> Optional[dict]:
    """分区视图：当前规则与历史、受影响票据、门点执行记录。"""
    z = get_zone(conn, zone_id)
    if z is None:
        return None
    now_str = iso(utcnow())
    lock = zone_lock_state(conn, zone_id)

    gates = []
    for g in conn.execute(
        "SELECT * FROM gates WHERE zone_id=? ORDER BY created_at", (zone_id,)
    ).fetchall():
        d = dict(g)
        d["revoked"] = bool(d["revoked"])
        gates.append(d)

    tickets = []
    for r in conn.execute(
        """SELECT * FROM tickets t
            WHERE EXISTS (SELECT 1 FROM json_each(t.zones) je WHERE je.value=?)
            ORDER BY issued_at DESC LIMIT 200""",
        (zone_id,),
    ).fetchall():
        t = ticket_dict(r)
        t["effective_status"] = _effective_status(t, now_str)
        tickets.append(t)

    attempts = []
    for a in conn.execute(
        """SELECT s.*, g.name AS gate_name
             FROM scan_attempts s JOIN gates g ON g.id=s.gate_id
            WHERE g.zone_id=? ORDER BY s.at DESC LIMIT 200""",
        (zone_id,),
    ).fetchall():
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
        "zone": {"id": z["id"], "name": z["name"], "created_at": z["created_at"]},
        "locked": lock["locked"],
        "current_rule": lock["rule"],
        "rules": list_rules(conn, zone_id=zone_id),
        "gates": gates,
        "tickets": tickets,
        "attempts": attempts,
        "policy_version": current_policy_version(conn),
    }


# ---------------- 发票 / 作废 ----------------

def _gen_unique_code(conn: sqlite3.Connection) -> str:
    for _ in range(5):
        code = _gen_code()
        exists = conn.execute(
            "SELECT 1 FROM tickets WHERE code=?", (code,)
        ).fetchone()
        if not exists:
            return code
    raise RuntimeError("无法生成唯一票面编号")  # pragma: no cover - 极小概率


def _issue_ticket_locked(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    valid_from: str,
    valid_until: str,
    note: Optional[str] = None,
    zones: Optional[list[str]] = None,
    batch_id: Optional[str] = None,
    application_id: Optional[str] = None,
    party_size: Optional[int] = None,
) -> tuple[str, int]:
    """已在 IMMEDIATE 事务内：插入票并写 TICKET_ISSUED 事件，返回 (code, version)。

    供管理端直接发票，也供预约审核/补录在同一事务内签票使用，
    保证“审核通过”与“票存在且与批次分区一致”原子可见。
    """
    now = iso(utcnow())
    zone_list = list(zones or [])
    code = _gen_unique_code(conn)
    conn.execute(
        """INSERT INTO tickets
               (code,person_id,valid_from,valid_until,issued_at,status,note,zones,
                batch_id,application_id,party_size)
           VALUES (?,?,?,?,?,'ACTIVE',?,?,?,?,?)""",
        (code, person_id, valid_from, valid_until, now, note,
         json.dumps(zone_list, ensure_ascii=False),
         batch_id, application_id, party_size),
    )
    payload = {
        "valid_from": valid_from,
        "valid_until": valid_until,
        "note": note,
        "zones": zone_list,
    }
    if batch_id is not None:
        payload["batch_id"] = batch_id
        payload["application_id"] = application_id
        payload["party_size"] = party_size
    version = add_event(
        conn,
        "TICKET_ISSUED",
        ts=now,
        ticket_code=code,
        person_id=person_id,
        payload=payload,
        batch_id=batch_id,
        application_id=application_id,
    )
    return code, version


def issue_ticket(
    conn: sqlite3.Connection,
    *,
    person_id: str,
    valid_from: str,
    valid_until: str,
    note: Optional[str] = None,
    zones: Optional[list[str]] = None,
) -> dict:
    with write_tx(conn):
        code, version = _issue_ticket_locked(
            conn,
            person_id=person_id,
            valid_from=valid_from,
            valid_until=valid_until,
            note=note,
            zones=zones,
        )
        row = conn.execute("SELECT * FROM tickets WHERE code=?", (code,)).fetchone()
    result = ticket_dict(row)
    result["version"] = version
    return result


def _revoke_ticket_locked(
    conn: sqlite3.Connection,
    *,
    code: str,
    reason: str,
    now: Optional[str] = None,
    event_reason: Optional[str] = None,
) -> tuple[Optional[sqlite3.Row], str]:
    """已在 IMMEDIATE 事务内：作废一张 ACTIVE 票并写事件。

    返回 (ticket_row_or_None, state)，state ∈
    ``ok`` / ``not_found`` / ``terminal``。供预约取消在同事务内
    撤销已签发的票（随后才把名额释放给候补）。
    """
    now = now or iso(utcnow())
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE code=?", (code,)
    ).fetchone()
    if ticket is None:
        return None, "not_found"

    _lazy_expire(conn, ticket, now)
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE code=?", (code,)
    ).fetchone()
    if ticket["status"] != "ACTIVE":
        return ticket, "terminal"

    add_event(
        conn,
        "TICKET_REVOKED",
        ts=now,
        ticket_code=code,
        person_id=ticket["person_id"],
        reason=reason or "admin_revoke",
        payload={"reason": reason},
        batch_id=ticket["batch_id"],
        application_id=ticket["application_id"],
    )
    conn.execute(
        """UPDATE tickets
              SET status='REVOKED', revoked_at=?, revoked_reason=?
            WHERE code=?""",
        (now, reason, code),
    )
    return conn.execute("SELECT * FROM tickets WHERE code=?", (code,)).fetchone(), "ok"


def revoke_ticket(
    conn: sqlite3.Connection, *, code: str, reason: str
) -> dict:
    now = iso(utcnow())
    with write_tx(conn):
        ticket, state = _revoke_ticket_locked(
            conn, code=code, reason=reason, now=now
        )
        if state == "not_found":
            return {"http_status": 404, "ok": False, "reason": "not_found"}
        if state == "terminal":
            return {
                "http_status": 409,
                "ok": False,
                "reason": "already_terminal",
                "status": ticket["status"],
            }
        version = conn.execute("SELECT MAX(id) AS m FROM events").fetchone()["m"]
    result = ticket_dict(ticket)
    result["version"] = version
    return {"http_status": 200, "ok": True, "ticket": result}


# ---------------- 核销（核心） ----------------

def _ticket_zones(ticket: sqlite3.Row) -> list[str]:
    try:
        return json.loads(ticket["zones"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def redeem(
    conn: sqlite3.Connection,
    *,
    raw_code: str,
    gate_id: str,
    attempt_id: str,
    policy_version: int = 0,
) -> dict:
    """核销一张票。

    返回 {"http_status":..., "response": {...给门点的报文...}}。
    整个判定+状态迁移+事件写入在一个 IMMEDIATE 事务中完成；
    条件 UPDATE 的 WHERE status='ACTIVE' 保证并发时仅一人成功。

    判定顺序（均在写事务内，与规则发布串行化，并发下有明确先后）：
      门点策略存在性（未配置/未知分区默认拒绝）
      -> 规则版本过旧（不记录扫码，门点补齐后可用同一 attempt_id 重试）
      -> 票面状态（不存在/未生效/已核销/已作废/已过期）
      -> 分区匹配 -> 封锁判定 -> 核销。
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
        response = json.loads(prior["result_json"])
        response["replayed"] = True
        return {"http_status": prior["http_status"], "response": response}

    with write_tx(conn):
        gate = conn.execute(
            "SELECT * FROM gates WHERE id=?", (gate_id,)
        ).fetchone()
        gate_zone = gate["zone_id"] if gate is not None else None
        policy_now = current_policy_version(conn)

        if gate_zone is None:
            # 缺少策略的门点：默认拒绝
            response = {
                "ok": False, "status": "NO_POLICY", "code": code,
                "gate_id": gate_id, "ts": now,
                "reason": "gate_no_policy",
                "reason_text": REASON_TEXT["gate_no_policy"],
            }
            http_status = 403
        elif get_zone(conn, gate_zone) is None:
            # 分区已被注销（未知分区）：默认拒绝
            response = {
                "ok": False, "status": "UNKNOWN_ZONE", "code": code,
                "gate_id": gate_id, "ts": now,
                "reason": "unknown_zone",
                "reason_text": REASON_TEXT["unknown_zone"],
                "zone_id": gate_zone,
            }
            http_status = 403
        elif policy_version < policy_now:
            # 规则版本过旧：不写 scan_attempts，门点按版本补齐策略事件后
            # 可用同一 attempt_id 安全重试（不会重放到过期的拒绝结果）
            return {
                "http_status": 409,
                "response": {
                    "ok": False, "status": "STALE_POLICY", "code": code,
                    "gate_id": gate_id, "ts": now,
                    "reason": "stale_policy",
                    "reason_text": REASON_TEXT["stale_policy"],
                    "policy_version": policy_version,
                    "current_policy_version": policy_now,
                },
            }
        else:
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE code=?", (code,)
            ).fetchone()

            if ticket is None:
                response = {
                    "ok": False, "status": "UNKNOWN", "code": code,
                    "gate_id": gate_id, "ts": now,
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
                        "ok": False, "status": "ACTIVE", "code": code,
                        "person_id": person_id, "gate_id": gate_id, "ts": now,
                        "reason": "not_yet_valid",
                        "reason_text": REASON_TEXT["not_yet_valid"],
                        "valid_from": ticket["valid_from"],
                        "valid_until": ticket["valid_until"],
                    }
                    http_status = 403
                elif status == "ACTIVE":
                    ticket_zones = _ticket_zones(ticket)
                    if gate_zone not in ticket_zones:
                        # 分区不匹配：不消耗票据
                        response = {
                            "ok": False, "status": "ACTIVE", "code": code,
                            "person_id": person_id, "gate_id": gate_id,
                            "ts": now,
                            "reason": "zone_mismatch",
                            "reason_text": REASON_TEXT["zone_mismatch"],
                            "zone_id": gate_zone,
                            "ticket_zones": ticket_zones,
                            "valid_until": ticket["valid_until"],
                        }
                        http_status = 403
                    else:
                        lock = zone_lock_state(conn, gate_zone)
                        if lock["locked"]:
                            # 紧急封锁中：不消耗票据
                            rule = lock["rule"]
                            response = {
                                "ok": False, "status": "ACTIVE", "code": code,
                                "person_id": person_id, "gate_id": gate_id,
                                "ts": now,
                                "reason": "locked",
                                "reason_text": REASON_TEXT["locked"],
                                "zone_id": gate_zone,
                                "lock_rule": rule,
                                "valid_until": ticket["valid_until"],
                            }
                            http_status = 423
                        else:
                            # 条件 UPDATE：并发核销时只有一个门点 rowcount=1
                            cur = conn.execute(
                                """UPDATE tickets
                                      SET status='REDEEMED', redeemed_at=?,
                                          redeemed_gate=?
                                    WHERE code=? AND status='ACTIVE'""",
                                (now, gate_id, code),
                            )
                            if cur.rowcount != 1:  # 理论上被锁保护，不会走到
                                response = {
                                    "ok": False, "status": "REDEEMED",
                                    "code": code, "person_id": person_id,
                                    "gate_id": gate_id, "ts": now,
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
                                    payload={
                                        "gate_id": gate_id,
                                        "attempt_id": attempt_id,
                                        "zone_id": gate_zone,
                                        "policy_version": policy_now,
                                    },
                                    batch_id=ticket["batch_id"],
                                    application_id=ticket["application_id"],
                                )
                                response = {
                                    "ok": True, "status": "REDEEMED",
                                    "code": code, "person_id": person_id,
                                    "gate_id": gate_id, "ts": now,
                                    "version": version,
                                    "valid_until": ticket["valid_until"],
                                    "reason_text": REASON_TEXT["ok"],
                                }
                                appt = appointment_info(conn, code)
                                if appt:
                                    response["appointment"] = appt
                                http_status = 200
                elif status == "REDEEMED":
                    response = {
                        "ok": False, "status": "REDEEMED", "code": code,
                        "person_id": person_id, "gate_id": gate_id, "ts": now,
                        "reason": "already_redeemed",
                        "reason_text": REASON_TEXT["already_redeemed"],
                        "redeemed_at": ticket["redeemed_at"],
                        "redeemed_gate": ticket["redeemed_gate"],
                    }
                    http_status = 409
                elif status == "REVOKED":
                    response = {
                        "ok": False, "status": "REVOKED", "code": code,
                        "person_id": person_id, "gate_id": gate_id, "ts": now,
                        "reason": "revoked",
                        "reason_text": REASON_TEXT["revoked"],
                        "revoked_at": ticket["revoked_at"],
                        "revoked_reason": ticket["revoked_reason"],
                    }
                    http_status = 410
                else:  # EXPIRED
                    response = {
                        "ok": False, "status": "EXPIRED", "code": code,
                        "person_id": person_id, "gate_id": gate_id, "ts": now,
                        "reason": "expired",
                        "reason_text": REASON_TEXT["expired"],
                        "valid_until": ticket["valid_until"],
                        "expired_at": ticket["expired_at"],
                    }
                    http_status = 410

        # 所有判定结果都带上门点分区与当时的策略版本，便于管理端审计
        response.setdefault("zone_id", gate_zone)
        response.setdefault("policy_version", policy_now)

        # 预约票（任何票面存在的判定分支）都附上批次与同行人数，门点核对整组人
        if "appointment" not in response:
            appt = appointment_info(conn, code)
            if appt:
                response["appointment"] = appt

        # 2) 记录扫码尝试（门点记录 + 幂等重放）
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


# ---------------- 访客预约批次 ----------------
#
# 容量与候补的核心规则（全部在 BEGIN IMMEDIATE 写事务内完成，天然串行）：
#   * 占用名额的状态只有 PENDING（待审核，先到先占座）和 APPROVED（已审核）。
#   * 提交申请时若整组人数能放进剩余名额 -> PENDING，否则 -> WAITLISTED
#     并分配批次内单调递增的 seq（即候补顺序）。
#   * 取消/拒绝任何占座申请（含取消已审核——同事务先作发票）后，按 seq
#     顺序扫描候补队列：队首整组放得下才晋级为 PENDING，放得下但后面的
#     更小也不跳过（严格 FIFO）；批次结束后不再晋级（名额无意义）。
#   * 审核 APPROVED 不改变占用数（PENDING 已占座），并在同一事务内签发
#     与批次分区一致的通行票；候补未晋级 / 取消 / 拒绝 / 过期均无票。
#   * 管理员补录 (source='admin') 跳过排队，但仍受容量约束，且在同一事务
#     内签票——任何路径都不可能超容量或产生无票的“通过”。

BATCH_PREFIX = "B-"
APP_PREFIX = "A-"
_TOKEN_BYTES = 12


def _gen_batch_id(conn: sqlite3.Connection) -> str:
    return BATCH_PREFIX + secrets.token_hex(5).upper()


def _gen_app_id(conn: sqlite3.Connection) -> str:
    return APP_PREFIX + secrets.token_hex(5).upper()


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def batch_dict(row: sqlite3.Row, *, used: Optional[int] = None,
               counts: Optional[dict] = None, now: Optional[str] = None) -> dict:
    d = dict(row)
    d["closed"] = bool(d.get("closed_at"))
    now = now or iso(utcnow())
    d["accepting"] = (not d["closed"]) and now < d["end_at"]
    if used is not None:
        d["used"] = used
        d["remaining"] = max(0, d["capacity"] - used)
    if counts is not None:
        d["counts"] = counts
    return d


def app_dict(row: sqlite3.Row, *, waitlist_position: Optional[int] = None) -> dict:
    d = dict(row)
    if waitlist_position is not None:
        d["waitlist_position"] = waitlist_position
    return d


def get_batch(conn: sqlite3.Connection, batch_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def get_batch_by_apply_token(
    conn: sqlite3.Connection, token: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE apply_token=?", (token,)
    ).fetchone()


def _used_seats(conn: sqlite3.Connection, batch_id: str) -> int:
    row = conn.execute(
        f"""SELECT COALESCE(SUM(party_size),0) s FROM applications
             WHERE batch_id=? AND status IN
                   ({','.join('?' for _ in CAPACITY_HOLDING_STATUSES)})""",
        (batch_id, *CAPACITY_HOLDING_STATUSES),
    ).fetchone()
    return int(row["s"])


def _status_counts(conn: sqlite3.Connection, batch_id: str) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) c, COALESCE(SUM(party_size),0) seats "
        "FROM applications WHERE batch_id=? GROUP BY status",
        (batch_id,),
    ).fetchall()
    counts = {s: {"count": 0, "seats": 0}
              for s in ("PENDING", "WAITLISTED", "APPROVED",
                        "CANCELLED", "REJECTED", "EXPIRED")}
    for r in rows:
        counts[r["status"]] = {"count": r["c"], "seats": int(r["seats"])}
    return counts


def _waitlist_position(
    conn: sqlite3.Connection, app_id: str, batch_id: Optional[str] = None
) -> Optional[int]:
    """候补名次（从 1 开始，按批次分区）；非 WAITLISTED 返回 None。"""
    if batch_id is None:
        row = conn.execute(
            "SELECT batch_id FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if row is None:
            return None
        batch_id = row["batch_id"]
    row = conn.execute(
        """WITH wl AS (
               SELECT id, ROW_NUMBER() OVER (ORDER BY seq) AS pos
                 FROM applications
                WHERE batch_id=? AND status='WAITLISTED'
           )
           SELECT pos FROM wl WHERE id=?""",
        (batch_id, app_id),
    ).fetchone()
    return int(row["pos"]) if row else None


def create_batch(
    conn: sqlite3.Connection,
    *,
    visit_date: str,
    start_at: str,
    end_at: str,
    zone_id: str,
    capacity: int,
    name: Optional[str] = None,
) -> dict:
    now = iso(utcnow())
    batch_id = _gen_batch_id(conn)
    token = _new_token()
    with write_tx(conn):
        conn.execute(
            """INSERT INTO batches
                   (id,name,visit_date,start_at,end_at,zone_id,capacity,
                    apply_token,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (batch_id, name, visit_date, start_at, end_at, zone_id, capacity,
             token, now),
        )
        conn.execute(
            """INSERT INTO batch_capacity_log
                   (batch_id,old_capacity,new_capacity,reason,changed_at)
               VALUES (?,?,?,?,?)""",
            (batch_id, None, capacity, "batch_created", now),
        )
        add_event(
            conn,
            "BATCH_CREATED",
            ts=now,
            batch_id=batch_id,
            reason="admin_create",
            payload={
                "name": name,
                "visit_date": visit_date,
                "start_at": start_at,
                "end_at": end_at,
                "zone_id": zone_id,
                "capacity": capacity,
            },
        )
        row = get_batch(conn, batch_id)
    d = batch_dict(row, used=0, counts=_empty_counts(), now=now)
    d["apply_token"] = token
    return d


def _empty_counts() -> dict:
    return {s: {"count": 0, "seats": 0}
            for s in ("PENDING", "WAITLISTED", "APPROVED",
                      "CANCELLED", "REJECTED", "EXPIRED")}


def list_batches(conn: sqlite3.Connection, *, limit: int = 200) -> list[dict]:
    now = iso(utcnow())
    rows = conn.execute(
        "SELECT * FROM batches ORDER BY start_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            batch_dict(
                r,
                used=_used_seats(conn, r["id"]),
                counts=_status_counts(conn, r["id"]),
                now=now,
            )
        )
    return out


def get_application(
    conn: sqlite3.Connection, application_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM applications WHERE id=?", (application_id,)
    ).fetchone()


def _promote_waitlist(
    conn: sqlite3.Connection, *, batch_id: str, now: str
) -> list[str]:
    """在写事务内：按 seq 严格 FIFO 把候补晋级到 PENDING，返回晋级申请 ID。

    队首整组放得下才晋级；放得下也不跳过队首去照顾后面的小团体。
    """
    promoted: list[str] = []
    if now >= get_batch(conn, batch_id)["end_at"]:
        return promoted  # 批次已结束，名额无意义，不再晋级
    while True:
        batch = get_batch(conn, batch_id)
        used = _used_seats(conn, batch_id)
        head = conn.execute(
            """SELECT * FROM applications
                WHERE batch_id=? AND status='WAITLISTED'
                ORDER BY seq LIMIT 1""",
            (batch_id,),
        ).fetchone()
        if head is None or used + head["party_size"] > batch["capacity"]:
            break
        conn.execute(
            """UPDATE applications
                  SET status='PENDING', promoted_at=?
                WHERE id=? AND status='WAITLISTED'""",
            (now, head["id"]),
        )
        add_event(
            conn,
            "APPLICATION_PROMOTED",
            ts=now,
            batch_id=batch_id,
            application_id=head["id"],
            person_id=head["id"],
            reason="seat_released",
            payload={
                "name": head["name"],
                "party_size": head["party_size"],
                "seq": head["seq"],
            },
        )
        promoted.append(head["id"])
    return promoted


def submit_application(
    conn: sqlite3.Connection,
    *,
    apply_token: str,
    request_id: str,
    name: str,
    contact: str,
    companions: int,
) -> dict:
    """访客通过一次性申请链接提交申请（公开端点，无管理员令牌）。

    幂等：request_id 唯一约束 + 写事务内复查；同一请求重放返回首次结果
    （replayed=True），同 request_id 不同载荷 -> 409 语义。
    """
    now = iso(utcnow())
    party_size = companions + 1
    # 事务前快速命中重放（绝大多数重复提交走这里）
    prior = conn.execute(
        "SELECT * FROM applications WHERE request_id=?", (request_id,)
    ).fetchone()
    if prior is not None:
        mismatch = _replay_mismatch(prior, name, contact, companions)
        if mismatch:
            return mismatch
        return _submit_reply(conn, prior, replayed=True)

    with write_tx(conn):
        prior = conn.execute(
            "SELECT * FROM applications WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior is not None:
            mismatch = _replay_mismatch(prior, name, contact, companions)
            if mismatch:
                return mismatch
            return _submit_reply(conn, prior, replayed=True)

        batch = get_batch_by_apply_token(conn, apply_token)
        if batch is None:
            return {"http_status": 404, "error": "申请链接无效或批次不存在"}
        if batch["closed_at"] is not None:
            return {"http_status": 410, "error": "该批次已关闭申请",
                    "batch_id": batch["id"]}
        if now >= batch["end_at"]:
            return {"http_status": 410, "error": "该批次访问时段已结束",
                    "batch_id": batch["id"]}

        seq = int(conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 s FROM applications WHERE batch_id=?",
            (batch["id"],),
        ).fetchone()["s"])
        used = _used_seats(conn, batch["id"])
        status = "PENDING" if used + party_size <= batch["capacity"] else "WAITLISTED"
        app_id = _gen_app_id(conn)
        manage_token = _new_token()
        conn.execute(
            """INSERT INTO applications
                   (id,batch_id,seq,name,contact,party_size,companions,status,
                    source,request_id,manage_token,created_at)
               VALUES (?,?,?,?,?,?,?,?,'visitor',?,?,?)""",
            (app_id, batch["id"], seq, name, contact, party_size, companions,
             status, request_id, manage_token, now),
        )
        add_event(
            conn,
            "APPLICATION_SUBMITTED",
            ts=now,
            batch_id=batch["id"],
            application_id=app_id,
            person_id=app_id,
            reason="visitor_form",
            payload={
                "name": name,
                "party_size": party_size,
                "companions": companions,
                "seq": seq,
                "status": status,
            },
        )
        row = get_application(conn, app_id)
        result = app_dict(row)
        result["http_status"] = 201
        result["batch_id"] = batch["id"]
        result["used"] = _used_seats(conn, batch["id"])
        result["capacity"] = batch["capacity"]
        if status == "WAITLISTED":
            result["waitlist_position"] = _waitlist_position(conn, app_id)
    result["manage_token"] = manage_token
    return result


def _replay_mismatch(
    prior: sqlite3.Row, name: str, contact: str, companions: int
) -> Optional[dict]:
    """同 request_id 但载荷关键内容不一致：明确报冲突，不做覆盖。"""
    if (prior["name"] != name or prior["contact"] != contact
            or prior["companions"] != companions):
        return {
            "http_status": 409,
            "error": "request_id 已用于内容不同的申请（幂等键冲突）",
            "application_id": prior["id"],
        }
    return None


def _submit_reply(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    replayed: bool,
) -> dict:
    """幂等重放报文：返回首次提交的申请状态（含候补名次/管理令牌）。"""
    d = app_dict(row, waitlist_position=(
        _waitlist_position(conn, row["id"]) if row["status"] == "WAITLISTED" else None
    ))
    d["http_status"] = 200
    d["replayed"] = replayed
    batch = get_batch(conn, row["batch_id"])
    d["batch_id"] = row["batch_id"]
    d["used"] = _used_seats(conn, row["batch_id"])
    d["capacity"] = batch["capacity"]
    d["manage_token"] = row["manage_token"]
    return d


def approve_application(
    conn: sqlite3.Connection, *, application_id: str, ttl_seconds: Optional[int] = None
) -> dict:
    """管理员审核通过：同事务签发与批次分区一致的通行票。

    - 候补申请不能越级通过（必须先按 FIFO 晋级），否则 409；
    - 批次已结束或已关闭 -> 410；容量被管理员调低等原因导致放不下 -> 409；
    - 票有效期：默认覆盖 [批次开始, 批次结束]（立即生效不早于现在），
      审核发生在批次进行中时，valid_from 取 max(now, start_at)。
    """
    now = iso(utcnow())
    with write_tx(conn):
        app = get_application(conn, application_id)
        if app is None:
            return {"http_status": 404, "error": "申请不存在"}
        batch = get_batch(conn, app["batch_id"])
        if app["status"] == "APPROVED":
            return _already_approved(conn, app, batch)
        if app["status"] != "PENDING":
            return {
                "http_status": 409,
                "error": f"申请当前状态为 {app['status']}，不可审核通过"
                         "（候补须先按顺序晋级；终态不可变更）",
                "status": app["status"],
            }
        if batch["closed_at"] is not None:
            return {"http_status": 410, "error": "批次已关闭申请",
                    "status": app["status"]}
        if now >= batch["end_at"]:
            return {"http_status": 410, "error": "批次访问时段已结束，不能审核",
                    "status": app["status"]}
        # PENDING 在提交时已占座，审核为 APPROVED 不新增占用，无需再判容量；
        # 这里仅保留一个不变量自检（容量只能通过受控接口调整）。
        if _used_seats(conn, batch["id"]) > batch["capacity"]:
            return {"http_status": 409,
                    "error": "当前占座已超过容量（容量曾被调低），请先调整容量",
                    "used": _used_seats(conn, batch["id"]),
                    "capacity": batch["capacity"]}

        if ttl_seconds:
            vf = utcnow()
            vu = vf.fromtimestamp(vf.timestamp() + ttl_seconds, tz=vf.tzinfo)
            valid_from, valid_until = iso(vf), iso(vu)
        else:
            start = parse_dt(batch["start_at"])
            end = parse_dt(batch["end_at"])
            vf = max(utcnow(), start)
            valid_from, valid_until = iso(vf), iso(end)

        code, ticket_version = _issue_ticket_locked(
            conn,
            person_id=app["id"],
            valid_from=valid_from,
            valid_until=valid_until,
            note=f"访客批次 {batch['id']} · {app['name']}（共{app['party_size']}人）",
            zones=[batch["zone_id"]],
            batch_id=batch["id"],
            application_id=app["id"],
            party_size=app["party_size"],
        )
        conn.execute(
            """UPDATE applications
                  SET status='APPROVED', decided_at=?, decide_reason='admin_approve',
                      ticket_code=?
                WHERE id=?""",
            (now, code, app["id"]),
        )
        add_event(
            conn,
            "APPLICATION_APPROVED",
            ts=now,
            batch_id=batch["id"],
            application_id=app["id"],
            person_id=app["id"],
            ticket_code=code,
            reason="admin_approve",
            payload={
                "name": app["name"],
                "party_size": app["party_size"],
                "ticket_code": code,
                "ticket_version": ticket_version,
                "zone_id": batch["zone_id"],
            },
        )
        row = get_application(conn, app["id"])
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (code,)
        ).fetchone()
    return {
        "http_status": 200,
        "application": app_dict(row),
        "ticket": ticket_dict(ticket),
    }


def _already_approved(
    conn: sqlite3.Connection, app: sqlite3.Row, batch: sqlite3.Row
) -> dict:
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE code=?", (app["ticket_code"],)
    ).fetchone()
    return {
        "http_status": 200,
        "duplicated": True,
        "application": app_dict(app),
        "ticket": ticket_dict(ticket) if ticket else None,
    }


def reject_application(
    conn: sqlite3.Connection, *, application_id: str, reason: str
) -> dict:
    """管理员拒绝未终态申请；拒绝 PENDING 会释放名额并晋级候补。"""
    now = iso(utcnow())
    with write_tx(conn):
        app = get_application(conn, application_id)
        if app is None:
            return {"http_status": 404, "error": "申请不存在"}
        if app["status"] in ("CANCELLED", "REJECTED", "EXPIRED", "APPROVED"):
            return {"http_status": 409,
                    "error": f"申请已处于终态 {app['status']}，不可拒绝",
                    "status": app["status"]}
        held = app["status"] == "PENDING"
        conn.execute(
            """UPDATE applications
                  SET status='REJECTED', decided_at=?, decide_reason=?
                WHERE id=?""",
            (now, reason or "admin_reject", app["id"]),
        )
        add_event(
            conn,
            "APPLICATION_REJECTED",
            ts=now,
            batch_id=app["batch_id"],
            application_id=app["id"],
            person_id=app["id"],
            reason=reason or "admin_reject",
            payload={"name": app["name"], "party_size": app["party_size"],
                     "from_status": app["status"]},
        )
        promoted = _promote_waitlist(conn, batch_id=app["batch_id"], now=now) if held else []
        row = get_application(conn, app["id"])
    return {"http_status": 200, "application": app_dict(row),
            "promoted": promoted}


def cancel_application(
    conn: sqlite3.Connection, *, application_id: str, reason: str
) -> dict:
    """取消申请。

    - WAITLISTED 取消：不占名额，直接终态；
    - PENDING 取消：释放名额并按候补顺序晋级；
    - APPROVED 取消：在同一事务内先作废已签发的票（票已核销则不允许取消，
      因为访客实际已到场），再释放名额并晋级候补；
    - 已终态：409（终态不可逆）。
    """
    now = iso(utcnow())
    with write_tx(conn):
        app = get_application(conn, application_id)
        if app is None:
            return {"http_status": 404, "error": "申请不存在"}
        if app["status"] in ("CANCELLED", "REJECTED", "EXPIRED"):
            return {"http_status": 409,
                    "error": f"申请已处于终态 {app['status']}，不可取消",
                    "status": app["status"]}

        ticket_revoke = None
        if app["status"] == "APPROVED":
            t = conn.execute(
                "SELECT * FROM tickets WHERE code=?", (app["ticket_code"],)
            ).fetchone()
            if t is not None and t["status"] == "REDEEMED":
                return {"http_status": 409,
                        "error": "通行票已核销（访客已到场），不可取消申请",
                        "status": "APPROVED", "ticket_status": "REDEEMED"}
            if t is not None and t["status"] == "ACTIVE":
                _revoke_ticket_locked(
                    conn,
                    code=t["code"],
                    reason=f"申请取消: {reason or 'visitor/admin cancel'}",
                    now=now,
                )
                ticket_revoke = t["code"]

        held = app["status"] in ("PENDING", "APPROVED")
        conn.execute(
            """UPDATE applications
                  SET status='CANCELLED', decided_at=?, decide_reason=?
                WHERE id=?""",
            (now, reason or "admin_cancel", app["id"]),
        )
        add_event(
            conn,
            "APPLICATION_CANCELLED",
            ts=now,
            batch_id=app["batch_id"],
            application_id=app["id"],
            person_id=app["id"],
            ticket_code=app["ticket_code"],
            reason=reason or "admin_cancel",
            payload={
                "name": app["name"],
                "party_size": app["party_size"],
                "from_status": app["status"],
                "ticket_revoked": ticket_revoke,
            },
        )
        promoted = _promote_waitlist(conn, batch_id=app["batch_id"], now=now) if held else []
        row = get_application(conn, app["id"])
    return {"http_status": 200, "application": app_dict(row),
            "ticket_revoked": ticket_revoke, "promoted": promoted}


def backfill_application(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    name: str,
    contact: str,
    companions: int,
    reason: Optional[str] = None,
    approve: bool = True,
) -> dict:
    """管理员补录：source='admin'，默认直接审核通过并签票。

    与访客申请一样受容量约束（同事务判定），因此补录也不可能超容量；
    容量不足时返回 409，不会留下“通过但无票”的记录。
    """
    now = iso(utcnow())
    party_size = companions + 1
    with write_tx(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            return {"http_status": 404, "error": "批次不存在"}
        if approve and (batch["closed_at"] is not None or now >= batch["end_at"]):
            return {"http_status": 410, "error": "批次已关闭或已结束，不能补录并通过"}
        if approve:
            used = _used_seats(conn, batch_id)
            if used + party_size > batch["capacity"]:
                return {"http_status": 409,
                        "error": "容量不足，无法补录（请先调整批次容量）",
                        "used": used, "capacity": batch["capacity"]}

        seq = int(conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 s FROM applications WHERE batch_id=?",
            (batch_id,),
        ).fetchone()["s"])
        app_id = _gen_app_id(conn)
        # 补录直接 APPROVED 时初始即占用名额，不经候补；不批准则进 PENDING 占座
        status = "APPROVED" if approve else "PENDING"
        conn.execute(
            """INSERT INTO applications
                   (id,batch_id,seq,name,contact,party_size,companions,status,
                    source,request_id,manage_token,created_at)
               VALUES (?,?,?,?,?,?,?,?,'admin',NULL,?,?)""",
            (app_id, batch_id, seq, name, contact, party_size, companions,
             status, _new_token(), now),
        )
        add_event(
            conn,
            "APPLICATION_SUBMITTED",
            ts=now,
            batch_id=batch_id,
            application_id=app_id,
            person_id=app_id,
            reason="admin_backfill",
            payload={"name": name, "party_size": party_size,
                     "companions": companions, "seq": seq, "status": status},
        )

        ticket_row = None
        if approve:
            start = parse_dt(batch["start_at"])
            end = parse_dt(batch["end_at"])
            vf = max(utcnow(), start)
            code, ticket_version = _issue_ticket_locked(
                conn,
                person_id=app_id,
                valid_from=iso(vf),
                valid_until=iso(end),
                note=f"管理员补录 {batch['id']} · {name}（共{party_size}人）",
                zones=[batch["zone_id"]],
                batch_id=batch_id,
                application_id=app_id,
                party_size=party_size,
            )
            conn.execute(
                """UPDATE applications
                      SET decided_at=?, decide_reason=?, ticket_code=?
                    WHERE id=?""",
                (now, reason or "admin_backfill", code, app_id),
            )
            add_event(
                conn,
                "APPLICATION_APPROVED",
                ts=now,
                batch_id=batch_id,
                application_id=app_id,
                person_id=app_id,
                ticket_code=code,
                reason=reason or "admin_backfill",
                payload={"name": name, "party_size": party_size,
                         "ticket_code": code, "ticket_version": ticket_version,
                         "zone_id": batch["zone_id"], "backfill": True},
            )
            ticket_row = conn.execute(
                "SELECT * FROM tickets WHERE code=?", (code,)
            ).fetchone()
        row = get_application(conn, app_id)
    out = {"http_status": 201, "application": app_dict(row)}
    if ticket_row is not None:
        out["ticket"] = ticket_dict(ticket_row)
    return out


def change_batch_capacity(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    new_capacity: int,
    reason: Optional[str] = None,
) -> dict:
    """调整人数上限（append-only 留痕）。

    - 不允许调到低于当前占座数（PENDING+APPROVED），避免“负余量”；
    - 调高时同事务按 FIFO 晋级候补（可能连续晋级多组，直到放不下队首）。
    """
    now = iso(utcnow())
    with write_tx(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            return {"http_status": 404, "error": "批次不存在"}
        used = _used_seats(conn, batch_id)
        if new_capacity < used:
            return {"http_status": 409,
                    "error": f"新容量 {new_capacity} 低于当前占座人数 {used}，"
                             "不能调低（请先取消/拒绝部分申请）",
                    "used": used}
        old_capacity = batch["capacity"]
        if new_capacity == old_capacity:
            return {"http_status": 200, "duplicated": True,
                    "batch": batch_dict(batch, used=used,
                                        counts=_status_counts(conn, batch_id), now=now)}
        conn.execute(
            "UPDATE batches SET capacity=? WHERE id=?", (new_capacity, batch_id)
        )
        conn.execute(
            """INSERT INTO batch_capacity_log
                   (batch_id,old_capacity,new_capacity,reason,changed_at)
               VALUES (?,?,?,?,?)""",
            (batch_id, old_capacity, new_capacity, reason or "admin_change", now),
        )
        add_event(
            conn,
            "BATCH_CAPACITY_CHANGED",
            ts=now,
            batch_id=batch_id,
            reason=reason or "admin_change",
            payload={"old_capacity": old_capacity,
                     "new_capacity": new_capacity},
        )
        promoted = (_promote_waitlist(conn, batch_id=batch_id, now=now)
                    if new_capacity > old_capacity else [])
        row = get_batch(conn, batch_id)
    return {
        "http_status": 200,
        "batch": batch_dict(row, used=_used_seats(conn, batch_id),
                            counts=_status_counts(conn, batch_id), now=now),
        "promoted": promoted,
    }


def close_batch(conn: sqlite3.Connection, *, batch_id: str) -> dict:
    """管理员提前关闭申请（已审核的票不受影响；候补不再晋级）。"""
    now = iso(utcnow())
    with write_tx(conn):
        batch = get_batch(conn, batch_id)
        if batch is None:
            return {"http_status": 404, "error": "批次不存在"}
        if batch["closed_at"] is not None:
            return {"http_status": 200, "duplicated": True,
                    "batch": batch_dict(batch, used=_used_seats(conn, batch_id),
                                        counts=_status_counts(conn, batch_id), now=now)}
        conn.execute("UPDATE batches SET closed_at=? WHERE id=?", (now, batch_id))
        add_event(
            conn,
            "BATCH_CLOSED",
            ts=now,
            batch_id=batch_id,
            reason="admin_close",
            payload={"visit_date": batch["visit_date"]},
        )
        row = get_batch(conn, batch_id)
    return {"http_status": 200,
            "batch": batch_dict(row, used=_used_seats(conn, batch_id),
                                counts=_status_counts(conn, batch_id), now=now)}


def public_batch_view(conn: sqlite3.Connection, apply_token: str) -> Optional[dict]:
    """访客申请链接看到的批次信息（不含其他访客数据）。"""
    batch = get_batch_by_apply_token(conn, apply_token)
    if batch is None:
        return None
    now = iso(utcnow())
    wl = conn.execute(
        "SELECT COUNT(*) c FROM applications WHERE batch_id=? AND status='WAITLISTED'",
        (batch["id"],),
    ).fetchone()["c"]
    d = batch_dict(batch, used=_used_seats(conn, batch["id"]), now=now)
    d["waitlist_count"] = wl
    # 申请令牌本身不回显
    d.pop("apply_token", None)
    return d


def public_application_view(
    conn: sqlite3.Connection, manage_token: str
) -> Optional[dict]:
    """访客凭申请后拿到的 manage_token 查询自身状态（候补名次/票据）。"""
    row = conn.execute(
        "SELECT * FROM applications WHERE manage_token=?", (manage_token,)
    ).fetchone()
    if row is None:
        return None
    d = app_dict(
        row,
        waitlist_position=(_waitlist_position(conn, row["id"])
                           if row["status"] == "WAITLISTED" else None),
    )
    d.pop("request_id", None)
    d.pop("manage_token", None)
    batch = get_batch(conn, row["batch_id"])
    d["batch"] = {
        "id": batch["id"], "name": batch["name"],
        "visit_date": batch["visit_date"], "start_at": batch["start_at"],
        "end_at": batch["end_at"], "zone_id": batch["zone_id"],
    }
    if row["ticket_code"]:
        t = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (row["ticket_code"],)
        ).fetchone()
        if t is not None:
            d["ticket"] = ticket_dict(t)
    return d


def list_applications(
    conn: sqlite3.Connection,
    *,
    batch_id: str,
    status: Optional[str] = None,
) -> list[dict]:
    sql = "SELECT * FROM applications WHERE batch_id=?"
    params: list = [batch_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY seq"
    rows = conn.execute(sql, params).fetchall()
    # 候补名次一次性算好
    wl_seq = [
        r["id"] for r in conn.execute(
            "SELECT id FROM applications WHERE batch_id=? AND status='WAITLISTED' "
            "ORDER BY seq", (batch_id,)
        ).fetchall()
    ]
    positions = {a_id: i + 1 for i, a_id in enumerate(wl_seq)}
    return [app_dict(r, waitlist_position=positions.get(r["id"])) for r in rows]


def capacity_log(conn: sqlite3.Connection, batch_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM batch_capacity_log WHERE batch_id=? ORDER BY id",
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def batch_detail(conn: sqlite3.Connection, batch_id: str) -> Optional[dict]:
    """管理员按批次查看：申请、候补顺序、已签发票、容量变化记录。"""
    batch = get_batch(conn, batch_id)
    if batch is None:
        return None
    now = iso(utcnow())
    applications = list_applications(conn, batch_id=batch_id)
    tickets = [
        ticket_dict(r) for r in conn.execute(
            "SELECT * FROM tickets WHERE batch_id=? ORDER BY issued_at", (batch_id,)
        ).fetchall()
    ]
    events = [
        event_dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
    ]
    return {
        "batch": batch_dict(
            batch, used=_used_seats(conn, batch_id),
            counts=_status_counts(conn, batch_id), now=now,
        ),
        "applications": applications,
        "waitlist": [a for a in applications if a["status"] == "WAITLISTED"],
        "tickets": tickets,
        "capacity_log": capacity_log(conn, batch_id),
        "events": events,
        "now": now,
    }


def sweep_applications(conn: sqlite3.Connection) -> list[str]:
    """后台周期任务：批次时段结束后，未决申请（待审核/候补）置为 EXPIRED。

    候补未晋级、待审核未处理的申请到期一律终态、且从未签发过票；
    APPROVED 的申请不动（其票由票据清扫按 valid_until 过期）。
    """
    now = iso(utcnow())
    expired: list[str] = []
    with write_tx(conn):
        rows = conn.execute(
            """SELECT a.* FROM applications a JOIN batches b ON b.id=a.batch_id
                WHERE a.status IN ('PENDING','WAITLISTED') AND b.end_at<=?""",
            (now,),
        ).fetchall()
        for a in rows:
            conn.execute(
                """UPDATE applications
                      SET status='EXPIRED', decided_at=?,
                          decide_reason='batch_ended'
                    WHERE id=?""",
                (now, a["id"]),
            )
            add_event(
                conn,
                "APPLICATION_EXPIRED",
                ts=now,
                batch_id=a["batch_id"],
                application_id=a["id"],
                person_id=a["id"],
                reason="batch_ended",
                payload={"name": a["name"], "party_size": a["party_size"],
                         "from_status": a["status"]},
            )
            expired.append(a["id"])
    return expired


# ---------------- 过期清扫（旧） ----------------

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
                batch_id=t["batch_id"],
                application_id=t["application_id"],
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
    batch_id: Optional[str] = None,
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
    if batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
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
    zone_rows = conn.execute("SELECT id FROM zones").fetchall()
    zones_locked = sum(1 for z in zone_rows if zone_lock_state(conn, z["id"])["locked"])
    batches_total = conn.execute(
        "SELECT COUNT(*) c FROM batches"
    ).fetchone()["c"]
    batches_open = conn.execute(
        "SELECT COUNT(*) c FROM batches WHERE closed_at IS NULL AND end_at>?",
        (now_str,),
    ).fetchone()["c"]
    apps_counts = {
        s: conn.execute(
            "SELECT COUNT(*) c FROM applications WHERE status=?", (s,)
        ).fetchone()["c"]
        for s in ("PENDING", "WAITLISTED", "APPROVED",
                  "CANCELLED", "REJECTED", "EXPIRED")
    }
    return {
        "now": now_str,
        "tickets": counts,
        "gates": conn.execute("SELECT COUNT(*) c FROM gates").fetchone()["c"],
        "gates_revoked": conn.execute(
            "SELECT COUNT(*) c FROM gates WHERE revoked=1"
        ).fetchone()["c"],
        "zones": len(zone_rows),
        "zones_locked": zones_locked,
        "policy_version": current_policy_version(conn),
        "events": conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
        "last_version": conn.execute(
            "SELECT COALESCE(MAX(id),0) m FROM events"
        ).fetchone()["m"],
        "batches": batches_total,
        "batches_open": batches_open,
        "applications": apps_counts,
    }
