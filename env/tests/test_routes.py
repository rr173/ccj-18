"""访客路线检查与区域停留监控模块的端到端测试（真实 HTTP，复用会话级服务）。

覆盖：
  * 管理员按分区编排有顺序的检查点路线、每点最长停留时间；发布不可变新版本；
  * 路线绑定到新签发的票/预约批次；首次核销在入口开始路线并固化版本；
  * 跳过、重复进入、进入已关闭检查点、停留超时的明确拒绝；
  * 每次门点检查保留票/人员/路线版本/检查点顺序/门点记录；
  * 网络抖动重发（同 attempt_id）不重复推进；多门点并发只有一个明确先后；
  * 完成/违规终态不可逆，旧事件不能改回进行中；
  * 暂停/替换未来使用的路线，已经开始的路线继续走原版本；
  * 门点离线期间事件重连按版本/顺序补齐，冲突保留记录交管理员处理；
  * 按分区、批次、人员、路线查询当前检查点/超时/完成/违规历史；
  * 杀进程重启后执行状态、版本固化、检查记录与冲突全部延续。
"""

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from test_system import gate_client


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


# 本模块独占的分区与门点，避免与其它测试文件共享状态
ZONE = "ZRT"
GATES = ["rt1", "rt2", "rt3", "rt4"]


@pytest.fixture(scope="module", autouse=True)
def route_setup(srv):
    with srv.client() as c:
        r = c.post("/api/admin/zones", json={"id": ZONE, "name": "路线测试区"})
        assert r.status_code in (200, 409)
        for gid in GATES:
            r = c.post("/api/admin/gates", json={"id": gid, "name": f"门-{gid}"})
            assert r.status_code in (200, 409)
            assert c.put(f"/api/admin/gates/{gid}/zone",
                         json={"zone_id": ZONE}).status_code == 200


def _unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_route(srv, checkpoints, *, zone=ZONE, name=None, client=None):
    """建路线 + 发布首个版本。checkpoints: [(gate, max_stay), ...]。返回路线详情。"""
    c = client or srv.client()
    rid = c.post("/api/admin/routes", json={
        "zone_id": zone, "name": name or _unique("路线")}).json()["id"]
    cps = [{"gate_id": g, "name": f"点{g}",
            **({"max_stay_seconds": ms} if ms is not None else {})}
           for g, ms in checkpoints]
    r = c.post(f"/api/admin/routes/{rid}/versions", json={"checkpoints": cps})
    assert r.status_code == 201, r.text
    return rid


def issue_route_ticket(srv, rid, person=None, ttl=3600, client=None):
    c = client or srv.client()
    r = c.post("/api/admin/tickets", json={
        "person_id": person or _unique("V"),
        "ttl_seconds": ttl, "zones": [ZONE], "route_id": rid})
    assert r.status_code == 200, r.text
    return r.json()["code"]


def redeem(srv, code, gate, client=None, route_version=10 ** 9):
    c = client or gate_client(srv)
    return c.post("/api/gate/redeem", json={
        "gate_id": gate, "code": code, "attempt_id": str(uuid.uuid4()),
        "policy_version": 0, "route_version": route_version})


def checkpoint(srv, code, gate, client=None, attempt=None, route_version=10 ** 9):
    c = client or gate_client(srv)
    return c.post("/api/gate/checkpoint", json={
        "gate_id": gate, "code": code,
        "attempt_id": attempt or str(uuid.uuid4()),
        "route_version": route_version})


def progress(srv, code):
    with srv.client() as c:
        return c.get(f"/api/admin/route-progress/{code}").json()["progress"]


def start_route(srv, rid, code, entry="rt1"):
    r = redeem(srv, code, entry)
    assert r.status_code == 200, r.text
    return r.json()["route"]


# ---------------- 1. 编排 / 校验 ----------------

