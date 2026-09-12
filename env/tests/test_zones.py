"""分区通行策略与紧急封锁的端到端测试。

注意：本文件按文件名字序在 test_system.py 之后运行（test_system 的用例
不携带策略版本，依赖“尚未发布任何规则”的初始状态）。本文件内所有核销
都通过 redeem_now() 携带当前策略版本。
"""

import threading
import uuid

import pytest

from test_system import gate_client, issue, redeem


@pytest.fixture(scope="module", autouse=True)
def zones_setup(srv):
    with srv.client() as c:
        for zid, zname in [("ZT1", "测试一区"), ("ZT2", "测试二区")]:
            r = c.post("/api/admin/zones", json={"id": zid, "name": zname})
            assert r.status_code in (200, 409)
        for gid in ("gz-a", "gz-b"):
            r = c.post("/api/admin/gates", json={"id": gid, "name": gid})
            assert r.status_code in (200, 409)
        assert c.put("/api/admin/gates/gz-a/zone",
                     json={"zone_id": "ZT1"}).status_code == 200
        assert c.put("/api/admin/gates/gz-b/zone",
                     json={"zone_id": "ZT2"}).status_code == 200


def publish(srv, action, zone_id=None, rule_id=None, reason="t"):
    with srv.client() as admin:
        r = admin.post("/api/admin/policy/rules", json={
            "action": action, "zone_id": zone_id,
            "rule_id": rule_id, "reason": reason,
        })
        assert r.status_code in (200, 201), r.text
        return r.json()


def policy_version(srv):
    with srv.client() as admin:
        return admin.get("/api/admin/policy/rules").json()["policy_version"]


def redeem_now(srv, code, gate, **kw):
    """携带当前策略版本的核销（模拟已同步到最新规则的门点）。"""
    return redeem(srv, code, gate, pv=policy_version(srv), **kw)


# 1. 分区不匹配：明确拒绝且不消耗票据；多分区票可跨区核销
def test_zone_mismatch_denied_and_not_consumed(srv):
    t = issue(srv, "PZ-001", zones=("ZT1",))
    r = redeem_now(srv, t["code"], "gz-b")  # gz-b 在 ZT2
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["reason"] == "zone_mismatch"
    assert body["zone_id"] == "ZT2"
    assert body["ticket_zones"] == ["ZT1"]

    # 拒绝不消耗票据：管理端看仍是 ACTIVE，到正确的区可核销
    with srv.client() as admin:
        assert admin.get(f"/api/admin/tickets/{t['code']}").json()["status"] == "ACTIVE"
    assert redeem_now(srv, t["code"], "gz-a").status_code == 200

    # 多分区票在两个区都能核销
    t2 = issue(srv, "PZ-001b", zones=("ZT1", "ZT2"))
    assert redeem_now(srv, t2["code"], "gz-b").status_code == 200


# 2. 缺少策略的门点默认拒绝；不允许任何分区的票也默认拒绝
def test_gate_without_policy_default_deny(srv):
    with srv.client() as admin:
        r = admin.post("/api/admin/gates", json={"id": "g-nozone", "name": "无分区门"})
        assert r.status_code in (200, 409)
    t = issue(srv, "PZ-002", zones=("ZT1",))
    r = redeem_now(srv, t["code"], "g-nozone")
    assert r.status_code == 403
    assert r.json()["reason"] == "gate_no_policy"

    empty = issue(srv, "PZ-002b", zones=())
    r = redeem_now(srv, empty["code"], "gz-a")
    assert r.status_code == 403 and r.json()["reason"] == "zone_mismatch"


# 3. 未知分区默认拒绝：分区被注销后，引用它的门点不能核销
def test_unknown_zone_default_deny(srv):
    with srv.client() as admin:
        assert admin.post("/api/admin/zones",
                          json={"id": "ZTMP", "name": "临时区"}).status_code in (200, 409)
        assert admin.post("/api/admin/gates",
                          json={"id": "g-ztmp", "name": "临时区门"}).status_code in (200, 409)
        assert admin.put("/api/admin/gates/g-ztmp/zone",
                         json={"zone_id": "ZTMP"}).status_code == 200
        # 分区存在时正常核销
    t_ok = issue(srv, "PZ-003", zones=("ZTMP",))
    assert redeem_now(srv, t_ok["code"], "g-ztmp").status_code == 200

    with srv.client() as admin:
        assert admin.delete("/api/admin/zones/ZTMP").status_code == 200

    t = issue(srv, "PZ-003b", zones=("ZT1",))
    r = redeem_now(srv, t["code"], "g-ztmp")
    assert r.status_code == 403
    assert r.json()["reason"] == "unknown_zone"


