import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor


def iso(dt):
    return dt.isoformat()


def make_delegation(
    srv,
    *,
    original="holder-1",
    proxy="agent-1",
    max_uses=1,
    high_risk=False,
    approvers=None,
    valid_from=None,
    valid_until=None,
    zones=("Z1",),
    purpose="代取设备",
):
    now = datetime.now(timezone.utc)
    payload = {
        "original_person_id": original,
        "proxy_person_id": proxy,
        "valid_from": iso(valid_from or now - timedelta(minutes=5)),
        "valid_until": iso(valid_until or now + timedelta(hours=2)),
        "zones": list(zones),
        "max_uses": max_uses,
        "purpose": purpose,
        "high_risk": high_risk,
    }
    if approvers is not None:
        payload["approvers"] = approvers
    with srv.client() as c:
        r = c.post("/api/admin/delegations", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def approve(srv, delegation_id, approver):
    with srv.client() as c:
        r = c.post(
            f"/api/admin/delegations/{delegation_id}/approvals",
            json={"approver_id": approver, "reason": "同意"},
        )
    return r


def proxy_verify(srv, gate, code, attempt, *, original="holder-1", proxy="agent-1",
                 token=None, **kwargs):
    payload = {
        "gate_id": gate,
        "code": code,
        "attempt_id": attempt,
        "original_person_id": original,
        "proxy_person_id": proxy,
    }
    payload.update(kwargs)
    with srv.client(token or srv.gate_token) as c:
        return c.post("/api/gate/proxy/verify", json=payload)


def test_high_risk_requires_two_distinct_named_admins_and_then_issues_credential(srv):
    d = make_delegation(srv, high_risk=True, approvers=["alice", "bob"])

    r = approve(srv, d["id"], "carol")
    assert r.status_code == 403

    r1 = approve(srv, d["id"], "alice")
    assert r1.status_code == 202
    assert r1.json()["pending_second_approval"] is True

    # 同一管理员重复点同意不能当作第二人。
    r_same = approve(srv, d["id"], "alice")
    assert r_same.status_code == 200
    assert r_same.json()["duplicated"] is True

    r2 = approve(srv, d["id"], "bob")
    assert r2.status_code == 201
    body = r2.json()
    assert body["status"] == "APPROVED"
    assert body["credential"]["code"].startswith("P-")
    assert body["credential"]["used_count"] == 0
    assert body["credential"]["remaining_uses"] == 1

    with srv.client() as c:
        late_reject = c.post(
            f"/api/admin/delegations/{d['id']}/rejections",
            json={"approver_id": "bob", "reason": "迟来的拒绝"},
        )
    assert late_reject.status_code == 409


def test_verification_requires_original_agent_time_and_zone(srv):
    d = make_delegation(srv, zones=["Z2"])
    assert approve(srv, d["id"], "alice").status_code in (200, 201)
    with srv.client() as c:
        detail = c.get(f"/api/admin/delegations/{d['id']}").json()
    code = detail["credential"]["code"]

    with srv.client(srv.gate_token) as c:
        missing = c.post("/api/gate/proxy/verify", json={
            "gate_id": "g1",
            "code": code,
            "attempt_id": "missing-original",
            "proxy_person_id": "agent-1",
        })
    assert missing.status_code == 422
    assert missing.json()["detail"]

    # g1 在 Z1，而委托只允许 Z2；动态注册一个 Z2 门点验证成功，再用 g1 验证分区拒绝。
    with srv.client() as c:
        assert c.post("/api/admin/gates",
                      json={"id": "gproxy-z2", "name": "代理Z2门"}).status_code in (200, 409)
        assert c.put("/api/admin/gates/gproxy-z2/zone",
                     json={"zone_id": "Z2"}).status_code == 200
    ok = proxy_verify(srv, "gproxy-z2", code, "valid-once")
    assert ok.status_code == 200, ok.text
    assert ok.json()["used_count"] == 1

    cases = [
        ({"original": "wrong-holder"}, "original_person_mismatch"),
        ({"proxy": "wrong-agent"}, "proxy_person_mismatch"),
    ]
    for override, reason in cases:
        r = proxy_verify(
            srv, "gproxy-z2", code, f"deny-{reason}",
            original=override.get("original", "holder-1"),
            proxy=override.get("proxy", "agent-1"),
        )
        assert r.status_code == 403
        assert r.json()["reason"] == reason

    # g1 在 Z1，而委托只允许 Z2。
    r_zone = proxy_verify(srv, "g1", code, "wrong-zone")
    assert r_zone.status_code == 403
    assert r_zone.json()["reason"] == "zone_mismatch"

    # 一次性凭证已用，后续明确拒绝。
    r_used = proxy_verify(srv, "gproxy-z2", code, "second-scan")
    assert r_used.status_code == 410
    assert r_used.json()["reason"] == "exhausted"


def test_network_retry_returns_first_result_and_concurrent_use_never_overdraws(srv):
    d = make_delegation(srv, max_uses=1)
    assert approve(srv, d["id"], "alice").status_code in (200, 201)
    with srv.client() as c:
        code = c.get(f"/api/admin/delegations/{d['id']}").json()["credential"]["code"]

    first = proxy_verify(srv, "g1", code, "same-attempt")
    assert first.status_code == 200
    replay = proxy_verify(srv, "g1", code, "same-attempt")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["verification_id"] == first.json()["verification_id"]

    d2 = make_delegation(srv, original="holder-concurrent", proxy="agent-c",
                         max_uses=2)
    assert approve(srv, d2["id"], "alice").status_code in (200, 201)
    with srv.client() as c:
        code2 = c.get(f"/api/admin/delegations/{d2['id']}").json()["credential"]["code"]

    def attempt(i):
        return proxy_verify(srv, f"g{1 + (i % 2)}", code2, f"parallel-{i}",
                            original="holder-concurrent", proxy="agent-c")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    statuses = sorted(r.status_code for r in results)
    assert statuses.count(200) == 2
    assert statuses.count(410) == 6
    with srv.client() as c:
        final = c.get(f"/api/admin/delegations/{d2['id']}").json()
    assert final["credential"]["status"] == "EXHAUSTED"
    assert final["used_count"] == 2
    assert final["remaining_uses"] == 0


def test_time_window_rejects_before_and_after_without_consuming(srv):
    now = datetime.now(timezone.utc)
    future = make_delegation(
        srv,
        original="future-holder",
        proxy="future-agent",
        valid_from=now + timedelta(hours=1),
        valid_until=now + timedelta(hours=2),
    )
    assert approve(srv, future["id"], "alice").status_code in (200, 201)
    with srv.client() as c:
        code = c.get(f"/api/admin/delegations/{future['id']}").json()["credential"]["code"]
    r = proxy_verify(srv, "g1", code, "too-early",
                     original="future-holder", proxy="future-agent")
    assert r.status_code == 403
    assert r.json()["reason"] == "not_yet_valid"

    past = make_delegation(
        srv,
        original="past-holder",
        proxy="past-agent",
        valid_from=now - timedelta(hours=2),
        valid_until=now - timedelta(hours=1),
    )
    # 后台清扫（1 秒）会先把未决委托终结；之后审批不能补签凭证。
    time.sleep(1.2)
    r_approval = approve(srv, past["id"], "alice")
    assert r_approval.status_code == 410
    with srv.client() as c:
        expired = c.get(f"/api/admin/delegations/{past['id']}").json()
    assert expired["status"] == "EXPIRED"
    assert expired["credential"] is None


def test_revocation_enters_version_stream_and_old_offline_event_requires_manual_resolution(srv):
    now = datetime.now(timezone.utc)
    d = make_delegation(srv, original="offline-holder", proxy="offline-agent",
                        max_uses=3)
    assert approve(srv, d["id"], "alice").status_code in (200, 201)
    with srv.client() as c:
        code = c.get(f"/api/admin/delegations/{d['id']}").json()["credential"]["code"]

    event_ts = iso(now - timedelta(seconds=10))
    with srv.client() as c:
        revoke = c.post(f"/api/admin/delegations/{d['id']}/revoke",
                        json={"reason": "紧急撤销"})
    assert revoke.status_code == 200
    revoked_version = revoke.json()["credential"]["revoked_version"]
    assert revoked_version > 0

    with srv.client(srv.gate_token) as c:
        replay = c.post("/api/gate/proxy/replay", json={
            "gate_id": "g2",
            "base_version": 10**9,
            "events": [{
                "code": code,
                "attempt_id": "offline-before-revoke",
                "original_person_id": "offline-holder",
                "proxy_person_id": "offline-agent",
                "event_ts": event_ts,
            }],
        })
    assert replay.status_code == 200
    item = replay.json()["results"][0]
    assert item["http_status"] == 409
    assert item["conflict_kind"] == "REVOKED_HISTORICAL"
    conflict_id = item["conflict_id"]

    # 撤销后的迟到事件只能被明确拒绝，不能恢复凭证。
    after_revoke = iso(now + timedelta(minutes=1))
    with srv.client(srv.gate_token) as c:
        replay2 = c.post("/api/gate/proxy/replay", json={
            "gate_id": "g2",
            "base_version": 10**9,
            "events": [{
                "code": code,
                "attempt_id": "offline-after-revoke",
                "original_person_id": "offline-holder",
                "proxy_person_id": "offline-agent",
                "event_ts": after_revoke,
            }],
        })
    assert replay2.json()["results"][0]["http_status"] == 410
    assert replay2.json()["results"][0]["reason"] == "revoked"

    with srv.client() as c:
        applied = c.post(f"/api/admin/proxy-conflicts/{conflict_id}/resolve",
                         json={"action": "APPLIED", "reason": "核实现场真实发生"})
    assert applied.status_code == 200, applied.text
    with srv.client() as c:
        final = c.get(f"/api/admin/delegations/{d['id']}").json()
    assert final["credential"]["status"] == "REVOKED"
    assert final["used_count"] == 1
    assert final["credential"]["remaining_uses"] == 0


def test_admin_queries_current_usage_denials_approvals_revocations_and_conflicts(srv):
    d = make_delegation(srv, original="query-holder", proxy="query-agent")
    assert approve(srv, d["id"], "alice").status_code in (200, 201)
    with srv.client() as c:
        code = c.get(f"/api/admin/delegations/{d['id']}").json()["credential"]["code"]
    assert proxy_verify(srv, "g1", code, "q-ok",
                        original="query-holder", proxy="query-agent").status_code == 200
    bad = proxy_verify(srv, "g1", code, "q-bad-agent",
                       original="query-holder", proxy="someone-else")
    assert bad.status_code == 403

    with srv.client() as c:
        by_holder = c.get("/api/admin/delegations",
                          params={"original_person_id": "query-holder",
                                  "zone_id": "Z1", "status": "APPROVED"})
        by_agent = c.get("/api/admin/delegations",
                         params={"proxy_person_id": "query-agent"})
        verifs = c.get("/api/admin/proxy-verifications",
                       params={"credential_code": code})
        denials = c.get("/api/admin/proxy-verifications",
                        params={"decision": "DENIED", "reason": "proxy_person_mismatch"})
        revocations = c.get("/api/admin/proxy-revocations",
                            params={"credential_code": code})
        person = c.get("/api/admin/people/query-agent/proxy")
    assert by_holder.status_code == 200
    assert len(by_holder.json()["delegations"]) == 1
    assert len(by_agent.json()["delegations"]) == 1
    records = verifs.json()["verifications"]
    assert {v["decision"] for v in records} == {"ALLOWED", "DENIED"}
    assert len(denials.json()["verifications"]) == 1
    detail = records[0]
    assert "event_version" in detail
    assert revocations.status_code == 200
    proxy_view = person.json()
    assert len(proxy_view["as_proxy"]) == 1

    with srv.client() as c:
        detail = c.get(f"/api/admin/delegations/{d['id']}").json()
    assert len(detail["approvals"]) == 1
    assert len(detail["verifications"]) == 2
    assert detail["events"]


def test_sync_returns_proxy_events_in_global_version_order(srv):
    d = make_delegation(srv, original="sync-holder", proxy="sync-agent")
    assert approve(srv, d["id"], "alice").status_code in (200, 201)
    with srv.client() as c:
        code = c.get(f"/api/admin/delegations/{d['id']}").json()["credential"]["code"]
    assert proxy_verify(srv, "g1", code, "sync-use",
                        original="sync-holder", proxy="sync-agent").status_code == 200
    with srv.client(srv.gate_token) as c:
        sync = c.post("/api/gate/sync", json={"gate_id": "g1", "since_version": 0})
    types = [e["type"] for e in sync.json()["events"]]
    assert "DELEGATION_CREATED" in types
    assert "PROXY_CREDENTIAL_ISSUED" in types
    assert "PROXY_VERIFICATION_ALLOWED" in types
    versions = [e["version"] for e in sync.json()["events"]]
    assert versions == sorted(versions)
