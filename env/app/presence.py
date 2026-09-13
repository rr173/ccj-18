"""访客在场清册 + 应急清点。

设计要点
--------
1. 每张票在 ``presence`` 表至多一行，状态机
   ``ARRIVED``（在场）→ ``DEPARTED``（门点确认离场）
                    ↘ ``REMOVED``（管理员误扫更正，从在场名单移除）。
   DEPARTED/REMOVED 下门点自动路径（核销/离场）不能再写入，**已经离场的人
   不可能因重放、并发或补扫又出现在当前在场名单**；管理员要纠正只能走带
   原因的人工更正（RESTORE/MARK_DEPARTED），原轨迹全部保留。

2. 到场 / 离场 / 每一次更正都向 append-only 的 ``presence_events`` 追加一条
   （票内 ``seq`` 单调），同时写一条全局 ``events``（``id`` 即版本号）。
   所有写动作都在调用方的 ``BEGIN IMMEDIATE`` 事务内，与核销、变更换发、
   快照发起共用同一个写锁——同一张票的到场、离场、人工更正只能排成一个
   明确顺序，不存在交叉。

3. 到场瞬间冻结整组人数与同行人名单（取自申请当前资料），之后申请变更
   只能换发新票、且旧票已核销后根本不允许变更，所以在场名单与清点快照
   不会被后续资料修改改写。

4. 应急清点快照（``rollcalls`` / ``rollcall_entries``）在发起事务内把当时
   全部在场组逐行复制落盘；快照行只插入、从不更新/删除。之后的到场、
   离场、更正只追加新事件，旧快照任何字段都不变（含批次分区与最后门点）。

5. 门点离场扫码与核销一样带 ``attempt_id``：(gate_id, attempt_id) 唯一
   约束保证离线重发只离场一次，重放返回首次结果。

门点动作（kind ∈ ARRIVED/DEPARTED）由核销/离场接口产生；
人工更正（kind ∈ MARK_ARRIVED/MARK_DEPARTED/REMOVE/RESTORE）只能管理员发起，
必须带原因，轨迹记录操作者。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import Optional

from .db import add_event, event_dict, iso, utcnow, write_tx

ROLLCALL_PREFIX = "R-"

# 在场轨迹类型
KIND_ARRIVED = "ARRIVED"                  # 门点核销成功自动登记
KIND_DEPARTED = "DEPARTED"                # 门点确认离场
KIND_MARK_ARRIVED = "MARK_ARRIVED"        # 人工更正：漏扫补登记到场
KIND_MARK_DEPARTED = "MARK_DEPARTED"      # 人工更正：漏扫离场
KIND_REMOVE = "REMOVE"                    # 人工更正：误扫，从在场名单移除
KIND_RESTORE = "RESTORE"                  # 人工更正：纠正误离场/误移除

GATE_KINDS = (KIND_ARRIVED, KIND_DEPARTED)
MANUAL_KINDS = (KIND_MARK_ARRIVED, KIND_MARK_DEPARTED, KIND_REMOVE, KIND_RESTORE)

# 每种更正允许的起始状态（None 表示此前没有在场行——用于旧数据补登记）
CORRECTION_FROM = {
    KIND_MARK_ARRIVED: (None, "DEPARTED", "REMOVED"),
    KIND_MARK_DEPARTED: ("ARRIVED",),
    KIND_REMOVE: ("ARRIVED",),
    KIND_RESTORE: ("DEPARTED", "REMOVED"),
}

REASON_TEXT = {
    "present": "在场",
    "departed": "已离场",
    "removed": "误扫已移除",
    "not_present": "该票当前不在场（已离场或未登记到场）",
    "already_present": "该票已登记到场，不能重复登记",
    "no_presence": "该票没有到场记录，不能登记离场（请用人工补登记到场）",
    "invalid_transition": "当前在场状态不允许该操作",
}


def _gen_rollcall_id() -> str:
    return ROLLCALL_PREFIX + secrets.token_hex(5).upper()


def get_presence(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM presence WHERE ticket_code=?", (code,)
    ).fetchone()


def presence_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["companion_names"] = json.loads(d.get("companion_names") or "[]")
    except json.JSONDecodeError:
        d["companion_names"] = []
    return d


def presence_event_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["operator_kind"] = (
            "gate" if d.get("kind") in GATE_KINDS
            else ("manual" if d.get("kind") in MANUAL_KINDS else "system")
        )
    except Exception:  # pragma: no cover
        pass
    return d


def _next_presence_seq(conn: sqlite3.Connection, code: str) -> int:
    return int(conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 s FROM presence_events WHERE ticket_code=?",
        (code,),
    ).fetchone()["s"])


def _ticket_party_snapshot(
    conn: sqlite3.Connection, ticket: sqlite3.Row
) -> dict:
    """到场瞬间冻结的整组信息：有申请关联则取申请资料，否则按单人票。"""
    app = None
    if ticket["application_id"]:
        app = conn.execute(
            "SELECT * FROM applications WHERE id=?",
            (ticket["application_id"],),
        ).fetchone()
    if app is not None:
        try:
            names = json.loads(app["companion_names"] or "[]")
        except json.JSONDecodeError:
            names = []
        return {
            "person_id": app["id"],
            "application_id": app["id"],
            "batch_id": app["batch_id"],
            "zone_id": (ticket["zones"] and _first_zone(ticket)) or None,
            "party_size": app["party_size"],
            "companions": app["companions"],
            "companion_names": names,
            "applicant_name": app["name"],
        }
    # 非预约票：分区取票面第一个授权分区（单人通行，无同行名单）
    return {
        "person_id": ticket["person_id"],
        "application_id": None,
        "batch_id": ticket["batch_id"],
        "zone_id": _first_zone(ticket),
        "party_size": 1,
        "companions": 0,
        "companion_names": [],
        "applicant_name": ticket["person_id"],
    }


def _first_zone(ticket: sqlite3.Row) -> Optional[str]:
    try:
        zones = json.loads(ticket["zones"] or "[]")
    except json.JSONDecodeError:
        return None
    return zones[0] if zones else None


# ---------------- 到场（核销同事务调用） ----------------

def register_arrival_locked(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    gate_id: str,
    attempt_id: str,
    now: str,
    policy_version: int,
) -> Optional[int]:
    """已在核销的 IMMEDIATE 事务内：把成功核销登记为到场。

    返回 PRESENCE_ARRIVED 全局事件版本；已有在场行（人工 REMOVED 或已
    DEPARTED）时不自动复活，返回 None，由调用方在核销响应里标注
    presence 状态。核销本身仍成功（票状态机与在场状态机相互独立）。
    """
    code = ticket["code"]
    existing = get_presence(conn, code)
    if existing is not None:
        return None

    snap = _ticket_party_snapshot(conn, ticket)
    seq = 1
    # 先写全局版本事件，再写票内轨迹（version 指向前者，门点按版本补齐）
    version = add_event(
        conn,
        "PRESENCE_ARRIVED",
        ts=now,
        ticket_code=code,
        person_id=snap["person_id"],
        gate_id=gate_id,
        reason="gate_scan",
        payload={
            "gate_id": gate_id,
            "attempt_id": attempt_id,
            "zone_id": snap["zone_id"],
            "policy_version": policy_version,
            "party_size": snap["party_size"],
            "companions": snap["companions"],
            "companion_names": snap["companion_names"],
            "applicant_name": snap["applicant_name"],
            "batch_id": snap["batch_id"],
            "application_id": snap["application_id"],
            "seq": seq,
        },
        batch_id=snap["batch_id"],
        application_id=snap["application_id"],
    )
    cur = conn.execute(
        """INSERT INTO presence_events
               (ticket_code,person_id,seq,kind,from_status,to_status,reason,
                operator,gate_id,version,attempt_id,ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, snap["person_id"], seq, KIND_ARRIVED, None, "ARRIVED",
         "gate_scan", f"gate:{gate_id}", gate_id, version, attempt_id, now),
    )
    event_id = int(cur.lastrowid)
    conn.execute(
        """INSERT INTO presence
               (ticket_code,person_id,application_id,batch_id,zone_id,status,
                party_size,companions,companion_names,applicant_name,
                arrived_at,arrived_gate,arrived_version,arrived_event_id)
           VALUES (?,?,?,?,?,'ARRIVED',?,?,?,?,?,?,?,?)""",
        (code, snap["person_id"], snap["application_id"], snap["batch_id"],
         snap["zone_id"], snap["party_size"], snap["companions"],
         json.dumps(snap["companion_names"], ensure_ascii=False),
         snap["applicant_name"], now, gate_id, version, event_id),
    )
    return version


