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
from . import presence as presence_svc
from . import routes as routes_svc

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
    """核销时附加的批次/同行人数信息（门点据此核对整组访客）。

    附带申请当前变更版本（change_version）、同行人名单，以及该票若为
    变更换发出来的新票，给出被替换的旧票号与替换原因；若为被替换掉
    的旧票，给出新票号（门点可明确告知“请扫新票”）。
    """
    row = conn.execute(
        """SELECT t.batch_id, t.application_id, t.party_size, t.replaced_code,
                  t.replacement_reason, t.replaced_by_code,
                  b.name AS batch_name, b.visit_date, b.zone_id,
                  a.name AS applicant_name, a.status AS application_status,
                  a.change_version, a.companion_names
             FROM tickets t
             LEFT JOIN batches b ON b.id = t.batch_id
             LEFT JOIN applications a ON a.id = t.application_id
            WHERE t.code=?""",
        (code,),
    ).fetchone()
    if row is None or row["batch_id"] is None:
        return None
    try:
        companion_names = json.loads(row["companion_names"] or "[]")
    except json.JSONDecodeError:
        companion_names = []
    info = {
        "batch_id": row["batch_id"],
        "batch_name": row["batch_name"],
        "visit_date": row["visit_date"],
        "zone_id": row["zone_id"],
        "application_id": row["application_id"],
        "applicant_name": row["applicant_name"],
        "application_status": row["application_status"],
        "party_size": row["party_size"],
        "change_version": row["change_version"],
        "companion_names": companion_names,
    }
    if row["replaced_code"]:
        info["replaced_code"] = row["replaced_code"]
        info["replacement_reason"] = row["replacement_reason"]
    if row["replaced_by_code"]:
        info["replaced_by_code"] = row["replaced_by_code"]
    return info


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
    replaced_code: Optional[str] = None,
    replacement_reason: Optional[str] = None,
    route_id: Optional[str] = None,
    bind_route: bool = False,
) -> tuple[str, int]:
    """已在 IMMEDIATE 事务内：插入票并写 TICKET_ISSUED 事件，返回 (code, version)。

    供管理端直接发票，也供预约审核/补录在同一事务内签票使用，
    保证“审核通过”与“票存在且与批次分区一致”原子可见。
    申请变更换发时 ``replaced_code`` 指向被原子撤销的旧票，
    ``replacement_reason`` 记录旧票替换原因（变更说明）。

    路线绑定：
      * 管理端发票显式给 ``route_id``（bind_route=True）：校验路线 ACTIVE 并
        写 route_bindings/ROUTE_BOUND，票固化当前版本；
      * 批次签票（batch_id 非空）：批次若已绑定路线，同事务把路线固化到票；
      * 变更换发新票：继承被替换旧票的路线版本（旧票已核销不能变更，因此能
        走到换票的票都尚未开始，继承的是其绑定尚未开始的路线）。
    """
    now = iso(utcnow())
    zone_list = list(zones or [])
    code = _gen_unique_code(conn)
    route_version: Optional[int] = None
    binding_event_version: Optional[int] = None
    if bind_route and route_id:
        # 管理端发票直接绑定：取 ACTIVE 当前版本并留绑定审计
        route = routes_svc.get_route(conn, route_id)
        if route is None:
            raise ValueError(f"路线不存在: {route_id}")
        if route["status"] == "PAUSED" or route["current_version"] < 1:
            raise ValueError("路线已暂停或尚未发布版本，不能绑定发票")
        route_version = int(route["current_version"])
    elif not route_id and batch_id:
        b = get_batch(conn, batch_id)
        if b is not None and b["route_id"]:
            route_id = b["route_id"]
            route_version = int(b["route_version"] or 0) or None
            # 批次绑定后若路线又发了新版本，签票时固化“当前 ACTIVE 版本”：
            # 批次绑定代表“这批人走这条路线”，具体版本以开始前最新发布为准，
            # 一旦开始则在 route_progress 永久固化。
            route = routes_svc.get_route(conn, route_id)
            if route is not None and route["status"] == "ACTIVE" and route["current_version"] >= 1:
                route_version = int(route["current_version"])
    elif route_id and not bind_route:
        # 换发继承：调用方显式传入旧票的 route_version（下方再被覆盖为票列）
        route_version = None

    conn.execute(
        """INSERT INTO tickets
               (code,person_id,valid_from,valid_until,issued_at,status,note,zones,
                batch_id,application_id,party_size,replaced_code,replacement_reason,
                route_id,route_version)
           VALUES (?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,?,?,?)""",
        (code, person_id, valid_from, valid_until, now, note,
         json.dumps(zone_list, ensure_ascii=False),
         batch_id, application_id, party_size, replaced_code, replacement_reason,
         route_id, route_version),
    )
    if bind_route and route_id and route_version is not None:
        ev = add_event(
            conn,
            "ROUTE_BOUND",
            ts=now,
            route_id=route_id,
            route_version=route_version,
            ticket_code=code,
            person_id=person_id,
            reason="issue_ticket",
            payload={"scope": "TICKET", "ticket_code": code,
                     "version": route_version, "operator": "admin"},
            batch_id=batch_id,
            application_id=application_id,
        )
        conn.execute(
            "INSERT INTO route_bindings "
            "(route_id,route_version,scope,ticket_code,batch_id,bound_at,bound_by,"
            "reason,event_version) VALUES (?,?,'TICKET',?,NULL,?,'admin',?,?)",
            (route_id, route_version, code, now, "issue_ticket", ev),
        )
        binding_event_version = ev
    elif batch_id and route_id and route_version is not None:
        # 批次签票继承路线：留票级绑定审计（批次本身绑定事件已在建批/改绑时写过）
        add_event(
            conn,
            "ROUTE_BOUND",
            ts=now,
            route_id=route_id,
            route_version=route_version,
            ticket_code=code,
            person_id=person_id,
            reason="issue_from_bound_batch",
            payload={"scope": "TICKET", "ticket_code": code,
                     "version": route_version, "from_batch": batch_id},
            batch_id=batch_id,
            application_id=application_id,
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
    if replaced_code is not None:
        payload["replaced_code"] = replaced_code
        payload["replacement_reason"] = replacement_reason
    if route_id is not None:
        payload["route_id"] = route_id
        payload["route_version"] = route_version
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
    route_id: Optional[str] = None,
) -> dict:
    with write_tx(conn):
        try:
            code, version = _issue_ticket_locked(
                conn,
                person_id=person_id,
                valid_from=valid_from,
                valid_until=valid_until,
                note=note,
                zones=zones,
                route_id=route_id,
                bind_route=bool(route_id),
            )
        except ValueError as e:
            return {"http_status": 400, "error": str(e)}
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
    replaced_by_code: Optional[str] = None,
) -> tuple[Optional[sqlite3.Row], str]:
    """已在 IMMEDIATE 事务内：作废一张 ACTIVE 票并写事件。

    返回 (ticket_row_or_None, state)，state ∈
    ``ok`` / ``not_found`` / ``terminal``。供预约取消在同事务内
    撤销已签发的票（随后才把名额释放给候补），也供申请变更审核通过时
    在同事务内撤销旧票（``replaced_by_code`` 记录原子换发的新票号）。
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
        payload={"reason": reason,
                 **({"replaced_by_code": replaced_by_code} if replaced_by_code else {})},
        batch_id=ticket["batch_id"],
        application_id=ticket["application_id"],
    )
    conn.execute(
        """UPDATE tickets
              SET status='REVOKED', revoked_at=?, revoked_reason=?,
                  replaced_by_code=COALESCE(?, replaced_by_code)
            WHERE code=?""",
        (now, reason, replaced_by_code, code),
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
    route_version: int = 0,
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
                            # 绑定了检查路线的票：核销即“开始路线”，必须从入口
                            # 检查点进入（首次核销开始路线）。stale_route 与
                            # stale_policy 一样不落扫码记录，门点同步后用同一
                            # attempt_id 重试；其余路线拒绝是明确业务结论。
                            route_pre = routes_svc.redeem_route_precheck_locked(
                                conn,
                                ticket=ticket,
                                gate_id=gate_id,
                                attempt_id=attempt_id,
                                client_route_version=route_version,
                                now=now,
                            )
                            if route_pre is not None and route_pre.get("early"):
                                return {
                                    "http_status": 409,
                                    "response": route_pre["early"],
                                }
                            route_reject = (
                                route_pre if route_pre is not None else None)
                            if route_reject is not None:
                                response = route_reject["response"]
                                http_status = route_reject["http_status"]
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
                                    # 核销成功即在同一事务登记到场（申请人+同行
                                    # 人数当场冻结）。已 DEPARTED/REMOVED 的票不
                                    # 自动复活，只在响应里标注当前在场状态。
                                    arrival_version = presence_svc.register_arrival_locked(
                                        conn,
                                        ticket=ticket,
                                        gate_id=gate_id,
                                        attempt_id=attempt_id,
                                        now=now,
                                        policy_version=policy_now,
                                    )
                                    pres = presence_svc.get_presence(conn, code)
                                    response["presence_status"] = (
                                        pres["status"] if pres else "ARRIVED")
                                    if arrival_version is not None:
                                        response["arrival_version"] = arrival_version
                                    # 首次核销同事务开始检查路线（固化路线版本）
                                    ticket_after = conn.execute(
                                        "SELECT * FROM tickets WHERE code=?", (code,)
                                    ).fetchone()
                                    route_block = routes_svc.start_route_after_redeem_locked(
                                        conn,
                                        ticket=ticket_after,
                                        gate_id=gate_id,
                                        attempt_id=attempt_id,
                                        now=now,
                                    )
                                    if route_block is not None:
                                        response["route"] = route_block
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
                    # 已开始检查路线的票：门点得知路线当前状态（应改用检查点模式）
                    if ticket["route_id"]:
                        rp = routes_svc.get_progress_by_ticket(conn, code)
                        if rp is not None:
                            response["route"] = routes_svc.route_status_block(
                                conn, rp, now)
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
                    # 被变更换发所替换的旧票：明确告知门点新票号，引导扫新票
                    if ticket["replaced_by_code"]:
                        response["replaced_by_code"] = ticket["replaced_by_code"]
                        response["replacement_reason"] = ticket["replacement_reason"]
                        response["reason_text"] = (
                            f"该票已因申请变更被替换，请改扫新票 {ticket['replaced_by_code']}"
                        )
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
CHANGE_PREFIX = "C-"
_TOKEN_BYTES = 12


def _gen_batch_id(conn: sqlite3.Connection) -> str:
    return BATCH_PREFIX + secrets.token_hex(5).upper()


def _gen_app_id(conn: sqlite3.Connection) -> str:
    return APP_PREFIX + secrets.token_hex(5).upper()


def _gen_change_id(conn: sqlite3.Connection) -> str:
    return CHANGE_PREFIX + secrets.token_hex(5).upper()


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
    try:
        d["companion_names"] = json.loads(d.get("companion_names") or "[]")
    except json.JSONDecodeError:
        d["companion_names"] = []
    if waitlist_position is not None:
        d["waitlist_position"] = waitlist_position
    return d


def change_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["new_companion_names"] = json.loads(d.get("new_companion_names") or "[]")
    except json.JSONDecodeError:
        d["new_companion_names"] = []
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
    route_id: Optional[str] = None,
    operator: str = "admin",
) -> dict:
    now = iso(utcnow())
    batch_id = _gen_batch_id(conn)
    token = _new_token()
    with write_tx(conn):
        route_version: Optional[int] = None
        if route_id:
            route = routes_svc.get_route(conn, route_id)
            if route is None:
                return {"http_status": 400, "error": f"路线不存在: {route_id}"}
            if route["zone_id"] != zone_id:
                return {"http_status": 400,
                        "error": f"路线分区 {route['zone_id']} 与批次分区 {zone_id} 不一致"}
            if route["status"] == "PAUSED" or route["current_version"] < 1:
                return {"http_status": 409,
                        "error": "路线已暂停或尚未发布版本，不能绑定批次"}
            route_version = int(route["current_version"])
        conn.execute(
            """INSERT INTO batches
                   (id,name,visit_date,start_at,end_at,zone_id,capacity,
                    apply_token,created_at,route_id,route_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, name, visit_date, start_at, end_at, zone_id, capacity,
             token, now, route_id, route_version),
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
                "route_id": route_id,
                "route_version": route_version,
            },
        )
        if route_id:
            bind_version = add_event(
                conn,
                "ROUTE_BOUND",
                ts=now,
                batch_id=batch_id,
                route_id=route_id,
                route_version=route_version,
                reason="create_batch",
                payload={"scope": "BATCH", "batch_id": batch_id,
                         "version": route_version, "operator": operator},
            )
            conn.execute(
                "INSERT INTO route_bindings "
                "(route_id,route_version,scope,ticket_code,batch_id,bound_at,bound_by,"
                "reason,event_version) VALUES (?,?,'BATCH',NULL,?,?,?,?,?)",
                (route_id, route_version, batch_id, now, operator,
                 "create_batch", bind_version),
            )
        row = get_batch(conn, batch_id)
    d = batch_dict(row, used=0, counts=_empty_counts(), now=now)
    d["apply_token"] = token
    d.pop("http_status", None)
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


