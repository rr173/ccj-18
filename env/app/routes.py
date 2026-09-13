"""访客路线检查与区域停留监控。

核心模型
--------
1. ``routes`` 是路线族（按分区编排），``route_versions`` 是不可变版本，
   ``route_checkpoints`` 是版本内带顺序的检查点（一个检查点绑定一个门点，
   带该点最长停留秒数 ``max_stay_seconds`` 与运行时开/闭状态）。
2. 票或预约批次绑定路线时把当时版本号固化（``tickets.route_version`` /
   ``route_bindings``）。首次在入口检查点核销即创建 ``route_progress`` 并再次
   固化版本——**已经开始的路线永远走原版本**，管理员之后发新版本、暂停都不
   影响在途访客；新版本只影响之后开始的票。
3. 执行状态机 ``IN_PROGRESS -> COMPLETED / VIOLATED``（终态不可逆）。跳过检查
   点、进入已关闭检查点、在上一检查点停留超时都判 VIOLATED；重复进入当前检查
   点是**非终态拒绝**（明确拒绝、不推进）。完成/违规后任何迟到的旧事件都不可
   能改回进行中。
4. 每次门点判定都向 ``route_checks`` 追加一行（票、人员、路线版本、检查点顺序、
   门点、attempt 幂等键、全局事件版本）。``(gate_id, attempt_id)`` 唯一约束保证
   网络抖动重发绝不重复推进；所有判定在调用方 ``BEGIN IMMEDIATE`` 事务内，多个
   门点并发上报只能排成一个明确先后。
5. 门点离线期间攒下的检查点事件重连时批量补齐（事件带门点本地时间）。无法自动
   判定的（中间缺口、迟到、终态后到达、时间戳异常、票/路线状态异常）写
   ``route_event_conflicts`` + ``ROUTE_CONFLICT`` 事件，保留冲突现场等管理员
   处理，绝不静默丢弃，也绝不擅自推进。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Optional

from .db import (
    CONFLICT_GAP,
    CONFLICT_INVALID_TS,
    CONFLICT_LATE,
    CONFLICT_NO_ROUTE,
    CONFLICT_TERMINAL,
    CONFLICT_TICKET,
    REJECT_DUPLICATE,
    VIOLATION_CLOSED,
    VIOLATION_DWELL,
    VIOLATION_SKIPPED,
    add_event,
    iso,
    parse_dt,
    utcnow,
    write_tx,
)

ROUTE_PREFIX = "RT-"
PROGRESS_PREFIX = "RP-"

# 构成“路线目录版本”的事件类型：门点同步游标落后于这些事件时要求先补齐
DEFINITION_EVENT_TYPES = (
    "ROUTE_CREATED",
    "ROUTE_VERSION_PUBLISHED",
    "ROUTE_PAUSED",
    "ROUTE_RESUMED",
    "ROUTE_CHECKPOINT_CLOSED",
    "ROUTE_CHECKPOINT_OPENED",
)

# 离线事件时间戳允许的未来时钟偏差（秒）
FUTURE_TS_SKEW_SECONDS = 60

REASON_TEXT = {
    "ok": "检查点通过",
    "route_completed": "路线已全部完成",
    "duplicate_entry": "重复进入当前检查点，路线不推进",
    "skipped_checkpoint": "跳过了未到的检查点，路线判违规",
    "checkpoint_closed": "检查点已关闭，禁止进入，路线判违规",
    "dwell_timeout": "在上一检查点停留超过最长允许时间，路线判违规",
    "wrong_entry_gate": "首次核销必须在路线入口检查点",
    "gate_not_on_route": "该门点不是本路线版本上的检查点",
    "route_paused": "路线已暂停，暂不接受新的访客开始",
    "route_not_started": "路线尚未在入口检查点核销开始",
    "no_route": "该票未绑定检查路线",
    "route_already_completed": "路线已完成，不能再上报检查点",
    "route_already_violated": "路线已违规终止，不能再上报检查点",
    "stale_route": "门点路线目录版本过旧，请先同步最新路线编排",
    "invalid_event_ts": "离线事件时间戳异常",
}


def _gen_route_id() -> str:
    return ROUTE_PREFIX + secrets.token_hex(5).upper()


def _gen_progress_id() -> str:
    return PROGRESS_PREFIX + secrets.token_hex(6).upper()


# ---------------- 序列化辅助 ----------------

def _checkpoint_dict(row: sqlite3.Row) -> dict:
    return {
        "seq": row["seq"],
        "gate_id": row["gate_id"],
        "name": row["name"],
        "max_stay_seconds": row["max_stay_seconds"],
        "closed": bool(row["closed"]),
        "closed_at": row["closed_at"],
        "closed_reason": row["closed_reason"],
    }


def _route_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["paused"] = d["status"] == "PAUSED"
    return d


def _progress_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    return d


def _conflict_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["detail"] = json.loads(d.get("detail") or "{}")
    except json.JSONDecodeError:
        pass
    return d


def _check_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["ok"] = bool(d.pop("ok_flag")) if "ok_flag" in d else None
    d["replayed"] = bool(d["replayed"])
    d["offline"] = bool(d["offline"])
    try:
        d["result_detail"] = json.loads(d.get("result_json") or "{}")
    except json.JSONDecodeError:
        pass
    return d


# ---------------- 读取辅助 ----------------

def get_route(conn: sqlite3.Connection, route_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()


def _version_checkpoints(
    conn: sqlite3.Connection, route_id: str, version: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM route_checkpoints WHERE route_id=? AND version=? ORDER BY seq",
        (route_id, version),
    ).fetchall()


def _checkpoint_by_gate(
    conn: sqlite3.Connection, route_id: str, version: int, gate_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM route_checkpoints WHERE route_id=? AND version=? AND gate_id=?",
        (route_id, version, gate_id),
    ).fetchone()


def get_progress_by_ticket(
    conn: sqlite3.Connection, code: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM route_progress WHERE ticket_code=?", (code,)
    ).fetchone()


def route_catalog_version(conn: sqlite3.Connection, route_id: str) -> int:
    """门点可见的该路线目录版本：定义类事件的最大全局版本（无则 0）。"""
    placeholders = ",".join("?" for _ in DEFINITION_EVENT_TYPES)
    return int(conn.execute(
        f"SELECT COALESCE(MAX(id),0) v FROM events WHERE route_id=? "
        f"AND type IN ({placeholders})",
        (route_id, *DEFINITION_EVENT_TYPES),
    ).fetchone()["v"])


def _dwell_deadline(entered_at: str, max_stay_seconds: Optional[int]) -> Optional[str]:
    if not max_stay_seconds:
        return None
    return iso(parse_dt(entered_at) + timedelta(seconds=int(max_stay_seconds)))


def route_status_block(
    conn: sqlite3.Connection, p: sqlite3.Row, now: str
) -> dict:
    """给门点/管理端的路线当前状态块（含停留计时与下一个检查点）。"""
    cps = _version_checkpoints(conn, p["route_id"], p["route_version"])
    current = next((c for c in cps if c["seq"] == p["current_seq"]), None)
    nxt = next((c for c in cps if c["seq"] == p["next_seq"]), None)
    max_stay = current["max_stay_seconds"] if current is not None else None
    deadline = _dwell_deadline(p["entered_at"], max_stay) if p["entered_at"] else None
    dwell_seconds = None
    overdue = False
    if p["entered_at"] and p["status"] == "IN_PROGRESS":
        dwell_seconds = int(
            (parse_dt(now) - parse_dt(p["entered_at"])).total_seconds()
        )
        if deadline is not None:
            overdue = now > deadline
    return {
        "route_id": p["route_id"],
        "route_version": p["route_version"],
        "status": p["status"],
        "current_seq": p["current_seq"],
        "next_seq": p["next_seq"],
        "total_checkpoints": len(cps),
        "current_checkpoint": _checkpoint_dict(current) if current else None,
        "next_checkpoint": _checkpoint_dict(nxt) if nxt else None,
        "entered_at": p["entered_at"],
        "max_stay_seconds": max_stay,
        "dwell_deadline": deadline,
        "dwell_seconds": dwell_seconds,
        "overdue": overdue,
        "violation_kind": p["violation_kind"],
        "violation_reason": p["violation_reason"],
        "completed_at": p["completed_at"],
    }


# ---------------- 路线编排（管理端） ----------------

def create_route(
    conn: sqlite3.Connection,
    *,
    zone_id: str,
    name: str,
    operator: str,
    route_id: Optional[str] = None,
) -> dict:
    from .services import get_zone

    now = iso(utcnow())
    with write_tx(conn):
        if get_zone(conn, zone_id) is None:
            return {"http_status": 400, "error": f"未知分区: {zone_id}"}
        rid = route_id or _gen_route_id()
        if get_route(conn, rid) is not None:
            return {"http_status": 409, "error": f"路线已存在: {rid}"}
        version = add_event(
            conn,
            "ROUTE_CREATED",
            ts=now,
            route_id=rid,
            reason="admin_create",
            payload={"zone_id": zone_id, "name": name, "operator": operator},
        )
        conn.execute(
            """INSERT INTO routes
                   (id,zone_id,name,status,current_version,created_at,created_by)
               VALUES (?,?,?,'ACTIVE',0,?,?)""",
            (rid, zone_id, name, now, operator),
        )
        row = get_route(conn, rid)
    out = _route_dict(row)
    out["http_status"] = 201
    out["event_version"] = version
    return out


def publish_route_version(
    conn: sqlite3.Connection,
    *,
    route_id: str,
    checkpoints: list[dict],
    note: Optional[str],
    operator: str,
) -> dict:
    """发布一个不可变新版本（替换“未来使用”的路线）。

    校验：路线存在且未暂停；至少一个检查点；门点均存在且属于路线分区；
    同一版本内门点不重复；最长停留为 NULL 或正整数秒。
    """
    now = iso(utcnow())
    if not checkpoints:
        return {"http_status": 400, "error": "路线至少需要一个检查点"}
    norm: list[dict] = []
    seen_gates: set[str] = set()
    for i, cp in enumerate(checkpoints, start=1):
        gate_id = str(cp.get("gate_id") or "").strip()
        if not gate_id:
            return {"http_status": 400, "error": f"第 {i} 个检查点缺少 gate_id"}
        if gate_id in seen_gates:
            return {"http_status": 400,
                    "error": f"同一门点 {gate_id} 在路线中重复出现"}
        seen_gates.add(gate_id)
        name = str(cp.get("name") or f"检查点 {i}").strip()
        max_stay = cp.get("max_stay_seconds")
        if max_stay is not None:
            try:
                max_stay = int(max_stay)
            except (TypeError, ValueError):
                return {"http_status": 400,
                        "error": f"检查点 {i} 的 max_stay_seconds 必须是整数秒或 null"}
            if max_stay <= 0:
                return {"http_status": 400,
                        "error": f"检查点 {i} 的最长停留时间必须为正数秒"}
        norm.append({"seq": i, "gate_id": gate_id, "name": name,
                     "max_stay": max_stay})

    with write_tx(conn):
        route = get_route(conn, route_id)
        if route is None:
            return {"http_status": 404, "error": "路线不存在"}
        if route["status"] == "PAUSED":
            return {"http_status": 409,
                    "error": "路线处于暂停状态，不能发布新版本（请先恢复）"}
        for cp in norm:
            gate = conn.execute(
                "SELECT * FROM gates WHERE id=?", (cp["gate_id"],)
            ).fetchone()
            if gate is None:
                return {"http_status": 400,
                        "error": f"未知门点: {cp['gate_id']}"}
            if gate["revoked"]:
                return {"http_status": 400,
                        "error": f"门点已停用: {cp['gate_id']}"}
            if gate["zone_id"] != route["zone_id"]:
                return {
                    "http_status": 400,
                    "error": f"门点 {cp['gate_id']} 属于分区 "
                             f"{gate['zone_id'] or '（未配置）'}，与路线分区 "
                             f"{route['zone_id']} 不一致",
                }

        version = int(route["current_version"]) + 1
        event_version = add_event(
            conn,
            "ROUTE_VERSION_PUBLISHED",
            ts=now,
            route_id=route_id,
            route_version=version,
            reason=note or "admin_publish_version",
            payload={
                "version": version,
                "note": note,
                "operator": operator,
                "checkpoints": norm,
            },
        )
        conn.execute(
            """INSERT INTO route_versions
                   (route_id,version,created_at,created_by,note,event_version)
               VALUES (?,?,?,?,?,?)""",
            (route_id, version, now, operator, note, event_version),
        )
        for cp in norm:
            conn.execute(
                """INSERT INTO route_checkpoints
                       (route_id,version,seq,gate_id,name,max_stay_seconds,closed)
                   VALUES (?,?,?,?,?,?,0)""",
                (route_id, version, cp["seq"], cp["gate_id"],
                 cp["name"], cp["max_stay"]),
            )
        conn.execute(
            "UPDATE routes SET current_version=? WHERE id=?",
            (version, route_id),
        )
        row = get_route(conn, route_id)
        cps = [dict(seq=c["seq"], gate_id=c["gate_id"], name=c["name"],
                    max_stay_seconds=c["max_stay_seconds"])
               for c in _version_checkpoints(conn, route_id, version)]
    out = _route_dict(row)
    out["http_status"] = 201
    out["published_version"] = version
    out["event_version"] = event_version
    out["checkpoints"] = cps
    return out


def pause_route(
    conn: sqlite3.Connection, *, route_id: str, reason: str, operator: str
) -> dict:
    """暂停路线：停止未来使用（不能再绑定/开始）；在途执行不受影响。"""
    now = iso(utcnow())
    with write_tx(conn):
        route = get_route(conn, route_id)
        if route is None:
            return {"http_status": 404, "error": "路线不存在"}
        if route["status"] == "PAUSED":
            return {"http_status": 200, "duplicated": True,
                    "route": _route_dict(route)}
        version = add_event(
            conn, "ROUTE_PAUSED", ts=now, route_id=route_id,
            route_version=route["current_version"],
            reason=reason or "admin_pause",
            payload={"operator": operator, "reason": reason},
        )
        conn.execute(
            "UPDATE routes SET status='PAUSED', paused_at=?, paused_reason=? WHERE id=?",
            (now, reason, route_id),
        )
        row = get_route(conn, route_id)
    out = _route_dict(row)
    out["http_status"] = 200
    out["event_version"] = version
    return out


def resume_route(
    conn: sqlite3.Connection, *, route_id: str, reason: str, operator: str
) -> dict:
    now = iso(utcnow())
    with write_tx(conn):
        route = get_route(conn, route_id)
        if route is None:
            return {"http_status": 404, "error": "路线不存在"}
        if route["status"] == "ACTIVE":
            return {"http_status": 200, "duplicated": True,
                    "route": _route_dict(route)}
        version = add_event(
            conn, "ROUTE_RESUMED", ts=now, route_id=route_id,
            route_version=route["current_version"],
            reason=reason or "admin_resume",
            payload={"operator": operator, "reason": reason},
        )
        conn.execute(
            "UPDATE routes SET status='ACTIVE', paused_at=NULL, paused_reason=NULL "
            "WHERE id=?",
            (route_id,),
        )
        row = get_route(conn, route_id)
    out = _route_dict(row)
    out["http_status"] = 200
    out["event_version"] = version
    return out


def set_checkpoint_closed(
    conn: sqlite3.Connection,
    *,
    route_id: str,
    version: Optional[int],
    seq: int,
    closed: bool,
    reason: Optional[str],
    operator: str,
) -> dict:
    """开闭某版本上的检查点（运行时状态；版本内容不变，在途访客同样受约束）。"""
    now = iso(utcnow())
    with write_tx(conn):
        route = get_route(conn, route_id)
        if route is None:
            return {"http_status": 404, "error": "路线不存在"}
        ver = version or route["current_version"]
        if ver < 1 or ver > route["current_version"]:
            return {"http_status": 400,
                    "error": f"路线版本 {ver} 不存在（当前 {route['current_version']}）"}
        cp = conn.execute(
            "SELECT * FROM route_checkpoints WHERE route_id=? AND version=? AND seq=?",
            (route_id, ver, seq),
        ).fetchone()
        if cp is None:
            return {"http_status": 404,
                    "error": f"版本 {ver} 上不存在顺序为 {seq} 的检查点"}
        if bool(cp["closed"]) == closed:
            return {"http_status": 200, "duplicated": True,
                    "checkpoint": _checkpoint_dict(cp)}
        event_type = "ROUTE_CHECKPOINT_CLOSED" if closed else "ROUTE_CHECKPOINT_OPENED"
        event_version = add_event(
            conn, event_type, ts=now, route_id=route_id, route_version=ver,
            checkpoint_seq=seq, reason=reason or ("admin_close" if closed else "admin_open"),
            payload={"seq": seq, "gate_id": cp["gate_id"], "name": cp["name"],
                     "closed": closed, "operator": operator, "reason": reason},
        )
        if closed:
            conn.execute(
                """UPDATE route_checkpoints
                      SET closed=1, closed_at=?, closed_reason=?
                    WHERE route_id=? AND version=? AND seq=?""",
                (now, reason, route_id, ver, seq),
            )
        else:
            conn.execute(
                """UPDATE route_checkpoints
                      SET closed=0, closed_at=NULL, closed_reason=NULL
                    WHERE route_id=? AND version=? AND seq=?""",
                (route_id, ver, seq),
            )
        row = conn.execute(
            "SELECT * FROM route_checkpoints WHERE route_id=? AND version=? AND seq=?",
            (route_id, ver, seq),
        ).fetchone()
    out = {"http_status": 200, "checkpoint": _checkpoint_dict(row),
           "event_version": event_version}
    return out


def list_routes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM routes ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        d = _route_dict(r)
        cps = _version_checkpoints(conn, r["id"], r["current_version"])
        d["checkpoint_count"] = len(cps)
        d["in_progress"] = conn.execute(
            "SELECT COUNT(*) c FROM route_progress WHERE route_id=? AND status='IN_PROGRESS'",
            (r["id"],),
        ).fetchone()["c"]
        d["completed"] = conn.execute(
            "SELECT COUNT(*) c FROM route_progress WHERE route_id=? AND status='COMPLETED'",
            (r["id"],),
        ).fetchone()["c"]
        d["violated"] = conn.execute(
            "SELECT COUNT(*) c FROM route_progress WHERE route_id=? AND status='VIOLATED'",
            (r["id"],),
        ).fetchone()["c"]
        out.append(d)
    return out


def route_detail(conn: sqlite3.Connection, route_id: str) -> Optional[dict]:
    route = get_route(conn, route_id)
    if route is None:
        return None
    versions = []
    for v in conn.execute(
        "SELECT * FROM route_versions WHERE route_id=? ORDER BY version",
        (route_id,),
    ).fetchall():
        vd = dict(v)
        vd["checkpoints"] = [
            _checkpoint_dict(c)
            for c in _version_checkpoints(conn, route_id, v["version"])
        ]
        versions.append(vd)
    bindings = [dict(b) for b in conn.execute(
        "SELECT * FROM route_bindings WHERE route_id=? ORDER BY id DESC LIMIT 200",
        (route_id,),
    ).fetchall()]
    now = iso(utcnow())
    progress = []
    for p in conn.execute(
        "SELECT * FROM route_progress WHERE route_id=? ORDER BY started_at DESC LIMIT 200",
        (route_id,),
    ).fetchall():
        block = route_status_block(conn, p, now)
        block.update({"id": p["id"], "ticket_code": p["ticket_code"],
                      "person_id": p["person_id"], "batch_id": p["batch_id"]})
        progress.append(block)
    return {
        "route": _route_dict(route),
        "versions": versions,
        "bindings": bindings,
        "progress": progress,
        "catalog_version": route_catalog_version(conn, route_id),
    }


# ---------------- 绑定（票 / 批次） ----------------

def _active_current_version(conn: sqlite3.Connection, route_id: str):
    """返回 (route_row, version, error_dict)。路线须存在、ACTIVE 且已发布版本。"""
    route = get_route(conn, route_id)
    if route is None:
        return None, None, {"http_status": 404, "error": f"路线不存在: {route_id}"}
    if route["status"] == "PAUSED":
        return None, None, {"http_status": 409, "error": "路线已暂停，不能绑定新使用"}
    if route["current_version"] < 1:
        return None, None, {"http_status": 409,
                            "error": "路线尚未发布任何检查点版本，不能绑定"}
    return route, int(route["current_version"]), None


def bind_route_to_ticket_locked(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    route_id: str,
    operator: str,
    reason: Optional[str],
    now: Optional[str] = None,
) -> dict:
    """在写事务内把路线绑定到一张尚未开始的票（发票/换发/管理员改绑共用）。"""
    now = now or iso(utcnow())
    route, version, err = _active_current_version(conn, route_id)
    if err:
        return err
    existing = get_progress_by_ticket(conn, ticket["code"])
    if existing is not None:
        return {"http_status": 409,
                "error": "该票的路线已经开始，不能改绑（在途路线继续使用原版本）",
                "status": existing["status"]}
    event_version = add_event(
        conn, "ROUTE_BOUND", ts=now, route_id=route_id, route_version=version,
        ticket_code=ticket["code"], person_id=ticket["person_id"],
        reason=reason or "bind_ticket",
        payload={"scope": "TICKET", "ticket_code": ticket["code"],
                 "version": version, "operator": operator},
    )
    conn.execute(
        "INSERT INTO route_bindings "
        "(route_id,route_version,scope,ticket_code,batch_id,bound_at,bound_by,reason,event_version) "
        "VALUES (?,?,'TICKET',?,NULL,?,?,?,?)",
        (route_id, version, ticket["code"], now, operator, reason, event_version),
    )
    conn.execute(
        "UPDATE tickets SET route_id=?, route_version=? WHERE code=?",
        (route_id, version, ticket["code"]),
    )
    return {"http_status": 200, "route_id": route_id, "route_version": version,
            "event_version": event_version}


def bind_route(
    conn: sqlite3.Connection,
    *,
    route_id: str,
    scope: str,
    code: Optional[str] = None,
    batch_id: Optional[str] = None,
    operator: str,
    reason: Optional[str] = None,
) -> dict:
    from .services import get_batch, normalize_code

    now = iso(utcnow())
    with write_tx(conn):
        route, version, err = _active_current_version(conn, route_id)
        if err:
            return err
        if scope == "TICKET":
            if not code:
                return {"http_status": 400, "error": "缺少票面编号"}
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE code=?", (normalize_code(code),)
            ).fetchone()
            if ticket is None:
                return {"http_status": 404, "error": "票面不存在"}
            if ticket["status"] != "ACTIVE":
                return {"http_status": 409,
                        "error": f"票已处于终态 {ticket['status']}，不能绑定路线"}
            result = bind_route_to_ticket_locked(
                conn, ticket=ticket, route_id=route_id, operator=operator,
                reason=reason or "admin_bind_ticket", now=now)
            if result.get("http_status") != 200:
                return result
            return {"http_status": 200, "scope": "TICKET",
                    "ticket_code": ticket["code"], **{
                        k: v for k, v in result.items() if k != "http_status"}}
        elif scope == "BATCH":
            if not batch_id:
                return {"http_status": 400, "error": "缺少批次 ID"}
            batch = get_batch(conn, batch_id)
            if batch is None:
                return {"http_status": 404, "error": "批次不存在"}
            if route["zone_id"] != batch["zone_id"]:
                return {"http_status": 400,
                        "error": f"路线分区 {route['zone_id']} 与批次分区 "
                                 f"{batch['zone_id']} 不一致"}
            event_version = add_event(
                conn, "ROUTE_BOUND", ts=now, route_id=route_id,
                route_version=version, batch_id=batch_id,
                reason=reason or "bind_batch",
                payload={"scope": "BATCH", "batch_id": batch_id,
                         "version": version, "operator": operator},
            )
            conn.execute(
                "INSERT INTO route_bindings "
                "(route_id,route_version,scope,ticket_code,batch_id,bound_at,bound_by,"
                "reason,event_version) VALUES (?,?,'BATCH',NULL,?,?,?,?,?)",
                (route_id, version, batch_id, now, operator, reason, event_version),
            )
            conn.execute(
                "UPDATE batches SET route_id=?, route_version=? WHERE id=?",
                (route_id, version, batch_id),
            )
            return {"http_status": 200, "scope": "BATCH", "batch_id": batch_id,
                    "route_id": route_id, "route_version": version,
                    "event_version": event_version}
        return {"http_status": 400, "error": "scope 必须是 TICKET 或 BATCH"}


# ---------------- 门点检查记录（route_checks）写入/幂等 ----------------

def _prior_check(
    conn: sqlite3.Connection, gate_id: str, attempt_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM route_checks WHERE gate_id=? AND attempt_id=?",
        (gate_id, attempt_id),
    ).fetchone()


def _insert_check_locked(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    attempt_id: str,
    code: str,
    person_id: Optional[str],
    route_id: Optional[str],
    route_version: Optional[int],
    seq: Optional[int],
    decision: str,
    result: str,
    reason: Optional[str],
    http_status: int,
    response: dict,
    now: str,
    event_version: Optional[int] = None,
    conflict_id: Optional[int] = None,
    event_ts: Optional[str] = None,
    offline: bool = False,
    replayed: bool = False,
) -> int:
    cur = conn.execute(
        """INSERT INTO route_checks
               (gate_id,attempt_id,ticket_code,person_id,route_id,route_version,
                checkpoint_seq,at,event_ts,decision,result,reason,event_version,
                conflict_id,replayed,offline,http_status,result_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (gate_id, attempt_id, code, person_id, route_id, route_version, seq,
         now, event_ts, decision, result, reason, event_version, conflict_id,
         1 if replayed else 0, 1 if offline else 0, http_status,
         json.dumps(response, ensure_ascii=False, sort_keys=True)),
    )
    return int(cur.lastrowid)


