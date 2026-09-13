"""FastAPI 入口：管理端 API + 门点 API + 静态页面 + 后台过期清扫线程。"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import services
from . import presence as presence_svc
from . import routes as routes_svc
from . import delegations as delegation_svc
from .db import DB_PATH, connect, init_db, iso, parse_dt, utcnow
from .services import SWEEP_INTERVAL

ADMIN_TOKEN = os.getenv("PASSPORT_ADMIN_TOKEN", "admin-change-me")
GATE_TOKEN = os.getenv("PASSPORT_GATE_TOKEN", "gate-change-me")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="ShortPass · 短时通行票系统", version="1.0.0")
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)
_stop_sweeper = threading.Event()


# ---------------- 鉴权 ----------------

def require_admin(authorization: Optional[str] = Header(default=None)) -> None:
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(status_code=401, detail="需要管理员令牌 (Bearer)")


def require_gate(authorization: Optional[str] = Header(default=None)) -> None:
    if authorization != f"Bearer {GATE_TOKEN}":
        raise HTTPException(status_code=401, detail="需要门点令牌 (Bearer)")


def get_conn() -> sqlite3.Connection:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def require_active_gate(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """门点令牌之外，还要核验 gate_id 已注册且未被吊销。"""
    body = {}
    try:
        body = request.scope.get("_json_body") or {}
    except Exception:  # pragma: no cover
        body = {}
    gate_id = body.get("gate_id")
    if not gate_id:
        raise HTTPException(status_code=400, detail="缺少 gate_id")
    gate = services.get_gate(conn, gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail=f"门点未注册: {gate_id}")
    if gate["revoked"]:
        raise HTTPException(status_code=410, detail=f"门点已停用: {gate_id}")
    return gate


@app.middleware("http")
async def parse_json_for_gate(request: Request, call_next):
    # 让 gate_id 校验中间件可以复用请求体
    ctype = request.headers.get("content-type", "")
    if request.method in ("POST", "PUT") and "application/json" in ctype:
        try:
            request.scope["_json_body"] = await request.json()
        except Exception:
            request.scope["_json_body"] = {}
    return await call_next(request)


# ---------------- 模型 ----------------

class GateIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


class ZoneIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


class GateZoneIn(BaseModel):
    zone_id: Optional[str] = None  # null = 清除分区（门点回到默认拒绝）


class RuleIn(BaseModel):
    rule_id: Optional[str] = Field(default=None, max_length=128)  # 留空自动生成
    action: str = Field(min_length=1, max_length=16)  # LOCK / UNLOCK
    zone_id: Optional[str] = None  # null = 全局规则
    reason: Optional[str] = Field(default=None, max_length=300)


class IssueIn(BaseModel):
    person_id: str = Field(min_length=1, max_length=128)
    valid_from: Optional[str] = None  # ISO8601，默认现在
    valid_until: Optional[str] = None
    ttl_seconds: Optional[int] = Field(default=None, ge=1, le=365 * 24 * 3600)
    note: Optional[str] = Field(default=None, max_length=500)
    zones: Optional[list[str]] = None  # 允许通行的分区；缺省=[] 即所有门点拒绝
    route_id: Optional[str] = Field(default=None, max_length=64)  # 绑定检查路线


class RevokeIn(BaseModel):
    code: str = Field(min_length=1)
    reason: str = Field(default="admin_revoke", max_length=200)


class RedeemIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    policy_version: int = Field(default=0, ge=0)  # 门点已同步的封锁规则版本
    route_version: int = Field(default=0, ge=0)  # 门点已同步的路线目录版本


class SyncIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    since_version: int = Field(default=0, ge=0)


# ---------------- 访客预约批次模型 ----------------

class BatchIn(BaseModel):
    visit_date: str = Field(min_length=4, max_length=10)  # YYYY-MM-DD
    start_at: str = Field(min_length=5)   # ISO8601 时段开始
    end_at: str = Field(min_length=5)     # ISO8601 时段结束
    zone_id: str = Field(min_length=1, max_length=64)
    capacity: int = Field(ge=1, le=100000)
    name: Optional[str] = Field(default=None, max_length=128)
    route_id: Optional[str] = Field(default=None, max_length=64)  # 绑定检查路线


class CapacityIn(BaseModel):
    capacity: int = Field(ge=1, le=100000)
    reason: Optional[str] = Field(default=None, max_length=300)


class BackfillIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    contact: str = Field(min_length=1, max_length=128)
    companions: int = Field(default=0, ge=0, le=1000)  # 同行人数（不含申请人）
    companion_names: Optional[list[str]] = None
    reason: Optional[str] = Field(default=None, max_length=300)
    approve: bool = True  # false = 仅补录为待审核（占座，仍受容量约束）


class DecideIn(BaseModel):
    reason: str = Field(default="admin_action", max_length=300)


class ApplicationSubmitIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=128)  # 浏览器生成的 UUID
    name: str = Field(min_length=1, max_length=128)
    contact: str = Field(min_length=1, max_length=128)
    companions: int = Field(default=0, ge=0, le=1000)
    companion_names: Optional[list[str]] = None  # 同行人姓名名单；长度须=companions


class ApplicationChangeIn(BaseModel):
    request_id: str = Field(min_length=8, max_length=128)  # 变更请求幂等键
    name: str = Field(min_length=1, max_length=128)
    contact: str = Field(min_length=1, max_length=128)
    companions: int = Field(default=0, ge=0, le=1000)
    companion_names: Optional[list[str]] = None


class ChangeCancelIn(BaseModel):
    change_id: str = Field(min_length=1, max_length=64)


class ChangeTokenIn(BaseModel):
    manage_token: str = Field(min_length=8, max_length=128)


# ---------------- 在场清册 / 应急清点模型 ----------------

class DepartureIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1, max_length=128)


class CorrectionIn(BaseModel):
    code: str = Field(min_length=1)
    action: str = Field(min_length=4, max_length=16)
    # MARK_ARRIVED 漏扫补登记 / MARK_DEPARTED 漏扫离场 / REMOVE 误扫移除 /
    # RESTORE 纠正误离场或误移除
    reason: str = Field(min_length=1, max_length=300)
    gate_id: Optional[str] = Field(default=None, max_length=64)
    operator: Optional[str] = Field(default=None, max_length=128)
    party_size: Optional[int] = Field(default=None, ge=1, le=1001)
    companion_names: Optional[list[str]] = None


class RollcallIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=300)
    zone_id: Optional[str] = Field(default=None, max_length=64)
    batch_id: Optional[str] = Field(default=None, max_length=64)
    operator: Optional[str] = Field(default=None, max_length=128)


# ---------------- 访客路线检查模型 ----------------

class RouteIn(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    zone_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


class CheckpointIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=128)
    max_stay_seconds: Optional[int] = Field(default=None, ge=1, le=365 * 24 * 3600)


class RouteVersionIn(BaseModel):
    checkpoints: list[CheckpointIn] = Field(min_length=1, max_length=100)
    note: Optional[str] = Field(default=None, max_length=300)


class RoutePauseIn(BaseModel):
    reason: str = Field(default="admin_pause", max_length=300)


class CheckpointStateIn(BaseModel):
    version: Optional[int] = Field(default=None, ge=1)
    seq: int = Field(ge=1, le=100)
    closed: bool
    reason: Optional[str] = Field(default=None, max_length=300)


class RouteBindIn(BaseModel):
    route_id: str = Field(min_length=1, max_length=64)
    scope: str = Field(default="TICKET", pattern="^(TICKET|BATCH)$")
    code: Optional[str] = Field(default=None, max_length=64)
    batch_id: Optional[str] = Field(default=None, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=300)


class CheckpointScanIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    route_version: int = Field(default=0, ge=0)  # 门点已同步的路线目录版本


class OfflineCheckpointEvent(BaseModel):
    code: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    event_ts: Optional[str] = None  # 门点本地事件时间（ISO8601）；缺省=处理时刻


class CheckpointReplayIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    events: list[OfflineCheckpointEvent] = Field(min_length=1, max_length=500)


class ConflictResolveIn(BaseModel):
    action: str = Field(pattern="^(DISMISSED|APPLIED|MARK_VIOLATED)$")
    reason: Optional[str] = Field(default=None, max_length=300)
    operator: Optional[str] = Field(default=None, max_length=128)


# ---------------- 访客授权委托与临时代理核验模型 ----------------

class DelegationIn(BaseModel):
    original_person_id: str = Field(min_length=1, max_length=128)
    proxy_person_id: str = Field(min_length=1, max_length=128)
    valid_from: str = Field(min_length=5)
    valid_until: str = Field(min_length=5)
    zones: list[str] = Field(min_length=1, max_length=50)
    max_uses: int = Field(ge=1, le=10000)
    purpose: str = Field(min_length=1, max_length=500)
    high_risk: bool = False
    ticket_code: Optional[str] = Field(default=None, max_length=64)
    approvers: Optional[list[str]] = None
    operator: Optional[str] = Field(default=None, max_length=128)
    request_id: Optional[str] = Field(default=None, min_length=8, max_length=128)


class DelegationDecisionIn(BaseModel):
    approver_id: str = Field(min_length=1, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=300)


class DelegationRevokeIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)
    operator: Optional[str] = Field(default=None, max_length=128)


class ProxyVerifyIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    original_person_id: str = Field(min_length=1, max_length=128)
    proxy_person_id: str = Field(min_length=1, max_length=128)
    original_ticket_code: Optional[str] = Field(default=None, max_length=64)
    event_ts: Optional[str] = None


class OfflineProxyEvent(BaseModel):
    code: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    original_person_id: Optional[str] = Field(default=None, max_length=128)
    proxy_person_id: Optional[str] = Field(default=None, max_length=128)
    original_ticket_code: Optional[str] = Field(default=None, max_length=64)
    event_ts: Optional[str] = None


class ProxyReplayIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    base_version: Optional[int] = Field(default=None, ge=0)
    events: list[OfflineProxyEvent] = Field(min_length=1, max_length=1000)


class ProxyConflictResolveIn(BaseModel):
    action: str = Field(pattern="^(DISMISSED|APPLIED)$")
    reason: Optional[str] = Field(default=None, max_length=300)
    operator: Optional[str] = Field(default=None, max_length=128)


# ---------------- 健康检查 / 页面 ----------------

@app.get("/healthz")
def healthz():
    return {"ok": True, "now": iso(utcnow())}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


@app.get("/gate")
def gate_page():
    return FileResponse(os.path.join(STATIC_DIR, "gate.html"))


@app.get("/apply")
def apply_page():
    # 访客申请页：链接形如 /apply?k=<apply_token>，令牌由页面 JS 读取
    return FileResponse(os.path.join(STATIC_DIR, "apply.html"))


# ---------------- 管理端 API ----------------

@app.post("/api/admin/gates", dependencies=[Depends(require_admin)])
def admin_create_gate(body: GateIn, conn: sqlite3.Connection = Depends(get_conn)):
    if services.get_gate(conn, body.id) is not None:
        raise HTTPException(status_code=409, detail=f"门点已存在: {body.id}")
    gate = services.create_gate(conn, body.id, body.name)
    conn.commit()
    return gate


@app.get("/api/admin/gates", dependencies=[Depends(require_admin)])
def admin_list_gates(conn: sqlite3.Connection = Depends(get_conn)):
    return {"gates": services.list_gates(conn)}


@app.delete("/api/admin/gates/{gate_id}", dependencies=[Depends(require_admin)])
def admin_revoke_gate(gate_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    gate = services.get_gate(conn, gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="门点不存在")
    services.revoke_gate(conn, gate_id)
    conn.commit()
    return {"id": gate_id, "revoked": True}


# ---------------- 分区与封锁策略（管理端） ----------------

@app.post("/api/admin/zones", dependencies=[Depends(require_admin)])
def admin_create_zone(body: ZoneIn, conn: sqlite3.Connection = Depends(get_conn)):
    if services.get_zone(conn, body.id) is not None:
        raise HTTPException(status_code=409, detail=f"分区已存在: {body.id}")
    zone = services.create_zone(conn, body.id, body.name)
    conn.commit()
    return zone


@app.get("/api/admin/zones", dependencies=[Depends(require_admin)])
def admin_list_zones(conn: sqlite3.Connection = Depends(get_conn)):
    return {
        "zones": services.list_zones(conn),
        "policy_version": services.current_policy_version(conn),
    }


@app.get("/api/admin/zones/{zone_id}", dependencies=[Depends(require_admin)])
def admin_zone_detail(zone_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = services.zone_detail(conn, zone_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="分区不存在")
    return detail


@app.delete("/api/admin/zones/{zone_id}", dependencies=[Depends(require_admin)])
def admin_delete_zone(zone_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    if services.get_zone(conn, zone_id) is None:
        raise HTTPException(status_code=404, detail="分区不存在")
    services.delete_zone(conn, zone_id)
    conn.commit()
    return {
        "id": zone_id,
        "deleted": True,
        "note": "仍引用该分区的门点将按“未知分区”默认拒绝",
    }


@app.put("/api/admin/gates/{gate_id}/zone", dependencies=[Depends(require_admin)])
def admin_set_gate_zone(
    gate_id: str, body: GateZoneIn, conn: sqlite3.Connection = Depends(get_conn)
):
    if services.get_gate(conn, gate_id) is None:
        raise HTTPException(status_code=404, detail="门点不存在")
    if body.zone_id is not None and services.get_zone(conn, body.zone_id) is None:
        raise HTTPException(status_code=400, detail=f"未知分区: {body.zone_id}")
    services.set_gate_zone(conn, gate_id, body.zone_id)
    conn.commit()
    return {"id": gate_id, "zone_id": body.zone_id}


@app.post("/api/admin/policy/rules", dependencies=[Depends(require_admin)])
def admin_publish_rule(body: RuleIn, conn: sqlite3.Connection = Depends(get_conn)):
    rule_id = body.rule_id or f"R-{secrets.token_hex(6).upper()}"
    result = services.publish_rule(
        conn,
        rule_id=rule_id,
        action=body.action.strip().upper(),
        zone_id=body.zone_id,
        reason=body.reason,
    )
    if result["http_status"] == 400:
        raise HTTPException(status_code=400, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse(status_code=result["http_status"], content=result)


@app.get("/api/admin/policy/rules", dependencies=[Depends(require_admin)])
def admin_list_rules(
    zone_id: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {
        "rules": services.list_rules(conn, zone_id=zone_id),
        "policy_version": services.current_policy_version(conn),
    }


@app.post("/api/admin/tickets", dependencies=[Depends(require_admin)])
def admin_issue_ticket(body: IssueIn, conn: sqlite3.Connection = Depends(get_conn)):
    now = utcnow()
    try:
        vf = parse_dt(body.valid_from) if body.valid_from else now
        if body.valid_until:
            vu = parse_dt(body.valid_until)
        elif body.ttl_seconds is not None:
            vu = vf.fromtimestamp(vf.timestamp() + body.ttl_seconds, tz=vf.tzinfo)
        else:
            raise HTTPException(status_code=400, detail="必须提供 valid_until 或 ttl_seconds")
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"时间格式错误: {e}")
    if vu <= vf:
        raise HTTPException(status_code=400, detail="valid_until 必须晚于 valid_from")
    zones: list[str] = []
    for z in body.zones or []:
        z = z.strip()
        if not z:
            continue
        if services.get_zone(conn, z) is None:
            raise HTTPException(status_code=400, detail=f"未知分区: {z}")
        if z not in zones:
            zones.append(z)
    if body.route_id:
        route = routes_svc.get_route(conn, body.route_id)
        if route is None:
            raise HTTPException(status_code=400, detail=f"路线不存在: {body.route_id}")
        if route["zone_id"] not in zones:
            raise HTTPException(
                status_code=400,
                detail=f"路线属于分区 {route['zone_id']}，但票面未授权该分区")
    ticket = services.issue_ticket(
        conn,
        person_id=body.person_id.strip(),
        valid_from=iso(vf),
        valid_until=iso(vu),
        note=body.note,
        zones=zones,
        route_id=body.route_id,
    )
    if ticket.get("http_status") == 400:
        # 路线暂停/未发布属于与当前状态冲突（不是参数格式问题）
        detail = ticket["error"]
        code_status = 409 if ("暂停" in detail or "未发布" in detail) else 400
        raise HTTPException(status_code=code_status, detail=detail)
    return ticket


@app.post("/api/admin/tickets/revoke", dependencies=[Depends(require_admin)])
def admin_revoke_ticket(body: RevokeIn, conn: sqlite3.Connection = Depends(get_conn)):
    result = services.revoke_ticket(
        conn, code=services.normalize_code(body.code), reason=body.reason
    )
    if result.get("http_status") == 404:
        raise HTTPException(status_code=404, detail="票面不存在")
    if result.get("http_status") == 409:
        raise HTTPException(
            status_code=409,
            detail=f"票据已处于终态 {result.get('status')}，不可作废（也不会复活）",
        )
    return result["ticket"]


@app.get("/api/admin/tickets/{code}", dependencies=[Depends(require_admin)])
def admin_get_ticket(code: str, conn: sqlite3.Connection = Depends(get_conn)):
    ticket = services.get_ticket(conn, services.normalize_code(code))
    if ticket is None:
        raise HTTPException(status_code=404, detail="票面不存在")
    return ticket


@app.get("/api/admin/people", dependencies=[Depends(require_admin)])
def admin_list_people(
    q: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"people": services.list_people(conn, q)}


@app.get("/api/admin/people/{person_id}", dependencies=[Depends(require_admin)])
def admin_person_view(person_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    view = services.get_person_view(conn, person_id)
    if view is None:
        raise HTTPException(status_code=404, detail="该人员没有票据")
    return view


@app.get("/api/admin/events", dependencies=[Depends(require_admin)])
def admin_events(
    since: Optional[int] = Query(default=None),
    code: Optional[str] = Query(default=None),
    person_id: Optional[str] = Query(default=None),
    batch_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"events": services.list_events(
        conn, since=since, code=code, person_id=person_id,
        batch_id=batch_id, limit=limit
    )}


@app.get("/api/admin/attempts", dependencies=[Depends(require_admin)])
def admin_attempts(
    gate_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"attempts": services.list_attempts(conn, gate_id=gate_id, limit=limit)}


@app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats(conn: sqlite3.Connection = Depends(get_conn)):
    return services.stats(conn)


# ---------------- 访客预约批次（管理端） ----------------

@app.post("/api/admin/batches", dependencies=[Depends(require_admin)])
def admin_create_batch(body: BatchIn, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        start = parse_dt(body.start_at)
        end = parse_dt(body.end_at)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"时间格式错误: {e}")
    if end <= start:
        raise HTTPException(status_code=400, detail="时段结束必须晚于开始")
    try:
        datetime.strptime(body.visit_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="visit_date 必须是 YYYY-MM-DD")
    if services.get_zone(conn, body.zone_id) is None:
        raise HTTPException(status_code=400, detail=f"未知分区: {body.zone_id}")
    batch = services.create_batch(
        conn,
        visit_date=body.visit_date,
        start_at=iso(start),
        end_at=iso(end),
        zone_id=body.zone_id,
        capacity=body.capacity,
        name=body.name.strip() if body.name else None,
        route_id=body.route_id,
    )
    if batch.get("http_status") == 400:
        raise HTTPException(status_code=400, detail=batch["error"])
    if batch.get("http_status") == 409:
        raise HTTPException(status_code=409, detail=batch["error"])
    conn.commit()
    return batch


@app.get("/api/admin/batches", dependencies=[Depends(require_admin)])
def admin_list_batches(conn: sqlite3.Connection = Depends(get_conn)):
    return {"batches": services.list_batches(conn)}


@app.get("/api/admin/batches/{batch_id}", dependencies=[Depends(require_admin)])
def admin_batch_detail(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = services.batch_detail(conn, batch_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return detail


@app.post("/api/admin/batches/{batch_id}/close", dependencies=[Depends(require_admin)])
def admin_close_batch(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    result = services.close_batch(conn, batch_id=batch_id)
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


@app.put("/api/admin/batches/{batch_id}/capacity", dependencies=[Depends(require_admin)])
def admin_change_capacity(
    batch_id: str, body: CapacityIn, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.change_batch_capacity(
        conn, batch_id=batch_id, new_capacity=body.capacity, reason=body.reason
    )
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


@app.post("/api/admin/batches/{batch_id}/backfill", dependencies=[Depends(require_admin)])
def admin_backfill(
    batch_id: str, body: BackfillIn, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.backfill_application(
        conn,
        batch_id=batch_id,
        name=body.name.strip(),
        contact=body.contact.strip(),
        companions=body.companions,
        companion_names=body.companion_names,
        reason=body.reason,
        approve=body.approve,
    )
    if result["http_status"] == 400:
        raise HTTPException(status_code=400, detail=result["error"])
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    if result["http_status"] == 410:
        raise HTTPException(status_code=410, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return JSONResponse(status_code=201, content=result)


@app.post("/api/admin/applications/{application_id}/approve",
          dependencies=[Depends(require_admin)])
def admin_approve_application(
    application_id: str, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.approve_application(conn, application_id=application_id)
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    if result["http_status"] == 410:
        raise HTTPException(status_code=410, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


@app.post("/api/admin/applications/{application_id}/reject",
          dependencies=[Depends(require_admin)])
def admin_reject_application(
    application_id: str, body: DecideIn, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.reject_application(
        conn, application_id=application_id, reason=body.reason
    )
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


@app.post("/api/admin/applications/{application_id}/cancel",
          dependencies=[Depends(require_admin)])
def admin_cancel_application(
    application_id: str, body: DecideIn, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.cancel_application(
        conn, application_id=application_id, reason=body.reason
    )
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


# ---------------- 申请变更审核（管理端） ----------------

@app.get("/api/admin/changes", dependencies=[Depends(require_admin)])
def admin_list_changes(
    batch_id: Optional[str] = Query(default=None),
    application_id: Optional[str] = Query(default=None),
    status_filter: str = Query(default="", alias="status"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"changes": services.list_changes(
        conn, batch_id=batch_id, application_id=application_id,
        status=status_filter.upper() or None)}


@app.post("/api/admin/changes/{change_id}/approve",
          dependencies=[Depends(require_admin)])
def admin_approve_change(
    change_id: str, body: DecideIn, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.approve_application_change(
        conn, change_id=change_id, reason=body.reason
    )
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    if result["http_status"] == 410:
        raise HTTPException(status_code=410, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


@app.post("/api/admin/changes/{change_id}/reject",
          dependencies=[Depends(require_admin)])
def admin_reject_change(
    change_id: str, body: DecideIn, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.reject_application_change(
        conn, change_id=change_id, reason=body.reason
    )
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


# ---------------- 访客在场清册 / 人工更正 / 应急清点（管理端） ----------------

def _presence_operator(body) -> str:
    return (body.operator.strip() if body.operator and body.operator.strip()
            else "admin")


@app.get("/api/admin/presence", dependencies=[Depends(require_admin)])
def admin_list_presence(
    view: str = Query(default="onsite"),
    zone_id: Optional[str] = Query(default=None),
    batch_id: Optional[str] = Query(default=None),
    person_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """当前在场清册；view=unconfirmed 为未确认离场口径（当前=在场集合）。"""
    status_filter = view.strip().upper()
    try:
        rows = presence_svc.list_presence(
            conn, status_filter=status_filter, zone_id=zone_id,
            batch_id=batch_id, person_id=person_id, q=q, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"view": status_filter, "count": len(rows),
            "summary": presence_svc.roster_summary(conn), "presence": rows}


@app.get("/api/admin/presence/summary", dependencies=[Depends(require_admin)])
def admin_presence_summary(conn: sqlite3.Connection = Depends(get_conn)):
    return presence_svc.roster_summary(conn)


@app.get("/api/admin/presence/{code}", dependencies=[Depends(require_admin)])
def admin_presence_detail(code: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = presence_svc.presence_detail(conn, code)
    if detail is None:
        raise HTTPException(status_code=404, detail="票面或在场记录不存在")
    return detail


@app.post("/api/admin/presence/corrections", dependencies=[Depends(require_admin)])
def admin_presence_correction(
    body: CorrectionIn, conn: sqlite3.Connection = Depends(get_conn)
):
    """漏扫/误扫的人工更正（必须带原因；原始轨迹与操作者保留）。"""
    result = presence_svc.manual_correction(
        conn,
        raw_code=body.code,
        action=body.action.strip().upper(),
        reason=body.reason,
        operator=f"admin:{_presence_operator(body)}",
        party_size=body.party_size,
        companion_names=body.companion_names,
        gate_id=body.gate_id,
    )
    http_status = result.pop("http_status", 200)
    if http_status == 400:
        raise HTTPException(status_code=400, detail=result["error"])
    if http_status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if http_status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=http_status, content=result)


@app.get("/api/admin/presence-events", dependencies=[Depends(require_admin)])
def admin_presence_events(
    code: Optional[str] = Query(default=None),
    person_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"events": presence_svc.list_presence_events(
        conn, code=code, person_id=person_id, limit=limit)}


@app.post("/api/admin/rollcalls", dependencies=[Depends(require_admin)])
def admin_take_rollcall(
    body: RollcallIn, conn: sqlite3.Connection = Depends(get_conn)
):
    """发起应急清点：固定当时在场人员、同行名单、批次分区与最后门点。"""
    result = presence_svc.take_rollcall(
        conn,
        operator=f"admin:{_presence_operator(body)}",
        reason=body.reason,
        zone_id=body.zone_id,
        batch_id=body.batch_id,
    )
    http_status = result.pop("http_status", 201)
    if http_status == 400:
        raise HTTPException(status_code=400, detail=result["error"])
    if http_status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=http_status, content=result)


@app.get("/api/admin/rollcalls", dependencies=[Depends(require_admin)])
def admin_list_rollcalls(
    zone_id: Optional[str] = Query(default=None),
    batch_id: Optional[str] = Query(default=None),
    person_id: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"rollcalls": presence_svc.list_rollcalls(
        conn, zone_id=zone_id, batch_id=batch_id, person_id=person_id)}


@app.get("/api/admin/rollcalls/{rc_id}", dependencies=[Depends(require_admin)])
def admin_rollcall_detail(rc_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    rc = presence_svc.get_rollcall(conn, rc_id)
    if rc is None:
        raise HTTPException(status_code=404, detail="清点快照不存在")
    return rc


# ---------------- 访客路线编排 / 绑定 / 监控（管理端） ----------------

def _route_operator(body=None) -> str:
    val = getattr(body, "operator", None) if body is not None else None
    return f"admin:{val.strip()}" if val and val.strip() else "admin"


@app.post("/api/admin/routes", dependencies=[Depends(require_admin)])
def admin_create_route(body: RouteIn, conn: sqlite3.Connection = Depends(get_conn)):
    result = routes_svc.create_route(
        conn, zone_id=body.zone_id, name=body.name.strip(),
        operator="admin", route_id=body.id.strip() if body.id else None)
    status = result.pop("http_status", 201)
    if status == 400:
        raise HTTPException(status_code=400, detail=result["error"])
    if status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=status, content=result)


@app.get("/api/admin/routes", dependencies=[Depends(require_admin)])
def admin_list_routes(conn: sqlite3.Connection = Depends(get_conn)):
    return {"routes": routes_svc.list_routes(conn),
            "summary": routes_svc.route_monitoring_summary(conn)}


@app.get("/api/admin/routes/{route_id}", dependencies=[Depends(require_admin)])
def admin_route_detail(route_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = routes_svc.route_detail(conn, route_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="路线不存在")
    return detail


@app.post("/api/admin/routes/{route_id}/versions",
          dependencies=[Depends(require_admin)])
def admin_publish_route_version(
    route_id: str, body: RouteVersionIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """发布不可变新版本（替换未来使用；在途执行继续走原版本）。"""
    result = routes_svc.publish_route_version(
        conn,
        route_id=route_id,
        checkpoints=[cp.model_dump() for cp in body.checkpoints],
        note=body.note,
        operator="admin",
    )
    status = result.pop("http_status", 201)
    if status in (400, 404, 409):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=status, content=result)


@app.post("/api/admin/routes/{route_id}/pause",
          dependencies=[Depends(require_admin)])
def admin_pause_route(
    route_id: str, body: RoutePauseIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = routes_svc.pause_route(
        conn, route_id=route_id, reason=body.reason, operator="admin")
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


@app.post("/api/admin/routes/{route_id}/resume",
          dependencies=[Depends(require_admin)])
def admin_resume_route(
    route_id: str, body: RoutePauseIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = routes_svc.resume_route(
        conn, route_id=route_id, reason=body.reason, operator="admin")
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


@app.put("/api/admin/routes/{route_id}/checkpoints",
         dependencies=[Depends(require_admin)])
def admin_set_checkpoint_closed(
    route_id: str, body: CheckpointStateIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """打开/关闭某版本上的检查点（在途访客同样受“已关闭检查点拒绝”约束）。"""
    result = routes_svc.set_checkpoint_closed(
        conn, route_id=route_id, version=body.version, seq=body.seq,
        closed=body.closed, reason=body.reason, operator="admin")
    status = result.pop("http_status", 200)
    if status in (400, 404):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return result


@app.post("/api/admin/routes/bindings", dependencies=[Depends(require_admin)])
def admin_bind_route(body: RouteBindIn, conn: sqlite3.Connection = Depends(get_conn)):
    """把路线绑定到新签发的票（尚未开始）或预约批次（未来使用）。"""
    result = routes_svc.bind_route(
        conn,
        route_id=body.route_id,
        scope=body.scope,
        code=body.code,
        batch_id=body.batch_id,
        operator="admin",
        reason=body.reason,
    )
    status = result.pop("http_status", 200)
    if status in (400, 404, 409):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return result


@app.get("/api/admin/route-progress", dependencies=[Depends(require_admin)])
def admin_route_progress(
    status_filter: str = Query(default="", alias="status"),
    zone_id: Optional[str] = Query(default=None),
    batch_id: Optional[str] = Query(default=None),
    person_id: Optional[str] = Query(default=None),
    route_id: Optional[str] = Query(default=None),
    overdue: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """路线执行监控：当前所在检查点 / 超时停留 / 已完成 / 违规（可多维过滤）。"""
    rows = routes_svc.list_progress(
        conn,
        status_filter=(status_filter.strip().upper() or None),
        zone_id=zone_id, batch_id=batch_id, person_id=person_id,
        route_id=route_id, overdue_only=overdue)
    return {"count": len(rows), "progress": rows,
            "summary": routes_svc.route_monitoring_summary(conn)}


@app.get("/api/admin/route-progress/{code}", dependencies=[Depends(require_admin)])
def admin_route_progress_detail(code: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = routes_svc.progress_detail(conn, code)
    if detail is None:
        raise HTTPException(status_code=404, detail="票面或路线执行不存在")
    return detail


@app.get("/api/admin/route-checks", dependencies=[Depends(require_admin)])
def admin_route_checks(
    gate_id: Optional[str] = Query(default=None),
    route_id: Optional[str] = Query(default=None),
    code: Optional[str] = Query(default=None),
    person_id: Optional[str] = Query(default=None),
    decision: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"checks": routes_svc.list_checks(
        conn, gate_id=gate_id, route_id=route_id, code=code,
        person_id=person_id, decision=decision)}


@app.get("/api/admin/route-conflicts", dependencies=[Depends(require_admin)])
def admin_route_conflicts(
    status_filter: str = Query(default="OPEN", alias="status"),
    zone_id: Optional[str] = Query(default=None),
    batch_id: Optional[str] = Query(default=None),
    person_id: Optional[str] = Query(default=None),
    route_id: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """离线补齐冲突队列（默认只看待处理）。"""
    return {"conflicts": routes_svc.list_conflicts(
        conn, status_filter=status_filter.upper(), zone_id=zone_id,
        batch_id=batch_id, person_id=person_id, route_id=route_id)}


@app.post("/api/admin/route-conflicts/{conflict_id}/resolve",
          dependencies=[Depends(require_admin)])
def admin_resolve_route_conflict(
    conflict_id: int, body: ConflictResolveIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """管理员处理离线冲突：DISMISSED / APPLIED / MARK_VIOLATED（记录保留）。"""
    result = routes_svc.resolve_conflict(
        conn, conflict_id=conflict_id, action=body.action,
        operator=_route_operator(body), reason=body.reason)
    status = result.pop("http_status", 200)
    if status in (400, 404, 409):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return result


# ---------------- 访客授权委托与临时代理核验（管理端） ----------------

def _delegation_operator(body=None) -> str:
    val = getattr(body, "operator", None) if body is not None else None
    return val.strip() if val and val.strip() else "admin"


@app.post("/api/admin/delegations", dependencies=[Depends(require_admin)])
def admin_create_delegation(
    body: DelegationIn, conn: sqlite3.Connection = Depends(get_conn)
):
    """创建带时间、分区、次数和用途的授权委托；高风险需两名不同管理员。"""
    result = delegation_svc.create_delegation(
        conn,
        original_person_id=body.original_person_id,
        proxy_person_id=body.proxy_person_id,
        valid_from=body.valid_from,
        valid_until=body.valid_until,
        zones=[z.strip() for z in body.zones if z.strip()],
        max_uses=body.max_uses,
        purpose=body.purpose,
        high_risk=body.high_risk,
        ticket_code=services.normalize_code(body.ticket_code)
        if body.ticket_code else None,
        approvers=body.approvers,
        operator=f"admin:{_delegation_operator(body)}",
        request_id=body.request_id,
    )
    status = result.pop("http_status", 201)
    if status in (400, 409):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=status, content=result)


@app.get("/api/admin/delegations", dependencies=[Depends(require_admin)])
def admin_list_delegations(
    original_person_id: Optional[str] = Query(default=None),
    proxy_person_id: Optional[str] = Query(default=None),
    zone_id: Optional[str] = Query(default=None),
    status_filter: str = Query(default="", alias="status"),
    valid_from: Optional[str] = Query(default=None),
    valid_to: Optional[str] = Query(default=None),
    active_from: Optional[str] = Query(default=None),
    active_to: Optional[str] = Query(default=None),
    active_at: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"delegations": delegation_svc.list_delegations(
        conn,
        original_person_id=original_person_id,
        proxy_person_id=proxy_person_id,
        zone_id=zone_id,
        status=status_filter.strip().upper() or None,
        start_from=valid_from,
        start_to=valid_to,
        active_from=active_from,
        active_to=active_to,
        active_at=active_at,
    )}


@app.get("/api/admin/delegations/{delegation_id}", dependencies=[Depends(require_admin)])
def admin_delegation_detail(delegation_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = delegation_svc.delegation_detail(conn, delegation_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="授权委托不存在")
    return detail


@app.post("/api/admin/delegations/{delegation_id}/approvals",
          dependencies=[Depends(require_admin)])
def admin_delegation_decision(
    delegation_id: str, body: DelegationDecisionIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """管理员批准/拒绝；高风险委托集齐两名不同管理员批准后才签发凭证。"""
    result = delegation_svc.record_delegation_decision(
        conn, delegation_id=delegation_id, approver_id=body.approver_id,
        action="APPROVE", reason=body.reason)
    status = result.pop("http_status", 200)
    if status in (400, 403, 404, 409, 410):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=status, content=result)


@app.post("/api/admin/delegations/{delegation_id}/rejections",
          dependencies=[Depends(require_admin)])
def admin_reject_delegation(
    delegation_id: str, body: DelegationDecisionIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = delegation_svc.record_delegation_decision(
        conn, delegation_id=delegation_id, approver_id=body.approver_id,
        action="REJECT", reason=body.reason)
    status = result.pop("http_status", 200)
    if status in (400, 403, 404, 409, 410):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return result


@app.post("/api/admin/delegations/{delegation_id}/revoke",
          dependencies=[Depends(require_admin)])
def admin_revoke_delegation(
    delegation_id: str, body: DelegationRevokeIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = delegation_svc.revoke_delegation(
        conn, delegation_id=delegation_id, reason=body.reason,
        operator=f"admin:{_delegation_operator(body)}")
    status = result.pop("http_status", 200)
    if status in (404, 409):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return result


@app.get("/api/admin/proxy-verifications", dependencies=[Depends(require_admin)])
def admin_proxy_verifications(
    original_person_id: Optional[str] = Query(default=None),
    proxy_person_id: Optional[str] = Query(default=None),
    zone_id: Optional[str] = Query(default=None),
    credential_code: Optional[str] = Query(default=None),
    delegation_id: Optional[str] = Query(default=None),
    decision: Optional[str] = Query(default=None),
    reason: Optional[str] = Query(default=None),
    start_ts: Optional[str] = Query(default=None),
    end_ts: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"verifications": delegation_svc.list_proxy_verifications(
        conn,
        original_person_id=original_person_id,
        proxy_person_id=proxy_person_id,
        zone_id=zone_id,
        credential_code=credential_code,
        delegation_id=delegation_id,
        decision=decision,
        reason=reason,
        start_ts=start_ts,
        end_ts=end_ts,
    )}


@app.get("/api/admin/proxy-conflicts", dependencies=[Depends(require_admin)])
def admin_proxy_conflicts(
    status_filter: str = Query(default="OPEN", alias="status"),
    original_person_id: Optional[str] = Query(default=None),
    proxy_person_id: Optional[str] = Query(default=None),
    zone_id: Optional[str] = Query(default=None),
    credential_code: Optional[str] = Query(default=None),
    delegation_id: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"conflicts": delegation_svc.list_proxy_conflicts(
        conn,
        status_filter=status_filter.upper(),
        original_person_id=original_person_id,
        proxy_person_id=proxy_person_id,
        zone_id=zone_id,
        credential_code=credential_code,
        delegation_id=delegation_id,
    )}


@app.post("/api/admin/proxy-conflicts/{conflict_id}/resolve",
          dependencies=[Depends(require_admin)])
def admin_resolve_proxy_conflict(
    conflict_id: int, body: ProxyConflictResolveIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = delegation_svc.resolve_proxy_conflict(
        conn, conflict_id=conflict_id, action=body.action,
        operator=f"admin:{_delegation_operator(body)}", reason=body.reason)
    status = result.pop("http_status", 200)
    if status in (400, 404, 409):
        raise HTTPException(status_code=status, detail=result["error"])
    conn.commit()
    return result


@app.get("/api/admin/proxy-revocations", dependencies=[Depends(require_admin)])
def admin_proxy_revocations(
    original_person_id: Optional[str] = Query(default=None),
    proxy_person_id: Optional[str] = Query(default=None),
    delegation_id: Optional[str] = Query(default=None),
    credential_code: Optional[str] = Query(default=None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"revocations": delegation_svc.list_proxy_revocations(
        conn,
        original_person_id=original_person_id,
        proxy_person_id=proxy_person_id,
        delegation_id=delegation_id,
        credential_code=credential_code,
    )}


@app.get("/api/admin/people/{person_id}/proxy", dependencies=[Depends(require_admin)])
def admin_person_proxy_view(person_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return delegation_svc.person_proxy_view(conn, person_id)


# ---------------- 访客预约（公开：凭链接令牌，无 Bearer） ----------------
@app.get("/api/public/batches/{token}")
def public_batch(token: str, conn: sqlite3.Connection = Depends(get_conn)):
    view = services.public_batch_view(conn, token)
    if view is None:
        raise HTTPException(status_code=404, detail="申请链接无效或批次不存在")
    return view


@app.post("/api/public/batches/{token}/applications")
def public_submit(
    token: str, body: ApplicationSubmitIn, conn: sqlite3.Connection = Depends(get_conn)
):
    result = services.submit_application(
        conn,
        apply_token=token,
        request_id=body.request_id.strip(),
        name=body.name.strip(),
        contact=body.contact.strip(),
        companions=body.companions,
        companion_names=body.companion_names,
    )
    status = result.pop("http_status", 200)
    if status == 400:
        raise HTTPException(status_code=400, detail=result["error"])
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    if status == 410:
        raise HTTPException(status_code=410, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=status, content=result)


@app.get("/api/public/applications/{manage_token}")
def public_application(manage_token: str, conn: sqlite3.Connection = Depends(get_conn)):
    view = services.public_application_view(conn, manage_token)
    if view is None:
        raise HTTPException(status_code=404, detail="申请不存在或链接无效")
    return view


@app.post("/api/public/applications/{manage_token}/changes")
def public_submit_change(
    manage_token: str,
    body: ApplicationChangeIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """访客凭申请的 manage_token 发起变更（公开，无 Bearer）。"""
    result = services.submit_application_change(
        conn,
        manage_token=manage_token,
        request_id=body.request_id.strip(),
        name=body.name.strip(),
        contact=body.contact.strip(),
        companions=body.companions,
        companion_names=body.companion_names,
    )
    status = result.pop("http_status", 200)
    if status == 400:
        raise HTTPException(status_code=400, detail=result["error"])
    if status == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if status == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    if status == 410:
        raise HTTPException(status_code=410, detail=result["error"])
    conn.commit()
    return JSONResponse(status_code=status, content=result)


@app.post("/api/public/changes/{change_id}/cancel")
def public_cancel_change(
    change_id: str,
    body: ChangeTokenIn,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """访客撤回自己的待审核变更（须携带该申请的 manage_token）。"""
    result = services.cancel_application_change(
        conn, manage_token=body.manage_token.strip(), change_id=change_id
    )
    if result["http_status"] == 404:
        raise HTTPException(status_code=404, detail=result["error"])
    if result["http_status"] == 409:
        raise HTTPException(status_code=409, detail=result["error"])
    conn.commit()
    result.pop("http_status", None)
    return result


# ---------------- 门点 API ----------------

@app.post("/api/gate/redeem", dependencies=[Depends(require_gate)])
def gate_redeem(
    body: RedeemIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = services.redeem(
        conn,
        raw_code=body.code,
        gate_id=body.gate_id,
        attempt_id=body.attempt_id,
        policy_version=body.policy_version,
        route_version=body.route_version,
    )
    return JSONResponse(status_code=result["http_status"], content=result["response"])


@app.post("/api/gate/sync", dependencies=[Depends(require_gate)])
def gate_sync(
    body: SyncIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """门点离线重连后按版本号补齐撤销/核销/过期与封锁策略事件。"""
    return services.get_events_since(
        conn, since_version=body.since_version, gate_id=body.gate_id
    )


@app.post("/api/gate/departure", dependencies=[Depends(require_gate)])
def gate_departure(
    body: DepartureIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """门点确认持票人离场（同样以 attempt_id 保证离线重发幂等）。"""
    result = presence_svc.gate_departure(
        conn,
        gate_id=body.gate_id,
        raw_code=body.code,
        attempt_id=body.attempt_id,
    )
    return JSONResponse(
        status_code=result["http_status"], content=result["response"])


@app.post("/api/gate/checkpoint", dependencies=[Depends(require_gate)])
def gate_checkpoint(
    body: CheckpointScanIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """门点上报一次路线检查点经过（已开始路线的逐点推进）。

    同一 (gate_id, attempt_id) 网络抖动重发幂等，绝不重复推进；
    跳过/重复进入/进入已关闭检查点/停留超时均给出门点可直接亮灯的明确结论。
    """
    result = routes_svc.gate_checkpoint(
        conn,
        gate_id=body.gate_id,
        raw_code=body.code,
        attempt_id=body.attempt_id,
        client_route_version=body.route_version,
    )
    return JSONResponse(
        status_code=result["http_status"], content=result["response"])


@app.post("/api/gate/checkpoints/replay", dependencies=[Depends(require_gate)])
def gate_checkpoint_replay(
    body: CheckpointReplayIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """门点离线期间攒下的检查点事件：重连时按版本/顺序批量补齐。

    无法自动裁决的（缺口、迟到、终态后到达、时间戳异常等）不静默丢弃也不
    擅自推进，而是保留冲突记录（route_event_conflicts + ROUTE_CONFLICT 事件）
    交管理员处理；每条事件都返回独立结论。
    """
    result = routes_svc.replay_checkpoint_events(
        conn,
        gate_id=body.gate_id,
        events=[e.model_dump() for e in body.events],
    )
    conn.commit()
    return result


@app.post("/api/gate/proxy/verify", dependencies=[Depends(require_gate)])
def gate_proxy_verify(
    body: ProxyVerifyIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """门点临时代理核验：原持票人、代理人、时间、分区任一不满足即明确拒绝。"""
    result = delegation_svc.verify_proxy(
        conn,
        gate_id=body.gate_id,
        code=body.code,
        attempt_id=body.attempt_id,
        original_person_id=body.original_person_id,
        proxy_person_id=body.proxy_person_id,
        original_ticket_code=body.original_ticket_code,
        event_ts=body.event_ts,
    )
    return JSONResponse(status_code=result["http_status"], content=result["response"])


@app.post("/api/gate/proxy/replay", dependencies=[Depends(require_gate)])
def gate_proxy_replay(
    body: ProxyReplayIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """门点离线期间代理核验记录重连后按现场时间和版本补齐。"""
    result = delegation_svc.replay_proxy_verifications(
        conn,
        gate_id=body.gate_id,
        events=[e.model_dump() for e in body.events],
        base_version=body.base_version,
    )
    conn.commit()
    return result


@app.get("/api/gate/heartbeat/{gate_id}", dependencies=[Depends(require_gate)])
def gate_heartbeat(gate_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    gate = services.get_gate(conn, gate_id)
    if gate is None:
        raise HTTPException(status_code=404, detail="门点未注册")
    return {"ok": not gate["revoked"], "revoked": bool(gate["revoked"])}


# ---------------- 后台过期清扫 ----------------

def _sweeper_loop() -> None:
    conn = connect()
    try:
        while not _stop_sweeper.wait(SWEEP_INTERVAL):
            try:
                expired = services.sweep_expired(conn)
                if expired:
                    app.state.expired_total = getattr(app.state, "expired_total", 0) + len(expired)
                expired_apps = services.sweep_applications(conn)
                if expired_apps:
                    app.state.expired_apps_total = (
                        getattr(app.state, "expired_apps_total", 0) + len(expired_apps)
                    )
                delegation_svc.sweep_expired(conn)
            except sqlite3.Error:
                # 下一轮重试，绝不让清扫线程死掉
                time.sleep(1)
    finally:
        conn.close()


@app.on_event("startup")
def _on_startup() -> None:
    init_db()
    app.state.expired_total = 0
    t = threading.Thread(target=_sweeper_loop, name="expiry-sweeper", daemon=True)
    t.start()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    _stop_sweeper.set()


def main() -> None:
    init_db()
    uvicorn.run(
        app,
        host=os.getenv("PASSPORT_HOST", "0.0.0.0"),
        port=int(os.getenv("PASSPORT_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