def _finalize_pending_changes(
    conn: sqlite3.Connection,
    app: sqlite3.Row,
    status: str,
    *,
    reason: str,
    now: str,
) -> list[str]:
    """在写事务内：把某申请名下所有 PENDING 变更置为终态（不产生任何票）。

    申请被拒绝/取消/过期时联动调用：待审核变更失去意义，随之
    REJECTED/CANCELLED/EXPIRED，并写对应事件（门点按版本可见）。
    """
    rows = conn.execute(
        "SELECT * FROM application_changes WHERE application_id=? AND status='PENDING'",
        (app["id"],),
    ).fetchall()
    event_type = {
        "REJECTED": "APPLICATION_CHANGE_REJECTED",
        "CANCELLED": "APPLICATION_CHANGE_CANCELLED",
        "EXPIRED": "APPLICATION_CHANGE_EXPIRED",
    }[status]
    out: list[str] = []
    for ch in rows:
        conn.execute(
            """UPDATE application_changes
                  SET status=?, decided_at=?, decide_reason=?
                WHERE id=? AND status='PENDING'""",
            (status, now, reason, ch["id"]),
        )
        add_event(
            conn,
            event_type,
            ts=now,
            batch_id=app["batch_id"],
            application_id=app["id"],
            person_id=app["id"],
            reason=reason,
            payload={"change_id": ch["id"], "change_seq": ch["change_seq"]},
        )
        out.append(ch["id"])
    return out