def _replay_response(prior: sqlite3.Row) -> dict:
    response = json.loads(prior["result_json"])
    response["replayed"] = True
    return {"http_status": prior["http_status"], "response": response}


# ---------------- 核销同事务：路线开始判定 ----------------

def redeem_route_precheck_locked(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    gate_id: str,
    attempt_id: str,
    client_route_version: int,
    now: str,
) -> Optional[dict]:
    """核销事务内、条件 UPDATE 之前调用。

    仅对绑定了路线的票生效；无绑定返回 None（正常核销）。
    返回：
      None                    允许继续核销（门点即入口检查点）
      {"early": response,...} 立即返回且不落 scan_attempts（stale，可同 attempt 重试）
      {"http_status","response"} 明确业务拒绝（核销继续走拒绝落库路径）
    """
    route_id = ticket["route_id"]
    if not route_id:
        return None
    code = ticket["code"]
    route = get_route(conn, route_id)
    pinned_version = int(ticket["route_version"] or 0)

    # 已经有执行记录（理论上票已 REDEEMED 走不到这里，防御性处理）
    if get_progress_by_ticket(conn, code) is not None:
        return None

    if client_route_version < route_catalog_version(conn, route_id):
        return {
            "early": {
                "ok": False, "status": "STALE_ROUTE", "code": code,
                "person_id": ticket["person_id"], "gate_id": gate_id, "ts": now,
                "reason": "stale_route",
                "reason_text": REASON_TEXT["stale_route"],
                "route_id": route_id,
                "current_route_version": route_catalog_version(conn, route_id),
                "client_route_version": client_route_version,
            }
        }

    entry = conn.execute(
        "SELECT * FROM route_checkpoints WHERE route_id=? AND version=? AND seq=1",
        (route_id, pinned_version),
    ).fetchone()
    base = {
        "code": code, "person_id": ticket["person_id"], "gate_id": gate_id, "ts": now,
        "route_id": route_id, "route_version": pinned_version,
    }
    if route is None or entry is None:
        response = {**base, "ok": False, "status": "ROUTE_BROKEN",
                    "reason": "no_route",
                    "reason_text": "票绑定的路线版本不存在"}
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=ticket["person_id"], route_id=route_id,
            route_version=pinned_version, seq=None, decision="REJECTED",
            result="no_route", reason=None, http_status=409, response=response,
            now=now)
        return {"http_status": 409, "response": response}

    if route["status"] == "PAUSED":
        response = {**base, "ok": False, "status": "ROUTE_PAUSED",
                    "reason": "route_paused",
                    "reason_text": REASON_TEXT["route_paused"]}
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=ticket["person_id"], route_id=route_id,
            route_version=pinned_version, seq=1, decision="REJECTED",
            result="route_paused", reason=None, http_status=423,
            response=response, now=now)
        return {"http_status": 423, "response": response}

    if entry["gate_id"] != gate_id:
        response = {
            **base, "ok": False, "status": "WRONG_ENTRY",
            "reason": "wrong_entry_gate",
            "reason_text": REASON_TEXT["wrong_entry_gate"],
            "checkpoint_seq": 1, "entry_gate": entry["gate_id"],
        }
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=ticket["person_id"], route_id=route_id,
            route_version=pinned_version, seq=1, decision="REJECTED",
            result="wrong_entry_gate", reason=None, http_status=403,
            response=response, now=now)
        return {"http_status": 403, "response": response}

    if entry["closed"]:
        response = {
            **base, "ok": False, "status": "CHECKPOINT_CLOSED",
            "reason": "checkpoint_closed",
            "reason_text": REASON_TEXT["checkpoint_closed"],
            "checkpoint_seq": 1,
            "closed_reason": entry["closed_reason"],
        }
        # 入口已关闭：核销即“进入已关闭检查点”，路线直接判违规终止
        response_violated, _ = _violate_locked(
            conn, ticket=ticket, route=route, pinned_version=pinned_version,
            gate_id=gate_id, attempt_id=attempt_id, seq=1, now=now,
            kind=VIOLATION_CLOSED, reason=entry["closed_reason"],
            response_status="CHECKPOINT_CLOSED", http_status=423,
            response_reason="checkpoint_closed", offline=False, event_ts=now)
        return {"http_status": 423, "response": response_violated}

    return None