def test_route_requires_known_zone_and_gates(srv):
    with srv.client() as c:
        r = c.post("/api/admin/routes", json={"zone_id": "NO-SUCH-Z", "name": "x"})
        assert r.status_code == 400
        rid = c.post("/api/admin/routes",
                     json={"zone_id": ZONE, "name": _unique("校验路线")}).json()["id"]
        # 空检查点
        r = c.post(f"/api/admin/routes/{rid}/versions", json={"checkpoints": []})
        assert r.status_code == 422  # pydantic min_length
        # 未知门点
        r = c.post(f"/api/admin/routes/{rid}/versions",
                   json={"checkpoints": [{"gate_id": "ghost-gate"}]})
        assert r.status_code == 400
        # 同一门点重复
        r = c.post(f"/api/admin/routes/{rid}/versions", json={"checkpoints": [
            {"gate_id": "rt1"}, {"gate_id": "rt1"}]})
        assert r.status_code == 400
        # 门点分区与路线分区不一致（g1 在 Z1）
        r = c.post(f"/api/admin/routes/{rid}/versions",
                   json={"checkpoints": [{"gate_id": "g1"}]})
        assert r.status_code == 400
        # 非法停留秒数
        r = c.post(f"/api/admin/routes/{rid}/versions",
                   json={"checkpoints": [{"gate_id": "rt1", "max_stay_seconds": 0}]})
        assert r.status_code == 422
        # 正常发布
        r = c.post(f"/api/admin/routes/{rid}/versions",
                   json={"checkpoints": [{"gate_id": "rt1"}, {"gate_id": "rt2"}]})
        assert r.status_code == 201 and r.json()["published_version"] == 1


def test_route_detail_lists_versions_checkpoints_and_bindings(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 60), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    with srv.client() as c:
        d = c.get(f"/api/admin/routes/{rid}").json()
    assert d["route"]["current_version"] == 1
    cps = d["versions"][0]["checkpoints"]
    assert [x["seq"] for x in cps] == [1, 2, 3]
    assert cps[1]["max_stay_seconds"] == 60
    assert any(b["ticket_code"] == code for b in d["bindings"])


# ---------------- 2. 绑定 + 首次核销开始路线 ----------------

