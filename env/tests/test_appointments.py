"""访客预约批次模块的端到端测试（真实 HTTP，复用 conftest 的会话级服务）。

覆盖：批次创建/链接、申请幂等、并发申请不超容量、候补 FIFO 晋级、
取消已审核释放名额并作发票、审核自动签发同分区票、门点看到批次与同行人数、
补录、容量调整与留痕、过期、离线版本补齐、重启持久化、管理批次视图。
"""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from test_system import gate_client


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture(scope="module", autouse=True)
def batch_setup(srv):
    with srv.client() as c:
        for zid, zname in [("ZB1", "批次一区"), ("ZB2", "批次二区")]:
            r = c.post("/api/admin/zones", json={"id": zid, "name": zname})
            assert r.status_code in (200, 409)
        for gid, gname, z in [("gb1", "批次一门", "ZB1"), ("gb2", "批次二门", "ZB2")]:
            r = c.post("/api/admin/gates", json={"id": gid, "name": gname})
            assert r.status_code in (200, 409)
            assert c.put(f"/api/admin/gates/{gid}/zone",
                         json={"zone_id": z}).status_code == 200


def make_batch(srv, capacity=3, zone="ZB1", ttl=3600, name=None, date=None):
    start = datetime.now(timezone.utc)
    end = start + timedelta(seconds=ttl)
    with srv.client() as c:
        r = c.post("/api/admin/batches", json={
            "name": name or f"批次-{uuid.uuid4().hex[:6]}",
            "visit_date": date or start.date().isoformat(),
            "start_at": _iso(start), "end_at": _iso(end),
            "zone_id": zone, "capacity": capacity,
        })
        assert r.status_code == 200, r.text
        return r.json()


def submit(srv, token, name, companions, request_id=None, client=None):
    c = client or _pub(srv)
    return c.post(f"/api/public/batches/{token}/applications", json={
        "request_id": request_id or uuid.uuid4().hex,
        "name": name, "contact": f"{name}@example.com",
        "companions": companions,
    })


def _pub(srv):
    import httpx
    return httpx.Client(base_url=srv.base, timeout=15)


def approve(srv, app_id):
    with srv.client() as c:
        return c.post(f"/api/admin/applications/{app_id}/approve")


def reject(srv, app_id, reason="r"):
    with srv.client() as c:
        return c.post(f"/api/admin/applications/{app_id}/reject", json={"reason": reason})


def cancel(srv, app_id, reason="c"):
    with srv.client() as c:
        return c.post(f"/api/admin/applications/{app_id}/cancel", json={"reason": reason})


def detail(srv, batch_id):
    with srv.client() as c:
        return c.get(f"/api/admin/batches/{batch_id}").json()


# 1. 建批：校验、链接与初始视图
def test_create_batch_and_link(srv):
    b = make_batch(srv, capacity=2)
    assert b["id"].startswith("B-") and b["apply_token"]
    assert b["used"] == 0 and b["remaining"] == 2 and b["accepting"] is True
    # 公开链接可查看批次
    pub = _pub(srv)
    r = pub.get(f"/api/public/batches/{b['apply_token']}")
    assert r.status_code == 200
    v = r.json()
    assert v["id"] == b["id"] and v["capacity"] == 2
    assert "apply_token" not in v  # 令牌本身不回显
    # 无效令牌 404（公开端点无 Bearer）
    assert pub.get("/api/public/batches/nope").status_code == 404
    # 未知分区不能建批
    with srv.client() as c:
        start = datetime.now(timezone.utc)
        r = c.post("/api/admin/batches", json={
            "visit_date": "2026-09-12", "start_at": _iso(start),
            "end_at": _iso(start + timedelta(hours=1)),
            "zone_id": "ZZZ", "capacity": 1})
        assert r.status_code == 400


# 2. 容量内 PENDING、满额 WAITLISTED 且按提交顺序排队
def test_submit_capacity_and_waitlist_order(srv):
    b = make_batch(srv, capacity=4)
    pub = _pub(srv)
    a1 = submit(srv, b["apply_token"], "钱一", 0, client=pub).json()  # 占1
    a2 = submit(srv, b["apply_token"], "孙二", 1, client=pub).json()  # 占2
    a3 = submit(srv, b["apply_token"], "周三", 0, client=pub).json()  # 占4 -> 满
    a4 = submit(srv, b["apply_token"], "吴四", 0, client=pub).json()  # 候补1
    a5 = submit(srv, b["apply_token"], "郑五", 2, client=pub).json()  # 候补2
    assert [a1["status"], a2["status"], a3["status"]] == ["PENDING"] * 3
    assert a4["status"] == "WAITLISTED" and a4["waitlist_position"] == 1
    assert a5["status"] == "WAITLISTED" and a5["waitlist_position"] == 2
    d = detail(srv, b["id"])
    assert d["batch"]["used"] == 4
    assert [a["id"] for a in d["waitlist"]] == [a4["id"], a5["id"]]