def submit_application(
    conn: sqlite3.Connection,
    *,
    apply_token: str,
    request_id: str,
    name: str,
    contact: str,
    companions: int,
    companion_names: Optional[list[str]] = None,
) -> dict:
    """访客通过一次性申请链接提交申请（公开端点，无管理员令牌）。

    幂等：request_id 唯一约束 + 写事务内复查；同一请求重放返回首次结果
    （replayed=True），同 request_id 不同载荷 -> 409 语义。
    companion_names 为同行人姓名名单（长度需等于 companions），用于门点
    核对整组访客；名单长度不一致 -> 400。
    """
    now = iso(utcnow())
    party_size = companions + 1
    names = _normalize_companion_names(companion_names, companions)
    if isinstance(names, dict):  # 校验错误报文
        return names
    names_json = json.dumps(names, ensure_ascii=False)
    # 事务前快速命中重放（绝大多数重复提交走这里）
    prior = conn.execute(
        "SELECT * FROM applications WHERE request_id=?", (request_id,)
    ).fetchone()
    if prior is not None:
        mismatch = _replay_mismatch(prior, name, contact, companions, names)
        if mismatch:
            return mismatch
        return _submit_reply(conn, prior, replayed=True)

    with write_tx(conn):
        prior = conn.execute(
            "SELECT * FROM applications WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior is not None:
            mismatch = _replay_mismatch(prior, name, contact, companions, names)
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
                    source,request_id,manage_token,created_at,companion_names)
               VALUES (?,?,?,?,?,?,?,?,'visitor',?,?,?,?)""",
            (app_id, batch["id"], seq, name, contact, party_size, companions,
             status, request_id, manage_token, now, names_json),
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
                "companion_names": names,
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


def _normalize_companion_names(
    companion_names: Optional[list[str]], companions: int
) -> list[str] | dict:
    """校验并规整同行人名单：长度必须等于同行人数（0 人时应为空名单）。"""
    if companion_names is None:
        return [""] * companions
    if not isinstance(companion_names, list):
        return {"http_status": 400, "error": "companion_names 必须是字符串数组"}
    names = [str(x).strip() for x in companion_names]
    if len(names) != companions:
        return {"http_status": 400,
                "error": f"同行人名单数量 {len(names)} 与同行人数 {companions} 不一致"}
    return names


def _replay_mismatch(
    prior: sqlite3.Row,
    name: str,
    contact: str,
    companions: int,
    companion_names: Optional[list[str]] = None,
) -> Optional[dict]:
    """同 request_id 但载荷关键内容不一致：明确报冲突，不做覆盖。"""
    if (prior["name"] != name or prior["contact"] != contact
            or prior["companions"] != companions):
        return {
            "http_status": 409,
            "error": "request_id 已用于内容不同的申请（幂等键冲突）",
            "application_id": prior["id"],
        }
    if companion_names is not None:
        try:
            prior_names = json.loads(prior["companion_names"] or "[]")
        except json.JSONDecodeError:
            prior_names = []
        if prior_names != companion_names:
            return {
                "http_status": 409,
                "error": "request_id 已用于同行人名单不同的申请（幂等键冲突）",
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
                "companion_names": json.loads(app["companion_names"] or "[]"),
                "change_version": app["change_version"],
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
        _finalize_pending_changes(
            conn, app, "CANCELLED", reason="application_rejected", now=now)
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
        _finalize_pending_changes(
            conn, app, "CANCELLED", reason="application_cancelled", now=now)
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
    companion_names: Optional[list[str]] = None,
) -> dict:
    """管理员补录：source='admin'，默认直接审核通过并签票。

    与访客申请一样受容量约束（同事务判定），因此补录也不可能超容量；
    容量不足时返回 409，不会留下“通过但无票”的记录。
    """
    now = iso(utcnow())
    party_size = companions + 1
    names = _normalize_companion_names(companion_names, companions)
    if isinstance(names, dict):
        return names
    names_json = json.dumps(names, ensure_ascii=False)
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
                    source,request_id,manage_token,created_at,companion_names)
               VALUES (?,?,?,?,?,?,?,?,'admin',NULL,?,?,?)""",
            (app_id, batch_id, seq, name, contact, party_size, companions,
             status, _new_token(), now, names_json),
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
                     "companions": companions, "companion_names": names,
                     "seq": seq, "status": status},
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
    pending = get_pending_change(conn, row["id"])
    if pending is not None:
        d["pending_change"] = change_dict(pending)
    d["changes"] = list_changes(conn, application_id=row["id"])
    return d


