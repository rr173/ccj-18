"""FastAPI 入口：管理端 API + 门点 API + 静态页面 + 后台过期清扫线程。"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import services
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


class IssueIn(BaseModel):
    person_id: str = Field(min_length=1, max_length=128)
    valid_from: Optional[str] = None  # ISO8601，默认现在
    valid_until: Optional[str] = None
    ttl_seconds: Optional[int] = Field(default=None, ge=1, le=365 * 24 * 3600)
    note: Optional[str] = Field(default=None, max_length=500)


class RevokeIn(BaseModel):
    code: str = Field(min_length=1)
    reason: str = Field(default="admin_revoke", max_length=200)


class RedeemIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1, max_length=128)


class SyncIn(BaseModel):
    gate_id: str = Field(min_length=1, max_length=64)
    since_version: int = Field(default=0, ge=0)


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
    ticket = services.issue_ticket(
        conn,
        person_id=body.person_id.strip(),
        valid_from=iso(vf),
        valid_until=iso(vu),
        note=body.note,
    )
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
    limit: int = Query(default=200, ge=1, le=1000),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return {"events": services.list_events(
        conn, since=since, code=code, person_id=person_id, limit=limit
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
    )
    return JSONResponse(status_code=result["http_status"], content=result["response"])


@app.post("/api/gate/sync", dependencies=[Depends(require_gate)])
def gate_sync(
    body: SyncIn,
    gate=Depends(require_active_gate),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """门点离线重连后按版本号补齐撤销/核销/过期结果。"""
    return services.get_events_since(
        conn, since_version=body.since_version, gate_id=body.gate_id
    )


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
