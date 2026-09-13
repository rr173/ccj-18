"""访客授权委托与临时代理核验。

状态与事件均落 SQLite：管理动作、凭证签发/撤销/过期/用尽、门点每次核验和
离线冲突全部写入同一个 append-only ``events`` 版本流。所有写操作都在
``BEGIN IMMEDIATE`` 事务中完成，因此网络重发由 ``(gate_id, attempt_id)``
幂等，多个门点并发使用由数据库单写锁和条件更新串行化，剩余次数不会超扣。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import timedelta
from typing import Optional

from .db import (
    PROXY_CONFLICT_KINDS,
    PROXY_CONFLICT_RESOLUTIONS,
    add_event,
    event_dict,
    iso,
    parse_dt,
    utcnow,
    write_tx,
)

DELEGATION_PREFIX = "D-"
CREDENTIAL_PREFIX = "P-"
FUTURE_TS_SKEW_SECONDS = 60

REASON_TEXT = {
    "ok": "代理核验通过",
    "missing_original_person": "缺少原持票人身份，拒绝通行",
    "missing_proxy_person": "缺少代理人身份，拒绝通行",
    "original_person_mismatch": "原持票人身份与授权不一致",
    "proxy_person_mismatch": "代理人身份与授权不一致",
    "not_yet_valid": "授权委托尚未生效",
    "expired": "授权委托或代理凭证已过期",
    "revoked": "授权委托或代理凭证已撤销",
    "exhausted": "代理凭证可用次数已用尽",
    "zone_mismatch": "代理凭证未授权本门点所属分区",
    "gate_no_policy": "门点未配置分区策略，默认拒绝",
    "unknown_zone": "门点所属分区未知，默认拒绝",
    "locked": "本分区处于紧急封锁中",
    "credential_not_found": "代理凭证不存在",
    "delegation_not_active": "授权委托尚未批准或已终结",
    "original_ticket_required": "授权要求核验原持票人短时票，但未提供票号",
    "original_ticket_not_found": "原持票人短时票不存在",
    "original_ticket_invalid": "原持票人短时票当前不可用",
    "invalid_event_ts": "离线核验事件时间戳异常",
    "future_timestamp": "离线核验事件时间戳在未来",
    "offline_conflict": "离线核验无法自动裁决，已进入冲突队列",
}


def _gen_id(prefix: str, bytes_len: int) -> str:
    return prefix + secrets.token_hex(bytes_len).upper()


def _zones(row: sqlite3.Row) -> list[str]:
    try:
        return json.loads(row["zones_json"] or "[]")
    except json.JSONDecodeError:
        return []


def _credential_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    d["zones"] = _zones(row)
    d.pop("zones_json", None)
    d["high_risk"] = None
    d["remaining_uses"] = max(0, int(row["max_uses"]) - int(row["used_count"]))
    return d


def _delegation_dict(row: sqlite3.Row, *, credential: Optional[sqlite3.Row] = None) -> dict:
    d = dict(row)
    d["zones"] = _zones(row)
    try:
        d["required_approver_ids"] = json.loads(d.pop("approvers_json", "[]") or "[]")
    except json.JSONDecodeError:
        d["required_approver_ids"] = []
        d.pop("approvers_json", None)
    d.pop("zones_json", None)
    d["high_risk"] = bool(row["high_risk"])
    d["credential"] = _credential_dict(credential)
    return d


def _verification_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["offline"] = bool(d["offline"])
    try:
        d["result"] = json.loads(d.get("result_json") or "{}")
    except json.JSONDecodeError:
        d["result"] = {}
    d.pop("result_json", None)
    return d


def _conflict_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["detail"] = json.loads(d.get("detail_json") or "{}")
    except json.JSONDecodeError:
        d["detail"] = {}
    d.pop("detail_json", None)
    return d


def get_delegation(conn: sqlite3.Connection, delegation_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM delegations WHERE id=?", (delegation_id,)
    ).fetchone()


def get_credential(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM proxy_credentials WHERE code=?", (code,)
    ).fetchone()


def normalize_credential_code(code: str) -> str:
    return "".join(str(code).split()).upper()


# ---------------- 管理端：创建 / 审批 / 撤销 ----------------

def create_delegation(
    conn: sqlite3.Connection,
    *,
    original_person_id: str,
    proxy_person_id: str,
    valid_from: str,
    valid_until: str,
    zones: list[str],
    max_uses: int,
    purpose: str,
    high_risk: bool = False,
    ticket_code: Optional[str] = None,
    approvers: Optional[list[str]] = None,
    operator: str = "admin",
    request_id: Optional[str] = None,
) -> dict:
    """创建授权委托；高风险委托必须给两名不同审批管理员。"""
    original = original_person_id.strip()
    proxy = proxy_person_id.strip()
    purpose = purpose.strip()
    if not original or not proxy:
        return {"http_status": 400, "error": "原持票人和代理人都不能为空"}
    if original == proxy:
        return {"http_status": 400, "error": "原持票人和代理人不能是同一人"}
    if not purpose:
        return {"http_status": 400, "error": "用途说明不能为空"}
    if not zones:
        return {"http_status": 400, "error": "至少需要一个允许分区"}
    if max_uses < 1:
        return {"http_status": 400, "error": "最大使用次数必须大于 0"}
    try:
        vf = parse_dt(valid_from)
        vu = parse_dt(valid_until)
    except (TypeError, ValueError) as e:
        return {"http_status": 400, "error": f"时间格式错误: {e}"}
    if vu <= vf:
        return {"http_status": 400, "error": "valid_until 必须晚于 valid_from"}

    approvers = [a.strip() for a in (approvers or []) if a and a.strip()]
    required = 2 if high_risk else 1
    if high_risk:
        if len(set(approvers)) != 2:
            return {"http_status": 400,
                    "error": "高风险委托必须指定两名不同的审批管理员"}
    else:
        approvers = approvers[:1]

    # request_id 仅用于创建请求网络重放；不暴露为业务主键。
    if request_id:
        prior = conn.execute(
            "SELECT * FROM delegations WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior is not None:
            d = _delegation_dict(
                prior,
                credential=get_credential(conn, prior["credential_code"])
                if prior["credential_code"] else None,
            )
            d["http_status"] = 200
            d["replayed"] = True
            return d

    with write_tx(conn):
        for z in zones:
            if conn.execute("SELECT 1 FROM zones WHERE id=?", (z,)).fetchone() is None:
                return {"http_status": 400, "error": f"未知分区: {z}"}
        ticket = None
        if ticket_code:
            ticket = conn.execute(
                "SELECT * FROM tickets WHERE code=?", (ticket_code,)
            ).fetchone()
            if ticket is None:
                return {"http_status": 400, "error": f"原持票人票号不存在: {ticket_code}"}
            if ticket["person_id"] != original:
                return {"http_status": 400,
                        "error": "绑定票号不属于指定的原持票人"}

        now = iso(utcnow())
        delegation_id = _gen_id(DELEGATION_PREFIX, 6)
        zones = list(dict.fromkeys(zones))
        event_version = add_event(
            conn,
            "DELEGATION_CREATED",
            ts=now,
            delegation_id=delegation_id,
            person_id=original,
            reason="admin_create_delegation",
            payload={
                "delegation_id": delegation_id,
                "original_person_id": original,
                "proxy_person_id": proxy,
                "valid_from": iso(vf),
                "valid_until": iso(vu),
                "zones": zones,
                "max_uses": max_uses,
                "purpose": purpose,
                "high_risk": high_risk,
                "ticket_code": ticket_code,
                "required_approvers": required,
                "approvers": approvers,
                "operator": operator,
                "request_id": request_id,
            },
        )
        conn.execute(
            """INSERT INTO delegations
                   (id,status,high_risk,original_person_id,proxy_person_id,
                    valid_from,valid_until,zones_json,max_uses,purpose,ticket_code,
                    required_approvers,approvers_json,created_at,created_by,request_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (delegation_id, "PENDING", 1 if high_risk else 0, original, proxy,
             iso(vf), iso(vu), json.dumps(zones, ensure_ascii=False), max_uses,
             purpose, ticket_code, required,
             json.dumps(approvers, ensure_ascii=False), now, operator, request_id),
        )
        row = get_delegation(conn, delegation_id)
    out = _delegation_dict(row)
    out["http_status"] = 201
    out["event_version"] = event_version
    return out