# ---------------- 申请变更（姓名 / 联系方式 / 同行人数及名单） ----------------
#
# 变更流程与不变量（全部在 BEGIN IMMEDIATE 写事务内串行完成）：
#   * 访客凭申请的 manage_token 提交变更（公开端点），request_id 唯一约束
#     保证重复提交幂等（重放返回首次结果，同键不同载荷 409）；每个申请
#     同时只允许一个 PENDING 变更。
#   * 只有“活的”申请可变更：CANCELLED/REJECTED/EXPIRED 拒绝；票已核销
#     （访客已到场）的 APPROVED 申请一律拒绝，任何路径都不能再变更。
#   * 管理员审核通过：
#       - PENDING/WAITLISTED：改写申请资料（姓名/联系方式/人数/名单），
#         不签发任何票；随后按“新总人数”重新判定候补：缩小即 FIFO 晋级
#         释放出的名额，放大但放不下则其名次按原提交序保留（由 _used_seats
#         实时读取新 party_size，所有容量计算天然按新人数重排）。
#       - APPROVED：先在同一事务内判定新总人数不超容量（并发多个变更由
#         IMMEDIATE 串行，第二个看到含第一个的新占用，放不下 409），再
#         原子“撤销旧票 + 签发新票”：旧票 REVOKED 并记新票号/替换原因，
#         新票 ACTIVE 并记旧票号；两者与申请更新、事件写入同提交，任何
#         中间崩溃都不会出现无票或两张可用票。
#   * 拒绝/撤回/过期的变更：不改申请、不动旧票、不产生可用票。

def get_change(conn: sqlite3.Connection, change_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM application_changes WHERE id=?", (change_id,)
    ).fetchone()


def get_change_by_request_id(
    conn: sqlite3.Connection, request_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM application_changes WHERE request_id=?", (request_id,)
    ).fetchone()


def get_pending_change(
    conn: sqlite3.Connection, application_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM application_changes WHERE application_id=? AND status='PENDING'",
        (application_id,),
    ).fetchone()


def _application_ticket_state(
    conn: sqlite3.Connection, app: sqlite3.Row
) -> Optional[sqlite3.Row]:
    if not app["ticket_code"]:
        return None
    return conn.execute(
        "SELECT * FROM tickets WHERE code=?", (app["ticket_code"],)
    ).fetchone()


