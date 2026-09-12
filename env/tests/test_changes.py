"""申请变更 + 同行名单确认的端到端测试（真实 HTTP，复用 conftest 的会话级服务）。

覆盖：
- 变更请求幂等（同 request_id 重放/不同载荷 409）、每申请仅一个 PENDING、
  无差异 noop、终态/已核销不可变更；
- 已通过申请变更：同事务原子撤销旧票+签发新票（无中间两可用票）、
  新票带新人数/名单/旧票号/替换原因、旧票门禁 410 并指向新票、
  旧票核销记录不丢、变更版本号递增；
- 并发变更审批不突破批次容量；候补按新总人数重排（缩小释放→FIFO 晋级，
  候补自身缩小→可晋级，PENDING 放大超容量 409）；
- 拒绝/撤回/过期变更不产生票；申请被取消/拒绝联动终结待审变更；
- 批次结束清扫把待审变更置 EXPIRED；
- 门点同步按版本补齐 提交/撤销/签发 事件；门点核销看到版本+名单+替换原因；
- 重启后变更版本、新旧票替换链延续。
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
def change_setup(srv):
    with srv.client() as c:
        for zid, zname in [("ZC1", "变更一区"), ("ZC2", "变更二区")]:
            r = c.post("/api/admin/zones", json={"id": zid, "name": zname})
            assert r.status_code in (200, 409)
        for gid, gname, z in [("gc1", "变更一门", "ZC1"), ("gc2", "变更二门", "ZC2")]:
            r = c.post("/api/admin/gates", json={"id": gid, "name": gname})
            assert r.status_code in (200, 409)
            assert c.put(f"/api/admin/gates/{gid}/zone",
                         json={"zone_id": z}).status_code == 200


def make_batch(srv, capacity=3, zone="ZC1", ttl=3600, name=None):
    start = datetime.now(timezone.utc)
    end = start + timedelta(seconds=ttl)
    with srv.client() as c:
        r = c.post("/api/admin/batches", json={
            "name": name or f"变更批-{uuid.uuid4().hex[:6]}",
            "visit_date": start.date().isoformat(),
            "start_at": _iso(start), "end_at": _iso(end),
            "zone_id": zone, "capacity": capacity,
        })
        assert r.status_code == 200, r.text
        return r.json()


def _pub(srv):
    import httpx
    return httpx.Client(base_url=srv.base, timeout=15)


def submit(srv, token, name, companions, names=None, client=None, rid=None):
    c = client or _pub(srv)
    body = {"request_id": rid or uuid.uuid4().hex,
            "name": name, "contact": f"{name}@example.com",
            "companions": companions}
    if names is not None:
        body["companion_names"] = names
    return c.post(f"/api/public/batches/{token}/applications", json=body)


def approve(srv, app_id):
    with srv.client() as c:
        return c.post(f"/api/admin/applications/{app_id}/approve")


def detail(srv, batch_id):
    with srv.client() as c:
        return c.get(f"/api/admin/batches/{batch_id}").json()


def app_view(srv, token):
    return _pub(srv).get(f"/api/public/applications/{token}")


def change_req(srv, manage_token, name, companions, names=None,
               rid=None, client=None):
    c = client or _pub(srv)
    body = {"request_id": rid or uuid.uuid4().hex,
            "name": name, "contact": f"{name}@example.com",
            "companions": companions}
    if names is not None:
        body["companion_names"] = names
    return c.post(f"/api/public/applications/{manage_token}/changes", json=body)


def approve_change(srv, change_id, reason="ok"):
    with srv.client() as c:
        return c.post(f"/api/admin/changes/{change_id}/approve",
                      json={"reason": reason})


def reject_change(srv, change_id, reason="no"):
    with srv.client() as c:
        return c.post(f"/api/admin/changes/{change_id}/reject",
                      json={"reason": reason})


# 1. 提交时即可登记同行人名单；名单数量必须与同行人数一致
def test_submit_with_companion_names(srv):
    b = make_batch(srv, capacity=5)
    r = submit(srv, b["apply_token"], "名单甲", 2,
               names=["同行A", "同行B"]).json()
    assert r["status"] == "PENDING"
    v = app_view(srv, r["manage_token"]).json()
    assert v["companion_names"] == ["同行A", "同行B"]
    assert v["change_version"] == 0
    # 名单数量与人数不一致 -> 400
    bad = submit(srv, b["apply_token"], "名单乙", 1, names=["x", "y"])
    assert bad.status_code == 400


# 2. 变更请求幂等：同 request_id 重放返回首次结果，不同载荷 409；
#    每申请同时仅一个 PENDING；无差异 noop
def test_change_request_idempotent_and_single_pending(srv):
    b = make_batch(srv, capacity=5)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "变更甲", 0, client=pub).json()
    mt = a["manage_token"]

    rid = uuid.uuid4().hex
    r1 = change_req(srv, mt, "变更甲新", 1, names=["同伴一"], rid=rid, client=pub)
    assert r1.status_code == 201, r1.text
    cid = r1.json()["id"]
    r2 = change_req(srv, mt, "变更甲新", 1, names=["同伴一"], rid=rid, client=pub)
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    assert r2.json()["id"] == cid
    # 同键不同载荷 -> 409
    rbad = change_req(srv, mt, "完全不同", 2, names=["x", "y"], rid=rid, client=pub)
    assert rbad.status_code == 409
    # 已有待审变更 -> 409（不会产生第二条）
    ragain = change_req(srv, mt, "另一个", 0, client=pub)
    assert ragain.status_code == 409
    d = detail(srv, b["id"])
    assert len([c for c in d["changes"] if c["application_id"] == a["id"]]) == 1
    # 无差异提交 -> noop，不产生变更单
    rnoop = change_req(srv, mt, "变更甲", 0, names=[], client=pub)
    assert rnoop.status_code == 200 and rnoop.json()["noop"] is True


# 3. 已通过申请的变更通过：原子撤销旧票 + 签发新票，版本/名单/替换链俱全
def test_approved_change_atomic_ticket_swap(srv):
    b = make_batch(srv, capacity=5)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "换票甲", 0, client=pub).json()
    t1 = approve(srv, a["id"]).json()["ticket"]
    assert t1["status"] == "ACTIVE"

    cr = change_req(srv, a["manage_token"], "换票甲", 2,
                    names=["同伴A", "同伴B"], client=pub).json()
    r = approve_change(srv, cr["id"], reason="加两人")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_ticket_code"] == t1["code"]
    t2 = body["new_ticket"]
    assert t2["code"] != t1["code"] and t2["status"] == "ACTIVE"
    assert t2["party_size"] == 3 and t2["replaced_code"] == t1["code"]
    assert "加两人" in t2["replacement_reason"]
    # 申请已指向新票、版本号 +1、资料与名单已更新
    app2 = body["application"]
    assert app2["ticket_code"] == t2["code"]
    assert app2["change_version"] == 1
    assert app2["companions"] == 2 and app2["companion_names"] == ["同伴A", "同伴B"]

    with srv.client() as c:
        old = c.get(f"/api/admin/tickets/{t1['code']}").json()
        assert old["status"] == "REVOKED"
        assert old["replaced_by_code"] == t2["code"]
        assert "加两人" in old["revoked_reason"]
    # 任何时刻不会有两张可用票：旧票已 REVOKED，新票 ACTIVE
    d = detail(srv, b["id"])
    active = [t for t in d["tickets"] if t["status"] == "ACTIVE"
              and t["application_id"] == a["id"]]
    assert [t["code"] for t in active] == [t2["code"]]

    # 门点核销旧票：410 revoked，且明确告知替换原因与新票号
    r_old = gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gc1", "code": t1["code"], "attempt_id": str(uuid.uuid4())})
    assert r_old.status_code == 410
    oj = r_old.json()
    assert oj["reason"] == "revoked"
    assert oj["appointment"]["replaced_by_code"] == t2["code"]
    assert oj["replaced_by_code"] == t2["code"]
    assert t2["code"] in oj["reason_text"]

    # 门点核销新票成功：看到当前变更版本、同行名单、旧票替换原因
    r_new = gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gc1", "code": t2["code"], "attempt_id": str(uuid.uuid4())})
    assert r_new.status_code == 200, r_new.text
    appt = r_new.json()["appointment"]
    assert appt["party_size"] == 3
    assert appt["change_version"] == 1
    assert appt["companion_names"] == ["同伴A", "同伴B"]
    assert appt["replaced_code"] == t1["code"]
    assert "加两人" in appt["replacement_reason"]


# 4. 旧票已核销（访客已到场）：提交即拒、审批也拒，任何路径不得再变更
def test_redeemed_application_never_changeable(srv):
    b = make_batch(srv, capacity=2)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "到场变更", 0, client=pub).json()
    t = approve(srv, a["id"]).json()["ticket"]
    assert gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gc1", "code": t["code"],
        "attempt_id": str(uuid.uuid4())}).status_code == 200
    # 核销后提交变更 -> 409
    r = change_req(srv, a["manage_token"], "到场变更X", 0, client=pub)
    assert r.status_code == 409
    # 审核与核销竞态：先提交（票未核销时允许），核销后审批 -> 409，旧票保持 REDEEMED
    b2 = make_batch(srv, capacity=2)
    a2 = submit(srv, b2["apply_token"], "竞态变更", 0, client=pub).json()
    t2c = approve(srv, a2["id"]).json()["ticket"]
    cr = change_req(srv, a2["manage_token"], "竞态变更", 1,
                    names=["p"], client=pub).json()
    assert gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gc1", "code": t2c["code"],
        "attempt_id": str(uuid.uuid4())}).status_code == 200
    r = approve_change(srv, cr["id"])
    assert r.status_code == 409
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{t2c['code']}").json()["status"] == "REDEEMED"
    # 该变更仍可被拒绝（终态处理，不复活任何票）
    assert reject_change(srv, cr["id"]).status_code == 200


# 5. 并发变更审批不突破容量：容量3，3 个已通过各1人；两个变更并发——
#    甲 1→2（3人变4 >3，放不下），乙保持 1 人（只改名，占用不变）。
#    IMMEDIATE 串行下：乙通过，甲 409，used 恒为 3
def test_concurrent_changes_never_break_capacity(srv):
    b = make_batch(srv, capacity=3)
    pub = _pub(srv)
    apps = []
    for i in range(3):
        a = submit(srv, b["apply_token"], f"并发变{i}", 0, client=pub).json()
        approve(srv, a["id"])
        apps.append(a)
    cr_grow = change_req(srv, apps[0]["manage_token"], "并发变0加人", 1,
                         names=["加0"], client=pub).json()
    cr_keep = change_req(srv, apps[1]["manage_token"], "并发变1改名", 0,
                         client=pub).json()
    ids = [cr_grow["id"], cr_keep["id"]]

    results = []
    barrier = threading.Barrier(2)

    def worker(cid):
        c = srv.client()
        barrier.wait()
        results.append((cid, c.post(f"/api/admin/changes/{cid}/approve",
                                    json={"reason": "并发变更"})))

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(worker, ids))

    by_id = {cid: r for cid, r in results}
    assert by_id[cr_keep["id"]].status_code == 200, by_id[cr_keep["id"]].text
    assert by_id[cr_grow["id"]].status_code == 409
    d = detail(srv, b["id"])
    assert d["batch"]["used"] == 3
    # 放不下的变更仍 PENDING（可拒绝/撤回），对应申请人数未变
    app0 = next(x for x in d["applications"] if x["id"] == apps[0]["id"])
    assert app0["party_size"] == 1
    assert len(d["pending_changes"]) >= 1
    active = [t for t in d["tickets"] if t["status"] == "ACTIVE"]
    assert len(active) == 3


# 6. 候补按新总人数重排：
#    容量3：A占2(待审)，W候补(2人)放不下；A 变更缩为1人通过 -> W 按 FIFO 晋级
def test_waitlist_reordered_by_new_party_size(srv):
    b = make_batch(srv, capacity=3)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "缩容甲", 1, client=pub).json()  # 2席
    assert a["status"] == "PENDING"
    w = submit(srv, b["apply_token"], "缩容候补", 1, client=pub).json()  # 需2
    assert w["status"] == "WAITLISTED" and w["waitlist_position"] == 1

    cr = change_req(srv, a["manage_token"], "缩容甲", 0, client=pub).json()
    r = approve_change(srv, cr["id"])
    assert r.status_code == 200
    assert w["id"] in r.json()["promoted"]
    d = detail(srv, b["id"])
    st = {x["id"]: x["status"] for x in d["applications"]}
    assert st[a["id"]] == "PENDING" and st[w["id"]] == "PENDING"
    app_a = next(x for x in d["applications"] if x["id"] == a["id"])
    assert app_a["party_size"] == 1 and app_a["change_version"] == 1

    # 候补自身把人数缩小后能否晋级，仍受严格 FIFO 约束：
    # 容量2：P 待审核占2（满），队首候补 W1 需2（放不下），队尾 W2 需1。
    # W1 变更缩为1人通过 -> 仍满放不下；再把 P 缩为1人通过 -> 释放1席，
    # W1（新总人数1）按 FIFO 晋级，W2 仍候补——全程按新人数重排。
    b2 = make_batch(srv, capacity=2)
    p = submit(srv, b2["apply_token"], "占位", 1, client=pub).json()  # 2席满
    w1 = submit(srv, b2["apply_token"], "候补队首", 1, client=pub).json()  # 需2
    w2 = submit(srv, b2["apply_token"], "候补队尾", 0, client=pub).json()  # 需1
    assert [w1["waitlist_position"], w2["waitlist_position"]] == [1, 2]
    cr_w1 = change_req(srv, w1["manage_token"], "候补队首", 0, client=pub).json()
    r_w1 = approve_change(srv, cr_w1["id"])
    assert r_w1.status_code == 200 and r_w1.json()["promoted"] == []
    cr_p = change_req(srv, p["manage_token"], "占位", 0, client=pub).json()
    r_p = approve_change(srv, cr_p["id"])
    assert w1["id"] in r_p.json()["promoted"] and w2["id"] not in r_p.json()["promoted"]
    d2 = detail(srv, b2["id"])
    st2 = {x["id"]: x["status"] for x in d2["applications"]}
    assert st2[w1["id"]] == "PENDING" and st2[w2["id"]] == "WAITLISTED"
    w1_app = next(x for x in d2["applications"] if x["id"] == w1["id"])
    assert w1_app["party_size"] == 1  # 按新总人数占1席完成晋级

    # PENDING 放大到超容量 -> 409，申请与变更都不变
    cr_big = change_req(srv, p["manage_token"], "占位大", 2,
                        names=["x", "y"], client=pub).json()
    assert approve_change(srv, cr_big["id"]).status_code == 409


# 7. 被拒绝 / 撤回 / 取消 / 过期的变更不产生可用票
def test_rejected_cancelled_expired_change_no_ticket(srv):
    b = make_batch(srv, capacity=2)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "被拒变", 0, client=pub).json()
    t = approve(srv, a["id"]).json()["ticket"]
    cr = change_req(srv, a["manage_token"], "被拒变", 1, names=["x"], client=pub).json()
    assert reject_change(srv, cr["id"]).status_code == 200
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{t['code']}").json()["status"] == "ACTIVE"
    v = app_view(srv, a["manage_token"]).json()
    assert v["change_version"] == 0 and v["ticket"]["code"] == t["code"]

    # 访客撤回：再提一个变更后撤回，旧票不动；撤回后可重新提交
    cr2 = change_req(srv, a["manage_token"], "撤回变", 0, client=pub).json()
    rc = pub.post(f"/api/public/changes/{cr2['id']}/cancel",
                  json={"manage_token": a["manage_token"]})
    assert rc.status_code == 200 and rc.json()["change"]["status"] == "CANCELLED"
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{t['code']}").json()["status"] == "ACTIVE"

    # 申请被取消：其待审变更联动 CANCELLED，不产生票
    b3 = make_batch(srv, capacity=2)
    a3 = submit(srv, b3["apply_token"], "连带取消", 0, client=pub).json()
    t3 = approve(srv, a3["id"]).json()["ticket"]
    cr3 = change_req(srv, a3["manage_token"], "连带取消", 1, names=["y"], client=pub).json()
    with srv.client() as c:
        rr = c.post(f"/api/admin/applications/{a3['id']}/cancel",
                    json={"reason": "不来了"})
        assert rr.status_code == 200
    d3 = detail(srv, b3["id"])
    ch3 = next(x for x in d3["changes"] if x["id"] == cr3["id"])
    assert ch3["status"] == "CANCELLED"
    revoked = [x for x in d3["tickets"] if x["code"] == t3["code"]][0]
    assert revoked["status"] == "REVOKED"

    # 批次结束清扫：待审变更 EXPIRED，申请资料与票均无变化路径（候补无票）
    b4 = make_batch(srv, capacity=1, ttl=1)
    a4 = submit(srv, b4["apply_token"], "过期变", 0, client=pub).json()
    cr4 = change_req(srv, a4["manage_token"], "过期变改名", 0, client=pub).json()
    assert cr4["id"]
    time.sleep(2.3)
    d4 = detail(srv, b4["id"])
    ch4 = next(x for x in d4["changes"] if x["id"] == cr4["id"])
    assert ch4["status"] == "EXPIRED"
    assert d4["tickets"] == []
    # 过期变更不能再审批
    assert approve_change(srv, cr4["id"]).status_code == 409


# 8. 门点离线重连按版本补齐：变更提交、旧票撤销、新票签发事件按序到达
def test_gate_sync_change_events(srv):
    b = make_batch(srv, capacity=3)
    c = gate_client(srv)
    since = c.post("/api/gate/sync",
                   json={"gate_id": "gc1", "since_version": 0}).json()["next_since"]
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "同步变更", 0, client=pub).json()
    t1 = approve(srv, a["id"]).json()["ticket"]
    cr = change_req(srv, a["manage_token"], "同步变更", 1, names=["伴"], client=pub).json()
    t2 = approve_change(srv, cr["id"]).json()["new_ticket"]["code"]

    batch = c.post("/api/gate/sync",
                   json={"gate_id": "gc1", "since_version": since}).json()
    versions = [e["version"] for e in batch["events"]]
    assert versions == sorted(versions)
    types = [e["type"] for e in batch["events"]]
    assert "APPLICATION_CHANGE_SUBMITTED" in types
    assert "APPLICATION_CHANGE_APPROVED" in types
    approved = next(e for e in batch["events"]
                    if e["type"] == "APPLICATION_CHANGE_APPROVED")
    assert approved["payload"]["old_ticket_code"] == t1["code"]
    assert approved["payload"]["new_ticket_code"] == t2
    revoke = next(e for e in batch["events"] if e["type"] == "TICKET_REVOKED"
                  and e["ticket_code"] == t1["code"])
    issue = next(e for e in batch["events"] if e["type"] == "TICKET_ISSUED"
                 and e["ticket_code"] == t2)
    # 顺序：撤销旧票 -> 签发新票（原子事务内的版本先后）
    assert revoke["version"] < issue["version"]
    assert revoke["payload"]["replaced_by_code"] == t2
    assert issue["payload"]["replaced_code"] == t1["code"]
    # 游标不丢不重
    again = c.post("/api/gate/sync",
                   json={"gate_id": "gc1",
                         "since_version": batch["next_since"]}).json()
    assert again["events"] == []


# 9. 管理员批次详情显示当前变更版本、同行名单、变更审核队列与旧票替换原因
def test_admin_detail_shows_version_names_and_changes(srv):
    b = make_batch(srv, capacity=4)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "详情变", 0, client=pub).json()
    t1 = approve(srv, a["id"]).json()["ticket"]
    cr = change_req(srv, a["manage_token"], "详情变新", 1,
                    names=["同事X"], client=pub).json()
    approve_change(srv, cr["id"], reason="行政确认")
    d = detail(srv, b["id"])
    app = next(x for x in d["applications"] if x["id"] == a["id"])
    assert app["change_version"] == 1
    assert app["companion_names"] == ["同事X"] and app["party_size"] == 2
    ch = next(x for x in d["changes"] if x["id"] == cr["id"])
    assert ch["status"] == "APPROVED" and ch["new_ticket_code"] == app["ticket_code"]
    assert ch["old_ticket_code"] == t1["code"]
    new_t = next(t for t in d["tickets"] if t["code"] == app["ticket_code"])
    old_t = next(t for t in d["tickets"] if t["code"] == t1["code"])
    assert new_t["replaced_code"] == t1["code"]
    assert "行政确认" in (old_t["revoked_reason"] or "")
    # 管理端可按状态过滤变更队列
    with srv.client() as c:
        r = c.get("/api/admin/changes", params={"batch_id": b["id"], "status": "PENDING"})
        assert all(x["status"] == "PENDING" for x in r.json()["changes"])
        r2 = c.get("/api/admin/changes", params={"batch_id": b["id"]})
        assert any(x["id"] == cr["id"] for x in r2.json()["changes"])


# 10. 连续两次变更：版本递增到 2，形成 旧→新→新新 的替换链
def test_second_change_version_two_chain(srv):
    b = make_batch(srv, capacity=6)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "连变", 0, client=pub).json()
    t0 = approve(srv, a["id"]).json()["ticket"]["code"]
    c1 = change_req(srv, a["manage_token"], "连变", 1, names=["一"], client=pub).json()
    t1 = approve_change(srv, c1["id"]).json()["new_ticket"]["code"]
    c2 = change_req(srv, a["manage_token"], "连变", 2, names=["一", "二"], client=pub).json()
    body2 = approve_change(srv, c2["id"])
    assert body2.status_code == 200, body2.text
    t2 = body2.json()["new_ticket"]["code"]
    assert body2.json()["application"]["change_version"] == 2
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{t0}").json()["status"] == "REVOKED"
        assert c.get(f"/api/admin/tickets/{t1}").json()["status"] == "REVOKED"
        assert c.get(f"/api/admin/tickets/{t2}").json()["status"] == "ACTIVE"
        assert c.get(f"/api/admin/tickets/{t2}").json()["replaced_code"] == t1
        assert c.get(f"/api/admin/tickets/{t1}").json()["replaced_by_code"] == t2
        assert c.get(f"/api/admin/tickets/{t1}").json()["replaced_code"] == t0


# 11. 重启后变更版本、替换链、待审变更全部延续
def test_change_persistence_after_restart(srv):
    b = make_batch(srv, capacity=5)
    pub = _pub(srv)
    a = submit(srv, b["apply_token"], "重启变", 0, client=pub).json()
    t1 = approve(srv, a["id"]).json()["ticket"]["code"]
    cr = change_req(srv, a["manage_token"], "重启变", 1, names=["伴"], client=pub).json()
    t2 = approve_change(srv, cr["id"]).json()["new_ticket"]["code"]
    # 再来一个待审变更（不应丢失）：改姓名以确保非 noop
    a2 = submit(srv, b["apply_token"], "重启待审", 0, client=pub).json()
    approve(srv, a2["id"])
    cr_pending = change_req(srv, a2["manage_token"], "重启待审新", 0,
                            client=pub).json()
    assert cr_pending["id"]

    srv.restart()

    with srv.client() as c:
        d = c.get(f"/api/admin/batches/{b['id']}").json()
        app = next(x for x in d["applications"] if x["id"] == a["id"])
        assert app["change_version"] == 1 and app["ticket_code"] == t2
        assert c.get(f"/api/admin/tickets/{t1}").json()["replaced_by_code"] == t2
        assert c.get(f"/api/admin/tickets/{t2}").json()["replaced_code"] == t1
        pend = next(x for x in d["changes"] if x["id"] == cr_pending["id"])
        assert pend["status"] == "PENDING"
    # 变更幂等在重启后仍成立：用已有 request_id 重放该 PENDING 变更
    replay = change_req(srv, a2["manage_token"], "重启待审新", 0,
                        rid=cr_pending["request_id"], client=pub)
    assert replay.status_code == 200 and replay.json()["id"] == cr_pending["id"]
    # 新票在重启后仍可核销，旧票仍 410 指向它
    assert gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gc1", "code": t1,
        "attempt_id": str(uuid.uuid4())}).status_code == 410
    assert gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "gc1", "code": t2,
        "attempt_id": str(uuid.uuid4())}).status_code == 200