# 4. 紧急封锁：过旧版本拒绝 -> 封锁拒绝 -> 解锁后放行，封锁不消耗票据
def test_lockdown_stale_and_lift(srv):
    t1 = issue(srv, "PZ-004a", zones=("ZT1",))
    t2 = issue(srv, "PZ-004b", zones=("ZT1",))

    rule = publish(srv, "LOCK", zone_id="ZT1", reason="fire drill")["rule"]
    assert rule["version"] >= 1

    # 门点规则版本过旧：409，并告知当前版本；不写扫码记录，可同 attempt 重试
    stale_attempt = str(uuid.uuid4())
    r = redeem(srv, t1["code"], "gz-a", attempt=stale_attempt, pv=0)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "stale_policy"
    assert body["current_policy_version"] == rule["version"]

    # 版本跟上后：封锁拒绝 423，说明是封锁且给出规则
    r = redeem(srv, t1["code"], "gz-a", attempt=stale_attempt, pv=rule["version"])
    assert r.status_code == 423
    body = r.json()
    assert body["reason"] == "locked"
    assert body["lock_rule"]["rule_id"] == rule["rule_id"]
    assert body["lock_rule"]["version"] == rule["version"]

    # 封锁拒绝不消耗票据
    with srv.client() as admin:
        assert admin.get(f"/api/admin/tickets/{t1['code']}").json()["status"] == "ACTIVE"

    # 解除封锁后放行
    unlock = publish(srv, "UNLOCK", zone_id="ZT1", reason="drill over")["rule"]
    assert redeem(srv, t1["code"], "gz-a", pv=unlock["version"]).status_code == 200
    assert redeem(srv, t2["code"], "gz-a", pv=unlock["version"]).status_code == 200


# 5. 全局封锁（zone_id=null）覆盖所有分区，全局解除后恢复
def test_global_lock_covers_all_zones(srv):
    t1 = issue(srv, "PZ-005a", zones=("ZT1",))
    t2 = issue(srv, "PZ-005b", zones=("ZT2",))

    lock = publish(srv, "LOCK", zone_id=None, reason="campus lockdown")["rule"]
    assert lock["zone_id"] is None
    pv = lock["version"]
    assert redeem(srv, t1["code"], "gz-a", pv=pv).status_code == 423
    assert redeem(srv, t2["code"], "gz-b", pv=pv).status_code == 423

    unlock = publish(srv, "UNLOCK", zone_id=None, reason="all clear")["rule"]
    pv = unlock["version"]
    assert redeem(srv, t1["code"], "gz-a", pv=pv).status_code == 200
    assert redeem(srv, t2["code"], "gz-b", pv=pv).status_code == 200


# 6. 重复发布同一规则不重复生效；同 rule_id 不同内容冲突
def test_publish_idempotent_and_conflict(srv):
    rule_id = "R-IDEM-" + uuid.uuid4().hex[:8]
    r1 = publish(srv, "LOCK", zone_id="ZT2", rule_id=rule_id, reason="first")
    assert r1["duplicated"] is False
    v1 = r1["rule"]["version"]

    r2 = publish(srv, "LOCK", zone_id="ZT2", rule_id=rule_id, reason="first")
    assert r2["duplicated"] is True
    assert r2["rule"]["version"] == v1  # 不产生新版本

    # 事件流中该规则只出现一次
    with srv.client() as admin:
        events = admin.get("/api/admin/events?limit=1000").json()["events"]
    hits = [e for e in events
            if e["type"] == "POLICY_LOCK" and e["payload"].get("rule_id") == rule_id]
    assert len(hits) == 1

    # 同 rule_id 不同内容 -> 409
    with srv.client() as admin:
        r = admin.post("/api/admin/policy/rules", json={
            "action": "UNLOCK", "zone_id": "ZT2", "rule_id": rule_id})
        assert r.status_code == 409

    publish(srv, "UNLOCK", zone_id="ZT2", reason="cleanup")