def submit_application_change(
    conn: sqlite3.Connection,
    *,
    manage_token: str,
    request_id: str,
    name: str,
    contact: str,
    companions: int,
    companion_names: Optional[list[str]] = None,
) -> dict:
    """访客凭 manage_token 发起变更请求（待管理员审核）。"""
    now = iso(utcnow())
    names = _normalize_companion_names(companion_names, companions)
    if isinstance(names, dict):
        return names
    new_party_size = companions + 1

    prior = get_change_by_request_id(conn, request_id)
    if prior is not None:
        return _change_replay(conn, prior, name, contact, companions, names)

    with write_tx(conn):
        prior = get_change_by_request_id(conn, request_id)
        if prior is not None:
            return _change_replay(conn, prior, name, contact, companions, names)

        app = conn.execute(
            "SELECT * FROM applications WHERE manage_token=?", (manage_token,)
        ).fetchone()
        if app is None:
            return {"http_status": 404, "error": "申请不存在或链接无效"}
        if app["status"] in ("CANCELLED", "REJECTED", "EXPIRED"):
            return {"http_status": 409,
                    "error": f"申请已处于终态 {app['status']}，不可变更",
                    "status": app["status"]}
        batch = get_batch(conn, app["batch_id"])
        if now >= batch["end_at"]:
            return {"http_status": 410, "error": "批次访问时段已结束，不能申请变更"}
        # 已核销 = 访客已到场：任何变更路径都拒绝（提交时就拦下，
        # 审核时还会在写事务内复查一次以防审核与核销竞态）
        ticket = _application_ticket_state(conn, app)
        if ticket is not None and ticket["status"] == "REDEEMED":
            return {"http_status": 409,
                    "error": "通行票已核销（访客已到场），不可再变更",
                    "status": "APPROVED", "ticket_status": "REDEEMED"}
        # 没有任何实际差异 -> 幂等视为无操作，但不生成变更单（即使已有
        # 待审变更，无差异请求也不应报冲突）
        try:
            cur_names = json.loads(app["companion_names"] or "[]")
        except json.JSONDecodeError:
            cur_names = []
        if (app["name"] == name and app["contact"] == contact
                and app["companions"] == companions and cur_names == names):
            return {"http_status": 200, "noop": True,
                    "application_id": app["id"]}

        existing = get_pending_change(conn, app["id"])
        if existing is not None:
            return {"http_status": 409,
                    "error": "该申请已有一个待审核的变更，请等待审核结果或先撤回",
                    "change_id": existing["id"]}

        change_seq = int(conn.execute(
            "SELECT COALESCE(MAX(change_seq),0)+1 s FROM application_changes "
            "WHERE application_id=?", (app["id"],)
        ).fetchone()["s"])
        change_id = _gen_change_id(conn)
        conn.execute(
            """INSERT INTO application_changes
                   (id,application_id,batch_id,change_seq,request_id,status,
                    old_name,new_name,old_contact,new_contact,old_party_size,
                    new_party_size,new_companions,new_companion_names,created_at)
               VALUES (?,?,?,?,?,'PENDING',?,?,?,?,?,?,?,?,?)""",
            (change_id, app["id"], app["batch_id"], change_seq, request_id,
             app["name"], name, app["contact"], contact, app["party_size"],
             new_party_size, companions, json.dumps(names, ensure_ascii=False), now),
        )
        add_event(
            conn,
            "APPLICATION_CHANGE_SUBMITTED",
            ts=now,
            batch_id=app["batch_id"],
            application_id=app["id"],
            person_id=app["id"],
            reason="visitor_change_request",
            payload={
                "change_id": change_id,
                "change_seq": change_seq,
                "old": {"name": app["name"], "contact": app["contact"],
                        "party_size": app["party_size"],
                        "companion_names": cur_names},
                "new": {"name": name, "contact": contact,
                        "party_size": new_party_size, "companion_names": names},
            },
        )
        row = get_change(conn, change_id)
        result = change_dict(row)
        result["http_status"] = 201
        result["application_id"] = app["id"]
    return result


def _change_replay(
    conn: sqlite3.Connection,
    prior: sqlite3.Row,
    name: str,
    contact: str,
    companions: int,
    companion_names: list[str],
) -> dict:
    """变更请求的幂等重放 / 同键不同载荷冲突。"""
    if (prior["new_name"] != name or prior["new_contact"] != contact
            or prior["new_companions"] != companions
            or json.loads(prior["new_companion_names"] or "[]") != companion_names):
        return {"http_status": 409,
                "error": "request_id 已用于内容不同的变更（幂等键冲突）",
                "change_id": prior["id"]}
    d = change_dict(prior)
    d["http_status"] = 200
    d["replayed"] = True
    return d