# 3. 重复提交幂等：同 request_id 返回首次结果，不同载荷 409
def test_submit_idempotent(srv):
    b = make_batch(srv, capacity=5)
    pub = _pub(srv)
    rid = uuid.uuid4().hex
    r1 = submit(srv, b["apply_token"], "冯重", 1, request_id=rid, client=pub)
    r2 = submit(srv, b["apply_token"], "冯重", 1, request_id=rid, client=pub)
    r3 = submit(srv, b["apply_token"], "冯重", 1, request_id=rid, client=pub)
    assert r1.status_code == 201 and r2.status_code == 200 and r3.json()["replayed"] is True
    first = r1.json()
    assert r3.json()["id"] == first["id"]
    d = detail(srv, b["id"])
    same = [a for a in d["applications"] if a["request_id"] == rid]
    assert len(same) == 1  # 没有产生第二条申请

    # 同一 request_id 不同内容 -> 冲突，不覆盖
    rbad = pub.post(f"/api/public/batches/{b['apply_token']}/applications", json={
        "request_id": rid, "name": "改名", "contact": "x", "companions": 3})
    assert rbad.status_code == 409


# 4. 并发申请不超过容量（同批次 20 组各 1 人、容量 10，恰 10 个在容量内）
def test_concurrent_submit_never_oversells(srv):
    b = make_batch(srv, capacity=10)
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(i):
        pub = _pub(srv)
        barrier.wait()
        r = submit(srv, b["apply_token"], f"并发{i}", 0, client=pub)
        with lock:
            results.append(r)

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(worker, range(20)))

    assert all(r.status_code in (201,) for r in results)
    bodies = [r.json() for r in results]
    pending = [x for x in bodies if x["status"] == "PENDING"]
    waiting = [x for x in bodies if x["status"] == "WAITLISTED"]
    assert len(pending) == 10 and len(waiting) == 10
    d = detail(srv, b["id"])
    assert d["batch"]["used"] == 10
    # 候补名次恰好覆盖 1..10
    pos = sorted(a["waitlist_position"] for a in d["waitlist"])
    assert pos == list(range(1, 11))


# 5. 审核通过自动签发与批次分区一致的票；候补不能越级通过
def test_approve_issues_ticket_and_zone(srv):
    b1 = make_batch(srv, capacity=2, zone="ZB1")
    pub = _pub(srv)
    a = submit(srv, b1["apply_token"], "王审", 1, client=pub).json()
    r = approve(srv, a["id"])
    assert r.status_code == 200, r.text
    t = r.json()["ticket"]
    assert t["zones"] == ["ZB1"]
    assert t["batch_id"] == b1["id"] and t["application_id"] == a["id"]
    assert t["party_size"] == 2 and t["status"] == "ACTIVE"
    # 重复审核幂等：返回同一张票，duplicated=true
    r2 = approve(srv, a["id"])
    assert r2.status_code == 200 and r2.json().get("duplicated") is True
    assert r2.json()["ticket"]["code"] == t["code"]

    # 满额后的候补不能直接通过
    full = make_batch(srv, capacity=1)
    submit(srv, full["apply_token"], "占座", 0, client=pub)
    w = submit(srv, full["apply_token"], "候补", 0, client=pub).json()
    assert w["status"] == "WAITLISTED"
    assert approve(srv, w["id"]).status_code == 409

    # 门点核销：批次/同行人数可见；错分区拒绝且不消耗
    r_gate = gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gb2", "code": t["code"], "attempt_id": str(uuid.uuid4())})
    assert r_gate.status_code == 403 and r_gate.json()["reason"] == "zone_mismatch"
    r_ok = gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gb1", "code": t["code"], "attempt_id": str(uuid.uuid4())})
    assert r_ok.status_code == 200, r_ok.text
    appt = r_ok.json()["appointment"]
    assert appt["batch_id"] == b1["id"]
    assert appt["party_size"] == 2 and appt["applicant_name"] == "王审"
    assert appt["zone_id"] == "ZB1"


