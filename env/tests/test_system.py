"""端到端测试：覆盖需求中的全部关键不变量。"""

import threading
import time
import uuid

import httpx
import pytest

ADMIN = None  # 占位，实际通过 srv.client() 拿


def issue(srv, person, ttl=600, client=None, note=None, zones=("Z1",)):
    c = client or srv.client()
    payload = {"person_id": person, "ttl_seconds": ttl, "zones": list(zones)}
    if note:
        payload["note"] = note
    r = c.post("/api/admin/tickets", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def gate_client(srv):
    return srv.client(token=srv.gate_token)


def redeem(srv, code, gate, attempt=None, client=None, pv=0):
    c = client or gate_client(srv)
    return c.post("/api/gate/redeem", json={
        "gate_id": gate, "code": code, "attempt_id": attempt or str(uuid.uuid4()),
        "policy_version": pv,
    })


# 1. 发票 + 基本核销
def test_issue_and_redeem(srv):
    t = issue(srv, "P-001")
    assert t["code"].startswith("T-") and t["status"] == "ACTIVE"
    assert t["version"] >= 1

    r = redeem(srv, t["code"], "g1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status"] == "REDEEMED"
    assert body["version"] == t["version"] + 1


# 2. 两个门点同时扫同一张：恰有一个成功，另一个明确得知“已使用”
def test_concurrent_redeem_single_winner(srv):
    t = issue(srv, "P-002")
    barrier = threading.Barrier(2)
    results = {}

    def worker(gate):
        client = gate_client(srv)
        barrier.wait()
        results[gate] = redeem(srv, t["code"], gate, client=client)

    threads = [threading.Thread(target=worker, args=(g,)) for g in ("g1", "g2")]
    for th in threads: th.start()
    for th in threads: th.join()

    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [200, 409], statuses
    fail = next(r for r in results.values() if r.status_code == 409).json()
    assert fail["ok"] is False
    assert fail["reason"] == "already_redeemed"
    assert fail["status"] == "REDEEMED"
    # 失败方要知道是谁、在什么时候核销的
    assert fail["redeemed_gate"] in ("g1", "g2")
    assert fail["redeemed_at"]

    # 终态不可再次核销（换第三个门点、换新 attempt_id 仍失败）
    r = redeem(srv, t["code"], "g3")
    assert r.status_code == 409 and r.json()["reason"] == "already_redeemed"


# 3. 同一人多张时间重叠的票：互不顶替，可分别核销
def test_overlapping_tickets_independent(srv):
    a = issue(srv, "P-003", ttl=600)
    b = issue(srv, "P-003", ttl=600)
    c = issue(srv, "P-003", ttl=600)
    codes = {a["code"], b["code"], c["code"]}
    assert len(codes) == 3

    assert redeem(srv, a["code"], "g1").status_code == 200
    assert redeem(srv, b["code"], "g2").status_code == 200
    # 已核销的 a 不影响仍可用的 c
    r = redeem(srv, c["code"], "g3")
    assert r.status_code == 200

    with srv.client() as admin:
        view = admin.get(f"/api/admin/people/P-003").json()
    p3 = [x for x in view["tickets"] if x["code"] in codes]
    assert all(x["status"] == "REDEEMED" for x in p3)
    assert len({x["redeemed_gate"] for x in p3}) == 3


# 4. 作废一张后，同一人其他在有效期内的票继续可用；作废不可复活
def test_revoke_isolates_other_tickets(srv):
    a = issue(srv, "P-004", ttl=600)
    b = issue(srv, "P-004", ttl=600)

    with srv.client() as admin:
        r = admin.post("/api/admin/tickets/revoke",
                       json={"code": a["code"], "reason": "visitor cancelled"})
        assert r.status_code == 200

    # 已作废的票：门点核销被拒且原因明确
    r = redeem(srv, a["code"], "g1")
    assert r.status_code == 410
    assert r.json()["reason"] == "revoked"
    assert r.json()["revoked_reason"] == "visitor cancelled"

    # 同一张票不能重复作废（终态保护）
    with srv.client() as admin:
        r = admin.post("/api/admin/tickets/revoke",
                       json={"code": a["code"], "reason": "again"})
        assert r.status_code == 409

    # 另一张票仍然可用
    assert redeem(srv, b["code"], "g1").status_code == 200


# 5. 过期不复活：延迟到期票先被判定过期，之后任何核销都拒绝
def test_expired_ticket_stays_dead(srv):
    t = issue(srv, "P-005", ttl=1)
    time.sleep(2.2)  # 超过清扫间隔(1s)与票有效期

    r = redeem(srv, t["code"], "g1")
    assert r.status_code == 410
    assert r.json()["reason"] == "expired"

    # 再来一次仍然是过期（没有 EXPIRED->ACTIVE 的任何路径）
    r = redeem(srv, t["code"], "g2")
    assert r.status_code == 410 and r.json()["reason"] == "expired"

    with srv.client() as admin:
        ev = admin.get(f"/api/admin/events?code={t['code']}").json()["events"]
    types = [e["type"] for e in ev]
    assert types.count("TICKET_EXPIRED") == 1  # 过期事件只记一次


# 6. 扫码请求幂等：同 attempt_id 重放返回首次结果，不会重复核销
def test_redeem_idempotent_replay(srv):
    t = issue(srv, "P-006")
    attempt = str(uuid.uuid4())
    c = gate_client(srv)
    r1 = redeem(srv, t["code"], "g1", attempt=attempt, client=c)
    r2 = redeem(srv, t["code"], "g1", attempt=attempt, client=c)
    assert r1.status_code == 200
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is True and body["replayed"] is True

    # 新 attempt_id（物理上的第二次扫码）必须得到“已使用”
    r3 = redeem(srv, t["code"], "g1")
    assert r3.status_code == 409 and r3.json()["reason"] == "already_redeemed"


# 7. 门点离线重连按版本补齐：g2 从旧游标拉到 g1 的核销与管理端作废/过期
def test_sync_catchup_by_version(srv):
    t1 = issue(srv, "P-007a", ttl=600)
    t2 = issue(srv, "P-007b", ttl=600)
    t3 = issue(srv, "P-007c", ttl=1)
    c = gate_client(srv)

    synced_before = c.post("/api/gate/sync",
                           json={"gate_id": "g2", "since_version": 0}).json()
    since = synced_before["next_since"]

    redeem(srv, t1["code"], "g1")
    with srv.client() as admin:
        admin.post("/api/admin/tickets/revoke",
                   json={"code": t2["code"], "reason": "test revoke"})
    time.sleep(2.2)  # t3 过期并由清扫线程产生事件

    batch1 = c.post("/api/gate/sync",
                    json={"gate_id": "g2", "since_version": since}).json()
    # 事件可能超过单页（200 条）：翻页拉全后再断言
    pages = [batch1]
    while pages[-1].get("has_more"):
        pages.append(c.post("/api/gate/sync", json={
            "gate_id": "g2",
            "since_version": pages[-1]["next_since"]}).json())
    all_events = [e for p in pages for e in p["events"]]
    batch1["events"] = all_events
    batch1["next_since"] = pages[-1]["next_since"]
    new_versions = [e["version"] for e in all_events]
    assert new_versions == sorted(new_versions)
    assert all(v > since for v in new_versions)

    kinds = {}
    for e in batch1["events"]:
        kinds.setdefault(e["ticket_code"], []).append(e["type"])
    # 核销成功后同一事务紧跟一条到场登记事件（门点按版本一并补齐）
    assert kinds[t1["code"]][-2:] == ["TICKET_REDEEMED", "PRESENCE_ARRIVED"]
    assert kinds[t2["code"]][-1] == "TICKET_REVOKED"
    assert kinds[t3["code"]][-1] == "TICKET_EXPIRED"

    # 补齐后 g2 对 t1 核销必须得到“已使用”，且知道是 g1 核销的
    r = redeem(srv, t1["code"], "g2")
    assert r.status_code == 409
    assert r.json()["redeemed_gate"] == "g1"

    # 游标推进后再拉为空（不丢不重）
    batch2 = c.post("/api/gate/sync", json={
        "gate_id": "g2", "since_version": batch1["next_since"]}).json()
    assert batch2["events"] == []

    # 管理端可见门点同步水位
    with srv.client() as admin:
        gates = admin.get("/api/admin/gates").json()["gates"]
    g2 = next(g for g in gates if g["id"] == "g2")
    assert g2["last_version"] >= batch1["next_since"]


# 8. 服务器重启：已核销/已作废/已过期状态与事件版本延续，绝不复活
def test_restart_does_not_revive(srv):
    a = issue(srv, "P-008a", ttl=600)
    b = issue(srv, "P-008b", ttl=600)
    redeem(srv, a["code"], "g1")
    with srv.client() as admin:
        admin.post("/api/admin/tickets/revoke",
                   json={"code": b["code"], "reason": "before restart"})
    version_before = srv.client().get("/api/admin/stats").json()["last_version"]

    srv.restart()

    with srv.client() as admin:
        assert admin.get(f"/api/admin/tickets/{a['code']}").json()["status"] == "REDEEMED"
        assert admin.get(f"/api/admin/tickets/{b['code']}").json()["status"] == "REVOKED"
        version_after = admin.get("/api/admin/stats").json()["last_version"]
        assert version_after == version_before  # AUTOINCREMENT 版本号延续

    assert redeem(srv, a["code"], "g2").status_code == 409
    assert redeem(srv, b["code"], "g2").status_code == 410

    # 重启后业务继续：新票可发、可核销，新版本号只增
    t = issue(srv, "P-008c", ttl=600)
    assert t["version"] > version_after
    assert redeem(srv, t["code"], "g2").status_code == 200


# 9. 门点必须注册；门点停用后核销/同步均被拒绝
def test_gate_registration_and_disable(srv):
    r = gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "ghost", "code": "T-00000000", "attempt_id": "x"})
    assert r.status_code == 404

    with srv.client() as admin:
        r = admin.post("/api/admin/gates", json={"id": "g-temp", "name": "临时门"})
        assert r.status_code == 200
        r = admin.put("/api/admin/gates/g-temp/zone", json={"zone_id": "Z1"})
        assert r.status_code == 200
        t = issue(srv, "P-009")
        assert gate_client(srv).post("/api/gate/redeem", json={
            "gate_id": "g-temp", "code": t["code"], "attempt_id": str(uuid.uuid4())
        }).status_code == 200

        assert admin.delete("/api/admin/gates/g-temp").status_code == 200

    r = gate_client(srv).post("/api/gate/redeem", json={
        "gate_id": "g-temp", "code": "T-00000000", "attempt_id": "y"})
    assert r.status_code == 410
    r = gate_client(srv).post("/api/gate/sync",
                              json={"gate_id": "g-temp", "since_version": 0})
    assert r.status_code == 410