def approve_application_change(
    conn: sqlite3.Connection, *, change_id: str, reason: Optional[str] = None
) -> dict:
    """管理员审核通过一条变更请求。

    通过在单个 IMMEDIATE 事务内完成，保证并发变更不突破容量、
    旧票撤销与新票签发原子可见。
    """
    now = iso(utcnow())
    with write_tx(conn):
        ch = get_change(conn, change_id)
        if ch is None:
            return {"http_status": 404, "error": "变更请求不存在"}
        if ch["status"] == "APPROVED":
            return _change_already_decided(conn, ch, duplicated=True)
        if ch["status"] != "PENDING":
            return {"http_status": 409,
                    "error": f"变更请求已处于终态 {ch['status']}，不可再审核",
                    "status": ch["status"]}
        app = get_application(conn, ch["application_id"])
        if app is None:
            return {"http_status": 404, "error": "对应申请不存在"}
        if app["status"] in ("CANCELLED", "REJECTED", "EXPIRED"):
            return {"http_status": 409,
                    "error": f"申请已处于终态 {app['status']}，变更不能通过",
                    "status": app["status"]}
        batch = get_batch(conn, app["batch_id"])

        old_ticket_code = None
        new_ticket_code = None
        new_ticket_version = None
        promoted: list[str] = []

        if app["status"] == "APPROVED":
            if batch["closed_at"] is not None or now >= batch["end_at"]:
                return {"http_status": 410,
                        "error": "批次已关闭或已结束，不能通过已通过申请的变更（换票）"}
            # 容量：以新总人数替换旧占用重新计算；并发变更在此串行排队
            used = _used_seats(conn, batch["id"])
            if used - app["party_size"] + ch["new_party_size"] > batch["capacity"]:
                return {"http_status": 409,
                        "error": "变更后总人数将超过批次容量，不能通过",
                        "used": used, "capacity": batch["capacity"],
                        "old_party_size": app["party_size"],
                        "new_party_size": ch["new_party_size"]}
            old_ticket = _application_ticket_state(conn, app)
            # 审核与门点核销竞态：旧票已被核销（访客到场）则禁止变更
            if old_ticket is not None and old_ticket["status"] == "REDEEMED":
                return {"http_status": 409,
                        "error": "旧票已核销（访客已到场），变更不能通过",
                        "ticket_status": "REDEEMED"}
            if old_ticket is not None and old_ticket["status"] != "ACTIVE":
                return {"http_status": 409,
                        "error": f"旧票当前状态为 {old_ticket['status']}，不能换发",
                        "ticket_status": old_ticket["status"]}

            replacement_reason = (
                f"申请变更通过: {reason or 'visitor change'} "
                f"({app['party_size']}人→{ch['new_party_size']}人)"
            )
            # 先撤销旧票（终态不可逆），再签发新票；与申请更新同一事务提交
            if old_ticket is not None:
                # 预生成新票号以便旧票记录“被哪张票替换”
                new_ticket_code = _gen_unique_code(conn)
                _, state = _revoke_ticket_locked(
                    conn,
                    code=old_ticket["code"],
                    reason=replacement_reason,
                    now=now,
                    replaced_by_code=new_ticket_code,
                )
                if state != "ok":  # 并发核销竞态兜底
                    return {"http_status": 409,
                            "error": f"旧票当前状态为 {state}，不能换发",
                            "ticket_status": state}
                old_ticket_code = old_ticket["code"]
                note = (f"访客批次 {batch['id']} · {ch['new_name']}"
                        f"（共{ch['new_party_size']}人）"
                        f"· 变更 v{app['change_version'] + 1} 换发")
                new_ticket_version = _issue_ticket_with_code_locked(
                    conn,
                    code=new_ticket_code,
                    person_id=app["id"],
                    valid_from=old_ticket["valid_from"],
                    valid_until=old_ticket["valid_until"],
                    note=note,
                    zones=[batch["zone_id"]],
                    batch_id=batch["id"],
                    application_id=app["id"],
                    party_size=ch["new_party_size"],
                    replaced_code=old_ticket["code"],
                    replacement_reason=replacement_reason,
                    now=now,
                    route_id=old_ticket["route_id"],
                    route_version=old_ticket["route_version"],
                )
        else:
            # PENDING / WAITLISTED：不发票；资料按新总人数改写后，重新跑一遍
            # FIFO 晋级——容量计算实时读取新 party_size（新人数重排候补），
            # 缩小释放名额时后面的候补可按序晋级，候补自身变更后若放得下也晋级。
            if app["status"] == "PENDING":
                # 待审核申请占着名额：放大不能把批次撑爆（并发变更在此串行）
                used = _used_seats(conn, batch["id"])
                if used - app["party_size"] + ch["new_party_size"] > batch["capacity"]:
                    return {"http_status": 409,
                            "error": "变更后总人数将超过批次容量，不能通过",
                            "used": used, "capacity": batch["capacity"],
                            "old_party_size": app["party_size"],
                            "new_party_size": ch["new_party_size"]}

        # 先改写申请资料（APPROVED 的换票票号也在此原子切换），
        # 候补重排/晋级随后读取到的就是新人数与新名单。
        names = json.loads(ch["new_companion_names"] or "[]")
        if new_ticket_code is not None:
            conn.execute(
                """UPDATE applications
                      SET name=?, contact=?, party_size=?, companions=?,
                          companion_names=?, change_version=change_version+1,
                          ticket_code=?
                    WHERE id=?""",
                (ch["new_name"], ch["new_contact"], ch["new_party_size"],
                 ch["new_companions"], ch["new_companion_names"],
                 new_ticket_code, app["id"]),
            )
        else:
            conn.execute(
                """UPDATE applications
                      SET name=?, contact=?, party_size=?, companions=?,
                          companion_names=?, change_version=change_version+1
                    WHERE id=?""",
                (ch["new_name"], ch["new_contact"], ch["new_party_size"],
                 ch["new_companions"], ch["new_companion_names"], app["id"]),
            )

        if app["status"] != "APPROVED" and now < batch["end_at"]:
            promoted = _promote_waitlist(conn, batch_id=batch["id"], now=now)

        conn.execute(
            """UPDATE application_changes
                  SET status='APPROVED', decided_at=?, decide_reason=?,
                      old_ticket_code=?, new_ticket_code=?
                WHERE id=?""",
            (now, reason or "admin_approve_change", old_ticket_code,
             new_ticket_code, ch["id"]),
        )
        add_event(
            conn,
            "APPLICATION_CHANGE_APPROVED",
            ts=now,
            batch_id=app["batch_id"],
            application_id=app["id"],
            person_id=app["id"],
            ticket_code=new_ticket_code,
            reason=reason or "admin_approve_change",
            payload={
                "change_id": ch["id"],
                "change_seq": ch["change_seq"],
                "change_version": app["change_version"] + 1,
                "old": {"name": ch["old_name"], "contact": ch["old_contact"],
                        "party_size": ch["old_party_size"]},
                "new": {"name": ch["new_name"], "contact": ch["new_contact"],
                        "party_size": ch["new_party_size"],
                        "companion_names": names},
                "old_ticket_code": old_ticket_code,
                "new_ticket_code": new_ticket_code,
                "new_ticket_version": new_ticket_version,
                "promoted": promoted,
            },
        )
        row = get_change(conn, change_id)
        app_row = get_application(conn, app["id"])
        new_ticket_row = (
            conn.execute("SELECT * FROM tickets WHERE code=?",
                         (new_ticket_code,)).fetchone()
            if new_ticket_code else None
        )
    out = {
        "http_status": 200,
        "change": change_dict(row),
        "application": app_dict(app_row),
        "old_ticket_code": old_ticket_code,
        "new_ticket": ticket_dict(new_ticket_row) if new_ticket_row else None,
        "promoted": promoted,
    }
    return out