# ---------------- 离场（门点确认） ----------------

def gate_departure(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    raw_code: str,
    attempt_id: str,
) -> dict:
    """门点确认持票人离场。

    - (gate_id, attempt_id) 幂等：离线重发返回首次结果（replayed=True）；
    - 只有当前 ARRIVED 的票可离场；未到场/已离场/误扫移除分别给出明确
      业务结论，且**任何路径都不会把已离场的人重新放回在场名单**。
    """
    from .services import normalize_code  # 避免模块加载期循环依赖

    now = iso(utcnow())
    code = normalize_code(raw_code)

    # 1) 幂等：同一门点同一 attempt_id 重放第一次的结果（含失败结论），
    #    与 /api/gate/redeem 的 scan_attempts 回放语义一致
    prior = conn.execute(
        "SELECT * FROM scan_attempts WHERE gate_id=? AND attempt_id=?",
        (gate_id, attempt_id),
    ).fetchone()
    if prior is not None:
        response = json.loads(prior["result_json"])
        response["replayed"] = True
        return {"http_status": prior["http_status"], "response": response}

    with write_tx(conn):
        # 事务内复查（并发重放）
        prior = conn.execute(
            "SELECT * FROM scan_attempts WHERE gate_id=? AND attempt_id=?",
            (gate_id, attempt_id),
        ).fetchone()
        if prior is not None:
            response = json.loads(prior["result_json"])
            response["replayed"] = True
            return {"http_status": prior["http_status"], "response": response}

        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (code,)
        ).fetchone()
        presence = get_presence(conn, code)

        if ticket is None:
            response = {
                "ok": False, "status": "NOT_FOUND", "code": code,
                "gate_id": gate_id, "ts": now,
                "reason": "not_found",
                "reason_text": "票面不存在，无法登记离场",
            }
            http_status = 404
        elif presence is None:
            response = {
                "ok": False, "status": "NO_PRESENCE", "code": code,
                "gate_id": gate_id, "ts": now,
                "person_id": ticket["person_id"],
                "reason": "no_presence",
                "reason_text": REASON_TEXT["no_presence"],
            }
            http_status = 409
        elif presence["status"] != "ARRIVED":
            response = {
                "ok": False,
                "status": "DEPARTED" if presence["status"] == "DEPARTED" else "REMOVED",
                "code": code, "gate_id": gate_id, "ts": now,
                "person_id": presence["person_id"],
                "reason": "not_present",
                "reason_text": REASON_TEXT["not_present"],
                "presence_status": presence["status"],
                "departed_at": presence["departed_at"],
                "removed_at": presence["removed_at"],
            }
            http_status = 409
        else:
            http_status, response, _, _ = _apply_departure_locked(
                conn,
                presence=presence,
                kind=KIND_DEPARTED,
                reason="gate_scan",
                operator=f"gate:{gate_id}",
                gate_id=gate_id,
                attempt_id=attempt_id,
                now=now,
            )

        zone_id = gate_zone_id(conn, gate_id)
        response.setdefault("zone_id", zone_id)

        # 与核销一致：门点每次离场尝试都落 scan_attempts（含失败/重放结论）
        conn.execute(
            """INSERT INTO scan_attempts
                   (gate_id,attempt_id,code,at,http_status,ok,status,result_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (gate_id, attempt_id, code, now, http_status,
             1 if response["ok"] else 0,
             response["status"],
             json.dumps(response, ensure_ascii=False, sort_keys=True)),
        )
    return {"http_status": http_status, "response": response}


def gate_zone_id(conn: sqlite3.Connection, gate_id: str) -> Optional[str]:
    g = conn.execute("SELECT zone_id FROM gates WHERE id=?", (gate_id,)).fetchone()
    return g["zone_id"] if g else None


def _apply_departure_locked(
    conn: sqlite3.Connection,
    *,
    presence: sqlite3.Row,
    kind: str,
    reason: str,
    operator: str,
    gate_id: Optional[str],
    attempt_id: Optional[str],
    now: str,
) -> tuple[int, dict]:
    """在写事务内：追加离场轨迹并把 presence 置 DEPARTED。

    既用于门点确认（kind=DEPARTED），也用于管理员漏扫离场更正
    （kind=MARK_DEPARTED，进 PRESENCE_CORRECTED 事件）。
    """
    code = presence["ticket_code"]
    seq = _next_presence_seq(conn, code)
    event_type = "PRESENCE_DEPARTED" if kind == KIND_DEPARTED else "PRESENCE_CORRECTED"
    version = add_event(
        conn,
        event_type,
        ts=now,
        ticket_code=code,
        person_id=presence["person_id"],
        gate_id=gate_id,
        reason=reason,
        payload={
            "kind": kind, "seq": seq, "operator": operator,
            "gate_id": gate_id, "attempt_id": attempt_id,
            "from_status": "ARRIVED", "to_status": "DEPARTED",
            "party_size": presence["party_size"],
            "batch_id": presence["batch_id"],
            "application_id": presence["application_id"],
        },
        batch_id=presence["batch_id"],
        application_id=presence["application_id"],
    )
    cur = conn.execute(
        """INSERT INTO presence_events
               (ticket_code,person_id,seq,kind,from_status,to_status,reason,
                operator,gate_id,version,attempt_id,ts)
           VALUES (?,?,?,?,'ARRIVED','DEPARTED',?,?,?,?,?,?)""",
        (code, presence["person_id"], seq, kind, reason, operator,
         gate_id, version, attempt_id, now),
    )
    event_id = int(cur.lastrowid)
    conn.execute(
        """UPDATE presence
              SET status='DEPARTED', departed_at=?, departed_gate=?,
                  departed_version=?, departed_event_id=?
            WHERE ticket_code=?""",
        (now, gate_id, version, event_id, code),
    )
    response = {
        "ok": True, "status": "DEPARTED",
        "code": code, "gate_id": gate_id, "ts": now,
        "person_id": presence["person_id"],
        "departed_at": now, "departed_gate": gate_id,
        "version": version,
        "reason_text": "离场已确认" if kind == KIND_DEPARTED
                       else f"人工登记离场：{reason}",
        "presence_status": "DEPARTED",
    }
    if kind != KIND_DEPARTED:
        response["manual"] = True
    return 200, response, version, seq


# ---------------- 人工更正（管理员，带原因 + 操作者） ----------------

def manual_correction(
    conn: sqlite3.Connection,
    *,
    raw_code: str,
    action: str,
    reason: str,
    operator: str,
    party_size: Optional[int] = None,
    companions: Optional[int] = None,
    companion_names: Optional[list[str]] = None,
    gate_id: Optional[str] = None,
) -> dict:
    """管理员对漏扫/误扫做人工更正。

    action ∈
      MARK_ARRIVED  漏扫补登记到场（旧数据无在场行；或把误离场/误移除纠正回场）
      MARK_DEPARTED 漏扫离场（当前在场但门点没扫到）
      REMOVE        误扫：票根本不该出现，从当前在场名单移除
      RESTORE       纠正一次错误的离场/移除（重新计入在场）

    所有更正：
      * 必须带原因（落 presence_events + PRESENCE_CORRECTED 全局事件）；
      * 只追加轨迹，从不删除/改写既有的到场、离场轨迹；
      * 在 BEGIN IMMEDIATE 事务内与门点动作串行，只有一个明确顺序；
      * 并发冲突（状态已被别的动作改变）返回 409，不会产生半截状态。
    """
    from .services import normalize_code

    now = iso(utcnow())
    code = normalize_code(raw_code)
    if action not in CORRECTION_FROM:
        return {"http_status": 400, "error": f"未知更正动作: {action}"}
    if not reason or not reason.strip():
        return {"http_status": 400, "error": "人工更正必须填写原因"}
    reason = reason.strip()

    with write_tx(conn):
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (code,)
        ).fetchone()
        if ticket is None:
            return {"http_status": 404, "error": "票面不存在"}
        presence = get_presence(conn, code)
        cur_status = presence["status"] if presence is not None else None
        allowed = CORRECTION_FROM[action]
        if cur_status not in allowed:
            return {
                "http_status": 409,
                "error": (
                    f"当前在场状态为 {cur_status or '无记录'}，"
                    f"不允许 {action}（允许的起始状态："
                    f"{', '.join(s or '无记录' for s in allowed)}）"
                ),
                "presence_status": cur_status,
            }
        if gate_id is not None and conn.execute(
            "SELECT 1 FROM gates WHERE id=?", (gate_id,)
        ).fetchone() is None:
            return {"http_status": 400, "error": f"未知门点: {gate_id}"}

        if action == KIND_MARK_DEPARTED:
            http_status, response, ver, seq = _apply_departure_locked(
                conn,
                presence=presence,
                kind=KIND_MARK_DEPARTED,
                reason=reason,
                operator=operator,
                gate_id=gate_id,
                attempt_id=None,
                now=now,
            )
            final = get_presence(conn, code)
            return {
                "http_status": http_status,
                "ok": True,
                "presence": presence_dict(final),
                "departure": {k: v for k, v in response.items()
                              if k in ("departed_at", "departed_gate", "version")},
                "correction": {
                    "action": action, "reason": reason, "operator": operator,
                    "seq": seq, "version": ver,
                },
                "reason_text": response["reason_text"],
            }

        # MARK_ARRIVED / REMOVE / RESTORE 都是“落到 ARRIVED 或 REMOVED”的
        # 轨迹追加；下面统一处理（含无在场行的补登记）。
        if action == KIND_MARK_ARRIVED and presence is None:
            # 漏扫补登记：默认按票面/申请冻结整组信息，允许管理员覆盖人数/名单
            snap = _ticket_party_snapshot(conn, ticket)
            if party_size is not None:
                names = _validate_names(companion_names, party_size - 1)
                if isinstance(names, dict):
                    return names
                snap["party_size"] = party_size
                snap["companions"] = party_size - 1
                snap["companion_names"] = names
            ticket_redeem_version = _redeem_active_ticket_manual(
                conn, ticket=ticket, reason=reason, operator=operator,
                gate_id=gate_id, now=now,
            )
            row = _insert_arrival_correction(
                conn, ticket=ticket, snap=snap, reason=reason,
                operator=operator, gate_id=gate_id, now=now,
                ticket_redeem_version=ticket_redeem_version,
            )
            return {"http_status": 200, **row}

        if action == KIND_MARK_ARRIVED:
            # 已有 DEPARTED/REMOVED 行：管理员补登记到场等价于“纠正回场”，
            # 原离场/移除轨迹保留，状态机只追加一条更正。
            return _apply_restore_locked(
                conn, presence=presence, reason=reason, operator=operator,
                gate_id=gate_id, now=now, kind=KIND_MARK_ARRIVED,
            )

        if action == KIND_REMOVE:
            return _apply_remove_locked(
                conn, presence=presence, reason=reason, operator=operator,
                gate_id=gate_id, now=now,
            )

        # RESTORE：纠正误离场/误移除，重新计入在场；原离场/移除轨迹保留
        return _apply_restore_locked(
            conn, presence=presence, reason=reason, operator=operator,
            gate_id=gate_id, now=now,
        )


def _validate_names(names: Optional[list[str]], companions: int):
    if names is None:
        return [""] * companions
    if not isinstance(names, list):
        return {"http_status": 400, "error": "companion_names 必须是字符串数组"}
    clean = [str(x).strip() for x in names]
    if len(clean) != companions:
        return {"http_status": 400,
                "error": f"同行人名单数量 {len(clean)} 与同行人数 {companions} 不一致"}
    return clean


def _correction_event(
    conn: sqlite3.Connection,
    *,
    code: str,
    presence: Optional[sqlite3.Row],
    person_id: str,
    kind: str,
    from_status: Optional[str],
    to_status: str,
    reason: str,
    operator: str,
    gate_id: Optional[str],
    now: str,
    payload_extra: Optional[dict] = None,
    batch_id: Optional[str] = None,
    application_id: Optional[str] = None,
) -> tuple[int, int, int]:
    """追加一条人工更正：全局 PRESENCE_CORRECTED 事件 + 票内轨迹。

    返回 (seq, global_version, presence_event_id)。
    """
    seq = _next_presence_seq(conn, code)
    payload = {
        "kind": kind, "seq": seq, "operator": operator,
        "gate_id": gate_id, "reason": reason,
        "from_status": from_status, "to_status": to_status,
    }
    if payload_extra:
        payload.update(payload_extra)
    version = add_event(
        conn,
        "PRESENCE_CORRECTED",
        ts=now,
        ticket_code=code,
        person_id=person_id,
        gate_id=gate_id,
        reason=reason,
        payload=payload,
        batch_id=batch_id or (presence["batch_id"] if presence else None),
        application_id=(application_id
                        or (presence["application_id"] if presence else None)),
    )
    cur = conn.execute(
        """INSERT INTO presence_events
               (ticket_code,person_id,seq,kind,from_status,to_status,reason,
                operator,gate_id,version,attempt_id,ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)""",
        (code, person_id, seq, kind, from_status, to_status, reason,
         operator, gate_id, version, now),
    )
    return seq, version, int(cur.lastrowid)


def _redeem_active_ticket_manual(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    reason: str,
    operator: str,
    gate_id: Optional[str],
    now: str,
) -> Optional[int]:
    """人工补登记到场且票仍 ACTIVE：同事务把票置 REDEEMED（漏扫核销）。

    与门点核销竞争时以本写事务内复查为准：已被并发核销/作废/过期则不动票。
    返回 TICKET_REDEEMED 事件版本（票本就非 ACTIVE 时返回 None）。
    """
    if ticket["status"] != "ACTIVE":
        return None
    if now >= ticket["valid_until"]:
        # 到点未清扫：人工登记到场不改写有效期语义，先按过期落事件
        add_event(
            conn,
            "TICKET_EXPIRED",
            ts=now,
            ticket_code=ticket["code"],
            person_id=ticket["person_id"],
            reason="valid_until_reached",
            payload={"valid_until": ticket["valid_until"],
                     "manual_presence_after_expiry": True},
            batch_id=ticket["batch_id"],
            application_id=ticket["application_id"],
        )
        conn.execute(
            "UPDATE tickets SET status='EXPIRED', expired_at=? WHERE code=?",
            (now, ticket["code"]),
        )
        return None
    version = add_event(
        conn,
        "TICKET_REDEEMED",
        ts=now,
        ticket_code=ticket["code"],
        person_id=ticket["person_id"],
        gate_id=gate_id,
        reason="manual_missed_scan",
        payload={"manual": True, "operator": operator,
                 "correction_reason": reason, "gate_id": gate_id},
        batch_id=ticket["batch_id"],
        application_id=ticket["application_id"],
    )
    conn.execute(
        """UPDATE tickets
              SET status='REDEEMED', redeemed_at=?, redeemed_gate=?
            WHERE code=? AND status='ACTIVE'""",
        (now, gate_id, ticket["code"]),
    )
    return version


def _insert_arrival_correction(
    conn: sqlite3.Connection,
    *,
    ticket: sqlite3.Row,
    snap: dict,
    reason: str,
    operator: str,
    gate_id: Optional[str],
    now: str,
    ticket_redeem_version: Optional[int] = None,
) -> dict:
    """无在场行时的漏扫补登记：seq=1，直接建 ARRIVED 行（轨迹标明人工）。"""
    code = ticket["code"]
    seq, version, event_id = _correction_event(
        conn,
        code=code, presence=None, person_id=snap["person_id"],
        kind=KIND_MARK_ARRIVED, from_status=None, to_status="ARRIVED",
        reason=reason, operator=operator, gate_id=gate_id, now=now,
        payload_extra={
            "party_size": snap["party_size"], "companions": snap["companions"],
            "companion_names": snap["companion_names"],
            "applicant_name": snap["applicant_name"],
            "zone_id": snap["zone_id"],
            **({"ticket_redeem_version": ticket_redeem_version}
               if ticket_redeem_version else {}),
        },
        batch_id=snap["batch_id"], application_id=snap["application_id"],
    )
    conn.execute(
        """INSERT INTO presence
               (ticket_code,person_id,application_id,batch_id,zone_id,status,
                party_size,companions,companion_names,applicant_name,
                arrived_at,arrived_gate,arrived_version,arrived_event_id,
                corrected_seq)
           VALUES (?,?,?,?,?,'ARRIVED',?,?,?,?,?,?,?,?,?)""",
        (code, snap["person_id"], snap["application_id"], snap["batch_id"],
         snap["zone_id"], snap["party_size"], snap["companions"],
         json.dumps(snap["companion_names"], ensure_ascii=False),
         snap["applicant_name"], now, gate_id, version, event_id, seq),
    )
    row = get_presence(conn, code)
    return {
        "ok": True, "presence": presence_dict(row),
        "correction": {"action": KIND_MARK_ARRIVED, "reason": reason,
                       "operator": operator, "seq": seq, "version": version},
        "reason_text": f"人工补登记到场：{reason}",
    }


def _apply_remove_locked(
    conn: sqlite3.Connection,
    *,
    presence: sqlite3.Row,
    reason: str,
    operator: str,
    gate_id: Optional[str],
    now: str,
) -> dict:
    code = presence["ticket_code"]
    seq, version, _ = _correction_event(
        conn,
        code=code, presence=presence, person_id=presence["person_id"],
        kind=KIND_REMOVE, from_status="ARRIVED", to_status="REMOVED",
        reason=reason, operator=operator, gate_id=gate_id, now=now,
    )
    conn.execute(
        """UPDATE presence
              SET status='REMOVED', removed_at=?, removed_reason=?,
                  removed_by=?, corrected_seq=?
            WHERE ticket_code=?""",
        (now, reason, operator, seq, code),
    )
    row = get_presence(conn, code)
    return {
        "http_status": 200,
        "ok": True, "presence": presence_dict(row),
        "correction": {"action": KIND_REMOVE, "reason": reason,
                       "operator": operator, "seq": seq, "version": version},
        "reason_text": f"误扫已从在场名单移除：{reason}",
    }


def _apply_restore_locked(
    conn: sqlite3.Connection,
    *,
    presence: sqlite3.Row,
    reason: str,
    operator: str,
    gate_id: Optional[str],
    now: str,
    kind: str = KIND_RESTORE,
) -> dict:
    """RESTORE / 对已离场行补登记到场：保留原轨迹，把人重新计为在场。

    不触碰原 arrived_* 字段；原离场/移除事件仍在轨迹与全局版本流中。
    """
    code = presence["ticket_code"]
    seq, version, _ = _correction_event(
        conn,
        code=code, presence=presence, person_id=presence["person_id"],
        kind=kind, from_status=presence["status"], to_status="ARRIVED",
        reason=reason, operator=operator, gate_id=gate_id, now=now,
    )
    conn.execute(
        """UPDATE presence
              SET status='ARRIVED', departed_at=NULL, departed_gate=NULL,
                  departed_version=NULL, departed_event_id=NULL,
                  removed_at=?, removed_reason=NULL, removed_by=?,
                  corrected_seq=?
            WHERE ticket_code=?""",
        # removed_at 列复用记录“最近一次更正时间”（原移除原因保存在轨迹里，不丢）
        (now, operator, seq, code),
    )
    row = get_presence(conn, code)
    text = (f"已纠正并重新计入在场：{reason}" if kind == KIND_RESTORE
            else f"人工补登记到场：{reason}")
    return {
        "http_status": 200,
        "ok": True, "presence": presence_dict(row),
        "correction": {"action": kind, "reason": reason,
                       "operator": operator, "seq": seq, "version": version},
        "reason_text": text,
    }


# ---------------- 当前在场清册 / 查询 ----------------

def list_presence(
    conn: sqlite3.Connection,
    *,
    status_filter: str = "ONSITE",
    zone_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    person_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """在场清册查询。

    status_filter:
      ONSITE           当前在场（status='ARRIVED'）——默认，即“当前在场”
      UNCONFIRMED      未确认离场（= 已到场但门点未确认；当前同样是 ARRIVED，
                       与 ONSITE 同集合，单独命名以对应清点口径）
      DEPARTED         已确认离场
      REMOVED          误扫移除
      ALL              全部
    """
    sql = "SELECT * FROM presence WHERE 1=1"
    params: list = []
    if status_filter in ("ONSITE", "UNCONFIRMED"):
        sql += " AND status='ARRIVED'"
    elif status_filter in ("DEPARTED", "REMOVED", "ARRIVED"):
        sql += " AND status=?"
        params.append(status_filter)
    elif status_filter == "ALL":
        pass
    else:
        raise ValueError(f"未知在场过滤: {status_filter}")
    if zone_id:
        sql += " AND zone_id=?"
        params.append(zone_id)
    if batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
    if person_id:
        sql += " AND person_id=?"
        params.append(person_id)
    if q:
        sql += (" AND (person_id LIKE ? OR applicant_name LIKE ?"
                " OR ticket_code LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    sql += " ORDER BY arrived_at DESC, ticket_code DESC LIMIT ?"
    params.append(min(limit, 2000))
    rows = conn.execute(sql, params).fetchall()
    return [presence_dict(r) for r in rows]


def roster_summary(conn: sqlite3.Connection) -> dict:
    """清册总览：当前在场组数/人数（含同行人），按分区、批次拆分。"""
    rows = conn.execute(
        """SELECT zone_id,
                  COUNT(*)            AS groups,
                  COALESCE(SUM(party_size),0) AS people
             FROM presence WHERE status='ARRIVED'
            GROUP BY zone_id"""
    ).fetchall()
    by_zone = {r["zone_id"] or "":
               {"zone_id": r["zone_id"], "groups": r["groups"],
                "people": int(r["people"])} for r in rows}
    brows = conn.execute(
        """SELECT batch_id,
                  COUNT(*)            AS groups,
                  COALESCE(SUM(party_size),0) AS people
             FROM presence WHERE status='ARRIVED'
            GROUP BY batch_id"""
    ).fetchall()
    by_batch = {r["batch_id"] or "":
                {"batch_id": r["batch_id"], "groups": r["groups"],
                 "people": int(r["people"])} for r in brows}
    totals = conn.execute(
        """SELECT COUNT(*) groups, COALESCE(SUM(party_size),0) people,
                  COALESCE(SUM(companions),0) companions
             FROM presence WHERE status='ARRIVED'"""
    ).fetchone()
    departed = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(party_size),0) p FROM presence "
        "WHERE status='DEPARTED'"
    ).fetchone()
    return {
        "onsite_groups": totals["groups"],
        "onsite_people": int(totals["people"]),
        "onsite_companions": int(totals["companions"]),
        "departed_groups": departed["c"],
        "departed_people": int(departed["p"]),
        "by_zone": list(by_zone.values()),
        "by_batch": list(by_batch.values()),
    }


def list_presence_events(
    conn: sqlite3.Connection,
    *,
    code: Optional[str] = None,
    person_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """原始在场轨迹（含人工更正前的原始到场/离场），按版本倒序。"""
    sql = "SELECT * FROM presence_events WHERE 1=1"
    params: list = []
    if code:
        from .services import normalize_code
        sql += " AND ticket_code=?"
        params.append(normalize_code(code))
    if person_id:
        sql += " AND person_id=?"
        params.append(person_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 1000))
    return [presence_event_dict(r) for r in conn.execute(sql, params).fetchall()]


def presence_detail(conn: sqlite3.Connection, code: str) -> Optional[dict]:
    """单票在场详情：当前状态 + 完整轨迹 + 关联票/申请/批次/门点名。"""
    from .services import normalize_code

    code = normalize_code(code)
    row = get_presence(conn, code)
    ticket = conn.execute(
        "SELECT * FROM tickets WHERE code=?", (code,)
    ).fetchone()
    if row is None and ticket is None:
        return None
    trail = [
        presence_event_dict(r) for r in conn.execute(
            "SELECT * FROM presence_events WHERE ticket_code=? ORDER BY seq",
            (code,),
        ).fetchall()
    ]
    gate_names = {g["id"]: g["name"]
                  for g in conn.execute("SELECT id,name FROM gates").fetchall()}
    for t in trail:
        t["gate_name"] = gate_names.get(t["gate_id"])
    out = {
        "ticket_code": code,
        "presence": presence_dict(row) if row else None,
        "trail": trail,
    }
    if ticket is not None:
        out["ticket_status"] = ticket["status"]
        if ticket["application_id"]:
            app = conn.execute(
                "SELECT id,name,contact,change_version FROM applications WHERE id=?",
                (ticket["application_id"],),
            ).fetchone()
            if app:
                out["application"] = dict(app)
        if ticket["batch_id"]:
            b = conn.execute(
                "SELECT id,name,visit_date,zone_id FROM batches WHERE id=?",
                (ticket["batch_id"],),
            ).fetchone()
            if b:
                out["batch"] = dict(b)
    return out


# ---------------- 应急清点快照 ----------------

def _onsite_rows(conn: sqlite3.Connection, *,
                 zone_id: Optional[str], batch_id: Optional[str]
                 ) -> list[sqlite3.Row]:
    sql = "SELECT * FROM presence WHERE status='ARRIVED'"
    params: list = []
    if zone_id:
        sql += " AND zone_id=?"
        params.append(zone_id)
    if batch_id:
        sql += " AND batch_id=?"
        params.append(batch_id)
    sql += " ORDER BY zone_id, batch_id, arrived_at"
    return conn.execute(sql, params).fetchall()


def _last_gate_for(conn: sqlite3.Connection, code: str) -> dict:
    """快照固定的“最后门点记录”：该票最近一条带门点的在场轨迹。"""
    row = conn.execute(
        """SELECT * FROM presence_events
            WHERE ticket_code=? AND gate_id IS NOT NULL
            ORDER BY seq DESC LIMIT 1""",
        (code,),
    ).fetchone()
    if row is None:
        # 没有门点轨迹（纯人工补登记）：回退到场登记门点（也可能为 NULL）
        p = get_presence(conn, code)
        return {"last_gate": p["arrived_gate"] if p else None,
                "last_event_kind": KIND_MARK_ARRIVED,
                "last_event_ts": p["arrived_at"] if p else None,
                "last_event_version": p["arrived_version"] if p else None,
                "presence_seq": 1}
    return {"last_gate": row["gate_id"], "last_event_kind": row["kind"],
            "last_event_ts": row["ts"], "last_event_version": row["version"],
            "presence_seq": row["seq"]}


def take_rollcall(
    conn: sqlite3.Connection,
    *,
    operator: str,
    reason: Optional[str] = None,
    zone_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict:
    """发起一次应急清点：事务内把当时全部在场组冻结成快照。

    返回后快照内容与当前状态完全脱钩：之后到场/离场/更正只写新事件、
    新快照，本快照行永不更新。
    """
    now = iso(utcnow())
    with write_tx(conn):
        if zone_id is not None and conn.execute(
            "SELECT 1 FROM zones WHERE id=?", (zone_id,)
        ).fetchone() is None:
            return {"http_status": 400, "error": f"未知分区: {zone_id}"}
        batch_name = None
        if batch_id is not None:
            b = conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
            if b is None:
                return {"http_status": 404, "error": "批次不存在"}
            batch_name = b["name"]

        rows = _onsite_rows(conn, zone_id=zone_id, batch_id=batch_id)
        rc_id = _gen_rollcall_id()
        # 先落快照事件拿到全局版本（门点离线按版本能补齐“发起过清点”）
        version = add_event(
            conn,
            "ROLLCALL_TAKEN",
            ts=now,
            reason=reason or "emergency_rollcall",
            batch_id=batch_id,
            rollcall_id=rc_id,
            payload={
                "rollcall_id": rc_id,
                "zone_id": zone_id, "batch_id": batch_id,
                "groups": len(rows),
                "headcount": int(sum(r["party_size"] for r in rows)),
                "operator": operator,
            },
        )
        scope = {"zone_id": zone_id, "batch_id": batch_id,
                 "batch_name": batch_name}
        conn.execute(
            """INSERT INTO rollcalls
                   (id,reason,zone_id,batch_id,created_at,created_by,version,
                    headcount,groups,scope_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rc_id, reason, zone_id, batch_id, now, operator, version,
             int(sum(r["party_size"] for r in rows)), len(rows),
             json.dumps(scope, ensure_ascii=False)),
        )
        for r in rows:
            last = _last_gate_for(conn, r["ticket_code"])
            try:
                names = json.loads(r["companion_names"] or "[]")
            except json.JSONDecodeError:
                names = []
            conn.execute(
                """INSERT INTO rollcall_entries
                       (rollcall_id,ticket_code,person_id,application_id,
                        batch_id,zone_id,applicant_name,party_size,companions,
                        companion_names,arrived_at,arrived_gate,last_gate,
                        last_event_kind,last_event_ts,last_event_version,
                        presence_seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rc_id, r["ticket_code"], r["person_id"], r["application_id"],
                 r["batch_id"], r["zone_id"], r["applicant_name"],
                 r["party_size"], r["companions"],
                 json.dumps(names, ensure_ascii=False),
                 r["arrived_at"], r["arrived_gate"], last["last_gate"],
                 last["last_event_kind"], last["last_event_ts"],
                 last["last_event_version"], last["presence_seq"]),
            )
    return {"http_status": 201, "rollcall": get_rollcall(conn, rc_id)}


def _rollcall_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["scope"] = json.loads(d.pop("scope_json") or "{}")
    except json.JSONDecodeError:
        d["scope"] = {}
    return d


def _entry_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["companion_names"] = json.loads(d.get("companion_names") or "[]")
    except json.JSONDecodeError:
        d["companion_names"] = []
    return d


def get_rollcall(conn: sqlite3.Connection, rc_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM rollcalls WHERE id=?", (rc_id,)
    ).fetchone()
    if row is None:
        return None
    entries = [
        _entry_dict(r) for r in conn.execute(
            "SELECT * FROM rollcall_entries WHERE rollcall_id=? "
            "ORDER BY zone_id, batch_id, arrived_at",
            (rc_id,),
        ).fetchall()
    ]
    out = _rollcall_dict(row)
    out["entries"] = entries
    return out


def list_rollcalls(
    conn: sqlite3.Connection,
    *,
    zone_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    person_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """清点列表（不含条目明细）。

    按人员过滤时返回“该人当时在场”的快照（查 rollcall_entries）。
    """
    if person_id:
        rows = conn.execute(
            """SELECT DISTINCT r.* FROM rollcalls r
                 JOIN rollcall_entries e ON e.rollcall_id=r.id
                WHERE e.person_id=?
                ORDER BY r.created_at DESC LIMIT ?""",
            (person_id, min(limit, 500)),
        ).fetchall()
    else:
        sql = "SELECT * FROM rollcalls WHERE 1=1"
        params: list = []
        if zone_id:
            sql += " AND (zone_id=? OR zone_id IS NULL)"
            params.append(zone_id)
        if batch_id:
            sql += " AND (batch_id=? OR batch_id IS NULL)"
            params.append(batch_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(limit, 500))
        rows = conn.execute(sql, params).fetchall()
    return [_rollcall_dict(r) for r in rows]


def find_person_in_rollcall(
    conn: sqlite3.Connection, rc_id: str, person_id: str
) -> Optional[dict]:
    """任一次清点结果中按人取其冻结条目（未在该快照中 -> None）。"""
    row = conn.execute(
        "SELECT * FROM rollcall_entries WHERE rollcall_id=? AND person_id=?",
        (rc_id, person_id),
    ).fetchone()
    return _entry_dict(row) if row else None