# 7. 门点离线重连按版本补齐策略事件
def test_policy_events_caught_up_by_sync(srv):
    c = gate_client(srv)
    batch = c.post("/api/gate/sync",
                   json={"gate_id": "gz-b", "since_version": 0}).json()
    versions = [e["version"] for e in batch["events"]]
    assert versions == sorted(versions)
    types = {e["type"] for e in batch["events"]}
    assert "POLICY_LOCK" in types and "POLICY_UNLOCK" in types
    # 策略事件带有规则内容，门点可据此更新本地策略版本
    lock_events = [e for e in batch["events"] if e["type"] == "POLICY_LOCK"]
    assert all("rule_id" in e["payload"] for e in lock_events)


# 8. 封锁发布与并发核销有明确先后：核销版本 < 规则版本才允许成功
def test_lock_racing_redeems_have_clear_order(srv):
    tickets = [issue(srv, f"PZ-008-{i}", zones=("ZT1",)) for i in range(6)]
    pv_before = policy_version(srv)
    barrier = threading.Barrier(len(tickets) + 1)
    results = {}
    lock_holder = {}

    def worker(t):
        c = gate_client(srv)
        barrier.wait()
        results[t["code"]] = redeem(srv, t["code"], "gz-a", client=c, pv=pv_before)

    def do_lock():
        barrier.wait()
        lock_holder["res"] = publish(srv, "LOCK", zone_id="ZT1", reason="race")

    threads = [threading.Thread(target=worker, args=(t,)) for t in tickets]
    threads.append(threading.Thread(target=do_lock))
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    rule_version = lock_holder["res"]["rule"]["version"]
    for t in tickets:
        r = results[t["code"]]
        if r.status_code == 200:
            # 成功核销的事件版本必须先于封锁规则版本（串行事务保证）
            assert r.json()["version"] < rule_version
        else:
            # 落在封锁之后的核销因版本过旧被拒，同步后重试即见封锁
            assert r.status_code == 409 and r.json()["reason"] == "stale_policy"

    pv_after = policy_version(srv)
    for t in tickets:
        r = redeem(srv, t["code"], "gz-a", pv=pv_after)
        if results[t["code"]].status_code == 200:
            assert r.status_code == 409 and r.json()["reason"] == "already_redeemed"
        else:
            assert r.status_code == 423 and r.json()["reason"] == "locked"

    publish(srv, "UNLOCK", zone_id="ZT1", reason="race over")


# 9. 管理端按分区查看：当前规则、受影响票据、门点执行记录
def test_admin_zone_views(srv):
    t1 = issue(srv, "PZ-009a", zones=("ZT1",))
    t2 = issue(srv, "PZ-009b", zones=("ZT1", "ZT2"))
    t3 = issue(srv, "PZ-009c", zones=("ZT2",))
    assert redeem_now(srv, t1["code"], "gz-a").status_code == 200

    with srv.client() as admin:
        detail = admin.get("/api/admin/zones/ZT1").json()
        assert detail["zone"]["id"] == "ZT1"
        assert detail["locked"] is False
        assert detail["current_rule"]["action"] == "UNLOCK"  # 最新适用规则
        assert detail["policy_version"] >= detail["current_rule"]["version"]
        assert any(r["action"] == "LOCK" for r in detail["rules"])  # 历史可追溯

        codes = {t["code"] for t in detail["tickets"]}
        assert t1["code"] in codes and t2["code"] in codes
        assert t3["code"] not in codes  # 与 ZT1 无关的票不出现

        assert any(g["id"] == "gz-a" for g in detail["gates"])
        hit = [a for a in detail["attempts"]
               if a["code"] == t1["code"] and a["gate_id"] == "gz-a"]
        assert hit and hit[0]["ok"] is True

        # 分区总览：状态/门点数/受影响票数
        zones = {z["id"]: z for z in admin.get("/api/admin/zones").json()["zones"]}
        assert zones["ZT1"]["affected_tickets"] >= 2
        assert zones["ZT1"]["gates"] >= 1
        assert zones["ZT1"]["locked"] is False

        # 不存在的分区 -> 404
        assert admin.get("/api/admin/zones/Z-NOPE").status_code == 404