def _issue_ticket_with_code_locked(
    conn: sqlite3.Connection, *, code: str, now: str, **kwargs
) -> int:
    """同 _issue_ticket_locked 但使用指定票号（原子换票时旧票先记了新票号）。"""
    zone_list = list(kwargs.get("zones") or [])
    # 变更换发：继承旧票的路线绑定（走到换票的票必然未核销=路线未开始，
    # 因此连同固化版本一起继承；已开始路线的票根本不允许变更）。
    inherited_route_id = kwargs.get("route_id")
    inherited_route_version = kwargs.get("route_version")
    conn.execute(
        """INSERT INTO tickets
               (code,person_id,valid_from,valid_until,issued_at,status,note,zones,
                batch_id,application_id,party_size,replaced_code,replacement_reason,
                route_id,route_version)
           VALUES (?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,?,?,?)""",
        (code, kwargs["person_id"], kwargs["valid_from"], kwargs["valid_until"], now,
         kwargs.get("note"), json.dumps(zone_list, ensure_ascii=False),
         kwargs.get("batch_id"), kwargs.get("application_id"),
         kwargs.get("party_size"), kwargs.get("replaced_code"),
         kwargs.get("replacement_reason"),
         inherited_route_id, inherited_route_version),
    )
    payload = {
        "valid_from": kwargs["valid_from"],
        "valid_until": kwargs["valid_until"],
        "note": kwargs.get("note"),
        "zones": zone_list,
        "replaced_code": kwargs.get("replaced_code"),
        "replacement_reason": kwargs.get("replacement_reason"),
        "party_size": kwargs.get("party_size"),
        "batch_id": kwargs.get("batch_id"),
        "application_id": kwargs.get("application_id"),
    }
    if inherited_route_id is not None:
        payload["route_id"] = inherited_route_id
        payload["route_version"] = inherited_route_version
    return add_event(
        conn,
        "TICKET_ISSUED",
        ts=now,
        ticket_code=code,
        person_id=kwargs["person_id"],
        payload=payload,
        batch_id=kwargs.get("batch_id"),
        application_id=kwargs.get("application_id"),
    )


def _change_already_decided(
    conn: sqlite3.Connection, ch: sqlite3.Row, *, duplicated: bool
) -> dict:
    d = {"http_status": 200, "duplicated": duplicated, "change": change_dict(ch)}
    if ch["new_ticket_code"]:
        t = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (ch["new_ticket_code"],)
        ).fetchone()
        d["new_ticket"] = ticket_dict(t) if t else None
    return d


def reject_application_change(
    conn: sqlite3.Connection, *, change_id: str, reason: str
) -> dict:
    """管理员拒绝变更：申请资料与旧票均不变，不产生任何票。"""
    now = iso(utcnow())
    with write_tx(conn):
        ch = get_change(conn, change_id)
        if ch is None:
            return {"http_status": 404, "error": "变更请求不存在"}
        if ch["status"] != "PENDING":
            return {"http_status": 409,
                    "error": f"变更请求已处于终态 {ch['status']}，不可再操作",
                    "status": ch["status"]}
        conn.execute(
            """UPDATE application_changes
                  SET status='REJECTED', decided_at=?, decide_reason=?
                WHERE id=?""",
            (now, reason or "admin_reject_change", ch["id"]),
        )
        add_event(
            conn,
            "APPLICATION_CHANGE_REJECTED",
            ts=now,
            batch_id=ch["batch_id"],
            application_id=ch["application_id"],
            person_id=ch["application_id"],
            reason=reason or "admin_reject_change",
            payload={"change_id": ch["id"], "change_seq": ch["change_seq"]},
        )
        row = get_change(conn, change_id)
    return {"http_status": 200, "change": change_dict(row)}


def cancel_application_change(
    conn: sqlite3.Connection, *, manage_token: str, change_id: str
) -> dict:
    """访客自行撤回自己的待审核变更（公开端点，凭 manage_token）。"""
    now = iso(utcnow())
    with write_tx(conn):
        app = conn.execute(
            "SELECT * FROM applications WHERE manage_token=?", (manage_token,)
        ).fetchone()
        if app is None:
            return {"http_status": 404, "error": "申请不存在或链接无效"}
        ch = get_change(conn, change_id)
        if ch is None or ch["application_id"] != app["id"]:
            return {"http_status": 404, "error": "变更请求不存在"}
        if ch["status"] != "PENDING":
            return {"http_status": 409,
                    "error": f"变更请求已处于终态 {ch['status']}，不可撤回",
                    "status": ch["status"]}
        conn.execute(
            """UPDATE application_changes
                  SET status='CANCELLED', decided_at=?, decide_reason='visitor_cancel'
                WHERE id=?""",
            (now, ch["id"]),
        )
        add_event(
            conn,
            "APPLICATION_CHANGE_CANCELLED",
            ts=now,
            batch_id=ch["batch_id"],
            application_id=ch["application_id"],
            person_id=ch["application_id"],
            reason="visitor_cancel",
            payload={"change_id": ch["id"], "change_seq": ch["change_seq"]},
        )
        row = get_change(conn, change_id)
    return {"http_status": 200, "change": change_dict(row)}