def test_first_redeem_must_be_at_entry_gate(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", None), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    # 在非入口门点先核销：明确拒绝，票不被核销
    r = redeem(srv, code, "rt2")
    assert r.status_code == 403
    body = r.json()
    assert body["reason"] == "wrong_entry_gate" and body["entry_gate"] == "rt1"
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{code}").json()["status"] == "ACTIVE"
    # 入口核销开始路线
    block = start_route(srv, rid, code)
    assert block["current_seq"] == 1 and block["next_seq"] == 2
    assert block["route_version"] == 1
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{code}").json()["status"] == "REDEEMED"


def test_stale_route_version_does_not_consume_and_retries(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", None)])
    code = issue_route_ticket(srv, rid)
    # 门点目录版本过旧：409 stale_route，票仍 ACTIVE（未记录判定，可同 attempt 重试）
    aid = str(uuid.uuid4())
    with gate_client(srv) as gc:
        r = gc.post("/api/gate/redeem", json={
            "gate_id": "rt1", "code": code, "attempt_id": aid,
            "route_version": 0})
    assert r.status_code == 409 and r.json()["reason"] == "stale_route"
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{code}").json()["status"] == "ACTIVE"
    # 同步后用同一 attempt_id 重试成功
    with gate_client(srv) as gc:
        r = gc.post("/api/gate/redeem", json={
            "gate_id": "rt1", "code": code, "attempt_id": aid,
            "route_version": 10 ** 9})
    assert r.status_code == 200, r.text


def test_bound_batch_tickets_inherit_route(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", None), ("rt3", None)])
    start = datetime.now(timezone.utc)
    with srv.client() as c:
        b = c.post("/api/admin/batches", json={
            "name": _unique("路线批次"), "visit_date": start.date().isoformat(),
            "start_at": _iso(start), "end_at": _iso(start + timedelta(hours=1)),
            "zone_id": ZONE, "capacity": 5, "route_id": rid}).json()
    pub = srv.client(token=None)
    import httpx
    pub = httpx.Client(base_url=srv.base, timeout=10)
    a = pub.post(f"/api/public/batches/{b['apply_token']}/applications", json={
        "request_id": uuid.uuid4().hex, "name": "路线访客",
        "contact": "v@example.com", "companions": 0}).json()
    with srv.client() as c:
        ap = c.post(f"/api/admin/applications/{a['id']}/approve").json()
    code = ap["ticket"]["code"]
    assert ap["ticket"]["route_id"] == rid
    block = start_route(srv, rid, code)
    assert block["route_id"] == rid
    # 批次详情包含路线监控
    with srv.client() as c:
        bd = c.get(f"/api/admin/batches/{b['id']}").json()
    assert bd["routes"]["route_id"] == rid
    assert bd["batch"]["route_id"] == rid


# ---------------- 3. 顺序推进 / 完成 ----------------

def test_orderly_completion(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    assert checkpoint(srv, code, "rt2").status_code == 200
    p = progress(srv, code)
    assert p["current_seq"] == 2 and p["next_seq"] == 3
    r = checkpoint(srv, code, "rt3")
    assert r.status_code == 200 and r.json()["status"] == "ROUTE_COMPLETED"
    p = progress(srv, code)
    assert p["status"] == "COMPLETED"


def test_gate_not_on_route_is_rejected(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    # rt4 不在该版本路线上
    r = checkpoint(srv, code, "rt4")
    assert r.status_code == 403 and r.json()["reason"] == "gate_not_on_route"
    assert progress(srv, code)["current_seq"] == 1


def test_checkpoint_before_route_start_rejected(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", None)])
    code = issue_route_ticket(srv, rid)
    r = checkpoint(srv, code, "rt1")
    assert r.status_code == 409 and r.json()["reason"] == "route_not_started"
    # 无路线绑定的票走检查点接口
    with srv.client() as c:
        from test_system import issue
        plain = issue(srv, _unique("P"), zones=(ZONE,))["code"]
    r = checkpoint(srv, plain, "rt1")
    assert r.status_code == 409 and r.json()["reason"] == "no_route"


# ---------------- 4. 重复 / 跳过 / 已关闭 / 停留超时 ----------------

def test_duplicate_entry_does_not_advance(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    # 重复进入当前检查点 rt1（已越过）：非终态拒绝
    r = checkpoint(srv, code, "rt1")
    assert r.status_code == 409 and r.json()["reason"] == "duplicate_entry"
    assert progress(srv, code)["status"] == "IN_PROGRESS"
    assert progress(srv, code)["next_seq"] == 2
    # 前进到 rt2 后，再扫 rt1/rt2 都是重复拒绝
    assert checkpoint(srv, code, "rt2").status_code == 200
    r = checkpoint(srv, code, "rt2")
    assert r.status_code == 409 and r.json()["reason"] == "duplicate_entry"
    assert progress(srv, code)["status"] == "IN_PROGRESS"


def test_skipped_checkpoint_violates(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None), ("rt4", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    r = checkpoint(srv, code, "rt3")  # 跳过 rt2
    assert r.status_code == 409 and r.json()["reason"] == "skipped_checkpoint"
    assert r.json()["violation_kind"] == "SKIPPED_CHECKPOINT"
    p = progress(srv, code)
    assert p["status"] == "VIOLATED" and p["violation_kind"] == "SKIPPED_CHECKPOINT"


def test_closed_checkpoint_rejected_and_violates(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    with srv.client() as c:
        r = c.put(f"/api/admin/routes/{rid}/checkpoints", json={
            "version": 1, "seq": 2, "closed": True, "reason": "展厅维护"})
        assert r.status_code == 200 and r.json()["checkpoint"]["closed"] is True
    r = checkpoint(srv, code, "rt2")
    assert r.status_code == 423 and r.json()["reason"] == "checkpoint_closed"
    assert r.json()["violation_kind"] == "CHECKPOINT_CLOSED"
    assert progress(srv, code)["status"] == "VIOLATED"
    # 幂等：关闭操作重复提交不重复产生事件
    with srv.client() as c:
        r = c.put(f"/api/admin/routes/{rid}/checkpoints", json={
            "version": 1, "seq": 2, "closed": True, "reason": "展厅维护"})
        assert r.status_code == 200 and r.json().get("duplicated") is True


def test_closed_entry_rejects_first_redeem(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", None)])
    code = issue_route_ticket(srv, rid)
    with srv.client() as c:
        c.put(f"/api/admin/routes/{rid}/checkpoints",
              json={"version": 1, "seq": 1, "closed": True, "reason": "封入口"})
    r = redeem(srv, code, "rt1")
    assert r.status_code == 423 and r.json()["reason"] == "checkpoint_closed"
    assert progress(srv, code)["status"] == "VIOLATED"
    assert progress(srv, code)["violation_kind"] == "CHECKPOINT_CLOSED"


def test_dwell_timeout_violates(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 1), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    assert checkpoint(srv, code, "rt2").status_code == 200
    time.sleep(1.2)  # 超过 rt2 最长停留 1 秒
    r = checkpoint(srv, code, "rt3")
    assert r.status_code == 409 and r.json()["reason"] == "dwell_timeout"
    assert r.json()["violation_kind"] == "DWELL_TIMEOUT"
    assert progress(srv, code)["status"] == "VIOLATED"


def test_overdue_query_reports_current_overstay(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 1), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    assert checkpoint(srv, code, "rt2").status_code == 200
    time.sleep(1.2)
    with srv.client() as c:
        rows = c.get("/api/admin/route-progress",
                     params={"overdue": "true", "route_id": rid}).json()["progress"]
    hit = [x for x in rows if x["ticket_code"] == code]
    assert hit and hit[0]["overdue"] is True
    assert hit[0]["current_checkpoint"]["max_stay_seconds"] == 1


# ---------------- 5. 幂等 / 并发 / 终态不可逆 ----------------

def test_replayed_attempt_does_not_advance_twice(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    aid = str(uuid.uuid4())
    r1 = checkpoint(srv, code, "rt2", attempt=aid)
    r2 = checkpoint(srv, code, "rt2", attempt=aid)  # 网络抖动重发
    assert r1.status_code == 200
    assert r2.status_code == 200 and r2.json().get("replayed") is True
    p = progress(srv, code)
    assert p["current_seq"] == 2 and p["next_seq"] == 3
    with srv.client() as c:
        checks = c.get("/api/admin/route-checks",
                       params={"code": code, "decision": "ADVANCED"}).json()["checks"]
    assert len([x for x in checks if x["checkpoint_seq"] == 2]) == 1


def test_cross_endpoint_attempt_cannot_advance(srv):
    """同一 attempt_id 已用于入口核销，不能再在检查点接口重放推进路线。"""
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    aid = str(uuid.uuid4())
    with gate_client(srv) as gc:
        r = gc.post("/api/gate/redeem", json={
            "gate_id": "rt1", "code": code, "attempt_id": aid,
            "route_version": 10 ** 9})
        assert r.status_code == 200
        # 同 (gate_id, attempt_id) 走检查点接口：返回首次结论（重放），不产生推进
        r2 = gc.post("/api/gate/checkpoint", json={
            "gate_id": "rt1", "code": code, "attempt_id": aid,
            "route_version": 10 ** 9})
    assert r2.json().get("replayed") is True
    # 路线仍停留在入口检查点（重放没有重复推进）
    p = progress(srv, code)
    assert p["current_seq"] == 1 and p["next_seq"] == 2


def test_concurrent_checkpoints_single_total_order(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    # 两个门点并发上报“同一个下一检查点” rt2：恰有一个推进，另一个明确拒绝
    barrier = threading.Barrier(2)
    results = {}

    def worker(tag):
        gc = gate_client(srv)
        barrier.wait()
        results[tag] = gc.post("/api/gate/checkpoint", json={
            "gate_id": "rt2", "code": code,
            "attempt_id": f"{tag}-{uuid.uuid4()}", "route_version": 10 ** 9})

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    statuses = sorted(r.status_code for r in results.values())
    assert statuses == [200, 409], statuses
    loser = next(r for r in results.values() if r.status_code == 409).json()
    assert loser["reason"] == "duplicate_entry"
    assert progress(srv, code)["current_seq"] == 2


def test_terminal_state_never_reverts(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    checkpoint(srv, code, "rt2")
    r = checkpoint(srv, code, "rt3")
    assert r.status_code == 200 and r.json()["status"] == "ROUTE_COMPLETED"
    # 旧事件（任何检查点）都不能把 COMPLETED 改回进行中
    for gate in ("rt1", "rt2", "rt3"):
        r = checkpoint(srv, code, gate)
        assert r.status_code == 409 and "completed" in r.json()["reason"]
    assert progress(srv, code)["status"] == "COMPLETED"


# ---------------- 6. 暂停 / 替换未来路线，在途走旧版本 ----------------

def test_pause_blocks_future_use_but_inflight_continues(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    inflight = issue_route_ticket(srv, rid)
    start_route(srv, rid, inflight)
    with srv.client() as c:
        assert c.post(f"/api/admin/routes/{rid}/pause",
                      json={"reason": "临时暂停"}).status_code == 200
        # 不能再绑定新票 / 发新版本
        r = c.post("/api/admin/tickets", json={
            "person_id": _unique("V"), "ttl_seconds": 600,
            "zones": [ZONE], "route_id": rid})
        assert r.status_code == 409
        r = c.post(f"/api/admin/routes/{rid}/versions",
                   json={"checkpoints": [{"gate_id": "rt1"}]})
        assert r.status_code == 409
    # 在途访客继续走原版本直到完成
    assert checkpoint(srv, inflight, "rt2").status_code == 200
    assert checkpoint(srv, inflight, "rt3").status_code == 200
    assert progress(srv, inflight)["status"] == "COMPLETED"
    with srv.client() as c:
        assert c.post(f"/api/admin/routes/{rid}/resume",
                      json={"reason": "恢复"}).status_code == 200


def test_new_version_replaces_future_use_inflight_pinned(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    old = issue_route_ticket(srv, rid)
    start_route(srv, rid, old)  # 在途，固化 v1
    # 发布 v2：rt3 与 rt2 顺序对调
    with srv.client() as c:
        r = c.post(f"/api/admin/routes/{rid}/versions", json={"checkpoints": [
            {"gate_id": "rt1", "name": "入口"},
            {"gate_id": "rt3", "name": "新中检", "max_stay_seconds": 3600},
            {"gate_id": "rt2", "name": "新出口"}]})
        assert r.status_code == 201 and r.json()["published_version"] == 2
    # 在途票仍走 v1：下一个是 rt2，rt2->rt3 完成
    assert checkpoint(srv, old, "rt2").status_code == 200
    r = checkpoint(srv, old, "rt3")
    assert r.status_code == 200 and r.json()["status"] == "ROUTE_COMPLETED"
    assert r.json()["route"]["route_version"] == 1
    # 新票走 v2：rt2 在 v2 里是第 3 点，先扫 rt2 即跳过
    new = issue_route_ticket(srv, rid)
    block = start_route(srv, rid, new)
    assert block["route_version"] == 2 and block["next_seq"] == 2
    r = checkpoint(srv, new, "rt2")
    assert r.status_code == 409 and r.json()["reason"] == "skipped_checkpoint"


def test_admin_rebind_unstarted_ticket_rejected_after_start(srv):
    rid_a = make_route(srv, [("rt1", None), ("rt2", None)])
    rid_b = make_route(srv, [("rt1", None), ("rt3", None)])
    code = issue_route_ticket(srv, rid_a)
    with srv.client() as c:
        r = c.post("/api/admin/routes/bindings", json={
            "route_id": rid_b, "scope": "TICKET", "code": code})
        assert r.status_code == 200 and r.json()["route_version"] == 1
    start_route(srv, rid_b, code)
    with srv.client() as c:
        r = c.post("/api/admin/routes/bindings", json={
            "route_id": rid_a, "scope": "TICKET", "code": code})
        assert r.status_code == 409  # 已经开始不能改绑


# ---------------- 7. 离线补齐 / 冲突保留 / 管理员处理 ----------------

def _replay(srv, gate, events):
    with gate_client(srv) as gc:
        return gc.post("/api/gate/checkpoints/replay",
                       json={"gate_id": gate, "events": events})


def test_offline_events_replay_in_order_and_idempotent(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    now = datetime.now(timezone.utc)
    payload = {"gate_id": "rt2", "events": [{
        "code": code, "attempt_id": str(uuid.uuid4()),
        "event_ts": _iso(now + timedelta(seconds=5))}]}
    r1 = _replay(srv, "rt2", payload["events"])
    r2 = _replay(srv, "rt2", payload["events"])  # 重发
    assert r1.status_code == 200 and r1.json()["results"][0]["ok"] is True
    item2 = r2.json()["results"][0]
    assert item2.get("replayed") is True
    assert progress(srv, code)["current_seq"] == 2


def test_offline_gap_becomes_conflict_and_admin_applies(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    now = datetime.now(timezone.utc)
    # 离线期间 rt3 直接上报（rt2 缺失）：保留冲突，路线不推进
    r = _replay(srv, "rt3", [{"code": code, "attempt_id": str(uuid.uuid4()),
                              "event_ts": _iso(now + timedelta(seconds=30))}])
    item = r.json()["results"][0]
    assert item["status"] == "ROUTE_CONFLICT"
    assert item["conflict_kind"] == "GAP_PENDING"
    cid = item["conflict_id"]
    assert progress(srv, code)["current_seq"] == 1
    # 冲突出现在管理队列
    with srv.client() as c:
        open_conflicts = c.get("/api/admin/route-conflicts",
                               params={"status": "OPEN"}).json()["conflicts"]
    assert any(x["id"] == cid for x in open_conflicts)
    # 补齐缺口 rt2 后，管理员把冲突事件按现场 APPLIED：推进到 rt3 并完成
    assert checkpoint(srv, code, "rt2").status_code == 200
    with srv.client() as c:
        r = c.post(f"/api/admin/route-conflicts/{cid}/resolve",
                   json={"action": "APPLIED", "reason": "缺口已补，放行"})
        assert r.status_code == 200, r.text
        assert r.json()["resolved"] == "APPLIED"
    assert progress(srv, code)["status"] == "COMPLETED"
    # 冲突记录保留（不删除），标记已处理
    with srv.client() as c:
        all_conflicts = c.get("/api/admin/route-conflicts",
                              params={"status": "ALL"}).json()["conflicts"]
    rec = next(x for x in all_conflicts if x["id"] == cid)
    assert rec["status"] == "RESOLVED" and rec["resolution"] == "APPLIED"


def test_offline_event_after_terminal_is_conflict_and_dismissible(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    now = datetime.now(timezone.utc)
    checkpoint(srv, code, "rt2")
    assert checkpoint(srv, code, "rt3").status_code == 200  # COMPLETED
    # 离线旧事件（rt2 的迟到上报）到达：ALREADY_TERMINAL 冲突，状态不回退
    r = _replay(srv, "rt2", [{"code": code, "attempt_id": str(uuid.uuid4()),
                              "event_ts": _iso(now + timedelta(seconds=5))}])
    item = r.json()["results"][0]
    assert item["conflict_kind"] == "ALREADY_TERMINAL"
    cid = item["conflict_id"]
    assert progress(srv, code)["status"] == "COMPLETED"
    with srv.client() as c:
        r = c.post(f"/api/admin/route-conflicts/{cid}/resolve",
                   json={"action": "DISMISSED", "reason": "迟到的重复事件"})
        assert r.status_code == 200 and r.json()["resolved"] == "DISMISSED"
    assert progress(srv, code)["status"] == "COMPLETED"


def test_admin_mark_violated_resolves_conflict(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    now = datetime.now(timezone.utc)
    r = _replay(srv, "rt3", [{"code": code, "attempt_id": str(uuid.uuid4()),
                              "event_ts": _iso(now + timedelta(seconds=20))}])
    cid = r.json()["results"][0]["conflict_id"]
    with srv.client() as c:
        r = c.post(f"/api/admin/route-conflicts/{cid}/resolve",
                   json={"action": "MARK_VIOLATED", "reason": "确认访客越区"})
        assert r.status_code == 200
    assert progress(srv, code)["status"] == "VIOLATED"
    assert progress(srv, code)["violation_kind"] == "ADMIN_MARK_VIOLATED"


def test_offline_future_timestamp_is_invalid_conflict(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    r = _replay(srv, "rt2", [{"code": code, "attempt_id": str(uuid.uuid4()),
                              "event_ts": _iso(future)}])
    item = r.json()["results"][0]
    assert item["status"] == "ROUTE_CONFLICT"
    assert item["conflict_kind"] == "INVALID_TIMESTAMP"
    assert progress(srv, code)["current_seq"] == 1


def test_route_events_deliver_through_gate_sync(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    checkpoint(srv, code, "rt2")
    # 门点按版本分页补齐（每页 200），翻页直到取完
    all_events = []
    since = 0
    with gate_client(srv) as gc:
        for _ in range(50):
            data = gc.post("/api/gate/sync",
                           json={"gate_id": "rt1", "since_version": since}).json()
            all_events.extend(data["events"])
            since = data["next_since"]
            if not data["has_more"]:
                break
    types = {e["type"] for e in all_events}
    assert {"ROUTE_CREATED", "ROUTE_VERSION_PUBLISHED", "ROUTE_BOUND",
            "ROUTE_STARTED", "ROUTE_COMPLETED"} <= types
    route_events = [e for e in all_events if e.get("route_id") == rid]
    started = next(e for e in route_events if e["type"] == "ROUTE_STARTED")
    assert started["checkpoint_seq"] == 1 and started["ticket_code"] == code


# ---------------- 8. 多维查询 ----------------

def test_monitoring_queries_by_zone_route_person(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    person = _unique("VQ")
    code = issue_route_ticket(srv, rid, person=person)
    start_route(srv, rid, code)
    checkpoint(srv, code, "rt2")
    with srv.client() as c:
        by_zone = c.get("/api/admin/route-progress",
                        params={"zone_id": ZONE, "status": "IN_PROGRESS"}).json()
        by_route = c.get("/api/admin/route-progress",
                         params={"route_id": rid, "status": "IN_PROGRESS"}).json()
        person_view = c.get(f"/api/admin/people/{person}").json()
        checks = c.get("/api/admin/route-checks",
                       params={"code": code}).json()["checks"]
    assert any(x["ticket_code"] == code for x in by_zone["progress"])
    assert any(x["ticket_code"] == code for x in by_route["progress"])
    pr = person_view["routes"][0]
    assert pr["ticket_code"] == code and pr["current_seq"] == 2
    # 门点检查记录含票/人员/路线版本/检查点顺序/门点
    decisions = {(x["gate_id"], x["checkpoint_seq"], x["decision"]) for x in checks}
    assert ("rt1", 1, "STARTED") in decisions
    assert ("rt2", 2, "ADVANCED") in decisions
    for x in checks:
        assert x["route_version"] == 1 and x["person_id"] == person


def test_violation_history_query(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    code = issue_route_ticket(srv, rid)
    start_route(srv, rid, code)
    checkpoint(srv, code, "rt3")  # skip -> violated
    with srv.client() as c:
        violated = c.get("/api/admin/route-progress",
                         params={"status": "VIOLATED", "route_id": rid}).json()
        rejected = c.get("/api/admin/route-checks",
                         params={"code": code, "decision": "VIOLATED"}).json()
    hit = [x for x in violated["progress"] if x["ticket_code"] == code]
    assert hit and hit[0]["violation_kind"] == "SKIPPED_CHECKPOINT"
    assert len(rejected["checks"]) == 1
    assert rejected["checks"][0]["result"] == "skipped_checkpoint"


# ---------------- 9. 重启持久化 ----------------

def test_route_state_persists_across_restart(srv):
    rid = make_route(srv, [("rt1", None), ("rt2", 3600), ("rt3", None)])
    done = issue_route_ticket(srv, rid)
    start_route(srv, rid, done)
    checkpoint(srv, done, "rt2")
    checkpoint(srv, done, "rt3")

    inflight = issue_route_ticket(srv, rid)
    start_route(srv, rid, inflight)
    checkpoint(srv, inflight, "rt2")

    bad = issue_route_ticket(srv, rid)
    start_route(srv, rid, bad)
    checkpoint(srv, bad, "rt3")  # skip -> violated

    # 造一个 OPEN 冲突
    now = datetime.now(timezone.utc)
    conflict_code = issue_route_ticket(srv, rid)
    start_route(srv, rid, conflict_code)
    cr = _replay(srv, "rt3", [{"code": conflict_code,
                               "attempt_id": str(uuid.uuid4()),
                               "event_ts": _iso(now + timedelta(seconds=40))}])
    cid = cr.json()["results"][0]["conflict_id"]

    srv.restart()

    assert progress(srv, done)["status"] == "COMPLETED"
    p = progress(srv, inflight)
    # 在途路线继续使用原固化版本，重启后仍可完成
    assert p["status"] == "IN_PROGRESS" and p["current_seq"] == 2
    assert p["route_version"] == 1
    assert checkpoint(srv, inflight, "rt3").status_code == 200
    assert progress(srv, inflight)["status"] == "COMPLETED"
    # 违规终态保持，旧事件不能复活
    assert progress(srv, bad)["status"] == "VIOLATED"
    r = checkpoint(srv, bad, "rt2")
    assert r.status_code == 409 and "violated" in r.json()["reason"]
    # OPEN 冲突仍在队列，处理后生效
    with srv.client() as c:
        open_conflicts = c.get("/api/admin/route-conflicts",
                               params={"status": "OPEN"}).json()["conflicts"]
    assert any(x["id"] == cid for x in open_conflicts)
    checkpoint(srv, conflict_code, "rt2")
    with srv.client() as c:
        r = c.post(f"/api/admin/route-conflicts/{cid}/resolve",
                   json={"action": "APPLIED", "reason": "重启后处理"})
        assert r.status_code == 200, r.text
    assert progress(srv, conflict_code)["status"] == "COMPLETED"