def _issue_credential_locked(
    conn: sqlite3.Connection, d: sqlite3.Row, *, now: str
) -> tuple[str, int]:
    """委托批准后原子签发一次性代理凭证；一个委托最多一张凭证。"""
    if d["credential_code"]:
        return d["credential_code"], int(d["approved_version"] or 0)
    code = _gen_id(CREDENTIAL_PREFIX, 8)
    version = add_event(
        conn,
        "PROXY_CREDENTIAL_ISSUED",
        ts=now,
        delegation_id=d["id"],
        proxy_credential_code=code,
        person_id=d["original_person_id"],
        reason="delegation_approved",
        payload={
            "credential_code": code,
            "delegation_id": d["id"],
            "original_person_id": d["original_person_id"],
            "proxy_person_id": d["proxy_person_id"],
            "valid_from": d["valid_from"],
            "valid_until": d["valid_until"],
            "zones": _zones(d),
            "max_uses": d["max_uses"],
            "purpose": d["purpose"],
            "ticket_code": d["ticket_code"],
        },
    )
    conn.execute(
        """INSERT INTO proxy_credentials
               (code,delegation_id,status,original_person_id,proxy_person_id,
                valid_from,valid_until,zones_json,max_uses,used_count,purpose,
                ticket_code,issued_at,issued_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, d["id"], "ACTIVE", d["original_person_id"], d["proxy_person_id"],
         d["valid_from"], d["valid_until"], d["zones_json"], d["max_uses"],
         0, d["purpose"], d["ticket_code"], now, version),
    )
    conn.execute(
        """UPDATE delegations
              SET status='APPROVED', decided_at=?, decided_reason='approved',
                  approved_version=?, credential_code=?
            WHERE id=?""",
        (now, version, code, d["id"]),
    )
    return code, version


def record_delegation_decision(
    conn: sqlite3.Connection,
    *,
    delegation_id: str,
    approver_id: str,
    action: str,
    reason: Optional[str] = None,
) -> dict:
    approver_id = approver_id.strip()
    action = action.strip().upper()
    if not approver_id:
        return {"http_status": 400, "error": "缺少审批管理员身份"}
    if action not in ("APPROVE", "REJECT"):
        return {"http_status": 400, "error": "action 必须是 APPROVE 或 REJECT"}
    now = iso(utcnow())
    with write_tx(conn):
        d = get_delegation(conn, delegation_id)
        if d is None:
            return {"http_status": 404, "error": "授权委托不存在"}
        if d["status"] not in ("PENDING", "APPROVED"):
            return {"http_status": 409,
                    "error": f"授权委托已处于终态 {d['status']}，不可审批",
                    "status": d["status"]}
        if d["status"] == "APPROVED":
            return {"http_status": 409,
                    "error": "授权委托已批准并签发凭证；如需停用请撤销，不能再追加审批决定",
                    "status": "APPROVED"}
        # 审批动作幂等地记录审计；未在创建时指定审批人的普通委托允许任意管理员。
        try:
            allowed_approvers = set(json.loads(d["approvers_json"] or "[]"))
        except json.JSONDecodeError:
            allowed_approvers = set()
        if allowed_approvers and approver_id not in allowed_approvers:
            return {"http_status": 403,
                    "error": "该审批管理员不在委托指定的审批人名单中"}

        prior = conn.execute(
            "SELECT * FROM delegation_approvals WHERE delegation_id=? AND approver_id=?",
            (delegation_id, approver_id),
        ).fetchone()
        if prior is not None:
            if prior["action"] != action:
                return {"http_status": 409,
                        "error": f"{approver_id} 已作出 {prior['action']} 决定，不能改成 {action}"}
            row = get_delegation(conn, delegation_id)
            out = _delegation_dict(
                row,
                credential=get_credential(conn, row["credential_code"])
                if row["credential_code"] else None,
            )
            out.update(http_status=200, duplicated=True,
                       approval_id=prior["id"], event_version=prior["event_version"])
            return out

        # 已批准后再有人补点同意只记录审计；拒绝不能推翻已签发凭证（须走撤销）。
        approval_event = add_event(
            conn,
            "DELEGATION_APPROVAL_RECORDED",
            ts=now,
            delegation_id=delegation_id,
            person_id=d["original_person_id"],
            reason=reason or action.lower(),
            payload={"approver_id": approver_id, "action": action,
                     "delegation_status_before": d["status"]},
        )
        cur = conn.execute(
            """INSERT INTO delegation_approvals
                   (delegation_id,approver_id,action,at,reason,event_version)
               VALUES (?,?,?,?,?,?)""",
            (delegation_id, approver_id, action, now, reason, approval_event),
        )
        approval_id = int(cur.lastrowid)

        if action == "REJECT":
            reject_version = add_event(
                conn,
                "DELEGATION_REJECTED",
                ts=now,
                delegation_id=delegation_id,
                person_id=d["original_person_id"],
                reason=reason or "admin_reject_delegation",
                payload={"approver_id": approver_id},
            )
            conn.execute(
                """UPDATE delegations
                      SET status='REJECTED', decided_at=?, decided_reason=?
                    WHERE id=? AND status='PENDING'""",
                (now, reason or "admin_reject_delegation", delegation_id),
            )
            row = get_delegation(conn, delegation_id)
            out = _delegation_dict(row)
            out.update(http_status=200, approved=False, rejected=True,
                       approval_id=approval_id, event_version=reject_version)
            return out

        approve_count = conn.execute(
            "SELECT COUNT(*) c FROM delegation_approvals "
            "WHERE delegation_id=? AND action='APPROVE'",
            (delegation_id,),
        ).fetchone()["c"]
        if int(approve_count) >= int(d["required_approvers"]):
            if now >= d["valid_until"]:
                version = add_event(
                    conn, "DELEGATION_EXPIRED", ts=now, delegation_id=d["id"],
                    person_id=d["original_person_id"], reason="valid_until_reached",
                    payload={"stage": "pending_approval",
                             "valid_until": d["valid_until"]},
                )
                conn.execute(
                    "UPDATE delegations SET status='EXPIRED', expired_at=? WHERE id=?",
                    (now, d["id"]),
                )
                return {"http_status": 410,
                        "error": "授权委托已超过有效期，不能再签发凭证",
                        "event_version": version}
            code, issued_version = _issue_credential_locked(conn, d, now=now)
            approved_event = add_event(
                conn,
                "DELEGATION_APPROVED",
                ts=now,
                delegation_id=delegation_id,
                proxy_credential_code=code,
                person_id=d["original_person_id"],
                reason=reason or "admin_approve_delegation",
                payload={"credential_code": code, "approver_id": approver_id,
                         "required_approvers": d["required_approvers"],
                         "credential_version": issued_version},
            )
            row = get_delegation(conn, delegation_id)
            cred = get_credential(conn, code)
            out = _delegation_dict(row, credential=cred)
            out.update(http_status=201, approved=True, approval_id=approval_id,
                       event_version=approved_event)
            return out

        row = get_delegation(conn, delegation_id)
        out = _delegation_dict(row)
        out.update({"http_status": 202, "approved": False,
                    "pending_second_approval": True,
                    "approval_id": approval_id, "event_version": approval_event,
                    "approvals_received": int(approve_count)})
        return out


def revoke_delegation(
    conn: sqlite3.Connection,
    *,
    delegation_id: str,
    reason: str,
    operator: str = "admin",
) -> dict:
    now = iso(utcnow())
    with write_tx(conn):
        d = get_delegation(conn, delegation_id)
        if d is None:
            return {"http_status": 404, "error": "授权委托不存在"}
        if d["status"] in ("REVOKED", "REJECTED", "EXPIRED"):
            return {"http_status": 409,
                    "error": f"授权委托已处于终态 {d['status']}，不可撤销",
                    "status": d["status"]}
        credential_code = d["credential_code"]
        credential_version = None
        if credential_code:
            credential_version = add_event(
                conn,
                "PROXY_CREDENTIAL_REVOKED",
                ts=now,
                delegation_id=delegation_id,
                proxy_credential_code=credential_code,
                person_id=d["original_person_id"],
                reason=reason,
                payload={"operator": operator, "reason": reason},
            )
            conn.execute(
                """UPDATE proxy_credentials
                      SET status='REVOKED', revoked_at=?, revoked_by=?,
                          revoked_reason=?, revoked_version=?
                    WHERE code=?""",
                (now, operator, reason, credential_version, credential_code),
            )
        version = add_event(
            conn,
            "DELEGATION_REVOKED",
            ts=now,
            delegation_id=delegation_id,
            proxy_credential_code=credential_code,
            person_id=d["original_person_id"],
            reason=reason,
            payload={"operator": operator, "reason": reason,
                     "credential_code": credential_code,
                     "credential_version": credential_version},
        )
        conn.execute(
            """INSERT INTO proxy_revocations
                   (delegation_id,credential_code,revoked_at,revoked_by,reason,event_version)
               VALUES (?,?,?,?,?,?)""",
            (delegation_id, credential_code, now, operator, reason, version),
        )
        conn.execute(
            "UPDATE delegations SET status='REVOKED', revoked_at=?, revoked_by=?, "
            "revoked_reason=? WHERE id=?",
            (now, operator, reason, delegation_id),
        )
        row = get_delegation(conn, delegation_id)
        cred = get_credential(conn, credential_code) if credential_code else None
    out = _delegation_dict(row, credential=cred)
    out["http_status"] = 200
    out["event_version"] = version
    return out


# ---------------- 过期清扫 ----------------

def sweep_expired(conn: sqlite3.Connection) -> list[str]:
    """把到点未决委托和已签发凭证置 EXPIRED；终态绝不回退。"""
    now_dt = utcnow()
    now = iso(now_dt)
    changed: list[str] = []
    with write_tx(conn):
        pending = conn.execute(
            "SELECT * FROM delegations WHERE status='PENDING' AND valid_until<=?",
            (now,),
        ).fetchall()
        for d in pending:
            version = add_event(
                conn, "DELEGATION_EXPIRED", ts=now, delegation_id=d["id"],
                person_id=d["original_person_id"], reason="valid_until_reached",
                payload={"stage": "pending_approval", "valid_until": d["valid_until"]},
            )
            conn.execute(
                "UPDATE delegations SET status='EXPIRED', expired_at=? WHERE id=?",
                (now, d["id"]),
            )
            changed.append(d["id"])

        creds = conn.execute(
            """SELECT * FROM proxy_credentials
                WHERE status='ACTIVE' AND valid_until<=?""",
            (now,),
        ).fetchall()
        for c in creds:
            cversion = add_event(
                conn, "PROXY_CREDENTIAL_EXPIRED", ts=now, delegation_id=c["delegation_id"],
                proxy_credential_code=c["code"], person_id=c["original_person_id"],
                reason="valid_until_reached",
                payload={"valid_until": c["valid_until"], "used_count": c["used_count"]},
            )
            conn.execute(
                """UPDATE proxy_credentials
                      SET status='EXPIRED', expired_at=?, expired_version=?
                    WHERE code=?""",
                (now, cversion, c["code"]),
            )
            dversion = add_event(
                conn, "DELEGATION_EXPIRED", ts=now, delegation_id=c["delegation_id"],
                proxy_credential_code=c["code"], person_id=c["original_person_id"],
                reason="credential_valid_until_reached",
                payload={"credential_code": c["code"],
                         "credential_version": cversion},
            )
            conn.execute(
                "UPDATE delegations SET status='EXPIRED', expired_at=? WHERE id=?",
                (now, c["delegation_id"]),
            )
            changed.append(c["code"])
    return changed


# ---------------- 门点核验 ----------------

def _historical_lock(conn: sqlite3.Connection, zone_id: str, event_ts: str) -> Optional[dict]:
    row = conn.execute(
        """SELECT * FROM policy_rules
            WHERE (zone_id IS NULL OR zone_id=?) AND created_at<=?
            ORDER BY version DESC LIMIT 1""",
        (zone_id, event_ts),
    ).fetchone()
    if row is not None and row["action"] == "LOCK":
        return {"rule_id": row["rule_id"], "action": row["action"],
                "zone_id": row["zone_id"], "reason": row["reason"],
                "created_at": row["created_at"], "version": row["version"]}
    return None


def _insert_verification_locked(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    attempt_id: str,
    credential: sqlite3.Row,
    zone_id: Optional[str],
    event_ts: str,
    now: str,
    offline: bool,
    decision: str,
    reason: str,
    http_status: int,
    usage_before: Optional[int],
    usage_after: Optional[int],
    response: dict,
    conflict_id: Optional[int] = None,
) -> tuple[int, Optional[int]]:
    cur = conn.execute(
        """INSERT INTO proxy_verifications
               (gate_id,attempt_id,credential_code,delegation_id,
                original_person_id,proxy_person_id,zone_id,event_ts,processed_at,
                offline,decision,reason,http_status,usage_before,usage_after,
                event_version,conflict_id,result_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (gate_id, attempt_id, credential["code"], credential["delegation_id"],
         response.get("original_person_id"), response.get("proxy_person_id"),
         zone_id, event_ts, now, 1 if offline else 0, decision, reason,
         http_status, usage_before, usage_after, None, conflict_id,
         json.dumps(response, ensure_ascii=False, sort_keys=True)),
    )
    verification_id = int(cur.lastrowid)
    event_type = ("PROXY_VERIFICATION_ALLOWED" if decision == "ALLOWED"
                  else "PROXY_VERIFICATION_DENIED" if decision == "DENIED"
                  else "PROXY_CONFLICT")
    event_version = add_event(
        conn, event_type, ts=now, gate_id=gate_id,
        delegation_id=credential["delegation_id"],
        proxy_credential_code=credential["code"],
        proxy_verification_id=verification_id,
        proxy_conflict_id=conflict_id,
        person_id=credential["original_person_id"],
        reason=reason,
        payload={"verification_id": verification_id, "gate_id": gate_id,
                 "attempt_id": attempt_id, "offline": offline,
                 "event_ts": event_ts, "decision": decision, "reason": reason,
                 "usage_before": usage_before, "usage_after": usage_after,
                 "zone_id": zone_id},
    )
    conn.execute(
        "UPDATE proxy_verifications SET event_version=? WHERE id=?",
        (event_version, verification_id),
    )
    if conflict_id is not None:
        conn.execute(
            "UPDATE proxy_conflicts SET verification_id=? WHERE id=?",
            (verification_id, conflict_id),
        )
    return verification_id, event_version


def _record_conflict_locked(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    attempt_id: str,
    credential: sqlite3.Row,
    zone_id: Optional[str],
    event_ts: Optional[str],
    now: str,
    kind: str,
    detail: dict,
    reason: str,
    http_status: int,
) -> dict:
    cur = conn.execute(
        """INSERT INTO proxy_conflicts
               (gate_id,attempt_id,credential_code,delegation_id,
                original_person_id,proxy_person_id,zone_id,kind,event_ts,
                detected_at,detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (gate_id, attempt_id, credential["code"], credential["delegation_id"],
         credential["original_person_id"], credential["proxy_person_id"], zone_id,
         kind, event_ts, now,
         json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )
    conflict_id = int(cur.lastrowid)
    response = {
        "ok": False, "status": "PROXY_CONFLICT", "code": credential["code"],
        "gate_id": gate_id, "attempt_id": attempt_id,
        "ts": now, "event_ts": event_ts, "offline": True,
        "reason": "offline_conflict", "reason_text": REASON_TEXT["offline_conflict"],
        "conflict_kind": kind, "conflict_id": conflict_id, "detail": detail,
        "original_person_id": credential["original_person_id"],
        "proxy_person_id": credential["proxy_person_id"],
    }
    verification_id, event_version = _insert_verification_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, credential=credential,
        zone_id=zone_id, event_ts=event_ts or now, now=now, offline=True,
        decision="CONFLICT", reason=reason, http_status=http_status,
        usage_before=None, usage_after=None, response=response,
        conflict_id=conflict_id,
    )
    response["verification_id"] = verification_id
    response["version"] = event_version
    conn.execute(
        "UPDATE proxy_conflicts SET verification_id=? WHERE id=?",
        (verification_id, conflict_id),
    )
    return {"http_status": http_status, "response": response,
            "conflict_id": conflict_id}


def _base_response(
    credential: sqlite3.Row, *, gate_id: str, attempt_id: str, ts: str
) -> dict:
    return {
        "ok": False,
        "status": "PROXY_DENIED",
        "code": credential["code"],
        "delegation_id": credential["delegation_id"],
        "gate_id": gate_id,
        "attempt_id": attempt_id,
        "ts": ts,
        "original_person_id": credential["original_person_id"],
        "proxy_person_id": credential["proxy_person_id"],
        "remaining_uses": max(0, int(credential["max_uses"]) - int(credential["used_count"])),
    }


def _deny_locked(
    conn: sqlite3.Connection,
    *,
    credential: sqlite3.Row,
    gate_id: str,
    attempt_id: str,
    zone_id: Optional[str],
    event_ts: str,
    now: str,
    offline: bool,
    reason: str,
    http_status: int,
    status: str = "PROXY_DENIED",
    extra: Optional[dict] = None,
    presented_original_person_id: Optional[str] = None,
    presented_proxy_person_id: Optional[str] = None,
) -> dict:
    response = _base_response(credential, gate_id=gate_id, attempt_id=attempt_id, ts=now)
    response["status"] = status
    response["event_ts"] = event_ts
    response["offline"] = offline
    response["reason"] = reason
    response["reason_text"] = REASON_TEXT.get(reason, reason)
    if extra:
        response.update(extra)
    if presented_original_person_id is not None:
        response["original_person_id"] = presented_original_person_id
        response["expected_original_person_id"] = credential["original_person_id"]
    if presented_proxy_person_id is not None:
        response["proxy_person_id"] = presented_proxy_person_id
        response["expected_proxy_person_id"] = credential["proxy_person_id"]
    verification_id, event_version = _insert_verification_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, credential=credential,
        zone_id=zone_id, event_ts=event_ts, now=now, offline=offline,
        decision="DENIED", reason=reason, http_status=http_status,
        usage_before=int(credential["used_count"]),
        usage_after=int(credential["used_count"]), response=response,
    )
    response["verification_id"] = verification_id
    response["version"] = event_version
    return {"http_status": http_status, "response": response}


def _consume_locked(
    conn: sqlite3.Connection,
    *,
    credential: sqlite3.Row,
    gate_id: str,
    attempt_id: str,
    zone_id: str,
    event_ts: str,
    now: str,
    offline: bool,
    allow_expired_status: bool = False,
    extra_statuses: tuple[str, ...] = (),
) -> dict:
    before = int(credential["used_count"])
    allowed_statuses = (("ACTIVE", "EXPIRED") if allow_expired_status
                        else ("ACTIVE",)) + tuple(extra_statuses)
    placeholders = ",".join("?" for _ in allowed_statuses)
    cur = conn.execute(
        f"""UPDATE proxy_credentials
               SET used_count=used_count+1
             WHERE code=? AND status IN ({placeholders})
               AND used_count<max_uses AND valid_from<=? AND valid_until>?""",
        (credential["code"], *allowed_statuses, event_ts, event_ts),
    )
    if cur.rowcount != 1:
        fresh = get_credential(conn, credential["code"])
        if fresh is None:
            reason, status, http = "credential_not_found", "NOT_FOUND", 404
        elif fresh["status"] == "REVOKED":
            reason, status, http = "revoked", "PROXY_REVOKED", 410
        elif fresh["status"] == "EXPIRED":
            reason, status, http = "expired", "PROXY_EXPIRED", 410
        else:
            reason, status, http = "exhausted", "PROXY_EXHAUSTED", 410
        return _deny_locked(
            conn, credential=fresh or credential, gate_id=gate_id,
            attempt_id=attempt_id, zone_id=zone_id, event_ts=event_ts, now=now,
            offline=offline, reason=reason, http_status=http, status=status,
        )

    fresh = get_credential(conn, credential["code"])
    after = int(fresh["used_count"])
    exhausted_event_version = None
    # 在线/有效凭证达到上限立即进入 EXHAUSTED 终态。历史补扣发生在已过期凭证
    # 上时只追加核验事实，不把 EXPIRED 改回任何“有效”状态。
    if fresh["status"] == "ACTIVE" and after >= int(fresh["max_uses"]):
        exhausted_event_version = add_event(
            conn, "PROXY_CREDENTIAL_EXHAUSTED", ts=now,
            delegation_id=fresh["delegation_id"], proxy_credential_code=fresh["code"],
            person_id=fresh["original_person_id"], gate_id=gate_id,
            reason="max_uses_reached",
            payload={"used_count": after, "gate_id": gate_id, "attempt_id": attempt_id,
                     "offline": offline, "event_ts": event_ts},
        )
        conn.execute(
            """UPDATE proxy_credentials
                  SET status='EXHAUSTED', exhausted_at=?, exhausted_version=?
                WHERE code=? AND status='ACTIVE'""",
            (now, exhausted_event_version, fresh["code"]),
        )
        fresh = get_credential(conn, fresh["code"])
    response = _base_response(
        fresh, gate_id=gate_id, attempt_id=attempt_id, ts=now)
    response.update({
        "ok": True,
        "status": "PROXY_ALLOWED",
        "event_ts": event_ts,
        "offline": offline,
        "reason": "ok",
        "reason_text": REASON_TEXT["ok"],
        "zone_id": zone_id,
        "valid_from": fresh["valid_from"],
        "valid_until": fresh["valid_until"],
        "zones": _zones(fresh),
        "purpose": fresh["purpose"],
        "max_uses": fresh["max_uses"],
        "used_count": after,
        "remaining_uses": max(0, int(fresh["max_uses"]) - after),
        "credential_status": fresh["status"],
    })
    verification_id, event_version = _insert_verification_locked(
        conn, gate_id=gate_id, attempt_id=attempt_id, credential=fresh,
        zone_id=zone_id, event_ts=event_ts, now=now, offline=offline,
        decision="ALLOWED", reason="ok", http_status=200,
        usage_before=before, usage_after=after, response=response,
    )
    response["verification_id"] = verification_id
    response["version"] = event_version
    if exhausted_event_version is not None:
        response["exhausted_version"] = exhausted_event_version
    return {"http_status": 200, "response": response}


def _adjudicate_locked(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    attempt_id: str,
    raw_code: str,
    original_person_id: Optional[str],
    proxy_person_id: Optional[str],
    original_ticket_code: Optional[str],
    event_ts: str,
    now: str,
    offline: bool,
    base_version: Optional[int] = None,
) -> dict:
    gate = conn.execute("SELECT * FROM gates WHERE id=?", (gate_id,)).fetchone()
    gate_zone = gate["zone_id"] if gate is not None else None
    code = normalize_credential_code(raw_code)
    credential = get_credential(conn, code)

    if gate is None:
        raise ValueError("gate_not_found")
    if gate_zone is None:
        # 凭证可能不存在，但也要留下门点拒绝记录；用临时内存行不可行，因此先返回。
        return {"http_status": 403, "response": {
            "ok": False, "status": "NO_POLICY", "code": code, "gate_id": gate_id,
            "ts": now, "event_ts": event_ts, "offline": offline,
            "reason": "gate_no_policy",
            "reason_text": REASON_TEXT["gate_no_policy"]}}
    if conn.execute("SELECT 1 FROM zones WHERE id=?", (gate_zone,)).fetchone() is None:
        return {"http_status": 403, "response": {
            "ok": False, "status": "UNKNOWN_ZONE", "code": code, "gate_id": gate_id,
            "ts": now, "event_ts": event_ts, "offline": offline,
            "reason": "unknown_zone", "reason_text": REASON_TEXT["unknown_zone"],
            "zone_id": gate_zone}}
    if credential is None:
        return {"http_status": 404, "response": {
            "ok": False, "status": "NOT_FOUND", "code": code, "gate_id": gate_id,
            "ts": now, "event_ts": event_ts, "offline": offline,
            "reason": "credential_not_found",
            "reason_text": REASON_TEXT["credential_not_found"], "zone_id": gate_zone}}

    # 离线时间戳卫生。在线 event_ts 固定为服务器时间，不可能走到未来/解析失败。
    ev_dt = parse_dt(event_ts)
    if offline and ev_dt > parse_dt(now) + timedelta(seconds=FUTURE_TS_SKEW_SECONDS):
        return _record_conflict_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, credential=credential,
            zone_id=gate_zone, event_ts=event_ts, now=now,
            kind="FUTURE_TIMESTAMP", reason="future_timestamp", http_status=409,
            detail={"server_now": now, "future_skew_seconds": FUTURE_TS_SKEW_SECONDS,
                    "desired_decision": "DENIED"})

    if not original_person_id:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone, event_ts=event_ts,
                           now=now, offline=offline, reason="missing_original_person",
                           http_status=400, status="VERIFICATION_INCOMPLETE")
    if not proxy_person_id:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone, event_ts=event_ts,
                           now=now, offline=offline, reason="missing_proxy_person",
                           http_status=400, status="VERIFICATION_INCOMPLETE")
    original_person_id = original_person_id.strip()
    proxy_person_id = proxy_person_id.strip()
    if original_person_id != credential["original_person_id"]:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone, event_ts=event_ts,
                           now=now, offline=offline, reason="original_person_mismatch",
                           http_status=403,
                           presented_original_person_id=original_person_id)
    if proxy_person_id != credential["proxy_person_id"]:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone, event_ts=event_ts,
                           now=now, offline=offline, reason="proxy_person_mismatch",
                           http_status=403,
                           presented_proxy_person_id=proxy_person_id)

    zones = _zones(credential)
    if gate_zone not in zones:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone, event_ts=event_ts,
                           now=now, offline=offline, reason="zone_mismatch",
                           http_status=403,
                           extra={"ticket_zones": zones, "zone_id": gate_zone})

    # 可选：要求原持票人同时持有有效短时票。在线按当前票态；离线票态不做历史
    # 状态机推断，无法确认时进入冲突，避免错误放行。
    if credential["ticket_code"]:
        if not original_ticket_code:
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=offline,
                               reason="original_ticket_required", http_status=400,
                               status="VERIFICATION_INCOMPLETE")
        ticket = conn.execute(
            "SELECT * FROM tickets WHERE code=?", (original_ticket_code.strip().upper(),)
        ).fetchone()
        if ticket is None:
            reason = "original_ticket_not_found"
            if offline:
                return _record_conflict_locked(
                    conn, gate_id=gate_id, attempt_id=attempt_id, credential=credential,
                    zone_id=gate_zone, event_ts=event_ts, now=now,
                    kind="BAD_HISTORICAL_CONTEXT", reason=reason, http_status=409,
                    detail={"ticket_code": original_ticket_code,
                            "desired_decision": "DENIED"})
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=False, reason=reason,
                               http_status=404)
        if ticket["person_id"] != credential["original_person_id"]:
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=offline,
                               reason="original_person_mismatch", http_status=403)
        ticket_ok = (ticket["status"] == "ACTIVE"
                     and ticket["valid_from"] <= event_ts < ticket["valid_until"])
        if not ticket_ok:
            if offline and ticket["status"] in ("REDEEMED", "REVOKED", "EXPIRED"):
                return _record_conflict_locked(
                    conn, gate_id=gate_id, attempt_id=attempt_id, credential=credential,
                    zone_id=gate_zone, event_ts=event_ts, now=now,
                    kind="BAD_HISTORICAL_CONTEXT", reason="original_ticket_invalid",
                    http_status=409,
                    detail={"ticket_code": ticket["code"],
                            "ticket_status": ticket["status"],
                            "desired_decision": "DENIED"})
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=offline,
                               reason="original_ticket_invalid", http_status=403,
                               extra={"ticket_status": ticket["status"]})

    status = credential["status"]
    if status == "ACTIVE" and event_ts >= credential["valid_until"]:
        # 清扫尚未跑到时，门点在线核验也要惰性终结凭证；事件仍只写一次。
        version = add_event(
            conn, "PROXY_CREDENTIAL_EXPIRED", ts=now,
            delegation_id=credential["delegation_id"],
            proxy_credential_code=credential["code"],
            person_id=credential["original_person_id"],
            reason="valid_until_reached_lazy",
            payload={"valid_until": credential["valid_until"],
                     "used_count": credential["used_count"]},
        )
        conn.execute(
            """UPDATE proxy_credentials
                  SET status='EXPIRED', expired_at=?, expired_version=?
                WHERE code=? AND status='ACTIVE'""",
            (now, version, credential["code"]),
        )
        add_event(
            conn, "DELEGATION_EXPIRED", ts=now,
            delegation_id=credential["delegation_id"],
            proxy_credential_code=credential["code"],
            person_id=credential["original_person_id"],
            reason="credential_valid_until_reached_lazy",
            payload={"credential_code": credential["code"], "credential_version": version},
        )
        conn.execute(
            "UPDATE delegations SET status='EXPIRED', expired_at=? WHERE id=?",
            (now, credential["delegation_id"]),
        )
        credential = get_credential(conn, credential["code"])
        status = credential["status"]
    if not offline:
        if event_ts < credential["valid_from"]:
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=False,
                               reason="not_yet_valid", http_status=403,
                               extra={"valid_from": credential["valid_from"],
                                      "valid_until": credential["valid_until"]})
        if status == "REVOKED":
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=False,
                               reason="revoked", http_status=410,
                               status="PROXY_REVOKED",
                               extra={"revoked_at": credential["revoked_at"],
                                      "revoked_reason": credential["revoked_reason"]})
        if status == "EXPIRED" or event_ts >= credential["valid_until"]:
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=False,
                               reason="expired", http_status=410,
                               status="PROXY_EXPIRED")
        if status == "EXHAUSTED":
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=False,
                               reason="exhausted", http_status=410,
                               status="PROXY_EXHAUSTED")
        from . import services as core
        lock = core.zone_lock_state(conn, gate_zone)
        if lock["locked"]:
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=False,
                               reason="locked", http_status=423,
                               status="LOCKED", extra={"lock_rule": lock["rule"]})
        return _consume_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=False)

    # 离线补裁决：按现场 event_ts 判断时间/封锁；终态事件不能复活凭证。
    if event_ts < credential["valid_from"]:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone,
                           event_ts=event_ts, now=now, offline=True,
                           reason="not_yet_valid", http_status=403)
    if event_ts >= credential["valid_until"]:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone,
                           event_ts=event_ts, now=now, offline=True,
                           reason="expired", http_status=410,
                           status="PROXY_EXPIRED")
    lock = _historical_lock(conn, gate_zone, event_ts)
    if lock is not None:
        return _deny_locked(conn, credential=credential, gate_id=gate_id,
                           attempt_id=attempt_id, zone_id=gate_zone,
                           event_ts=event_ts, now=now, offline=True,
                           reason="locked", http_status=423, status="LOCKED",
                           extra={"lock_rule": lock})

    if status == "REVOKED":
        revoked_at = credential["revoked_at"] or now
        # 撤销之后的迟到事件是明确拒绝；撤销之前的现场事件需要管理员按版本
        # 判断是否在断线期间真实发生，避免旧事件复活凭证或错误补扣次数。
        if parse_dt(event_ts) >= parse_dt(revoked_at):
            return _deny_locked(conn, credential=credential, gate_id=gate_id,
                               attempt_id=attempt_id, zone_id=gate_zone,
                               event_ts=event_ts, now=now, offline=True,
                               reason="revoked", http_status=410,
                               status="PROXY_REVOKED")
        return _record_conflict_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, credential=credential,
            zone_id=gate_zone, event_ts=event_ts, now=now,
            kind="REVOKED_HISTORICAL", reason="revoked", http_status=409,
            detail={"revoked_at": credential["revoked_at"],
                    "revoked_version": credential["revoked_version"],
                    "base_version": base_version,
                    "used_count": credential["used_count"],
                    "max_uses": credential["max_uses"],
                    "desired_decision": "ALLOWED"})

    if status == "EXHAUSTED":
        return _record_conflict_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, credential=credential,
            zone_id=gate_zone, event_ts=event_ts, now=now,
            kind="EXHAUSTED_HISTORICAL", reason="exhausted", http_status=409,
            detail={"exhausted_at": credential["exhausted_at"],
                    "exhausted_version": credential["exhausted_version"],
                    "base_version": base_version,
                    "used_count": credential["used_count"],
                    "max_uses": credential["max_uses"],
                    "desired_decision": "ALLOWED"})

    # EXPIRED 状态中，有效期内的历史事件可作为历史使用次数补记，但状态保持 EXPIRED。
    return _consume_locked(
        conn, credential=credential, gate_id=gate_id, attempt_id=attempt_id,
        zone_id=gate_zone, event_ts=event_ts, now=now, offline=True,
        allow_expired_status=(status == "EXPIRED"),
    )


def _prior_verification(conn: sqlite3.Connection, gate_id: str, attempt_id: str):
    return conn.execute(
        "SELECT * FROM proxy_verifications WHERE gate_id=? AND attempt_id=?",
        (gate_id, attempt_id),
    ).fetchone()


def verify_proxy(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    code: str,
    attempt_id: str,
    original_person_id: Optional[str] = None,
    proxy_person_id: Optional[str] = None,
    original_ticket_code: Optional[str] = None,
    event_ts: Optional[str] = None,
) -> dict:
    now = iso(utcnow())
    event_ts = iso(parse_dt(event_ts)) if event_ts else now
    prior = _prior_verification(conn, gate_id, attempt_id)
    if prior is not None:
        response = json.loads(prior["result_json"])
        response["replayed"] = True
        return {"http_status": prior["http_status"], "response": response}
    with write_tx(conn):
        prior = _prior_verification(conn, gate_id, attempt_id)
        if prior is not None:
            response = json.loads(prior["result_json"])
            response["replayed"] = True
            return {"http_status": prior["http_status"], "response": response}
        return _adjudicate_locked(
            conn, gate_id=gate_id, attempt_id=attempt_id, raw_code=code,
            original_person_id=original_person_id, proxy_person_id=proxy_person_id,
            original_ticket_code=original_ticket_code, event_ts=event_ts, now=now,
            offline=False,
        )


def replay_proxy_verifications(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    events: list[dict],
    base_version: Optional[int] = None,
) -> dict:
    """门点离线核验记录重连后批量补齐。

    调用方应先通过 /api/gate/sync 拉版本，再把当前游标作为 base_version。
    整批在单写事务内按现场 event_ts 排序裁决；同一物理 attempt 重放首次结果。
    """
    now = iso(utcnow())
    normalized = []
    for ev in events:
        try:
            event_ts = iso(parse_dt(str(ev.get("event_ts") or now)))
        except (TypeError, ValueError):
            event_ts = None
        normalized.append({**ev, "_event_ts": event_ts})
    normalized.sort(key=lambda x: (x["_event_ts"] or "", str(x.get("gate_id") or gate_id),
                                   str(x.get("attempt_id") or "")))
    results: list[dict] = []
    with write_tx(conn):
        for ev in normalized:
            attempt_id = str(ev.get("attempt_id") or "").strip()
            raw_code = str(ev.get("code") or "").strip()
            event_ts = ev["_event_ts"]
            if not attempt_id or not raw_code:
                results.append({"ok": False, "http_status": 400,
                                "error": "缺少 code 或 attempt_id",
                                "attempt_id": attempt_id or None})
                continue
            prior = _prior_verification(conn, gate_id, attempt_id)
            if prior is not None:
                item = json.loads(prior["result_json"])
                item["http_status"] = prior["http_status"]
                item["replayed"] = True
                results.append(item)
                continue
            if event_ts is None:
                # 仍需找到凭证以便冲突和版本事件可关联；未知凭证给 400 且不入流。
                cred = get_credential(conn, normalize_credential_code(raw_code))
                if cred is None:
                    results.append({"ok": False, "http_status": 400,
                                    "error": REASON_TEXT["invalid_event_ts"],
                                    "code": raw_code, "attempt_id": attempt_id})
                    continue
                r = _record_conflict_locked(
                    conn, gate_id=gate_id, attempt_id=attempt_id,
                    credential=cred,
                    zone_id=None, event_ts=ev.get("event_ts"), now=now,
                    kind="INVALID_TIMESTAMP", reason="invalid_event_ts",
                    http_status=400, detail={"raw_event_ts": ev.get("event_ts"),
                                             "desired_decision": "DENIED"})
            else:
                try:
                    r = _adjudicate_locked(
                        conn, gate_id=gate_id, attempt_id=attempt_id,
                        raw_code=raw_code,
                        original_person_id=ev.get("original_person_id"),
                        proxy_person_id=ev.get("proxy_person_id"),
                        original_ticket_code=ev.get("original_ticket_code"),
                        event_ts=event_ts, now=now, offline=True,
                        base_version=base_version,
                    )
                except ValueError as exc:
                    results.append({"ok": False, "http_status": 404, "error": str(exc),
                                    "attempt_id": attempt_id})
                    continue
            item = dict(r["response"])
            item["http_status"] = r["http_status"]
            if r.get("conflict_id"):
                item["conflict_id"] = r["conflict_id"]
            results.append(item)
    return {"gate_id": gate_id, "base_version": base_version, "processed_at": now,
            "count": len(results), "results": results}


def _backfill_conflict_allowed_locked(
    conn: sqlite3.Connection,
    *,
    conflict: sqlite3.Row,
    credential: sqlite3.Row,
    now: str,
) -> dict:
    """管理员确认一条离线冲突“现场确为通过”时安全补记次数。

    撤销/用尽状态绝不改回 ACTIVE；仅在现场时间处于有效期、分区未锁且仍有
    剩余次数时，在原终态上补记一条历史使用。补后记满则维持原终态，不复活凭证。
    """
    event_ts = conflict["event_ts"]
    gate_zone = conflict["zone_id"]
    if not event_ts or not gate_zone:
        return {"http_status": 409, "response": {"reason": "bad_historical_context"}}
    if event_ts < credential["valid_from"] or event_ts >= credential["valid_until"]:
        return {"http_status": 409, "response": {"reason": "expired"}}
    if gate_zone not in _zones(credential):
        return {"http_status": 403, "response": {"reason": "zone_mismatch"}}
    if _historical_lock(conn, gate_zone, event_ts) is not None:
        return {"http_status": 423, "response": {"reason": "locked"}}
    r = _consume_locked(
        conn,
        credential=credential,
        gate_id=conflict["gate_id"],
        attempt_id=f"resolve-{conflict['id']}-apply",
        zone_id=gate_zone,
        event_ts=event_ts,
        now=now,
        offline=True,
        allow_expired_status=(credential["status"] == "EXPIRED"),
        extra_statuses=("REVOKED", "EXHAUSTED"),
    )
    # _consume_locked 可能因条件更新竞态拒绝；返回其结果给管理员。
    return r


# ---------------- 冲突处理 ----------------

def list_proxy_conflicts(
    conn: sqlite3.Connection,
    *,
    status_filter: str = "OPEN",
    original_person_id: Optional[str] = None,
    proxy_person_id: Optional[str] = None,
    zone_id: Optional[str] = None,
    credential_code: Optional[str] = None,
    delegation_id: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM proxy_conflicts WHERE 1=1"
    params: list = []
    if status_filter and status_filter != "ALL":
        sql += " AND status=?"
        params.append(status_filter)
    if original_person_id:
        sql += " AND original_person_id=?"
        params.append(original_person_id)
    if proxy_person_id:
        sql += " AND proxy_person_id=?"
        params.append(proxy_person_id)
    if zone_id:
        sql += " AND zone_id=?"
        params.append(zone_id)
    if credential_code:
        sql += " AND credential_code=?"
        params.append(normalize_credential_code(credential_code))
    if delegation_id:
        sql += " AND delegation_id=?"
        params.append(delegation_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 2000))
    return [_conflict_dict(r) for r in conn.execute(sql, params).fetchall()]


def resolve_proxy_conflict(
    conn: sqlite3.Connection,
    *,
    conflict_id: int,
    action: str,
    operator: str,
    reason: Optional[str],
) -> dict:
    action = action.strip().upper()
    if action not in PROXY_CONFLICT_RESOLUTIONS:
        return {"http_status": 400,
                "error": "action 必须是 DISMISSED 或 APPLIED"}
    now = iso(utcnow())
    with write_tx(conn):
        fc = conn.execute(
            "SELECT * FROM proxy_conflicts WHERE id=?", (conflict_id,)
        ).fetchone()
        if fc is None:
            return {"http_status": 404, "error": "冲突不存在"}
        if fc["status"] == "RESOLVED":
            return {"http_status": 409, "error": "冲突已处理",
                    "resolution": fc["resolution"]}
        if action == "DISMISSED":
            version = add_event(
                conn, "PROXY_CONFLICT_RESOLVED", ts=now,
                delegation_id=fc["delegation_id"],
                proxy_credential_code=fc["credential_code"],
                proxy_conflict_id=fc["id"], person_id=fc["original_person_id"],
                reason=reason or "admin_dismissed",
                payload={"conflict_id": fc["id"], "action": "DISMISSED",
                         "operator": operator, "kind": fc["kind"]},
            )
            conn.execute(
                """UPDATE proxy_conflicts
                      SET status='RESOLVED', resolution='DISMISSED', resolved_at=?,
                          resolved_by=?, resolve_reason=?
                    WHERE id=?""",
                (now, operator, reason, conflict_id),
            )
            return {"http_status": 200, "resolved": "DISMISSED",
                    "event_version": version,
                    "conflict": _conflict_dict(
                        conn.execute("SELECT * FROM proxy_conflicts WHERE id=?",
                                     (conflict_id,)).fetchone())}

        # APPLIED 只允许补记“现场本应通过”的历史事件；拒绝类冲突应 DISMISSED。
        cred = get_credential(conn, fc["credential_code"])
        if cred is None:
            return {"http_status": 409, "error": "代理凭证已不存在，无法补记"}
        try:
            detail = json.loads(fc["detail_json"] or "{}")
        except json.JSONDecodeError:
            detail = {}
        if detail.get("desired_decision") != "ALLOWED" or not fc["event_ts"]:
            return {"http_status": 409,
                    "error": "该冲突现场不是可补记的通过事件，请使用 DISMISSED"}
        r = _backfill_conflict_allowed_locked(
            conn, conflict=fc, credential=cred, now=now)
        if r["http_status"] != 200:
            return {"http_status": 409,
                    "error": "按当前版本仍不能安全补记，冲突保持待处理",
                    "latest": r["response"]}
        version = add_event(
            conn, "PROXY_CONFLICT_RESOLVED", ts=now,
            delegation_id=fc["delegation_id"],
            proxy_credential_code=fc["credential_code"],
            proxy_conflict_id=fc["id"], person_id=fc["original_person_id"],
            reason=reason or "admin_applied",
            payload={"conflict_id": fc["id"], "action": "APPLIED",
                     "operator": operator, "kind": fc["kind"],
                     "applied_verification": r["response"].get("verification_id")},
        )
        conn.execute(
            """UPDATE proxy_conflicts
                  SET status='RESOLVED', resolution='APPLIED', resolved_at=?,
                      resolved_by=?, resolve_reason=?
                WHERE id=?""",
            (now, operator, reason, conflict_id),
        )
        return {"http_status": 200, "resolved": "APPLIED",
                "event_version": version, "applied": r["response"],
                "conflict": _conflict_dict(
                    conn.execute("SELECT * FROM proxy_conflicts WHERE id=?",
                                 (conflict_id,)).fetchone())}


# ---------------- 查询视图 ----------------

def delegation_detail(conn: sqlite3.Connection, delegation_id: str) -> Optional[dict]:
    d = get_delegation(conn, delegation_id)
    if d is None:
        return None
    cred = get_credential(conn, d["credential_code"]) if d["credential_code"] else None
    out = _delegation_dict(d, credential=cred)
    out["approvals"] = [
        {**dict(r), "administrator_id": r["approver_id"]}
        for r in conn.execute(
            "SELECT * FROM delegation_approvals WHERE delegation_id=? ORDER BY id",
            (delegation_id,),
        ).fetchall()
    ]
    out["verifications"] = [_verification_dict(r) for r in conn.execute(
        "SELECT * FROM proxy_verifications WHERE delegation_id=? ORDER BY id",
        (delegation_id,),
    ).fetchall()]
    out["conflicts"] = [_conflict_dict(r) for r in conn.execute(
        "SELECT * FROM proxy_conflicts WHERE delegation_id=? ORDER BY id",
        (delegation_id,),
    ).fetchall()]
    out["revocations"] = [dict(r) for r in conn.execute(
        "SELECT * FROM proxy_revocations WHERE delegation_id=? ORDER BY id",
        (delegation_id,),
    ).fetchall()]
    out["events"] = [
        event_dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE delegation_id=? ORDER BY id",
            (delegation_id,),
        ).fetchall()
    ]
    if cred is not None:
        out["used_count"] = cred["used_count"]
        out["remaining_uses"] = max(0, int(cred["max_uses"]) - int(cred["used_count"]))
    else:
        out["used_count"] = 0
        out["remaining_uses"] = None
    return out


def list_delegations(
    conn: sqlite3.Connection,
    *,
    original_person_id: Optional[str] = None,
    proxy_person_id: Optional[str] = None,
    zone_id: Optional[str] = None,
    status: Optional[str] = None,
    start_from: Optional[str] = None,
    start_to: Optional[str] = None,
    active_from: Optional[str] = None,
    active_to: Optional[str] = None,
    active_at: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT d.* FROM delegations d WHERE 1=1"
    params: list = []
    if original_person_id:
        sql += " AND d.original_person_id=?"
        params.append(original_person_id)
    if proxy_person_id:
        sql += " AND d.proxy_person_id=?"
        params.append(proxy_person_id)
    if status:
        sql += " AND d.status=?"
        params.append(status.upper())
    if zone_id:
        sql += (" AND EXISTS (SELECT 1 FROM json_each(d.zones_json) je "
                "WHERE je.value=?)")
        params.append(zone_id)
    if start_from:
        sql += " AND d.valid_from>=?"
        params.append(start_from)
    if start_to:
        sql += " AND d.valid_from<=?"
        params.append(start_to)
    if active_from:
        sql += " AND d.valid_until>?"
        params.append(active_from)
    if active_to:
        sql += " AND d.valid_from<?"
        params.append(active_to)
    if active_at:
        sql += " AND d.valid_from<=? AND d.valid_until>?"
        params.extend([active_at, active_at])
    sql += " ORDER BY d.created_at DESC, d.id DESC LIMIT ?"
    params.append(min(limit, 1000))
    rows = conn.execute(sql, params).fetchall()
    out = []
    for d in rows:
        cred = get_credential(conn, d["credential_code"]) if d["credential_code"] else None
        item = _delegation_dict(d, credential=cred)
        item["used_count"] = cred["used_count"] if cred else 0
        item["remaining_uses"] = (
            max(0, int(cred["max_uses"]) - int(cred["used_count"])) if cred else None)
        out.append(item)
    return out


def list_proxy_verifications(
    conn: sqlite3.Connection,
    *,
    original_person_id: Optional[str] = None,
    proxy_person_id: Optional[str] = None,
    zone_id: Optional[str] = None,
    credential_code: Optional[str] = None,
    delegation_id: Optional[str] = None,
    decision: Optional[str] = None,
    reason: Optional[str] = None,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM proxy_verifications WHERE 1=1"
    params: list = []
    if original_person_id:
        sql += " AND (original_person_id=? OR credential_code IN "
        sql += "(SELECT code FROM proxy_credentials WHERE original_person_id=?))"
        params.extend([original_person_id, original_person_id])
    if proxy_person_id:
        sql += " AND (proxy_person_id=? OR credential_code IN "
        sql += "(SELECT code FROM proxy_credentials WHERE proxy_person_id=?))"
        params.extend([proxy_person_id, proxy_person_id])
    if zone_id:
        sql += " AND zone_id=?"
        params.append(zone_id)
    if credential_code:
        sql += " AND credential_code=?"
        params.append(normalize_credential_code(credential_code))
    if delegation_id:
        sql += " AND delegation_id=?"
        params.append(delegation_id)
    if decision:
        sql += " AND decision=?"
        params.append(decision.upper())
    if reason:
        sql += " AND reason=?"
        params.append(reason)
    if start_ts:
        sql += " AND event_ts>=?"
        params.append(start_ts)
    if end_ts:
        sql += " AND event_ts<=?"
        params.append(end_ts)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 2000))
    return [_verification_dict(r) for r in conn.execute(sql, params).fetchall()]


def list_proxy_revocations(
    conn: sqlite3.Connection,
    *,
    original_person_id: Optional[str] = None,
    proxy_person_id: Optional[str] = None,
    delegation_id: Optional[str] = None,
    credential_code: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    sql = """SELECT pr.*, d.original_person_id, d.proxy_person_id
               FROM proxy_revocations pr JOIN delegations d ON d.id=pr.delegation_id
              WHERE 1=1"""
    params: list = []
    if original_person_id:
        sql += " AND d.original_person_id=?"
        params.append(original_person_id)
    if proxy_person_id:
        sql += " AND d.proxy_person_id=?"
        params.append(proxy_person_id)
    if delegation_id:
        sql += " AND pr.delegation_id=?"
        params.append(delegation_id)
    if credential_code:
        sql += " AND pr.credential_code=?"
        params.append(normalize_credential_code(credential_code))
    sql += " ORDER BY pr.id DESC LIMIT ?"
    params.append(min(limit, 1000))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def person_proxy_view(conn: sqlite3.Connection, person_id: str) -> dict:
    delegations = list_delegations(conn, limit=2000)
    delegations = [
        d for d in delegations
        if d["original_person_id"] == person_id or d["proxy_person_id"] == person_id
    ]
    verifications = list_proxy_verifications(conn, limit=2000)
    conflicts = list_proxy_conflicts(conn, status_filter="ALL", limit=2000)
    return {
        "person_id": person_id,
        "as_original": [d for d in delegations if d["original_person_id"] == person_id],
        "as_proxy": [d for d in delegations if d["proxy_person_id"] == person_id],
        "verifications": [
            v for v in verifications
            if v.get("original_person_id") == person_id
            or v.get("proxy_person_id") == person_id
        ],
        "conflicts": [
            c for c in conflicts
            if c.get("original_person_id") == person_id
            or c.get("proxy_person_id") == person_id
        ],
    }