def list_changes(
    conn: sqlite3.Connection,
    *,
    batch_id: Optional[str] = None,
    application_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    sql = "SELECT * FROM application_changes WHERE 1=1"
    params: list = []
    if batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
    if application_id:
        sql += " AND application_id=?"
        params.append(application_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC"
    return [change_dict(r) for r in conn.execute(sql, params).fetchall()]


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
    # 在场清册：本批次当前在场 / 已离场 / 误扫移除分组计数与逐组状态
    presence_rows = [
        presence_svc.presence_dict(r) for r in conn.execute(
            "SELECT * FROM presence WHERE batch_id=? ORDER BY arrived_at",
            (batch_id,),
        ).fetchall()
    ]
    presence_summary = {
        "onsite": [p for p in presence_rows if p["status"] == "ARRIVED"],
        "departed": [p for p in presence_rows if p["status"] == "DEPARTED"],
        "removed": [p for p in presence_rows if p["status"] == "REMOVED"],
        "onsite_people": sum(
            p["party_size"] for p in presence_rows if p["status"] == "ARRIVED"),
    }
    # 检查路线：本批次当前所在检查点 / 超时停留 / 已完成 / 违规
    route_progress = routes_svc.list_progress(conn, batch_id=batch_id)
    route_summary = {
        "route_id": batch["route_id"],
        "route_version": batch["route_version"],
        "in_progress": [p for p in route_progress if p["status"] == "IN_PROGRESS"],
        "overdue": [p for p in route_progress if p.get("overdue")],
        "completed": [p for p in route_progress if p["status"] == "COMPLETED"],
        "violated": [p for p in route_progress if p["status"] == "VIOLATED"],
    }
    return {
        "batch": batch_dict(
            batch, used=_used_seats(conn, batch_id),
            counts=_status_counts(conn, batch_id), now=now,
        ),
        "applications": applications,
        "waitlist": [a for a in applications if a["status"] == "WAITLISTED"],
        "tickets": tickets,
        "changes": list_changes(conn, batch_id=batch_id),
        "pending_changes": list_changes(conn, batch_id=batch_id, status="PENDING"),
        "capacity_log": capacity_log(conn, batch_id),
        "events": events,
        "presence": presence_summary,
        "routes": route_summary,
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
            # 该申请若有待审核变更，随批次结束过期（不产生票、不改申请资料）
            _finalize_pending_changes(
                conn, a, "EXPIRED", reason="batch_ended", now=now)
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
        "presence": _person_presence(conn, person_id, codes),
        "routes": _person_routes(conn, person_id),
    }


def _person_routes(conn: sqlite3.Connection, person_id: str) -> list[dict]:
    """人员视图：该人名下每条已开始路线的当前状态/超时/终态。"""
    out = []
    now = iso(utcnow())
    for p in conn.execute(
        "SELECT * FROM route_progress WHERE person_id=? ORDER BY started_at DESC",
        (person_id,),
    ).fetchall():
        block = routes_svc.route_status_block(conn, p, now)
        block.update({"id": p["id"], "ticket_code": p["ticket_code"],
                      "batch_id": p["batch_id"]})
        out.append(block)
    return out


def _person_presence(
    conn: sqlite3.Connection, person_id: str, codes: list[str]
) -> list[dict]:
    """人员视图：每张票的当前在场状态与轨迹（含人工更正与操作者）。"""
    out = []
    rows = conn.execute(
        "SELECT * FROM presence WHERE person_id=? ORDER BY arrived_at DESC",
        (person_id,),
    ).fetchall()
    for r in rows:
        d = presence_svc.presence_dict(r)
        d["trail"] = [
            presence_svc.presence_event_dict(t) for t in conn.execute(
                "SELECT * FROM presence_events WHERE ticket_code=? ORDER BY seq",
                (r["ticket_code"],),
            ).fetchall()
        ]
        out.append(d)
    return out


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
    changes_counts = {
        s: conn.execute(
            "SELECT COUNT(*) c FROM application_changes WHERE status=?", (s,)
        ).fetchone()["c"]
        for s in ("PENDING", "APPROVED", "REJECTED", "CANCELLED", "EXPIRED")
    }
    onsite = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(party_size),0) p FROM presence "
        "WHERE status='ARRIVED'"
    ).fetchone()
    from . import delegations
    delegations_counts = {
        s: conn.execute(
            "SELECT COUNT(*) c FROM delegations WHERE status=?", (s,)
        ).fetchone()["c"]
        for s in ("PENDING", "APPROVED", "REJECTED", "REVOKED", "EXPIRED")
    }
    proxy_counts = {
        s: conn.execute(
            "SELECT COUNT(*) c FROM proxy_credentials WHERE status=?", (s,)
        ).fetchone()["c"]
        for s in ("ACTIVE", "REVOKED", "EXPIRED", "EXHAUSTED")
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
        "changes": changes_counts,
        "onsite_groups": onsite["c"],
        "onsite_people": int(onsite["p"]),
        "rollcalls": conn.execute(
            "SELECT COUNT(*) c FROM rollcalls"
        ).fetchone()["c"],
        "routes": conn.execute(
            "SELECT COUNT(*) c FROM routes"
        ).fetchone()["c"],
        "routes_paused": conn.execute(
            "SELECT COUNT(*) c FROM routes WHERE status='PAUSED'"
        ).fetchone()["c"],
        "route_in_progress": conn.execute(
            "SELECT COUNT(*) c FROM route_progress WHERE status='IN_PROGRESS'"
        ).fetchone()["c"],
        "route_completed": conn.execute(
            "SELECT COUNT(*) c FROM route_progress WHERE status='COMPLETED'"
        ).fetchone()["c"],
        "route_violated": conn.execute(
            "SELECT COUNT(*) c FROM route_progress WHERE status='VIOLATED'"
        ).fetchone()["c"],
        "route_open_conflicts": conn.execute(
            "SELECT COUNT(*) c FROM route_event_conflicts WHERE status='OPEN'"
        ).fetchone()["c"],
        "delegations": delegations_counts,
        "proxy_credentials": proxy_counts,
        "proxy_open_conflicts": conn.execute(
            "SELECT COUNT(*) c FROM proxy_conflicts WHERE status='OPEN'"
        ).fetchone()["c"],
    }
