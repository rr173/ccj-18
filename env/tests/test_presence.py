"""访客在场清册 + 应急清点模块的端到端测试（真实 HTTP，复用会话级服务）。

覆盖：
  * 核销成功自动登记到场（申请人+同行人数/名单冻结）、门点确认离场；
  * 已离场不会因重放/重扫/并发回到在场名单；到场-离场-更正只有一个明确顺序；
  * 管理员漏扫/误扫人工更正（带原因+操作者），原始轨迹保留；
  * 应急清点快照固定在场人员/同行名单/批次分区/最后门点，之后变化不改旧快照；
  * 门点离线重连按版本补齐到场/离场/更正/清点事件；
  * 按分区/批次/人员查询当前在场、未确认离场、任一次清点结果；
  * 重启持久化。
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from test_system import gate_client, issue, redeem


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


@pytest.fixture(scope="module", autouse=True)
def presence_setup(srv):
    with srv.client() as c:
        for zid, zname in [("ZP1", "在场一区"), ("ZP2", "在场二区"),
                           ("ZPR", "清点专用区")]:
            r = c.post("/api/admin/zones", json={"id": zid, "name": zname})
            assert r.status_code in (200, 409)
        for gid, gname, z in [("gp1", "在场一门", "ZP1"),
                              ("gp2", "在场二门", "ZP1"),
                              ("gp3", "在场三区门", "ZP2"),
                              ("gpr", "清点专用门", "ZPR")]:
            r = c.post("/api/admin/gates", json={"id": gid, "name": gname})
            assert r.status_code in (200, 409)
            assert c.put(f"/api/admin/gates/{gid}/zone",
                         json={"zone_id": z}).status_code == 200


def _pub(srv):
    import httpx
    return httpx.Client(base_url=srv.base, timeout=15)


def make_batch(srv, capacity=10, zone="ZP1", ttl=3600, companions=0, name=None):
    """建批+申请（指定同行人数）+审核，返回 (batch, app, ticket)。"""
    start = datetime.now(timezone.utc)
    pub = _pub(srv)
    with srv.client() as c:
        b = c.post("/api/admin/batches", json={
            "name": name or f"在场批-{uuid.uuid4().hex[:6]}",
            "visit_date": start.date().isoformat(),
            "start_at": _iso(start), "end_at": _iso(start + timedelta(seconds=ttl)),
            "zone_id": zone, "capacity": capacity,
        }).json()
        a = pub.post(f"/api/public/batches/{b['apply_token']}/applications", json={
            "request_id": uuid.uuid4().hex,
            "name": f"访客{uuid.uuid4().hex[:5]}",
            "contact": "v@example.com",
            "companions": companions,
            "companion_names": [f"同行{i}" for i in range(companions)],
        }).json()
        t = c.post(f"/api/admin/applications/{a['id']}/approve").json()["ticket"]
    return b, a, t


def depart(srv, code, gate="gp2", attempt=None, client=None):
    c = client or gate_client(srv)
    return c.post("/api/gate/departure", json={
        "gate_id": gate, "code": code,
        "attempt_id": attempt or str(uuid.uuid4())})


def correct(srv, code, action, reason="人工处理", **extra):
    with srv.client() as c:
        body = {"code": code, "action": action, "reason": reason}
        body.update(extra)
        return c.post("/api/admin/presence/corrections", json=body)


def roster(srv, **params):
    with srv.client() as c:
        return c.get("/api/admin/presence", params=params).json()


def presence_detail(srv, code):
    with srv.client() as c:
        return c.get(f"/api/admin/presence/{code}").json()


def take_rollcall(srv, reason="消防演练清点", **params):
    with srv.client() as c:
        body = {"reason": reason}
        body.update(params)
        r = c.post("/api/admin/rollcalls", json=body)
        assert r.status_code == 201, r.text
        return r.json()["rollcall"]


def get_rollcall(srv, rc_id):
    with srv.client() as c:
        return c.get(f"/api/admin/rollcalls/{rc_id}").json()


# 1. 核销成功即登记到场：人数/名单/分区/批次/门点冻结
def test_redeem_registers_arrival(srv):
    b, a, t = make_batch(srv, companions=2)
    r = redeem(srv, t["code"], "gp1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["presence_status"] == "ARRIVED"
    av = body["arrival_version"]
    assert av == body["version"] + 1  # 核销事件后紧跟到场事件

    d = presence_detail(srv, t["code"])
    p = d["presence"]
    assert p["status"] == "ARRIVED"
    assert p["party_size"] == 3 and p["companions"] == 2
    assert p["companion_names"] == ["同行0", "同行1"]
    assert p["applicant_name"] == a["name"]
    assert p["zone_id"] == "ZP1" and p["batch_id"] == b["id"]
    assert p["arrived_gate"] == "gp1"
    # 轨迹第一条到场：操作者为门点
    assert d["trail"][0]["kind"] == "ARRIVED"
    assert d["trail"][0]["operator"] == "gate:gp1"
    assert d["trail"][0]["version"] == av

    # 当前在场名单包含该票（按批次过滤）
    found = [x for x in roster(srv, batch_id=b["id"])["presence"]
             if x["ticket_code"] == t["code"]]
    assert len(found) == 1 and found[0]["party_size"] == 3


# 2. 门点确认离场；离场后重放/重扫都不能复活
def test_departure_confirms_and_never_revives(srv):
    _, _, t = make_batch(srv, companions=1)
    redeem(srv, t["code"], "gp1")
    attempt = str(uuid.uuid4())
    r1 = depart(srv, t["code"], "gp2", attempt=attempt)
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "DEPARTED"
    assert r1.json()["departed_gate"] == "gp2"

    # 同一 attempt_id 重放：幂等，且不会再产生轨迹
    r2 = depart(srv, t["code"], "gp2", attempt=attempt)
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    # 新 attempt_id 再扫离场：已离场 -> 409
    r3 = depart(srv, t["code"], "gp2")
    assert r3.status_code == 409 and r3.json()["reason"] == "not_present"
    # 重新核销（新 attempt）也只是 already_redeemed，人不会回到在场名单
    r4 = redeem(srv, t["code"], "gp1")
    assert r4.status_code == 409
    # 在场名单查不到；离场名单能查到
    onsite = roster(srv, view="onsite", q=t["code"])["presence"]
    assert onsite == []
    gone = [x for x in roster(srv, view="departed", q=t["code"])["presence"]
            if x["ticket_code"] == t["code"]]
    assert len(gone) == 1 and gone[0]["status"] == "DEPARTED"

    d = presence_detail(srv, t["code"])
    assert [e["kind"] for e in d["trail"]] == ["ARRIVED", "DEPARTED"]


# 3. 未到场先离场：明确结论 409，不产生任何在场行
def test_departure_without_arrival(srv):
    _, _, t = make_batch(srv)
    r = depart(srv, t["code"], "gp2")
    assert r.status_code == 409 and r.json()["reason"] == "no_presence"
    assert roster(srv, q=t["code"], view="all")["presence"] == []
    # 票面不存在
    assert depart(srv, "T-NOPE0000", "gp2").status_code == 404


# 4. 到场/离场/人工更正并发：只有一个明确顺序，状态自洽
def test_concurrent_depart_and_correct_single_order(srv):
    cases = []
    for _ in range(6):
        _, _, t = make_batch(srv)
        redeem(srv, t["code"], "gp1")
        cases.append(t["code"])

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def worker(code, kind):
        barrier.wait()
        if kind == "depart":
            r = depart(srv, code, "gp2")
        else:
            r = correct(srv, code, "MARK_DEPARTED", reason="漏扫离场并发")
        with lock:
            results.append((code, kind, r.status_code, r.json()))

    with ThreadPoolExecutor(max_workers=12) as ex:
        for code in cases:
            ex.submit(worker, code, "depart")
            ex.submit(worker, code, "correct")

    # 每张票：恰有一个动作成功，另一个撞上 409；最终状态唯一为 DEPARTED
    for code in cases:
        mine = [x for x in results if x[0] == code]
        assert sorted(x[2] for x in mine) == [200, 409], mine
        d = presence_detail(srv, code)
        assert d["presence"]["status"] == "DEPARTED"
        # 顺序明确：轨迹版本严格递增，只追加，无重复 seq
        seqs = [e["seq"] for e in d["trail"]]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        kinds = [e["kind"] for e in d["trail"]]
        assert kinds[0] == "ARRIVED" and kinds[-1] in ("DEPARTED", "MARK_DEPARTED")
    # 所有票都不在当前在场名单（已离场的人不会出现）
    onsite_codes = {x["ticket_code"] for x in roster(srv, view="onsite")["presence"]}
    assert not (set(cases) & onsite_codes)


# 5. 误扫更正：REMOVE 把误登记的人移出在场名单；RESTORE 纠正误离场
def test_remove_and_restore_corrections(srv):
    _, _, wrong = make_batch(srv)   # 被误扫进场
    _, _, real = make_batch(srv)    # 真到场但被错误登记离场
    redeem(srv, wrong["code"], "gp1")
    redeem(srv, real["code"], "gp1")

    # 误扫移除：必须带原因，操作者落轨迹
    r = correct(srv, wrong["code"], "REMOVE", reason="访客走错门，误扫票号",
                operator="安保-张")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["presence"]["status"] == "REMOVED"
    assert body["correction"]["operator"] == "admin:安保-张"
    # 没有原因 -> 400
    bad = correct(srv, wrong["code"], "RESTORE", reason="   ")
    assert bad.status_code in (400, 422)
    # 已 REMOVED 不在在场名单；门点自动路径不能复活（核销已使用、离场 409）
    assert roster(srv, q=wrong["code"])["presence"] == []
    assert depart(srv, wrong["code"], "gp2").status_code == 409

    # RESTORE：管理员确认误移除，纠正回场；原轨迹完整保留
    r2 = correct(srv, wrong["code"], "RESTORE", reason="核对监控后人其实在场",
                 operator="安保-张")
    assert r2.status_code == 200
    d = presence_detail(srv, wrong["code"])
    assert d["presence"]["status"] == "ARRIVED"
    kinds = [e["kind"] for e in d["trail"]]
    assert kinds == ["ARRIVED", "REMOVE", "RESTORE"]
    # 原始移除记录的原因/操作者仍在
    remove_ev = next(e for e in d["trail"] if e["kind"] == "REMOVE")
    assert remove_ev["reason"] == "访客走错门，误扫票号"
    assert remove_ev["operator"] == "admin:安保-张"
    # 重新出现在场名单（人工纠正后）
    assert any(x["ticket_code"] == wrong["code"]
               for x in roster(srv)["presence"])

    # 门点误登记离场后管理员纠正回场，原 DEPARTED 轨迹保留
    depart(srv, real["code"], "gp2")
    assert presence_detail(srv, real["code"])["presence"]["status"] == "DEPARTED"
    r3 = correct(srv, real["code"], "RESTORE", reason="离场扫错票，人还在场内")
    assert r3.status_code == 200
    d2 = presence_detail(srv, real["code"])
    assert [e["kind"] for e in d2["trail"]] == ["ARRIVED", "DEPARTED", "RESTORE"]
    assert any(x["ticket_code"] == real["code"]
               for x in roster(srv)["presence"])


# 6. 漏扫更正：未核销的票管理员补登记到场（票同事务核销）；在场者漏扫离场
def test_missed_scan_corrections(srv):
    _, _, never_scanned = make_batch(srv, companions=1)
    _, _, missed_exit = make_batch(srv)

    # 漏扫到场：票仍 ACTIVE，补登记把票核销 + 人计入在场
    ticket_before = None
    with srv.client() as c:
        ticket_before = c.get(f"/api/admin/tickets/{never_scanned['code']}").json()
    assert ticket_before["status"] == "ACTIVE"
    r = correct(srv, never_scanned["code"], "MARK_ARRIVED",
                reason="访客走侧门，设备离线未扫", gate_id="gp1")
    assert r.status_code == 200, r.text
    p = r.json()["presence"]
    assert p["status"] == "ARRIVED" and p["arrived_gate"] == "gp1"
    assert p["party_size"] == 2  # 整组人数来自申请
    with srv.client() as c:
        assert c.get(f"/api/admin/tickets/{never_scanned['code']}").json()[
            "status"] == "REDEEMED"
    # 此后门点扫到该票：already_redeemed（人仍在场，不产生重复到场）
    rr = redeem(srv, never_scanned["code"], "gp2")
    assert rr.status_code == 409
    assert presence_detail(srv, never_scanned["code"])["presence"]["status"] == "ARRIVED"

    # 状态机保护：已在场不能再 MARK_ARRIVED
    assert correct(srv, never_scanned["code"], "MARK_ARRIVED",
                   reason="重复补登").status_code == 409

    # 漏扫离场：在场但没从门点走，管理员登记离场
    redeem(srv, missed_exit["code"], "gp1")
    r2 = correct(srv, missed_exit["code"], "MARK_DEPARTED",
                 reason="监控确认已从围栏缺口离开", gate_id="gp2")
    assert r2.status_code == 200
    assert r2.json()["presence"]["status"] == "DEPARTED"
    assert r2.json()["presence"]["departed_gate"] == "gp2"
    d = presence_detail(srv, missed_exit["code"])
    assert [e["kind"] for e in d["trail"]] == ["ARRIVED", "MARK_DEPARTED"]
    assert all(e["operator"].startswith("admin:") for e in d["trail"][1:])


# 7. 对已离场票的漏扫补登记到场：轨迹追加，不删原离场记录
def test_mark_arrived_after_departed(srv):
    _, _, t = make_batch(srv)
    redeem(srv, t["code"], "gp1")
    depart(srv, t["code"], "gp2")
    r = correct(srv, t["code"], "MARK_ARRIVED", reason="二次入场，人工补登记")
    assert r.status_code == 200
    d = presence_detail(srv, t["code"])
    assert d["presence"]["status"] == "ARRIVED"
    assert [e["kind"] for e in d["trail"]] == ["ARRIVED", "DEPARTED", "MARK_ARRIVED"]


# 8. 应急清点快照：固定人员/名单/分区/批次/最后门点，之后变化不改旧快照
def test_rollcall_snapshot_is_immutable(srv):
    # 用清点专用分区，避免会话库里其他测试的在场残留影响全量快照
    _, _, t1 = make_batch(srv, companions=1, zone="ZPR")
    _, _, t2 = make_batch(srv, companions=0, zone="ZPR")
    _, _, t3 = make_batch(srv, companions=3, zone="ZPR")
    redeem(srv, t1["code"], "gpr")
    redeem(srv, t2["code"], "gpr")
    redeem(srv, t3["code"], "gpr")
    # t2 离开：快照固定其不在场，且 t1 的“最后门点”在再次确认前保持到场门
    depart(srv, t2["code"], "gpr")

    rc = take_rollcall(srv, reason="首次应急清点", operator="值班经理")
    assert rc["id"].startswith("R-")
    # t2 已离场，不在快照；本测试的三个在场票里只有 t1(2人) 与 t3(4人)
    codes = {e["ticket_code"]: e for e in rc["entries"]
             if e["ticket_code"] in (t1["code"], t2["code"], t3["code"])}
    assert set(codes) == {t1["code"], t3["code"]}
    own = [e for e in rc["entries"]
           if e["ticket_code"] in (t1["code"], t2["code"], t3["code"])]
    assert sum(e["party_size"] for e in own) == 6
    e1 = codes[t1["code"]]
    assert e1["party_size"] == 2 and e1["companion_names"] == ["同行0"]
    assert e1["zone_id"] == "ZPR"
    assert e1["last_gate"] == "gpr" and e1["last_event_kind"] == "ARRIVED"
    e3 = codes[t3["code"]]
    assert e3["party_size"] == 4 and e3["zone_id"] == "ZPR"

    # 快照之后：t1 离场、t4 到场
    depart(srv, t1["code"], "gpr")
    _, _, t4 = make_batch(srv, companions=0, zone="ZPR")
    redeem(srv, t4["code"], "gpr")

    # 旧快照内容完全不变（人员/人数/最后门点/版本）
    rc_again = get_rollcall(srv, rc["id"])
    own_again = [e for e in rc_again["entries"]
                 if e["ticket_code"] in (t1["code"], t2["code"], t3["code"],
                                         t4["code"])]
    assert own_again == own
    assert rc_again["version"] == rc["version"]

    # 新清点反映新状态：t3+t4 在场，t1 已离场
    rc2 = take_rollcall(srv, reason="第二次清点")
    new_codes = {e["ticket_code"] for e in rc2["entries"]
                 if e["ticket_code"] in (t1["code"], t2["code"], t3["code"],
                                         t4["code"])}
    assert new_codes == {t3["code"], t4["code"]}
    assert rc2["version"] > rc["version"]


# 9. 按分区/批次清点过滤；按人查询任一次清点结果
def test_rollcall_scoped_filters_and_person_lookup(srv):
    bz1, a1, t1 = make_batch(srv, companions=1, zone="ZPR")
    _, _, t2 = make_batch(srv, companions=0, zone="ZP2")
    redeem(srv, t1["code"], "gpr")
    redeem(srv, t2["code"], "gp3")

    rc_zone = take_rollcall(srv, zone_id="ZPR", reason="只清点专用区")
    assert t1["code"] in [e["ticket_code"] for e in rc_zone["entries"]]
    assert t2["code"] not in [e["ticket_code"] for e in rc_zone["entries"]]
    e1 = next(e for e in rc_zone["entries"] if e["ticket_code"] == t1["code"])
    assert e1["party_size"] == 2
    # 批次范围精确隔离：本批次只有 t1
    rc_batch = take_rollcall(srv, batch_id=bz1["id"], reason="只清点本批次")
    assert [e["ticket_code"] for e in rc_batch["entries"]] == [t1["code"]]
    assert rc_batch["headcount"] == 2 and rc_batch["groups"] == 1
    # 未知分区/批次
    with srv.client() as c:
        assert c.post("/api/admin/rollcalls",
                      json={"zone_id": "ZZZ"}).status_code == 400
        assert c.post("/api/admin/rollcalls",
                      json={"batch_id": "B-NOPE"}).status_code == 404

    # 按人查“任一次清点结果”：列表过滤 + 快照内条目
    with srv.client() as c:
        lst = c.get("/api/admin/rollcalls",
                    params={"person_id": a1["id"]}).json()["rollcalls"]
    rc_ids = {x["id"] for x in lst}
    assert rc_zone["id"] in rc_ids and rc_batch["id"] in rc_ids
    full = get_rollcall(srv, rc_batch["id"])
    entry = next(e for e in full["entries"] if e["person_id"] == a1["id"])
    assert entry["ticket_code"] == t1["code"] and entry["party_size"] == 2
    # 该人不在 ZP2 范围的快照里
    other = take_rollcall(srv, zone_id="ZP2")
    assert a1["id"] not in [e["person_id"] for e in other["entries"]]


# 10. 查询：按分区/批次/人员过滤当前在场；未确认离场口径
def test_roster_queries(srv):
    b1, a1, t1 = make_batch(srv, companions=2, zone="ZP1")
    b2, a2, t2 = make_batch(srv, companions=0, zone="ZP2")
    redeem(srv, t1["code"], "gp1")
    redeem(srv, t2["code"], "gp3")
    _, _, gone = make_batch(srv, companions=0, zone="ZP1")
    redeem(srv, gone["code"], "gp1")
    depart(srv, gone["code"], "gp2")

    with srv.client() as c:
        z1 = c.get("/api/admin/presence",
                   params={"zone_id": "ZP1"}).json()
        assert t1["code"] in [x["ticket_code"] for x in z1["presence"]]
        assert t2["code"] not in [x["ticket_code"] for x in z1["presence"]]
        bb = c.get("/api/admin/presence",
                   params={"batch_id": b1["id"]}).json()
        assert [x["ticket_code"] for x in bb["presence"]] == [t1["code"]]
        pp = c.get("/api/admin/presence",
                   params={"person_id": a1["id"]}).json()
        assert [x["ticket_code"] for x in pp["presence"]] == [t1["code"]]
        # 关键字模糊（姓名/票号/人员标识）
        qq = c.get("/api/admin/presence",
                   params={"q": a1["name"]}).json()
        assert any(x["ticket_code"] == t1["code"] for x in qq["presence"])
        # 未确认离场 = 当前在场集合
        unc = c.get("/api/admin/presence",
                    params={"view": "unconfirmed"}).json()
        onsite_ids = {x["ticket_code"] for x in c.get(
            "/api/admin/presence", params={"view": "onsite"}).json()["presence"]}
        assert {x["ticket_code"] for x in unc["presence"]} == onsite_ids
        assert gone["code"] not in onsite_ids
        # 汇总：组数/总人数含同行人
        summ = c.get("/api/admin/presence/summary").json()
        assert summ["onsite_groups"] >= 2
        assert summ["onsite_people"] >= 3
        by_zone = {z["zone_id"]: z for z in summ["by_zone"]}
        assert by_zone["ZP1"]["people"] >= 3  # t1 整组3人


# 11. 门点离线重连：按版本补齐到场/离场/更正/清点事件
def test_gate_sync_presence_events(srv):
    c = gate_client(srv)
    since = c.post("/api/gate/sync",
                   json={"gate_id": "gp1", "since_version": 0}).json()["next_since"]
    _, _, t = make_batch(srv, companions=1)
    redeem(srv, t["code"], "gp1")
    depart(srv, t["code"], "gp2")
    correct(srv, t["code"], "RESTORE", reason="离线期间的人工更正")
    rc = take_rollcall(srv, reason="离线期间清点")

    pages, all_events, cursor = [], [], since
    for _ in range(10):
        data = c.post("/api/gate/sync",
                      json={"gate_id": "gp1", "since_version": cursor}).json()
        all_events.extend(data["events"])
        cursor = data["next_since"]
        if not data["has_more"]:
            break
    by_code = {}
    rollcall_versions = []
    for e in all_events:
        if e["ticket_code"] == t["code"]:
            by_code.setdefault(e["type"], e)
        if e["type"] == "ROLLCALL_TAKEN":
            rollcall_versions.append(e)
    assert "PRESENCE_ARRIVED" in by_code
    assert "PRESENCE_DEPARTED" in by_code
    corr = by_code["PRESENCE_CORRECTED"]
    assert corr["payload"]["kind"] == "RESTORE"
    assert corr["payload"]["reason"] == "离线期间的人工更正"
    assert any(e["rollcall_id"] == rc["id"] for e in rollcall_versions)
    # 事件严格按版本有序
    versions = [e["version"] for e in all_events]
    assert versions == sorted(versions)


# 12. 批次详情含在场清册；人员视图含在场轨迹
def test_admin_views_include_presence(srv):
    b, a, t = make_batch(srv, companions=1)
    redeem(srv, t["code"], "gp1")
    with srv.client() as c:
        d = c.get(f"/api/admin/batches/{b['id']}").json()
        codes = [x["ticket_code"] for x in d["presence"]["onsite"]]
        assert t["code"] in codes
        assert d["presence"]["onsite_people"] >= 2
        pv = c.get(f"/api/admin/people/{a['id']}").json()
        rec = next(x for x in pv["presence"] if x["ticket_code"] == t["code"])
        assert rec["status"] == "ARRIVED"
        assert rec["trail"][0]["kind"] == "ARRIVED"


# 13. 重启后：在场状态、轨迹、快照全部延续
def test_presence_persistence_across_restart(srv):
    b, a, t = make_batch(srv, companions=2, zone="ZPR")
    redeem(srv, t["code"], "gpr")
    depart(srv, t["code"], "gpr")
    correct(srv, t["code"], "RESTORE", reason="重启前纠正")
    rc = take_rollcall(srv, reason="重启前清点", batch_id=b["id"])
    last_version = srv.client().get("/api/admin/stats").json()["last_version"]

    srv.restart()

    with srv.client() as c:
        d = c.get(f"/api/admin/presence/{t['code']}").json()
        assert d["presence"]["status"] == "ARRIVED"
        assert d["presence"]["party_size"] == 3
        assert [e["kind"] for e in d["trail"]] == [
            "ARRIVED", "DEPARTED", "RESTORE"]
        rc2 = c.get(f"/api/admin/rollcalls/{rc['id']}").json()
        assert rc2["headcount"] == 3 and rc2["groups"] == 1
        assert any(e["ticket_code"] == t["code"] for e in rc2["entries"])
        assert c.get("/api/admin/stats").json()["last_version"] == last_version
    # 已离场纠正后在场：门点再次确认离场成功（不会重放旧离场）
    r = depart(srv, t["code"], "gpr")
    assert r.status_code == 200
    with srv.client() as c:
        assert c.get(f"/api/admin/presence/{t['code']}").json()[
            "presence"]["status"] == "DEPARTED"


# 14. 混合并发压测：每张票 4 个动作（门点离场/漏扫离场/误扫移除/纠正回场）同时打，
#     结果必须是一条严格有序、状态自洽的轨迹，且名单与最终状态一致
def test_mixed_concurrent_corrections_are_linearizable(srv):
    tickets = []
    for _ in range(8):
        _, _, t = make_batch(srv)
        redeem(srv, t["code"], "gp1")
        tickets.append(t["code"])

    actions = [
        ("depart", None),
        ("correct", ("MARK_DEPARTED", "并发漏扫离场")),
        ("correct", ("REMOVE", "并发误扫移除")),
        ("correct", ("RESTORE", "并发纠正回场")),
    ]
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(32)

    def worker(code, kind, payload):
        barrier.wait()
        if kind == "depart":
            r = depart(srv, code, "gp2")
        else:
            action, reason = payload
            r = correct(srv, code, action, reason=reason)
        with lock:
            results.append((code, kind, r.status_code, r.json()))

    with ThreadPoolExecutor(max_workers=32) as ex:
        for code in tickets:
            for kind, payload in actions:
                ex.submit(worker, code, kind, payload)

    status_ok = {200, 409}
    for code in tickets:
        mine = [x for x in results if x[0] == code]
        assert all(x[2] in status_ok for x in mine), mine
        d = presence_detail(srv, code)
        p = d["presence"]
        trail = d["trail"]
        # 轨迹严格按 seq 追加；每一步的 to_status 必须等于当时 presence.status
        assert [e["seq"] for e in trail] == list(range(1, len(trail) + 1))
        for e in trail:
            assert e["to_status"] in ("ARRIVED", "DEPARTED", "REMOVED")
        # 最后一条轨迹的目标状态必须等于 presence 当前状态
        assert p["status"] == trail[-1]["to_status"]
        # 操作者/原因齐备：门点动作 gate:*，更正动作 admin:* 且带原因
        for e in trail[1:]:
            if e["kind"] in ("DEPARTED",):
                assert e["operator"].startswith("gate:")
            else:
                assert e["operator"].startswith("admin:") and e["reason"]
        # 名单一致性：只有 ARRIVED 出现在当前在场名单
        onsite = any(x["ticket_code"] == code
                     for x in roster(srv, view="onsite")["presence"])
        assert onsite == (p["status"] == "ARRIVED")
        # 已离场/移除状态下，门点自动离场一定被拒（不能复活）
        if p["status"] in ("DEPARTED", "REMOVED"):
            assert depart(srv, code, "gp2").status_code == 409