def start_route_after_redeem_locked(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    gate_id: str,
    attempt_id: str,
    now: str,
) -> Optional[dict]:
    """核销成功且已登记到场后调用：首次核销开始路线（创建执行 + 事件/记录）。"""
    route_id = ticket["route_id"]
    if not route_id:
        return None
    code = ticket["code"]
    if get_progress_by_ticket(conn, code) is not None:
        p = get_progress_by_ticket(conn, code)
        return route_status_block(conn, p, now)

    pinned_version = int(ticket["route_version"] or 0)
    entry = conn.execute(
        "SELECT * FROM route_checkpoints WHERE route_id=? AND version=? AND seq=1",
        (route_id, pinned_version),
    ).fetchone()
    total = len(_version_checkpoints(conn, route_id, pinned_version))
    pid = _gen_progress_id()
    zone_id = _route_zone(conn, route_id)
    started_version = add_event(
        conn, "ROUTE_STARTED", ts=now, ticket_code=code,
        person_id=ticket["person_id"], gate_id=gate_id, route_id=route_id,
        route_version=pinned_version, checkpoint_seq=1, progress_id=pid,
        reason="first_redeem",
        payload={
            "gate_id": gate_id, "attempt_id": attempt_id,
            "checkpoint": _checkpoint_dict(entry) if entry else None,
            "total_checkpoints": total,
            "batch_id": ticket["batch_id"],
            "application_id": ticket["application_id"],
        },
        batch_id=ticket["batch_id"], application_id=ticket["application_id"],
    )
    conn.execute(
        """INSERT INTO route_progress
               (id,ticket_code,person_id,route_id,route_version,application_id,
                batch_id,zone_id,status,next_seq,current_seq,started_at,started_gate,
                started_version,entered_at,last_seq,last_gate,last_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, code, ticket["person_id"], route_id, pinned_version,
         ticket["application_id"], ticket["batch_id"], zone_id,
         "IN_PROGRESS", 2, 1, now, gate_id, started_version, now, 1,
         gate_id, now),
    )
    p = get_progress_by_ticket(conn, code)
    block = route_status_block(conn, p, now)
    _insert_check_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
        person_id=ticket["person_id"], route_id=route_id,
        route_version=pinned_version, seq=1, decision="STARTED", result="ok",
        reason="first_redeem", http_status=200,
        response={"ok": True, "status": "ROUTE_STARTED", "code": code,
                  "gate_id": gate_id, "ts": now, "route": block},
        now=now, event_version=started_version)
    return block


def _route_zone(conn: sqlite3.Connection, route_id: str) -> Optional[str]:
    r = get_route(conn, route_id)
    return r["zone_id"] if r else None


# ---------------- 违规落盘（事务内） ----------------

def _violate_locked(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    route: sqlite3.Row,
    pinned_version: int,
    gate_id: str,
    attempt_id: str,
    seq: Optional[int],
    now: str,
    kind: str,
    reason: Optional[str],
    response_status: str,
    http_status: int,
    response_reason: str,
    offline: bool,
    event_ts: Optional[str],
) -> tuple[dict, int]:
    """把路线置 VIOLATED（终态）并写事件 + 门点记录。返回 (response, event_version)。

    入口核销即违规（如入口检查点已关闭）时执行记录尚不存在，这里直接补一行
    VIOLATED 进度（current_seq=1、未正式进入），保证违规历史可查、终态可追溯。
    """
    code = ticket["code"]
    p = get_progress_by_ticket(conn, code)
    precreated = p is None
    if precreated:
        # 尚未开始就违规：补建一条终态执行记录（版本固化、违规点=seq）
        pid = _gen_progress_id()
        conn.execute(
            """INSERT INTO route_progress
                   (id,ticket_code,person_id,route_id,route_version,application_id,
                    batch_id,zone_id,status,next_seq,current_seq,started_at,started_gate,
                    started_version,entered_at,last_seq,last_gate,last_at,
                    violated_at,violation_kind,violation_reason)
               VALUES (?,?,?,?,?,?,?,?,?,1,NULL,?,?,NULL,NULL,?,?,?,
                       ?,?,?)""",
            (pid, code, ticket["person_id"], route["id"], pinned_version,
             ticket["application_id"], ticket["batch_id"], route["zone_id"],
             "VIOLATED", gate_id, now, seq, gate_id, now,
             now, kind, reason),
        )
        p = get_progress_by_ticket(conn, code)
    pid = p["id"] if p is not None else None
    event_version = add_event(
        conn, "ROUTE_VIOLATED", ts=now, ticket_code=code,
        person_id=ticket["person_id"], gate_id=gate_id, route_id=route["id"],
        route_version=pinned_version, checkpoint_seq=seq, progress_id=pid,
        reason=response_reason,
        payload={"kind": kind, "reason": reason, "gate_id": gate_id,
                 "attempt_id": attempt_id, "offline": offline,
                 "event_ts": event_ts},
        batch_id=ticket["batch_id"], application_id=ticket["application_id"],
    )
    if p is not None:
        if precreated:
            # 入口即违规：补建的终态行只需回填违规事件版本
            conn.execute(
                "UPDATE route_progress SET violated_version=? WHERE id=?",
                (event_version, p["id"]),
            )
        else:
            conn.execute(
                """UPDATE route_progress
                      SET status='VIOLATED', violated_at=?, violated_version=?,
                          violation_kind=?, violation_reason=?, last_gate=?, last_at=?
                    WHERE id=? AND status='IN_PROGRESS'""",
                (now, event_version, kind, reason, gate_id, now, p["id"]),
            )
        p = get_progress_by_ticket(conn, code)
        block = route_status_block(conn, p, now)
    else:
        block = None
    response = {
        "ok": False, "status": response_status, "code": code,
        "person_id": ticket["person_id"], "gate_id": gate_id, "ts": now,
        "reason": response_reason, "reason_text": REASON_TEXT.get(
            response_reason, response_reason),
        "route_id": route["id"], "route_version": pinned_version,
        "checkpoint_seq": seq, "violation_kind": kind,
        "violation_reason": reason,
    }
    if block:
        response["route"] = block
    if offline:
        response["offline"] = True
    _insert_check_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
        person_id=ticket["person_id"], route_id=route["id"],
        route_version=pinned_version, seq=seq, decision="VIOLATED",
        result=response_reason, reason=reason, http_status=http_status,
        response=response, now=now, event_version=event_version,
        event_ts=event_ts, offline=offline)
    return response, event_version


# ---------------- 门点在线检查点上报 ----------------

def gate_checkpoint(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    raw_code: str,
    attempt_id: str,
    client_route_version: int = 0,
) -> dict:
    """门点对已开始路线的访客上报一次检查点经过（在线单次）。"""
    from .services import normalize_code

    now = iso(utcnow())
    code = normalize_code(raw_code)

    prior = _prior_check(conn, gate_id, attempt_id)
    if prior is not None:
        return _replay_response(prior)
    # 同一物理上报若已在核销/离场接口落过 scan_attempts（如入口核销），
    # 不能在检查点接口重放推进路线：返回首次结论并标注重放来源。
    prior_scan = conn.execute(
        "SELECT * FROM scan_attempts WHERE gate_id=? AND attempt_id=?",
        (gate_id, attempt_id),
    ).fetchone()
    if prior_scan is not None:
        response = json.loads(prior_scan["result_json"])
        response["replayed"] = True
        response["replayed_from"] = "scan_attempts"
        return {"http_status": prior_scan["http_status"], "response": response}

    with write_tx(conn):
        prior = _prior_check(conn, gate_id, attempt_id)
        if prior is not None:
            return _replay_response(prior)
        prior_scan = conn.execute(
            "SELECT * FROM scan_attempts WHERE gate_id=? AND attempt_id=?",
            (gate_id, attempt_id),
        ).fetchone()
        if prior_scan is not None:
            response = json.loads(prior_scan["result_json"])
            response["replayed"] = True
            response["replayed_from"] = "scan_attempts"
            return {"http_status": prior_scan["http_status"], "response": response}
        result = _adjudicate_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            event_ts=now, now=now, offline=False,
            client_route_version=client_route_version)
    return {"http_status": result["http_status"], "response": result["response"]}


def _adjudicate_locked(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    attempt_id: str,
    code: str,
    event_ts: str,
    now: str,
    offline: bool,
    client_route_version: Optional[int] = None,
) -> dict:
    """在写事务内裁决一次检查点事件（在线/离线共用）。

    返回 {"http_status", "response", "conflict_id"?}。所有路径都落 route_checks；
    无法自动裁决的离线事件额外落 route_event_conflicts + ROUTE_CONFLICT。
    """
    def finish(http_status: int, response: dict, **kw) -> dict:
        return {"http_status": http_status, "response": response, **kw}

    ticket = conn.execute(
        "SELECT * FROM tickets WHERE code=?", (code,)
    ).fetchone()
    if ticket is None:
        if offline:
            return _record_conflict(
                conn, kind=CONFLICT_TICKET, gate_id=gate_id, attempt_id=attempt_id,
                code=code, person_id=None, route_id=None, route_version=None,
                seq=None, event_ts=event_ts, now=now,
                detail={"error": "ticket_not_found"},
                response_reason="ticket_not_found", http_status=404)
        response = {"ok": False, "status": "NOT_FOUND", "code": code,
                    "gate_id": gate_id, "ts": now, "reason": "not_found",
                    "reason_text": "票面不存在"}
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=None, route_id=None, route_version=None, seq=None,
            decision="REJECTED", result="not_found", reason=None,
            http_status=404, response=response, now=now, event_ts=event_ts,
            offline=offline)
        return finish(404, response)

    route_id = ticket["route_id"]
    p = get_progress_by_ticket(conn, code)

    if not route_id or p is None:
        if offline:
            kind = CONFLICT_NO_ROUTE
            detail = {"has_route_binding": bool(route_id), "progress": None}
            return _record_conflict(
                conn, kind=kind, gate_id=gate_id, attempt_id=attempt_id, code=code,
                person_id=ticket["person_id"], route_id=route_id,
                route_version=ticket["route_version"], seq=None,
                event_ts=event_ts, now=now, detail=detail,
                response_reason="route_not_started", http_status=409)
        reason = "no_route" if not route_id else "route_not_started"
        response = {
            "ok": False,
            "status": "NO_ROUTE" if not route_id else "ROUTE_NOT_STARTED",
            "code": code, "person_id": ticket["person_id"], "gate_id": gate_id,
            "ts": now, "reason": reason, "reason_text": REASON_TEXT.get(reason, reason),
            "route_id": route_id,
        }
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=ticket["person_id"], route_id=route_id,
            route_version=ticket["route_version"], seq=None, decision="REJECTED",
            result=reason, reason=None, http_status=409, response=response,
            now=now, event_ts=event_ts, offline=offline)
        return finish(409, response)

    route = get_route(conn, route_id)
    pinned_version = int(p["route_version"])
    base = {
        "code": code, "person_id": ticket["person_id"], "gate_id": gate_id,
        "ts": now, "route_id": route_id, "route_version": pinned_version,
    }

    # 目录版本过旧（仅在线拦截；离线批量补齐前门点已先 /gate/sync）
    if (not offline and client_route_version is not None
            and client_route_version < route_catalog_version(conn, route_id)):
        response = {**base, "ok": False, "status": "STALE_ROUTE",
                    "reason": "stale_route",
                    "reason_text": REASON_TEXT["stale_route"],
                    "current_route_version": route_catalog_version(conn, route_id),
                    "client_route_version": client_route_version}
        # 与 stale_policy 一致：不落任何记录，门点同步后用同一 attempt_id 重试
        return finish(409, response, stale=True)

    cp = _checkpoint_by_gate(conn, route_id, pinned_version, gate_id)
    if cp is None:
        reason = "gate_not_on_route"
        response = {**base, "ok": False, "status": "GATE_NOT_ON_ROUTE",
                    "reason": reason, "reason_text": REASON_TEXT[reason]}
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=ticket["person_id"], route_id=route_id,
            route_version=pinned_version, seq=None, decision="REJECTED",
            result=reason, reason=None, http_status=403, response=response,
            now=now, event_ts=event_ts, offline=offline)
        return finish(403, response)

    seq = int(cp["seq"])

    # 终态保护：完成/违规后任何事件（含离线迟到事件）都不能改回进行中
    if p["status"] in ("COMPLETED", "VIOLATED"):
        if offline:
            return _record_conflict(
                conn, kind=CONFLICT_TERMINAL, gate_id=gate_id, attempt_id=attempt_id,
                code=code, person_id=ticket["person_id"], route_id=route_id,
                route_version=pinned_version, seq=seq, event_ts=event_ts, now=now,
                detail={"progress_status": p["status"],
                        "violation_kind": p["violation_kind"],
                        "next_seq": p["next_seq"]},
                response_reason=("route_already_completed"
                                 if p["status"] == "COMPLETED"
                                 else "route_already_violated"),
                http_status=409)
        reason = ("route_already_completed" if p["status"] == "COMPLETED"
                  else "route_already_violated")
        block = route_status_block(conn, p, now)
        response = {**base, "ok": False,
                    "status": "ROUTE_COMPLETED" if p["status"] == "COMPLETED"
                              else "ROUTE_VIOLATED",
                    "reason": reason, "reason_text": REASON_TEXT[reason],
                    "checkpoint_seq": seq, "route": block}
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=ticket["person_id"], route_id=route_id,
            route_version=pinned_version, seq=seq, decision="REJECTED",
            result=reason, reason=None, http_status=409, response=response,
            now=now, event_ts=event_ts)
        return finish(409, response)

    # IN_PROGRESS 分支
    next_seq = int(p["next_seq"])
    current_seq = int(p["current_seq"])

    if offline:
        # 时间戳卫生：未来时间 / 早于当前检查点进入时间（顺序倒挂）
        try:
            ev_dt = parse_dt(event_ts)
        except (TypeError, ValueError):
            return _record_conflict(
                conn, kind=CONFLICT_INVALID_TS, gate_id=gate_id,
                attempt_id=attempt_id, code=code, person_id=ticket["person_id"],
                route_id=route_id, route_version=pinned_version, seq=seq,
                event_ts=event_ts, now=now, detail={"error": "unparseable"},
                response_reason="invalid_event_ts", http_status=400)
        if ev_dt > parse_dt(now) + timedelta(seconds=FUTURE_TS_SKEW_SECONDS):
            return _record_conflict(
                conn, kind=CONFLICT_INVALID_TS, gate_id=gate_id,
                attempt_id=attempt_id, code=code, person_id=ticket["person_id"],
                route_id=route_id, route_version=pinned_version, seq=seq,
                event_ts=event_ts, now=now,
                detail={"error": "future_timestamp", "server_now": now},
                response_reason="invalid_event_ts", http_status=400)
        if p["entered_at"] and ev_dt < parse_dt(p["entered_at"]):
            return _record_conflict(
                conn, kind=CONFLICT_LATE, gate_id=gate_id, attempt_id=attempt_id,
                code=code, person_id=ticket["person_id"], route_id=route_id,
                route_version=pinned_version, seq=seq, event_ts=event_ts, now=now,
                detail={"error": "event_before_current_entry",
                        "entered_at": p["entered_at"], "next_seq": next_seq},
                response_reason="late_event", http_status=409)

    if seq > next_seq:
        # 跳过检查点：在线直接判违规；离线（可能是别的门点事件还没补到）记冲突
        if offline:
            return _record_conflict(
                conn, kind=CONFLICT_GAP, gate_id=gate_id, attempt_id=attempt_id,
                code=code, person_id=ticket["person_id"], route_id=route_id,
                route_version=pinned_version, seq=seq, event_ts=event_ts, now=now,
                detail={"missing_seq": next_seq, "event_seq": seq},
                response_reason="gap_pending", http_status=409)
        return _terminal_violation(
            conn, ticket=ticket, route=route, p=p, gate_id=gate_id,
            attempt_id=attempt_id, seq=seq, now=now, kind=VIOLATION_SKIPPED,
            reason=f"应先到检查点 {next_seq}，直接进入 {seq}",
            response_status="SKIPPED_CHECKPOINT", http_status=409,
            response_reason="skipped_checkpoint", offline=False, event_ts=now)

    if seq < next_seq:
        # 已越过该点：在线=重复进入（非终态拒绝）；离线=迟到事件冲突
        if offline:
            return _record_conflict(
                conn, kind=CONFLICT_LATE, gate_id=gate_id, attempt_id=attempt_id,
                code=code, person_id=ticket["person_id"], route_id=route_id,
                route_version=pinned_version, seq=seq, event_ts=event_ts, now=now,
                detail={"current_seq": current_seq, "event_seq": seq},
                response_reason="late_event", http_status=409)
        block = route_status_block(conn, p, now)
        response = {**base, "ok": False, "status": "DUPLICATE_ENTRY",
                    "reason": "duplicate_entry",
                    "reason_text": REASON_TEXT["duplicate_entry"],
                    "checkpoint_seq": seq, "route": block}
        _insert_check_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
            person_id=ticket["person_id"], route_id=route_id,
            route_version=pinned_version, seq=seq, decision="REJECTED",
            result=REJECT_DUPLICATE, reason=None, http_status=409,
            response=response, now=now)
        return finish(409, response)

    # seq == next_seq：正常推进或完成；先检查在上一检查点的停留是否超时
    prev_cp = conn.execute(
        "SELECT * FROM route_checkpoints WHERE route_id=? AND version=? AND seq=?",
        (route_id, pinned_version, current_seq),
    ).fetchone()
    if prev_cp is not None and prev_cp["max_stay_seconds"] and p["entered_at"]:
        deadline = parse_dt(p["entered_at"]) + timedelta(
            seconds=int(prev_cp["max_stay_seconds"]))
        if parse_dt(event_ts) > deadline:
            kind_reason = (
                f"在检查点 {current_seq}（{prev_cp['name']}）停留超过 "
                f"{prev_cp['max_stay_seconds']} 秒"
            )
            return _terminal_violation(
                conn, ticket=ticket, route=route, p=p, gate_id=gate_id,
                attempt_id=attempt_id, seq=seq, now=now, kind=VIOLATION_DWELL,
                reason=kind_reason, response_status="DWELL_TIMEOUT",
                http_status=409, response_reason="dwell_timeout",
                offline=offline, event_ts=event_ts)

    # 进入已关闭检查点（离线时按关闭时间与事件时间比对：事件发生在关闭后才违规）
    if cp["closed"] and (not offline or not cp["closed_at"]
                         or parse_dt(cp["closed_at"]) <= parse_dt(event_ts)):
        return _terminal_violation(
            conn, ticket=ticket, route=route, p=p, gate_id=gate_id,
            attempt_id=attempt_id, seq=seq, now=now, kind=VIOLATION_CLOSED,
            reason=cp["closed_reason"], response_status="CHECKPOINT_CLOSED",
            http_status=423, response_reason="checkpoint_closed",
            offline=offline, event_ts=event_ts, effective_ts=event_ts)

    return _advance_locked(
        conn, ticket=ticket, route=route, p=p, cp=cp, gate_id=gate_id,
        attempt_id=attempt_id, now=now, event_ts=event_ts, offline=offline)


def _terminal_violation(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    route: sqlite3.Row,
    p: sqlite3.Row,
    gate_id: str,
    attempt_id: str,
    seq: Optional[int],
    now: str,
    kind: str,
    reason: Optional[str],
    response_status: str,
    http_status: int,
    response_reason: str,
    offline: bool,
    event_ts: str,
    effective_ts: Optional[str] = None,
) -> dict:
    """已有进行中执行时的违规落盘（区别于入口核销前违规）。"""
    code = ticket["code"]
    pinned_version = int(p["route_version"])
    event_version = add_event(
        conn, "ROUTE_VIOLATED", ts=now, ticket_code=code,
        person_id=ticket["person_id"], gate_id=gate_id, route_id=route["id"],
        route_version=pinned_version, checkpoint_seq=seq, progress_id=p["id"],
        reason=response_reason,
        payload={"kind": kind, "reason": reason, "gate_id": gate_id,
                 "attempt_id": attempt_id, "offline": offline,
                 "event_ts": event_ts},
        batch_id=ticket["batch_id"], application_id=ticket["application_id"],
    )
    conn.execute(
        """UPDATE route_progress
              SET status='VIOLATED', violated_at=?, violated_version=?,
                  violation_kind=?, violation_reason=?, last_seq=?,
                  last_gate=?, last_at=?
            WHERE id=? AND status='IN_PROGRESS'""",
        (now, event_version, kind, reason, seq, gate_id, now, p["id"]),
    )
    p2 = get_progress_by_ticket(conn, code)
    block = route_status_block(conn, p2, now)
    response = {
        "ok": False, "status": response_status, "code": code,
        "person_id": ticket["person_id"], "gate_id": gate_id, "ts": now,
        "reason": response_reason,
        "reason_text": REASON_TEXT.get(response_reason, response_reason),
        "route_id": route["id"], "route_version": pinned_version,
        "checkpoint_seq": seq, "violation_kind": kind,
        "violation_reason": reason, "route": block,
    }
    if offline:
        response["offline"] = True
    _insert_check_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
        person_id=ticket["person_id"], route_id=route["id"],
        route_version=pinned_version, seq=seq, decision="VIOLATED",
        result=response_reason, reason=reason, http_status=http_status,
        response=response, now=now, event_version=event_version,
        event_ts=event_ts, offline=offline)
    return {"http_status": http_status, "response": response}


def _advance_locked(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    route: sqlite3.Row,
    p: sqlite3.Row,
    cp: sqlite3.Row,
    gate_id: str,
    attempt_id: str,
    now: str,
    event_ts: str,
    offline: bool,
) -> dict:
    """推进到下一检查点；若是最后一个则 COMPLETED（终态）。"""
    code = ticket["code"]
    seq = int(cp["seq"])
    total = len(_version_checkpoints(conn, route["id"], p["route_version"]))
    completed = seq >= total
    if completed:
        event_type = "ROUTE_COMPLETED"
        decision = "COMPLETED"
    else:
        event_type = "ROUTE_CHECKPOINT"
        decision = "ADVANCED"
    event_version = add_event(
        conn, event_type, ts=now, ticket_code=code,
        person_id=ticket["person_id"], gate_id=gate_id, route_id=route["id"],
        route_version=p["route_version"], checkpoint_seq=seq, progress_id=p["id"],
        reason="gate_check" if not offline else "gate_check_offline",
        payload={"seq": seq, "gate_id": gate_id, "attempt_id": attempt_id,
                 "offline": offline, "event_ts": event_ts,
                 "max_stay_seconds": cp["max_stay_seconds"]},
        batch_id=ticket["batch_id"], application_id=ticket["application_id"],
    )
    if completed:
        conn.execute(
            """UPDATE route_progress
                  SET status='COMPLETED', current_seq=?, next_seq=?, entered_at=?,
                      last_seq=?, last_gate=?, last_at=?, completed_at=?,
                      completed_version=?
                WHERE id=?""",
            (seq, seq + 1, event_ts, seq, gate_id, event_ts, now, event_version,
             p["id"]),
        )
    else:
        conn.execute(
            """UPDATE route_progress
                  SET current_seq=?, next_seq=?, entered_at=?, last_seq=?,
                      last_gate=?, last_at=?
                WHERE id=?""",
            (seq, seq + 1, event_ts, seq, gate_id, event_ts, p["id"]),
        )
    p2 = get_progress_by_ticket(conn, code)
    block = route_status_block(conn, p2, now)
    response = {
        "ok": True,
        "status": "ROUTE_COMPLETED" if completed else "CHECKPOINT_OK",
        "code": code, "person_id": ticket["person_id"], "gate_id": gate_id,
        "ts": now, "reason": "ok",
        "reason_text": (REASON_TEXT["route_completed"] if completed
                        else REASON_TEXT["ok"]),
        "route_id": route["id"], "route_version": p["route_version"],
        "checkpoint_seq": seq, "route": block,
    }
    if offline:
        response["offline"] = True
    _insert_check_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
        person_id=ticket["person_id"], route_id=route["id"],
        route_version=p["route_version"], seq=seq, decision=decision,
        result="ok", reason=None, http_status=200, response=response, now=now,
        event_version=event_version, event_ts=event_ts, offline=offline)
    return {"http_status": 200, "response": response}


# ---------------- 离线冲突落盘 ----------------

def _record_conflict(
    conn: sqlite3.Connection,
    *,
    kind: str,
    gate_id: str,
    attempt_id: str,
    code: str,
    person_id: Optional[str],
    route_id: Optional[str],
    route_version: Optional[int],
    seq: Optional[int],
    event_ts: Optional[str],
    now: str,
    detail: dict,
    response_reason: str,
    http_status: int,
) -> dict:
    """保留离线冲突现场：conflicts 表 + CONFLICT 门点记录 + ROUTE_CONFLICT 事件。

    绝不推进路线；管理员稍后在冲突队列里处理。
    """
    cur = conn.execute(
        """INSERT INTO route_event_conflicts
               (gate_id,attempt_id,ticket_code,person_id,route_id,route_version,
                checkpoint_seq,kind,event_ts,detected_at,detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (gate_id, attempt_id, code, person_id, route_id, route_version, seq,
         kind, event_ts, now,
         json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )
    conflict_id = int(cur.lastrowid)
    event_version = add_event(
        conn, "ROUTE_CONFLICT", ts=now, ticket_code=code, person_id=person_id,
        gate_id=gate_id, route_id=route_id, route_version=route_version,
        checkpoint_seq=seq, reason=kind,
        payload={"conflict_id": conflict_id, "kind": kind, "gate_id": gate_id,
                 "attempt_id": attempt_id, "event_ts": event_ts, "detail": detail},
    )
    response = {
        "ok": False, "status": "ROUTE_CONFLICT", "code": code,
        "person_id": person_id, "gate_id": gate_id, "ts": now,
        "reason": "route_conflict",
        "reason_text": f"离线检查点事件存在冲突（{kind}），已保留记录待管理员处理",
        "route_id": route_id, "route_version": route_version,
        "checkpoint_seq": seq, "conflict_kind": kind,
        "conflict_id": conflict_id, "offline": True, "event_ts": event_ts,
    }
    _insert_check_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
        person_id=person_id, route_id=route_id, route_version=route_version,
        seq=seq, decision="CONFLICT", result=kind,
        reason=response_reason, http_status=http_status, response=response,
        now=now, event_version=event_version, conflict_id=conflict_id,
        event_ts=event_ts, offline=True)
    return {"http_status": http_status, "response": response,
            "conflict_id": conflict_id}


# ---------------- 门点离线批量补齐 ----------------

def replay_checkpoint_events(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    events: list[dict],
) -> dict:
    """门点重连后按事件顺序批量补齐离线期间的检查点上报。

    整个批次在单个 IMMEDIATE 事务内串行（跨门点批次之间由写锁排序，全局只有
    一个明确先后）。同一 (gate_id, attempt_id) 重放返回首次结果，不重复推进；
    无法自动裁决的落冲突队列。
    """
    now = iso(utcnow())
    results: list[dict] = []
    with write_tx(conn):
        for ev in events:
            attempt_id = str(ev.get("attempt_id") or "").strip()
            raw_code = str(ev.get("code") or "").strip()
            event_ts = str(ev.get("event_ts") or now).strip()
            if not attempt_id or not raw_code:
                results.append({"ok": False, "http_status": 400,
                                "error": "缺少 code 或 attempt_id",
                                "attempt_id": attempt_id or None})
                continue
            from .services import normalize_code
            code = normalize_code(raw_code)
            prior = _prior_check(conn, gate_id, attempt_id)
            if prior is not None:
                replay = _replay_response(prior)
                item = replay["response"]
                item["http_status"] = replay["http_status"]
                results.append(item)
                continue
            # 已在核销/离场接口用过的同一物理上报：重放首次结论，不推进路线
            prior_scan = conn.execute(
                "SELECT * FROM scan_attempts WHERE gate_id=? AND attempt_id=?",
                (gate_id, attempt_id),
            ).fetchone()
            if prior_scan is not None:
                item = json.loads(prior_scan["result_json"])
                item["replayed"] = True
                item["replayed_from"] = "scan_attempts"
                item["http_status"] = prior_scan["http_status"]
                results.append(item)
                continue
            try:
                event_ts = iso(parse_dt(event_ts))
            except (TypeError, ValueError):
                # 无法解析的时间戳本身就是冲突证据
                ticket = conn.execute(
                    "SELECT * FROM tickets WHERE code=?", (code,)
                ).fetchone()
                r = _record_conflict(
                    conn, kind=CONFLICT_INVALID_TS, gate_id=gate_id,
                    attempt_id=attempt_id, code=code,
                    person_id=ticket["person_id"] if ticket else None,
                    route_id=ticket["route_id"] if ticket else None,
                    route_version=ticket["route_version"] if ticket else None,
                    seq=None, event_ts=event_ts, now=now,
                    detail={"error": "unparseable_event_ts"},
                    response_reason="invalid_event_ts", http_status=400)
                item = dict(r["response"])
                item["http_status"] = r["http_status"]
                results.append(item)
                continue
            r = _adjudicate_locked(
                conn, gate_id=gate_id, attempt_id=attempt_id, code=code,
                event_ts=event_ts, now=now, offline=True)
            item = dict(r["response"])
            item["http_status"] = r["http_status"]
            if r.get("conflict_id"):
                item["conflict_id"] = r["conflict_id"]
            results.append(item)
    return {"gate_id": gate_id, "processed_at": now,
            "count": len(results), "results": results}


# ---------------- 冲突队列（管理员处理） ----------------

def list_conflicts(
    conn: sqlite3.Connection,
    *,
    status_filter: str = "OPEN",
    zone_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    person_id: Optional[str] = None,
    route_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT fc.* FROM route_event_conflicts fc WHERE 1=1"
    params: list = []
    if status_filter and status_filter != "ALL":
        sql += " AND fc.status=?"
        params.append(status_filter)
    if route_id:
        sql += " AND fc.route_id=?"
        params.append(route_id)
    if person_id:
        sql += " AND fc.person_id=?"
        params.append(person_id)
    if zone_id or batch_id:
        sql += " AND EXISTS (SELECT 1 FROM tickets t WHERE t.code=fc.ticket_code"
        if zone_id:
            sql += " AND EXISTS (SELECT 1 FROM json_each(t.zones) je WHERE je.value=?)"
            params.append(zone_id)
        if batch_id:
            sql += " AND t.batch_id=?"
            params.append(batch_id)
        sql += ")"
    sql += " ORDER BY fc.id DESC LIMIT ?"
    params.append(min(limit, 1000))
    return [_conflict_dict(r) for r in conn.execute(sql, params).fetchall()]


def get_conflict(conn: sqlite3.Connection, conflict_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM route_event_conflicts WHERE id=?", (conflict_id,)
    ).fetchone()


def resolve_conflict(
    conn: sqlite3.Connection,
    *,
    conflict_id: int,
    action: str,
    operator: str,
    reason: Optional[str],
) -> dict:
    """管理员处理离线冲突：DISMISSED（确认忽略）/ APPLIED（按现场补推进）/
    MARK_VIOLATED（把该路线判违规终止）。只追加处理结论，冲突记录保留。"""
    now = iso(utcnow())
    action = action.strip().upper()
    if action not in ("DISMISSED", "APPLIED", "MARK_VIOLATED"):
        return {"http_status": 400,
                "error": "action 必须是 DISMISSED / APPLIED / MARK_VIOLATED"}
    with write_tx(conn):
        fc = get_conflict(conn, conflict_id)
        if fc is None:
            return {"http_status": 404, "error": "冲突记录不存在"}
        if fc["status"] == "RESOLVED":
            return {"http_status": 409, "error": "冲突已处理",
                    "resolution": fc["resolution"]}

        if action == "DISMISSED":
            conn.execute(
                """UPDATE route_event_conflicts
                      SET status='RESOLVED', resolution='DISMISSED', resolved_at=?,
                          resolved_by=?, resolve_reason=?
                    WHERE id=?""",
                (now, operator, reason, conflict_id),
            )
            out = {"http_status": 200, "resolved": "DISMISSED",
                   "conflict": _conflict_dict(get_conflict(conn, conflict_id))}
            return out

        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (fc["ticket_code"],)
        ).fetchone()
        p = get_progress_by_ticket(conn, fc["ticket_code"])

        if action == "MARK_VIOLATED":
            if ticket is None or p is None:
                return {"http_status": 409,
                        "error": "票或路线执行不存在，无法标记违规（可直接 DISMISSED）"}
            if p["status"] != "IN_PROGRESS":
                return {"http_status": 409,
                        "error": f"路线已处于终态 {p['status']}，无需标记",
                        "status": p["status"]}
            route = get_route(conn, p["route_id"])
            _terminal_violation(
                conn, ticket=ticket, route=route, p=p, gate_id=fc["gate_id"],
                attempt_id=f"resolve-{conflict_id}-violate", seq=fc["checkpoint_seq"],
                now=now, kind="ADMIN_MARK_VIOLATED",
                reason=reason or f"管理员处理冲突 #{conflict_id}：判定违规",
                response_status="ADMIN_VIOLATED", http_status=200,
                response_reason="admin_mark_violated", offline=False,
                event_ts=fc["event_ts"])
            conn.execute(
                """UPDATE route_event_conflicts
                      SET status='RESOLVED', resolution='MARK_VIOLATED', resolved_at=?,
                          resolved_by=?, resolve_reason=?
                    WHERE id=?""",
                (now, operator, reason, conflict_id),
            )
            return {"http_status": 200, "resolved": "MARK_VIOLATED",
                    "conflict": _conflict_dict(get_conflict(conn, conflict_id))}

        # APPLIED：把冲突事件按其现场重新裁决（此时缺口可能已补齐）
        if ticket is None or p is None:
            return {"http_status": 409,
                    "error": "票或路线执行不存在，无法补处理（可 DISMISSED）"}
        if p["status"] != "IN_PROGRESS":
            return {"http_status": 409,
                    "error": f"路线已处于终态 {p['status']}，不能再补推进"
                             "（请改用 DISMISSED 或 MARK_VIOLATED）",
                    "status": p["status"]}
        r = _adjudicate_locked(
            conn, gate_id=fc["gate_id"],
            attempt_id=f"resolve-{conflict_id}-apply",
            code=fc["ticket_code"], event_ts=fc["event_ts"] or now, now=now,
            offline=False)
        if r["http_status"] != 200:
            # 仍然无法自动处理：保持 OPEN，把最新结论返回给管理员
            return {"http_status": 409,
                    "error": "按当前状态仍无法补推进，冲突保持待处理",
                    "latest": r["response"]}
        conn.execute(
            """UPDATE route_event_conflicts
                      SET status='RESOLVED', resolution='APPLIED', resolved_at=?,
                          resolved_by=?, resolve_reason=?
                    WHERE id=?""",
            (now, operator, reason, conflict_id),
        )
        return {"http_status": 200, "resolved": "APPLIED",
                "applied": r["response"],
                "conflict": _conflict_dict(get_conflict(conn, conflict_id))}


# ---------------- 监控查询 ----------------

def list_progress(
    conn: sqlite3.Connection,
    *,
    status_filter: Optional[str] = None,
    zone_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    person_id: Optional[str] = None,
    route_id: Optional[str] = None,
    overdue_only: bool = False,
    limit: int = 500,
) -> list[dict]:
    """路线执行总览：当前所在检查点、停留计时/超时、完成与违规统一入口。"""
    now = iso(utcnow())
    sql = """SELECT p.* FROM route_progress p
             WHERE 1=1"""
    params: list = []
    if status_filter:
        sql += " AND p.status=?"
        params.append(status_filter)
    if zone_id:
        sql += " AND p.zone_id=?"
        params.append(zone_id)
    if batch_id:
        sql += " AND p.batch_id=?"
        params.append(batch_id)
    if person_id:
        sql += " AND p.person_id=?"
        params.append(person_id)
    if route_id:
        sql += " AND p.route_id=?"
        params.append(route_id)
    sql += " ORDER BY COALESCE(p.entered_at,p.started_at) DESC LIMIT ?"
    params.append(min(limit, 2000))
    out = []
    for p in conn.execute(sql, params).fetchall():
        block = route_status_block(conn, p, now)
        block.update(_progress_dict(p))
        # 申请姓名/票号冗余，便于管理台直接展示
        app = conn.execute(
            "SELECT name FROM applications WHERE id=?", (p["application_id"],)
        ).fetchone() if p["application_id"] else None
        block["applicant_name"] = app["name"] if app else None
        if overdue_only and not block["overdue"]:
            continue
        out.append(block)
    return out


def progress_detail(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    """单票路线执行详情：当前状态 + 版本检查点定义 + 完整门点检查记录。"""
    from .services import normalize_code

    code = normalize_code(code)
    p = get_progress_by_ticket(conn, code)
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE code=?", (code,)
    ).fetchone()
    if p is None and ticket is None:
        return None
    now = iso(utcnow())
    out: dict = {"ticket_code": code}
    if ticket is not None:
        out["bound_route_id"] = ticket["route_id"]
        out["bound_route_version"] = ticket["route_version"]
        out["ticket_status"] = ticket["status"]
    if p is not None:
        out["progress"] = route_status_block(conn, p, now)
        out["progress"]["id"] = p["id"]
        cps = [
            _checkpoint_dict(c)
            for c in _version_checkpoints(conn, p["route_id"], p["route_version"])
        ]
        gate_names = {g["id"]: g["name"]
                      for g in conn.execute("SELECT id,name FROM gates").fetchall()}
        for c in cps:
            c["gate_name"] = gate_names.get(c["gate_id"])
        out["checkpoints"] = cps
    checks = []
    for r in conn.execute(
        "SELECT * FROM route_checks WHERE ticket_code=? ORDER BY id", (code,)
    ).fetchall():
        d = dict(r)
        d["replayed"] = bool(d["replayed"])
        d["offline"] = bool(d["offline"])
        d["gate_name"] = conn.execute(
            "SELECT name FROM gates WHERE id=?", (r["gate_id"],)
        ).fetchone()["name"] if conn.execute(
            "SELECT 1 FROM gates WHERE id=?", (r["gate_id"],)
        ).fetchone() else None
        checks.append(d)
    out["checks"] = checks
    conflicts = [_conflict_dict(r) for r in conn.execute(
        "SELECT * FROM route_event_conflicts WHERE ticket_code=? ORDER BY id",
        (code,),
    ).fetchall()]
    out["conflicts"] = conflicts
    return out


def list_checks(
    conn: sqlite3.Connection,
    *,
    gate_id: Optional[str] = None,
    route_id: Optional[str] = None,
    code: Optional[str] = None,
    person_id: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """门点路线检查记录（成功推进/拒绝/违规/冲突全部保留）。"""
    from .services import normalize_code

    sql = """SELECT rc.*, g.name AS gate_name FROM route_checks rc
             LEFT JOIN gates g ON g.id=rc.gate_id WHERE 1=1"""
    params: list = []
    if gate_id:
        sql += " AND rc.gate_id=?"
        params.append(gate_id)
    if route_id:
        sql += " AND rc.route_id=?"
        params.append(route_id)
    if code:
        sql += " AND rc.ticket_code=?"
        params.append(normalize_code(code))
    if person_id:
        sql += " AND rc.person_id=?"
        params.append(person_id)
    if decision:
        sql += " AND rc.decision=?"
        params.append(decision.upper())
    sql += " ORDER BY rc.id DESC LIMIT ?"
    params.append(min(limit, 1000))
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["replayed"] = bool(d["replayed"])
        d["offline"] = bool(d["offline"])
        out.append(d)
    return out


def route_monitoring_summary(conn: sqlite3.Connection) -> dict:
    now = iso(utcnow())
    rows = conn.execute(
        """SELECT p.route_id, p.route_version, p.zone_id, p.status,
                  p.current_seq, p.entered_at,
                  c.max_stay_seconds
             FROM route_progress p
             LEFT JOIN route_checkpoints c
               ON c.route_id=p.route_id AND c.version=p.route_version
              AND c.seq=p.current_seq"""
    ).fetchall()
    by_zone: dict[str, dict] = {}
    by_route: dict[str, dict] = {}
    overdue = 0
    for r in rows:
        z = by_zone.setdefault(r["zone_id"] or "",
                               {"zone_id": r["zone_id"], "in_progress": 0,
                                "completed": 0, "violated": 0, "overdue": 0})
        rt = by_route.setdefault(r["route_id"],
                                 {"route_id": r["route_id"], "in_progress": 0,
                                  "completed": 0, "violated": 0, "overdue": 0})
        key = r["status"].lower()
        if key in z:
            z[key] += 1
            rt[key] += 1
        is_overdue = (
            r["status"] == "IN_PROGRESS" and r["entered_at"] and r["max_stay_seconds"]
            and now > _dwell_deadline(r["entered_at"], r["max_stay_seconds"])
        )
        if is_overdue:
            z["overdue"] += 1
            rt["overdue"] += 1
            overdue += 1
    return {
        "now": now,
        "total": len(rows),
        "in_progress": sum(1 for r in rows if r["status"] == "IN_PROGRESS"),
        "completed": sum(1 for r in rows if r["status"] == "COMPLETED"),
        "violated": sum(1 for r in rows if r["status"] == "VIOLATED"),
        "overdue": overdue,
        "open_conflicts": conn.execute(
            "SELECT COUNT(*) c FROM route_event_conflicts WHERE status='OPEN'"
        ).fetchone()["c"],
        "routes": conn.execute("SELECT COUNT(*) c FROM routes").fetchone()["c"],
        "by_zone": list(by_zone.values()),
        "by_route": list(by_route.values()),
    }