# 6. 取消已审核申请：同事务作废票据 + 按候补顺序释放名额
def test_cancel_approved_revokes_ticket_and_promotes_fifo(srv):
    b = make_batch(srv, capacity=3)
    pub = _pub(srv)
    a1 = submit(srv, b["apply_token"], "甲", 0, client=pub).json()  # 1
    a2 = submit(srv, b["apply_token"], "乙", 1, client=pub).json()  # 占2（共3）满
    w1 = submit(srv, b["apply_token"], "候补甲", 1, client=pub).json()  # 需2放不下
    w2 = submit(srv, b["apply_token"], "候补乙", 0, client=pub).json()  # 需1但队首轮空（FIFO 不跳过）
    assert w1["waitlist_position"] == 1 and w2["waitlist_position"] == 2

    t1 = approve(srv, a1["id"]).json()["ticket"]
    r = cancel(srv, a1["id"], reason="访客不来了")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_revoked"] == t1["code"]
    # 释放 1 个名额：队首 w1 需 2 人放不下 -> 严格 FIFO 不跳过，无人晋级
    assert body["promoted"] == []
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{t1['code']}").json()["status"] == "REVOKED"

    # 再取消占 2 人的乙（待审核拒绝同样释放）：先放 2 个名额 -> w1 晋级；剩 1 -> w2 晋级
    r = reject(srv, a2["id"])
    assert r.status_code == 200
    promoted = r.json()["promoted"]
    assert promoted == [w1["id"], w2["id"]]
    d = detail(srv, b["id"])
    st = {a["id"]: a["status"] for a in d["applications"]}
    assert st[w1["id"]] == "PENDING" and st[w2["id"]] == "PENDING"
    assert all(a["promoted_at"] for a in d["applications"] if a["id"] in promoted)

    # 候补未晋级阶段绝不会有票
    before_promote_ticket = next(
        (a for a in d["tickets"] if a["application_id"] == w2["id"]), None)
    assert before_promote_ticket is None
    # 晋级后通过审核才有票
    tw2 = approve(srv, w2["id"]).json()["ticket"]
    assert tw2["zones"] == ["ZB1"]


# 7. 票已核销（人已到场）的已审核申请不能取消
def test_cannot_cancel_after_redeem(srv):
    b = make_batch(srv, capacity=1)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "到场客", 0, client=pub).json()
    t = approve(srv, a["id"]).json()["ticket"]
    assert gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gb1", "code": t["code"],
        "attempt_id": str(uuid.uuid4())}).status_code == 200
    r = cancel(srv, a["id"])
    assert r.status_code == 409


# 8. 终态不可逆 + 过期清扫：未决申请到期 EXPIRED 且无票
def test_terminal_and_expiry(srv):
    b = make_batch(srv, capacity=1, ttl=1)
    pub = _pub(srv)
    a1 = submit(srv, b["apply_token"], "短批甲", 0, client=pub).json()
    a2 = submit(srv, b["apply_token"], "短批候补", 0, client=pub).json()
    # 再次取消/拒绝已取消的 -> 409
    assert cancel(srv, a1["id"]).status_code == 200
    assert cancel(srv, a1["id"]).status_code == 409
    assert reject(srv, a1["id"]).status_code == 409
    # 批次结束后清扫线程把候补置 EXPIRED
    time.sleep(2.3)
    d = detail(srv, b["id"])
    st = {a["id"]: a["status"] for a in d["applications"]}
    assert st[a2["id"]] == "EXPIRED"
    assert d["tickets"] == []  # 取消、过期、候补未晋级都不产生票
    # 批次结束后不能再提交
    r = submit(srv, b["apply_token"], "迟到", 0, client=pub)
    assert r.status_code == 410
    # 批次结束后补录并通过也被拒绝
    with srv.client() as c:
        r = c.post(f"/api/admin/batches/{b['id']}/backfill", json={
            "name": "晚补", "contact": "x", "companions": 0})
        assert r.status_code == 410


# 9. 管理员补录：立即通过签票，同样受容量约束
def test_backfill(srv):
    b = make_batch(srv, capacity=2)
    with srv.client() as c:
        r = c.post(f"/api/admin/batches/{b['id']}/backfill", json={
            "name": "补录甲", "contact": "vip", "companions": 1})
        assert r.status_code == 201, r.text
        app = r.json()["application"]
        t = r.json()["ticket"]
        assert app["source"] == "admin" and app["status"] == "APPROVED"
        assert t["party_size"] == 2 and t["zones"] == ["ZB1"]
        # 再补录 1 人（共占3）超容量 -> 409，不留“通过无票”
        r2 = c.post(f"/api/admin/batches/{b['id']}/backfill", json={
            "name": "补录乙", "contact": "vip2", "companions": 0})
        assert r2.status_code == 409
    d = detail(srv, b["id"])
    assert d["batch"]["used"] == 2