# 10. 鉴权 + 不存在票面
def test_auth_and_unknown_code(srv):
    bad = httpx.Client(base_url=srv.base,
                       headers={"Authorization": "Bearer wrong"}, timeout=5)
    assert bad.get("/api/admin/stats").status_code == 401
    assert bad.post("/api/gate/redeem", json={
        "gate_id": "g1", "code": "T-NOPE", "attempt_id": "z"}).status_code == 401

    r = redeem(srv, "T-NOPE0000", "g1")
    assert r.status_code == 404
    assert r.json()["reason"] == "not_found"


# 11. 管理视图：按人看当前可用票、扫码记录、状态变化原因
def test_admin_person_view(srv):
    person = "P-011"
    a = issue(srv, person, ttl=600, note="白天访客")
    b = issue(srv, person, ttl=600)
    redeem(srv, a["code"], "g1")
    redeem(srv, a["code"], "g2")  # 第二次被拒也要进记录

    with srv.client() as admin:
        v = admin.get(f"/api/admin/people/{person}").json()

    assert len(v["tickets"]) == 2
    valid = [t for t in v["valid_tickets"]]
    assert [t["code"] for t in valid] == [b["code"]]

    reasons = [(e["type"], e.get("reason")) for e in v["events"]]
    assert ("TICKET_ISSUED", None) in reasons
    assert ("TICKET_REDEEMED", "gate_scan") in reasons

    attempts = v["scan_attempts"]
    assert any(x["ok"] and x["gate_id"] == "g1" for x in attempts)
    assert any((not x["ok"]) and x["status"] == "REDEEMED" and x["gate_id"] == "g2"
               for x in attempts)
    assert all(x.get("gate_name") for x in attempts if x["gate_id"] in ("g1", "g2"))