# 10. 容量调整：下调受占座约束、上调自动晋级候补，全部 append-only 留痕
def test_capacity_change_and_log(srv):
    b = make_batch(srv, capacity=1)
    pub = _pub(srv)
    submit(srv, b["apply_token"], "容量甲", 0, client=pub)
    w = submit(srv, b["apply_token"], "容量候补", 0, client=pub).json()
    with srv.client() as c:
        # pydantic 层面容量必须 >=1（422）
        r = c.put(f"/api/admin/batches/{b['id']}/capacity",
                  json={"capacity": 0, "reason": "x"})
        assert r.status_code == 422
        # 已占 1，与现值相同 -> duplicated，无新事件
        r = c.put(f"/api/admin/batches/{b['id']}/capacity",
                  json={"capacity": 1, "reason": "持平"})
        assert r.status_code == 200 and r.json().get("duplicated") is True
        # 上调到 3：候补晋级
        r = c.put(f"/api/admin/batches/{b['id']}/capacity",
                  json={"capacity": 3, "reason": "加位"})
        assert r.status_code == 200
        assert w["id"] in r.json()["promoted"]
        # 晋级后占座 2，下调到 1（低于占座）被拒绝
        r = c.put(f"/api/admin/batches/{b['id']}/capacity",
                  json={"capacity": 1, "reason": "硬减"})
        assert r.status_code == 409
    d = detail(srv, b["id"])
    reasons = [l["reason"] for l in d["capacity_log"]]
    pairs = [(l["old_capacity"], l["new_capacity"]) for l in d["capacity_log"]]
    assert pairs[0] == (None, 1) and (1, 3) in pairs
    assert "batch_created" in reasons and "加位" in reasons


# 11. 离线重连：门点按版本补齐预约/票据事件
def test_gate_sync_appointment_events(srv):
    b = make_batch(srv, capacity=2)
    c = gate_client(srv)
    since = c.post("/api/gate/sync",
                   json={"gate_id": "gb1", "since_version": 0}).json()["next_since"]
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "同步客", 0, client=pub).json()
    t = approve(srv, a["id"]).json()["ticket"]
    batch = c.post("/api/gate/sync",
                   json={"gate_id": "gb1", "since_version": since}).json()
    types = [e["type"] for e in batch["events"]]
    assert "APPLICATION_SUBMITTED" in types
    assert "APPLICATION_APPROVED" in types
    appr = next(e for e in batch["events"] if e["type"] == "APPLICATION_APPROVED")
    assert appr["application_id"] == a["id"]
    assert appr["batch_id"] == b["id"]
    assert appr["ticket_code"] == t["code"]
    # 事件按版本有序、游标不丢不重
    versions = [e["version"] for e in batch["events"]]
    assert versions == sorted(versions)
    again = c.post("/api/gate/sync",
                   json={"gate_id": "gb1", "since_version": batch["next_since"]}).json()
    assert again["events"] == []


# 12. 管理批次视图：申请、候补顺序、已签发票、容量记录俱全
def test_admin_batch_detail_view(srv):
    b = make_batch(srv, capacity=2, name="视图批")
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "视图客", 0, client=pub).json()
    approve(srv, a["id"])
    with srv.client() as c:
        d = c.get(f"/api/admin/batches/{b['id']}").json()
        assert d["batch"]["id"] == b["id"] and d["batch"]["zone_id"] == "ZB1"
        assert any(x["id"] == a["id"] for x in d["applications"])
        assert any(t["application_id"] == a["id"] for t in d["tickets"])
        assert any(l["old_capacity"] is None and l["new_capacity"] == 2
                   for l in d["capacity_log"])
        ev_types = {e["type"] for e in d["events"]}
        assert {"BATCH_CREATED", "APPLICATION_SUBMITTED",
                "APPLICATION_APPROVED", "TICKET_ISSUED"} <= ev_types
        assert c.get("/api/admin/batches/B-NOPE").status_code == 404
        # 列表包含本批
        ids = [x["id"] for x in c.get("/api/admin/batches").json()["batches"]]
        assert b["id"] in ids


# 13. 重启后申请/批次/票状态与版本号延续
def test_restart_persistence(srv):
    b = make_batch(srv, capacity=2)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "重启客", 0, client=pub).json()
    t = approve(srv, a["id"]).json()["ticket"]
    version_before = srv.client().get("/api/admin/stats").json()["last_version"]

    srv.restart()

    with srv.client() as c:
        d = c.get(f"/api/admin/batches/{b['id']}").json()
        app = next(x for x in d["applications"] if x["id"] == a["id"])
        assert app["status"] == "APPROVED" and app["ticket_code"] == t["code"]
        assert c.get(f"/api/admin/tickets/{t['code']}").json()["status"] == "ACTIVE"
        assert c.get("/api/admin/stats").json()["last_version"] == version_before
        # 申请链接依然可用、幂等重放仍是同一申请
        r = pub.post(f"/api/public/batches/{b['apply_token']}/applications", json={
            "request_id": a["request_id"], "name": "重启客",
            "contact": "重启客@example.com", "companions": 0})
        assert r.status_code == 200 and r.json()["id"] == a["id"]
