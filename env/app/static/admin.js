// 管理台逻辑
let token = localStorage.getItem(TOKEN_KEY) || "";
document.getElementById("token").value = token;

const $ = id => document.getElementById(id);
const adminApi = (m, p, b) => api(m, p, b, token);

function requireToken() {
  if (!token) { toast("请先填写管理员令牌", "error"); throw new Error("no token"); }
}

$("saveToken").onclick = () => {
  token = $("token").value.trim();
  localStorage.setItem(TOKEN_KEY, token);
  toast("令牌已保存在本浏览器", "success");
  refreshAll();
};

// ---------------- Tabs ----------------
const TAB_IDS = ["routes", "presence", "rollcall", "batch", "person", "ticket",
                 "zone", "events", "attempts"];
document.querySelectorAll(".tabs button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    TAB_IDS.forEach(t => $(`tab-${t}`).hidden = t !== btn.dataset.tab);
    if (btn.dataset.tab === "zone") loadZoneView();
    if (btn.dataset.tab === "batch") loadBatchView();
    if (btn.dataset.tab === "presence") loadPresence();
    if (btn.dataset.tab === "rollcall") loadRollcallList();
    if (btn.dataset.tab === "routes") { loadRouteMonitor(); loadRouteConflicts(); }
  };
});

const PRES_BADGE = {
  ARRIVED: "ACTIVE", DEPARTED: "EXPIRED", REMOVED: "REVOKED",
};
const PRES_TEXT = { ARRIVED: "在场", DEPARTED: "已离场", REMOVED: "误扫移除" };
const KIND_TEXT = {
  ARRIVED: "门点到场", DEPARTED: "门点确认离场",
  MARK_ARRIVED: "人工补登记到场", MARK_DEPARTED: "人工登记离场",
  REMOVE: "人工移除（误扫）", RESTORE: "人工纠正回场",
};

async function loadPresence() {
  try {
    requireToken();
    const params = new URLSearchParams({ view: $("pr-view").value, limit: "500" });
    if ($("pr-zone").value) params.set("zone_id", $("pr-zone").value);
    if ($("pr-batch").value) params.set("batch_id", $("pr-batch").value);
    if ($("pr-q").value.trim()) params.set("q", $("pr-q").value.trim());
    const v = await adminApi("GET", "/api/admin/presence?" + params.toString());
    const s = v.summary;
    $("pr-summary").innerHTML = [
      ["在场组数", s.onsite_groups, "#4ade80"],
      ["在场人数（含同行）", s.onsite_people, "#93c5fd"],
      ["其中同行人", s.onsite_companions, "#c4b5fd"],
      ["已离场组", s.departed_groups, "#fcd34d"],
    ].map(([k, val, c]) => `<div class="stat"><b style="color:${c}">${val}</b>${k}</div>`).join("");
    const rows = v.presence.map(p => `<tr>
      <td class="mono code-cell">${esc(p.ticket_code)}</td>
      <td>${esc(p.applicant_name || p.person_id)}<div class="muted" style="font-size:11px">${esc((p.companion_names || []).filter(Boolean).map(n => esc(n)).join("、"))}</div></td>
      <td><b>${p.party_size}</b>（同行 ${p.companions}）</td>
      <td><span class="badge ${PRES_BADGE[p.status]}">${PRES_TEXT[p.status] || p.status}</span></td>
      <td class="mono">${esc(p.zone_id || "")}<div class="muted" style="font-size:11px">${esc(p.batch_id || "")}</div></td>
      <td>${p.arrived_gate ? esc(p.arrived_gate) : "人工"}<div class="muted" style="font-size:11px">${fmtTime(p.arrived_at)}</div></td>
      <td>${p.departed_gate ? esc(p.departed_gate) + "<div class='muted' style='font-size:11px'>" + fmtTime(p.departed_at) + "</div>" : (p.status === "REMOVED" ? "—" : '<span class="muted">未确认</span>')}</td>
      <td><button class="btn small" onclick="loadPresenceDetail('${esc(p.ticket_code)}')">轨迹/更正</button></td>
    </tr>`).join("");
    $("pr-list").innerHTML = `<table><thead><tr>
      <th>票号</th><th>申请人</th><th>整组</th><th>状态</th><th>分区/批次</th>
      <th>到场</th><th>离场</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan=8 class=muted>无记录</td></tr>'}</tbody></table>`;
  } catch (e) { toast(e.message, "error"); }
}

window.loadPresenceDetail = async code => {
  try {
    const d = await adminApi("GET", `/api/admin/presence/${encodeURIComponent(code)}`);
    const trail = (d.trail || []).map(t => `<tr>
      <td>${t.seq}</td><td><span class="badge ${t.to_status === "ARRIVED" ? "ACTIVE" : (t.to_status === "DEPARTED" ? "EXPIRED" : "REVOKED")}">${esc(KIND_TEXT[t.kind] || t.kind)}</span></td>
      <td>${esc(t.from_status || "—")} → <b>${esc(t.to_status)}</b></td>
      <td>${t.gate_id ? esc(t.gate_id) : "—"}</td>
      <td>${esc(t.operator || "")}</td>
      <td>${esc(t.reason || "")}</td>
      <td>${fmtTime(t.ts)}<div class="muted" style="font-size:11px">#${t.version ?? ""}</div></td>
    </tr>`).join("");
    const p = d.presence;
    $("pr-detail").innerHTML =
      `<h2>${esc(code)} ${p ? `<span class="badge ${PRES_BADGE[p.status]}">${PRES_TEXT[p.status] || p.status}</span>` : '<span class="muted">无在场记录</span>'}</h2>
       ${d.application ? `<p class="hint">申请 ${esc(d.application.id)} · ${esc(d.application.name)} · 资料版本 v${d.application.change_version}</p>` : ""}
       <div class="row" style="margin-top:8px">
         <input id="pc-reason" placeholder="更正原因（必填）" style="flex:1">
         <select id="pc-gate"><option value="">门点（可选）</option></select>
       </div>
       <div class="row" style="margin-top:6px">
         <button class="btn primary" onclick="presenceCorrect('${esc(code)}','MARK_ARRIVED')">漏扫：补登记到场</button>
         <button class="btn" onclick="presenceCorrect('${esc(code)}','MARK_DEPARTED')">漏扫：登记离场</button>
         <button class="btn danger" onclick="presenceCorrect('${esc(code)}','REMOVE')">误扫：从在场移除</button>
         <button class="btn primary" onclick="presenceCorrect('${esc(code)}','RESTORE')">纠正：重新计入在场</button>
       </div>
       <p class="hint">人工更正只追加轨迹（原始到场/离场不删除），记录操作者与原因；已离场的人不会被门点自动流程重新放回名单，RESTORE 必须管理员显式发起。</p>
       <table><thead><tr><th>#</th><th>动作</th><th>状态变化</th><th>门点</th><th>操作者</th><th>原因</th><th>时间/版本</th></tr></thead>
       <tbody>${trail || '<tr><td colspan=7 class=muted>无轨迹</td></tr>'}</tbody></table>`;
    const gates = await adminApi("GET", "/api/admin/gates");
    $("pc-gate").innerHTML = `<option value="">门点（可选）</option>` +
      gates.gates.map(g => `<option value="${esc(g.id)}">${esc(g.id)} · ${esc(g.name)}</option>`).join("");
  } catch (e) { toast(e.message, "error"); }
};

window.presenceCorrect = async (code, action) => {
  const reason = $("pc-reason").value.trim();
  if (!reason) return toast("人工更正必须填写原因", "error");
  const gate_id = $("pc-gate").value || null;
  if (!confirm(`${KIND_TEXT[action]}：${code}？\n原因：${reason}`)) return;
  try {
    await adminApi("POST", "/api/admin/presence/corrections",
      { code, action, reason, gate_id });
    toast("更正已记录（原轨迹保留）", "success");
    await Promise.all([loadPresence(), loadPresenceDetail(code), loadStats()]);
  } catch (e) { toast("更正失败：" + e.message, "error"); }
};

$("btn-pr").onclick = () => { try { requireToken(); loadPresence(); } catch (_) {} };

// ---------------- 应急清点 ----------------
$("btn-rollcall").onclick = async () => {
  try {
    requireToken();
    const reason = $("rc-reason").value.trim();
    if (!reason) return toast("请填写清点原因", "error");
    const body = { reason };
    if ($("rc-zone").value) body.zone_id = $("rc-zone").value;
    if ($("rc-batch").value) body.batch_id = $("rc-batch").value;
    if (!confirm("发起应急清点？快照一旦生成不可修改，之后的到场/离场不影响本快照。")) return;
    const r = await adminApi("POST", "/api/admin/rollcalls", body);
    toast(`清点快照 ${r.rollcall.id} 已生成：${r.rollcall.groups} 组 / ${r.rollcall.headcount} 人`, "success");
    await loadRollcallList(r.rollcall.id);
  } catch (e) { toast("清点失败：" + e.message, "error"); }
};

async function loadRollcallList(selectId) {
  try {
    requireToken();
    const { rollcalls } = await adminApi("GET", "/api/admin/rollcalls?limit=100");
    $("rc-list").innerHTML = `<option value="">历史快照（${rollcalls.length}）…</option>` +
      rollcalls.map(r => `<option value="${esc(r.id)}">${esc(r.id)} · ${fmtTime(r.created_at)} · ${r.groups}组/${r.headcount}人${r.reason ? " · " + esc(r.reason) : ""}</option>`).join("");
    if (selectId) { $("rc-list").value = selectId; await loadRollcallDetail(selectId); }
  } catch (e) { /* 静默 */ }
}

window.loadRollcallDetail = async id => {
  if (!id) return;
  try {
    const r = await adminApi("GET", `/api/admin/rollcalls/${encodeURIComponent(id)}`);
    const scope = r.scope || {};
    const rows = r.entries.map(e => `<tr>
      <td class="mono code-cell">${esc(e.ticket_code)}</td>
      <td>${esc(e.applicant_name || e.person_id)}<div class="muted" style="font-size:11px">${esc((e.companion_names || []).filter(Boolean).join("、"))}</div></td>
      <td><b>${e.party_size}</b>（同行 ${e.companions}）</td>
      <td class="mono">${esc(e.zone_id || "")}</td>
      <td class="mono">${esc(e.batch_id || "")}</td>
      <td>${e.arrived_gate ? esc(e.arrived_gate) : "人工"}<div class="muted" style="font-size:11px">${fmtTime(e.arrived_at)}</div></td>
      <td>${esc(e.last_gate || "—")}<div class="muted" style="font-size:11px">${esc(KIND_TEXT[e.last_event_kind] || e.last_event_kind)} · ${fmtTime(e.last_event_ts)}</div></td>
    </tr>`).join("");
    $("rc-detail").innerHTML =
      `<h2>${esc(r.id)} <span class="badge ACTIVE">${r.groups} 组 / ${r.headcount} 人</span></h2>
       <table><tbody>
        <tr><th>发起时间</th><td>${fmtTime(r.created_at)} · 版本 #${r.version}</td></tr>
        <tr><th>发起人</th><td>${esc(r.created_by)}</td></tr>
        <tr><th>原因</th><td>${esc(r.reason || "")}</td></tr>
        <tr><th>范围</th><td>分区 ${esc(scope.zone_id || "全部")} · 批次 ${esc(scope.batch_id || "全部")}</td></tr>
       </tbody></table>
       <p class="hint">以下为发起瞬间冻结的在场名单（含同行人、批次分区、最后门点记录）；后续变化只写新快照，本内容永不改变。</p>
       <table><thead><tr><th>票号</th><th>申请人/同行名单</th><th>整组</th><th>分区</th><th>批次</th><th>到场门点</th><th>最后门点记录</th></tr></thead>
       <tbody>${rows || '<tr><td colspan=7 class=muted>快照内无在场人员</td></tr>'}</tbody></table>`;
  } catch (e) { toast(e.message, "error"); }
};

$("btn-rc-view").onclick = () => loadRollcallDetail($("rc-list").value);

// ---------------- 发票 ----------------
$("btn-issue").onclick = async () => {
  try {
    requireToken();
    const person = $("f-person").value.trim();
    if (!person) return toast("请填写人员标识", "error");
    const minutes = parseInt($("f-min").value, 10);
    if (!minutes || minutes < 1) return toast("有效期（分钟）无效", "error");
    const zones = [...document.querySelectorAll("#f-zones input[type=checkbox]:checked")]
      .map(cb => cb.value);
    const payload = { person_id: person, ttl_seconds: minutes * 60, zones };
    if ($("f-route").value) payload.route_id = $("f-route").value;
    if ($("f-from").value) {
      delete payload.ttl_seconds;
      payload.valid_from = new Date($("f-from").value).toISOString();
      payload.valid_until = new Date(new Date($("f-from").value).getTime() + minutes * 60000).toISOString();
    }
    if ($("f-note").value.trim()) payload.note = $("f-note").value.trim();
    const t = await adminApi("POST", "/api/admin/tickets", payload);
    $("issue-result").innerHTML =
      `<div class="result-box ok">已签发 <span class="big-code mono">${esc(t.code)}</span>
       <div class="detail">有效期至 ${fmtTime(t.valid_until)} · 版本 #${t.version}<br>
       允许分区：${t.zones && t.zones.length ? t.zones.map(esc).join("、") : "（无 · 所有门点拒绝）"}<br>
	       ${t.route_id ? `检查路线：<b>${esc(t.route_id)}</b> v${t.route_version}（首次核销须在入口检查点）` : ""}</div></div>`;
    toast("发票成功", "success");
    refreshAll();
  } catch (e) { toast("发票失败：" + e.message, "error"); }
};

// ---------------- 作废 ----------------
$("btn-revoke").onclick = async () => {
  try {
    requireToken();
    const code = $("r-code").value.trim();
    if (!code) return toast("请填写票面编号", "error");
    if (!confirm(`确认作废 ${code}？作废后不可恢复，同一人的其他票不受影响。`)) return;
    await adminApi("POST", "/api/admin/tickets/revoke",
      { code, reason: $("r-reason").value.trim() || "admin_revoke" });
    toast("已作废：" + code, "success");
    refreshAll();
  } catch (e) { toast("作废失败：" + e.message, "error"); }
};

// ---------------- 门点 ----------------
$("btn-gate").onclick = async () => {
  try {
    requireToken();
    const id = $("g-id").value.trim(), name = $("g-name").value.trim();
    if (!id || !name) return toast("门点 ID 与名称必填", "error");
    await adminApi("POST", "/api/admin/gates", { id, name });
    toast("门点已注册：" + id, "success");
    $("g-id").value = $("g-name").value = "";
    loadGates();
  } catch (e) { toast("注册失败：" + e.message, "error"); }
};

async function loadGates() {
  try {
    requireToken();
    const { gates } = await adminApi("GET", "/api/admin/gates");
    const zoneOpts = z => `<option value="">（未配置 · 默认拒绝）</option>` +
      zoneCache.map(zn => `<option value="${esc(zn.id)}" ${z === zn.id ? "selected" : ""}>${esc(zn.id)} · ${esc(zn.name)}</option>`).join("");
    $("gates").innerHTML = `<table><thead><tr><th>ID</th><th>名称</th><th>分区</th><th>状态</th><th>同步水位</th><th></th></tr></thead><tbody>` +
      gates.map(g => `<tr>
        <td class="mono">${esc(g.id)}</td><td>${esc(g.name)}</td>
        <td><select onchange="setGateZone('${esc(g.id)}', this.value)">${zoneOpts(g.zone_id || "")}</select>
            ${g.zone_id && !g.zone_name ? '<div class="muted" style="font-size:11px">⚠ 分区已失效，默认拒绝</div>' : ""}</td>
        <td>${g.revoked ? '<span class="badge REVOKED">已停用</span>' : '<span class="badge ACTIVE">启用中</span>'}</td>
        <td class="mono">#${g.last_version ?? 0}</td>
        <td>${g.revoked ? "" : `<button class="btn small danger" onclick="revokeGate('${esc(g.id)}')">停用</button>`}</td>
      </tr>`).join("") + "</tbody></table>" +
      `<p class="hint">所有门点共用令牌 PASSPORT_GATE_TOKEN，门点 ID 必须先在此注册；未配置分区的门点核销默认拒绝。</p>`;
    const sel = $("a-gate");
    sel.innerHTML = `<option value="">全部门点</option>` +
      gates.map(g => `<option value="${esc(g.id)}">${esc(g.id)} · ${esc(g.name)}</option>`).join("");
  } catch (e) { /* 未填令牌时静默 */ }
}
window.setGateZone = async (gid, zid) => {
  try {
    await adminApi("PUT", `/api/admin/gates/${encodeURIComponent(gid)}/zone`,
      { zone_id: zid || null });
    toast(`门点 ${gid} 分区已更新`, "success");
    loadGates();
  } catch (e) { toast(e.message, "error"); loadGates(); }
};
window.revokeGate = async id => {
  if (!confirm(`停将门点 ${id}？停用后该门点核销与同步都会被拒绝。`)) return;
  try { await adminApi("DELETE", `/api/admin/gates/${id}`); toast("门点已停用", "success"); loadGates(); }
  catch (e) { toast(e.message, "error"); }
};

// ---------------- 分区与封锁规则 ----------------
let zoneCache = [];

$("btn-zone").onclick = async () => {
  try {
    requireToken();
    const id = $("z-id").value.trim(), name = $("z-name").value.trim();
    if (!id || !name) return toast("分区 ID 与名称必填", "error");
    await adminApi("POST", "/api/admin/zones", { id, name });
    toast("分区已创建：" + id, "success");
    $("z-id").value = $("z-name").value = "";
    await loadZones();
    loadGates();
  } catch (e) { toast("创建失败：" + e.message, "error"); }
};

async function loadZones() {
  try {
    requireToken();
    const { zones, policy_version } = await adminApi("GET", "/api/admin/zones");
    zoneCache = zones;
    $("zones").innerHTML = zones.length ? `<table><thead>
      <tr><th>ID</th><th>名称</th><th>状态</th><th>门点</th><th>受影响票</th><th></th></tr></thead><tbody>` +
      zones.map(z => `<tr>
        <td class="mono">${esc(z.id)}</td><td>${esc(z.name)}</td>
        <td>${z.locked ? '<span class="badge LOCKED">封锁中</span>' : '<span class="badge OPEN">正常</span>'}
            ${z.current_rule ? `<div class="muted" style="font-size:11px">#${z.current_rule.version} ${esc(z.current_rule.reason || "")}</div>` : ""}</td>
        <td>${z.gates}</td><td>${z.affected_tickets}</td>
        <td><button class="btn small danger" onclick="deleteZone('${esc(z.id)}')">注销</button></td>
      </tr>`).join("") + "</tbody></table>" +
      `<p class="hint">当前策略版本 #${policy_version} · 注销分区后，引用它的门点按“未知分区”默认拒绝。</p>`
      : `<p class="muted">尚无分区。未配置分区的门点核销默认拒绝。</p>`;
    // 发票分区勾选
    $("f-zones").innerHTML = zones.length
      ? zones.map(z => `<label><input type="checkbox" value="${esc(z.id)}" checked>${esc(z.id)}</label>`).join("")
      : `<span class="muted">尚无分区，请先创建</span>`;
    // 批次分区选择
    const bz = $("b-zone");
    if (bz) bz.innerHTML = zones.map(z =>
      `<option value="${esc(z.id)}">${esc(z.id)} · ${esc(z.name)}</option>`).join("")
      || `<option value="">（请先创建分区）</option>`;
    // 规则目标分区
    $("pr-zone").innerHTML = `<option value="">全局（所有分区）</option>` +
      zones.map(z => `<option value="${esc(z.id)}">${esc(z.id)} · ${esc(z.name)}</option>`).join("");
    // 分区视图选择
    $("zv-zone").innerHTML = zones.map(z =>
      `<option value="${esc(z.id)}">${esc(z.id)} · ${esc(z.name)}</option>`).join("");
    // 在场清册 / 应急清点的分区过滤
    const zoneOpts = `<option value="">全部分区</option>` +
      zones.map(z => `<option value="${esc(z.id)}">${esc(z.id)} · ${esc(z.name)}</option>`).join("");
    if ($("pr-zone")) $("pr-zone").innerHTML = zoneOpts;
    if ($("rc-zone")) $("rc-zone").innerHTML = zoneOpts;
  } catch (e) { /* 未填令牌时静默 */ }
}
window.deleteZone = async id => {
  if (!confirm(`注销分区 ${id}？引用它的门点将按“未知分区”默认拒绝。`)) return;
  try {
    await adminApi("DELETE", `/api/admin/zones/${encodeURIComponent(id)}`);
    toast("分区已注销", "success");
    await loadZones(); loadGates();
  } catch (e) { toast(e.message, "error"); }
};

$("btn-publish").onclick = async () => {
  try {
    requireToken();
    const action = $("pr-action").value;
    const zone_id = $("pr-zone").value || null;
    const rule_id = $("pr-rule").value.trim() || null;
    const reason = $("pr-reason").value.trim() || null;
    const target = zone_id || "全局";
    if (!confirm(`确认发布${action === "LOCK" ? "紧急封锁" : "解除封锁"}规则？\n目标：${target}\n发布即产生新版本，门点按版本补齐后生效。`)) return;
    const r = await adminApi("POST", "/api/admin/policy/rules",
      { action, zone_id, rule_id, reason });
    toast(r.duplicated
      ? `规则 ${r.rule.rule_id} 已存在（版本 #${r.rule.version}），未重复生效`
      : `规则已发布：${r.rule.rule_id} · 版本 #${r.rule.version}`, "success");
    $("pr-rule").value = "";
    await loadRules(); await loadZones();
  } catch (e) { toast("发布失败：" + e.message, "error"); }
};

async function loadRules() {
  try {
    requireToken();
    const { rules, policy_version } = await adminApi("GET", "/api/admin/policy/rules");
    $("rules").innerHTML = rules.length ? `<table><thead>
      <tr><th>版本</th><th>动作</th><th>目标</th><th>原因</th><th>规则 ID</th><th>时间</th></tr></thead><tbody>` +
      rules.map(r => `<tr>
        <td class="mono">#${r.version}</td>
        <td>${r.action === "LOCK" ? '<span class="badge LOCKED">封锁</span>' : '<span class="badge OPEN">解锁</span>'}</td>
        <td class="mono">${esc(r.zone_id || "全局")}</td>
        <td>${esc(r.reason || "")}</td>
        <td class="mono muted">${esc(r.rule_id)}</td>
        <td>${fmtTime(r.created_at)}</td>
      </tr>`).join("") + "</tbody></table>"
      : `<p class="muted">尚无规则。当前策略版本 #${policy_version}。</p>`;
  } catch (e) { /* 未填令牌时静默 */ }
}

// ---------------- 按分区查看 ----------------
$("btn-zoneview").onclick = () => loadZoneView();
async function loadZoneView() {
  try {
    requireToken();
    const zid = $("zv-zone").value;
    if (!zid) { $("zv-detail").innerHTML = `<p class="muted">请先创建分区。</p>`; return; }
    const v = await adminApi("GET", `/api/admin/zones/${encodeURIComponent(zid)}`);
    const cur = v.current_rule;
    const ruleRows = v.rules.map(r => `<tr>
      <td class="mono">#${r.version}</td>
      <td>${r.action === "LOCK" ? '<span class="badge LOCKED">封锁</span>' : '<span class="badge OPEN">解锁</span>'}</td>
      <td class="mono">${esc(r.zone_id || "全局")}</td>
      <td>${esc(r.reason || "")}</td><td>${fmtTime(r.created_at)}</td>
    </tr>`).join("");
    const ticketRows = v.tickets.map(t => `<tr>
      <td class="mono code-cell">${esc(t.code)}</td>
      <td class="mono">${esc(t.person_id)}</td>
      <td>${badge(t.effective_status)}</td>
      <td>${fmtTime(t.valid_from)}<br><span class="muted">至 ${fmtTime(t.valid_until)}</span></td>
      <td class="mono">${(t.zones || []).map(esc).join(", ")}</td>
    </tr>`).join("");
    const attemptRows = v.attempts.map(a => `<tr>
      <td>${fmtTime(a.at)}</td><td>${esc(a.gate_name || a.gate_id)}</td>
      <td class="mono">${esc(a.code)}</td>
      <td><span class="badge ${a.ok ? "ok" : "fail"}">${a.ok ? "放行" : "拒绝"}</span></td>
      <td>${esc(a.status)}</td><td>${a.http_status}</td>
    </tr>`).join("");
    $("zv-detail").innerHTML =
      `<h2>${esc(v.zone.id)} · ${esc(v.zone.name)} ·
        ${v.locked ? '<span class="badge LOCKED">封锁中</span>' : '<span class="badge OPEN">正常</span>'}
        <span class="muted" style="font-size:12px">策略版本 #${v.policy_version}</span></h2>
       <table><tbody>
        <tr><th>当前规则</th><td>${cur
          ? `#${cur.version} · ${cur.action === "LOCK" ? "封锁" : "解锁"} · ${esc(cur.zone_id || "全局")} · ${esc(cur.reason || "")} · ${fmtTime(cur.created_at)}`
          : "（尚无规则，默认正常通行）"}</td></tr>
        <tr><th>门点</th><td>${v.gates.map(g => `<span class="mono">${esc(g.id)}</span>${g.revoked ? "（已停用）" : ""}`).join("、") || "无"}</td></tr>
       </tbody></table>
       <div class="section"><h2>规则历史（含全局规则，按版本倒序）</h2>
       <table><thead><tr><th>版本</th><th>动作</th><th>目标</th><th>原因</th><th>时间</th></tr></thead>
       <tbody>${ruleRows || '<tr><td colspan=5 class=muted>无</td></tr>'}</tbody></table></div>
       <div class="section"><h2>受影响票据（允许本分区的票）</h2>
       <table><thead><tr><th>票号</th><th>持票人</th><th>状态</th><th>有效期</th><th>允许分区</th></tr></thead>
       <tbody>${ticketRows || '<tr><td colspan=5 class=muted>无</td></tr>'}</tbody></table></div>
       <div class="section"><h2>门点执行记录（本分区门点的扫码结果）</h2>
       <table><thead><tr><th>时间</th><th>门点</th><th>票号</th><th>结果</th><th>状态</th><th>HTTP</th></tr></thead>
       <tbody>${attemptRows || '<tr><td colspan=6 class=muted>无</td></tr>'}</tbody></table></div>`;
  } catch (e) { toast(e.message, "error"); }
}


// ---------------- 访客预约批次 ----------------
let batchCache = [];

function batchStateBadge(b) {
  if (b.closed) return '<span class="badge REVOKED">已关闭</span>';
  if (!b.accepting) return '<span class="badge EXPIRED">已结束</span>';
  return b.remaining > 0
    ? '<span class="badge ACTIVE">申请中</span>'
    : '<span class="badge EXPIRED">满额候补</span>';
}

$("btn-batch").onclick = async () => {
  try {
    requireToken();
    const zone_id = $("b-zone").value;
    const visit_date = $("b-date").value;
    const capacity = parseInt($("b-cap").value, 10);
    if (!zone_id) return toast("请选择所属分区", "error");
    if (!visit_date) return toast("请选择访问日期", "error");
    if (!$("b-start").value || !$("b-end").value) return toast("请选择时段", "error");
    if (!capacity || capacity < 1) return toast("人数上限无效", "error");
    const body = {
      name: $("b-name").value.trim() || null,
      visit_date,
      start_at: new Date($("b-start").value).toISOString(),
      end_at: new Date($("b-end").value).toISOString(),
      zone_id, capacity,
    };
    if ($("b-route").value) body.route_id = $("b-route").value;
    const b = await adminApi("POST", "/api/admin/batches", body);
    const link = `${location.origin}/apply?k=${encodeURIComponent(b.apply_token)}`;
    toast("批次已创建：" + b.id, "success");
    $("b-name").value = "";
    await loadBatches();
    selectBatch(b.id);
    $("bv-detail").insertAdjacentHTML("afterbegin",
      `<div class="result-box ok" style="margin-bottom:12px">一次性申请链接（发给访客）：<br>
        <span class="mono" style="word-break:break-all">${esc(link)}</span></div>`);
  } catch (e) { toast("创建批次失败：" + e.message, "error"); }
};

async function loadBatches() {
  try {
    requireToken();
    const { batches } = await adminApi("GET", "/api/admin/batches");
    batchCache = batches;
    $("batches").innerHTML = batches.length ? `<table><thead><tr>
      <th>批次</th><th>时段 / 分区</th><th>名额</th><th>状态</th><th></th></tr></thead><tbody>` +
      batches.map(b => {
        const c = b.counts || {};
        const cnt = s => (c[s] ? c[s].count : 0);
        return `<tr>
          <td class="mono">${esc(b.id)}${b.name ? "<div class='muted' style='font-size:11px'>" + esc(b.name) + "</div>" : ""}</td>
          <td>${esc(b.visit_date)}<br><span class="muted" style="font-size:11px">${fmtTime(b.start_at)}~${fmtTime(b.end_at)} · ${esc(b.zone_id)}</span></td>
          <td><b>${b.used}</b>/${b.capacity}<br><span class="muted" style="font-size:11px">候补 ${cnt("WAITLISTED")} 组</span></td>
          <td>${batchStateBadge(b)}</td>
          <td><button class="btn small" onclick="selectBatch('${esc(b.id)}')">详情</button></td>
        </tr>`;
      }).join("") + "</tbody></table>"
      : `<p class="muted">尚无批次。</p>`;
    $("bv-batch").innerHTML = batches.map(b =>
      `<option value="${esc(b.id)}">${esc(b.id)} · ${esc(b.visit_date)} · ${esc(b.zone_id)}</option>`).join("");
    const batchOpts = `<option value="">全部批次</option>` +
      batches.map(b => `<option value="${esc(b.id)}">${esc(b.id)} · ${esc(b.visit_date)}</option>`).join("");
    if ($("pr-batch")) $("pr-batch").innerHTML = batchOpts;
    if ($("rc-batch")) $("rc-batch").innerHTML = batchOpts;
  } catch (e) { /* 未填令牌时静默 */ }
}

window.selectBatch = async id => {
  $("bv-batch").value = id;
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x.dataset.tab === "batch"));
  TAB_IDS.forEach(t => $(`tab-${t}`).hidden = t !== "batch");
  await loadBatchView();
};
$("btn-batchview").onclick = () => loadBatchView();

const APP_BADGE = {
  PENDING: "online", WAITLISTED: "offline", APPROVED: "ACTIVE",
  CANCELLED: "REVOKED", REJECTED: "REVOKED", EXPIRED: "EXPIRED",
};
const APP_TEXT = {
  PENDING: "待审核", WAITLISTED: "候补中", APPROVED: "已通过",
  CANCELLED: "已取消", REJECTED: "已拒绝", EXPIRED: "已过期",
};

async function loadBatchView() {
  try {
    requireToken();
    const id = $("bv-batch").value;
    if (!id) { $("bv-detail").innerHTML = `<p class="muted">请先创建批次。</p>`; return; }
    const v = await adminApi("GET", `/api/admin/batches/${encodeURIComponent(id)}`);
    const b = v.batch;
    const link = `${location.origin}/apply?k=${encodeURIComponent(b.apply_token)}`;
    const capRows = v.capacity_log.map(l => `<tr>
      <td>${fmtTime(l.changed_at)}</td>
      <td>${l.old_capacity === null ? "（建批）" : l.old_capacity} → <b>${l.new_capacity}</b></td>
      <td>${esc(l.reason || "")}</td>
    </tr>`).join("");
    const appRows = v.applications.map(a => {
      const names = (a.companion_names || []).filter(Boolean);
      return `<tr>
      <td>${a.seq}${a.status === "WAITLISTED" ? `<div class="muted">#候补${a.waitlist_position}</div>` : ""}</td>
      <td>${esc(a.name)}<div class="muted" style="font-size:11px">${esc(a.contact)}</div>
          ${names.length ? `<div class="muted" style="font-size:11px">同行：${names.map(esc).join("、")}</div>` : ""}
          ${a.change_version ? `<div class="muted" style="font-size:11px">资料版本 v${a.change_version}</div>` : ""}</td>
      <td>${a.party_size}${a.source === "admin" ? ' <span class="badge UNKNOWN">补录</span>' : ""}</td>
      <td><span class="badge ${APP_BADGE[a.status] || "UNKNOWN"}">${APP_TEXT[a.status] || a.status}</span>
          ${a.promoted_at ? `<div class="muted" style="font-size:11px">${fmtTime(a.promoted_at)} 晋级</div>` : ""}</td>
      <td class="mono">${a.ticket_code
        ? `${esc(a.ticket_code)}`
        : "—"}</td>
      <td>${esc(a.decide_reason || "")}</td>
      <td>${actionButtons(a, b)}</td>
    </tr>`; }).join("");
    const ticketRows = v.tickets.map(t => `<tr>
      <td class="mono code-cell">${esc(t.code)}
          ${t.replaced_code ? `<div class="muted" style="font-size:11px">换自 ${esc(t.replaced_code)}</div>` : ""}
          ${t.replaced_by_code ? `<div class="muted" style="font-size:11px">→新票 ${esc(t.replaced_by_code)}</div>` : ""}</td>
      <td>${badge(t.status)}</td>
      <td>${t.party_size ?? ""}</td>
      <td>${fmtTime(t.valid_from)}<br><span class="muted">至 ${fmtTime(t.valid_until)}</span></td>
      <td>${t.redeemed_gate ? esc(t.redeemed_gate) + " @ " + fmtTime(t.redeemed_at) : "—"}</td>
      <td>${t.revoked_reason ? esc(t.revoked_reason) : "—"}</td>
    </tr>`).join("");
    const eventRows = v.events.map(e => `<tr>
      <td class="mono">#${e.version}</td><td>${fmtTime(e.ts)}</td>
      <td>${esc(e.type)}</td>
      <td class="mono">${esc(e.application_id || "")}</td>
      <td class="mono">${esc(e.ticket_code || "")}</td>
      <td>${esc(e.reason || "")}</td>
    </tr>`).join("");

    const changeRows = (v.changes || []).map(ch => {
      const C_BADGE = { PENDING: "offline", APPROVED: "ACTIVE", REJECTED: "REVOKED",
                        CANCELLED: "REVOKED", EXPIRED: "EXPIRED" };
      const C_TEXT = { PENDING: "待审核", APPROVED: "已通过", REJECTED: "已拒绝",
                       CANCELLED: "已撤回", EXPIRED: "已过期" };
      const newNames = (ch.new_companion_names || []).filter(Boolean);
      return `<tr>
        <td class="mono">${esc(ch.id)}<div class="muted" style="font-size:11px">${esc(ch.application_id)}</div></td>
        <td><span class="badge ${C_BADGE[ch.status]}">${C_TEXT[ch.status]}</span>
            <div class="muted" style="font-size:11px">${fmtTime(ch.created_at)}</div></td>
        <td>${esc(ch.old_name)}（${ch.old_party_size}人）<br>→ <b>${esc(ch.new_name)}</b>
            （${ch.new_party_size}人）
            ${newNames.length ? `<div class="muted" style="font-size:11px">同行：${newNames.map(esc).join("、")}</div>` : ""}</td>
        <td class="mono">${ch.old_ticket_code ? esc(ch.old_ticket_code) : "—"}</td>
        <td class="mono">${ch.new_ticket_code ? esc(ch.new_ticket_code) : "—"}</td>
        <td>${esc(ch.decide_reason || "")}</td>
        <td>${ch.status === "PENDING"
          ? `<button class="btn small primary" onclick="decideChange('${esc(ch.id)}','approve')">通过${ch.old_ticket_code === null && ch.new_ticket_code === null ? "" : ""}</button>
             <button class="btn small danger" onclick="decideChange('${esc(ch.id)}','reject')">拒绝</button>`
          : ""}</td>
      </tr>`;
    }).join("");
    const pendingCount = (v.pending_changes || []).length;

    $("bv-detail").innerHTML =
      `<h2>${esc(b.id)} ${b.name ? "· " + esc(b.name) : ""} ${batchStateBadge(b)}</h2>
       <table><tbody>
        <tr><th>访问时段</th><td>${esc(b.visit_date)} · ${fmtTime(b.start_at)} ～ ${fmtTime(b.end_at)}</td></tr>
        <tr><th>所属分区</th><td class="mono">${esc(b.zone_id)}（签发票的允许分区固定为本批次分区）</td></tr>
        <tr><th>名额</th><td><b>${b.used}</b> / ${b.capacity} 已占 · 剩余 ${b.remaining}
          · 候补 ${b.counts.WAITLISTED.count} 组</td></tr>
        <tr><th>申请链接</th><td><span class="mono" style="word-break:break-all">${esc(link)}</span></td></tr>
       </tbody></table>
       <div class="row" style="margin-top:10px">
         <input id="bv-cap" type="number" min="1" value="${b.capacity}" placeholder="新容量">
         <input id="bv-capreason" placeholder="调整原因（可选）">
         <button class="btn primary" onclick="changeCap('${esc(b.id)}')">调整容量</button>
         ${b.closed || !b.accepting ? "" : `<button class="btn danger" onclick="closeBatch('${esc(b.id)}')">提前关闭申请</button>`}
       </div>
       <div class="row" style="margin-top:10px">
         <input id="bf-name" placeholder="补录姓名">
         <input id="bf-contact" placeholder="补录联系方式">
         <input id="bf-comp" type="number" min="0" value="0" title="同行人数（不含本人）" oninput="syncBfNames()">
         <button class="btn primary" onclick="backfill('${esc(b.id)}')">补录并通过签票</button>
       </div>
       <div class="row" id="bf-names" style="margin-top:6px"></div>
       <div class="section"><h2>申请变更审核${pendingCount ? ` <span class="badge offline">${pendingCount} 条待审</span>` : ""}</h2>
       <table><thead><tr><th>变更</th><th>状态</th><th>内容（旧→新）</th><th>旧票</th><th>新票</th><th>原因</th><th>操作</th></tr></thead>
       <tbody>${changeRows || '<tr><td colspan=7 class=muted>暂无变更。访客可凭申请后的管理链接修改姓名/联系方式/同行人数及同行名单。</td></tr>'}</tbody></table>
       <p class="hint">已通过申请的变更审核通过时，在同一事务原子撤销旧票并签发新票；已核销（访客到场）的申请不能变更。候补申请的变更按新总人数重新判定名次与晋级。</p></div>
       <div class="section"><h2>申请（按提交顺序；候补名次即顺序）</h2>
       <table><thead><tr><th>#</th><th>访客</th><th>人数</th><th>状态</th><th>通行票</th><th>原因/备注</th><th>操作</th></tr></thead>
       <tbody>${appRows || '<tr><td colspan=7 class=muted>无申请</td></tr>'}</tbody></table></div>
       <div class="section"><h2>已签发的票（${v.tickets.length}，含变更替换掉的旧票）</h2>
       <table><thead><tr><th>票号</th><th>状态</th><th>人数</th><th>有效期</th><th>核销</th><th>作废/替换原因</th></tr></thead>
       <tbody>${ticketRows || '<tr><td colspan=6 class=muted>无（仅审核通过/补录通过才签票；取消、过期、候补未晋级均无票）</td></tr>'}</tbody></table></div>
       <div class="section"><h2>容量变化记录</h2>
       <table><thead><tr><th>时间</th><th>容量</th><th>原因</th></tr></thead>
       <tbody>${capRows}</tbody></table></div>
       <div class="section"><h2>批次事件流水（版本号即全局事件版本，门点离线按此补齐）</h2>
       <table><thead><tr><th>版本</th><th>时间</th><th>事件</th><th>申请</th><th>票号</th><th>原因</th></tr></thead>
       <tbody>${eventRows || '<tr><td colspan=6 class=muted>无</td></tr>'}</tbody></table></div>`;
  } catch (e) { toast(e.message, "error"); }
}

function actionButtons(a, b) {
  const id = a.id;
  if (a.status === "PENDING")
    return `<button class="btn small primary" onclick="decideApp('${id}','approve')">通过签票</button>
            <button class="btn small danger" onclick="decideApp('${id}','reject')">拒绝</button>
            <button class="btn small danger" onclick="decideApp('${id}','cancel')">取消</button>`;
  if (a.status === "WAITLISTED")
    return `<span class="muted">候补 #${a.waitlist_position}，释放名额时自动晋级</span>
            <button class="btn small danger" onclick="decideApp('${id}','cancel')">取消</button>`;
  if (a.status === "APPROVED")
    return `<button class="btn small danger" onclick="decideApp('${id}','cancel')">取消（作发票并释放名额）</button>`;
  return "";
}

window.decideApp = async (id, action) => {
  const map = {
    approve: ["POST", `通过申请 ${id}？将立即签发与批次分区一致的通行票。`, "审核通过并签票"],
    reject: ["POST", `拒绝申请 ${id}？占座名额将按候补顺序释放。`, "admin_reject"],
    cancel: ["POST", `取消申请 ${id}？已审核的票将作废，名额按候补顺序释放。`, "admin_cancel"],
  };
  const [, confirmText, reason] = map[action];
  if (!confirm(confirmText)) return;
  try {
    if (action === "approve") {
      const r = await adminApi("POST", `/api/admin/applications/${encodeURIComponent(id)}/approve`);
      toast(`已通过并签票：${r.ticket.code}`, "success");
    } else {
      await adminApi("POST", `/api/admin/applications/${encodeURIComponent(id)}/${action}`, { reason });
      toast(`申请已${action === "reject" ? "拒绝" : "取消"}`, "success");
    }
    await Promise.all([loadBatchView(), loadBatches(), loadStats()]);
  } catch (e) { toast(e.message, "error"); }
};

window.decideChange = async (id, action) => {
  const isApprove = action === "approve";
  if (!confirm(isApprove
    ? `通过变更 ${id}？已通过的申请将在同一事务撤销旧票并签发新票（容量不足会被拒绝）。`
    : `拒绝变更 ${id}？申请资料与旧票保持不变。`)) return;
  try {
    const r = await adminApi("POST", `/api/admin/changes/${encodeURIComponent(id)}/${action}`,
      { reason: isApprove ? "admin_approve_change" : "admin_reject_change" });
    if (isApprove && r.new_ticket) {
      toast(`变更已通过：旧票 ${r.old_ticket_code} 已撤销，新票 ${r.new_ticket.code} 已签发`, "success");
    } else {
      toast(isApprove ? "变更已通过" : "变更已拒绝", "success");
    }
    await Promise.all([loadBatchView(), loadBatches(), loadStats()]);
  } catch (e) { toast(e.message, "error"); }
};

window.changeCap = async (id) => {
  const capacity = parseInt($("bv-cap").value, 10);
  if (!capacity) return toast("容量无效", "error");
  try {
    const r = await adminApi("PUT", `/api/admin/batches/${encodeURIComponent(id)}/capacity`,
      { capacity, reason: $("bv-capreason").value.trim() || "admin_change" });
    toast(`容量已调整为 ${capacity}` + (r.promoted && r.promoted.length ? `，候补晋级 ${r.promoted.length} 组` : ""), "success");
    await Promise.all([loadBatchView(), loadBatches()]);
  } catch (e) { toast(e.message, "error"); }
};

window.closeBatch = async id => {
  if (!confirm("提前关闭该批次的申请？已审核的票不受影响，候补不再晋级。")) return;
  try {
    await adminApi("POST", `/api/admin/batches/${encodeURIComponent(id)}/close`);
    toast("批次已关闭", "success");
    await Promise.all([loadBatchView(), loadBatches()]);
  } catch (e) { toast(e.message, "error"); }
};

window.backfill = async id => {
  const name = $("bf-name").value.trim(), contact = $("bf-contact").value.trim();
  const companions = parseInt($("bf-comp").value, 10);
  if (!name || !contact) return toast("补录姓名与联系方式必填", "error");
  const n = isNaN(companions) ? 0 : companions;
  const companionNames = [];
  document.querySelectorAll("#bf-names input").forEach((inp, i) => {
    if (i < n) companionNames.push(inp.value.trim());
  });
  try {
    const r = await adminApi("POST", `/api/admin/batches/${encodeURIComponent(id)}/backfill`,
      { name, contact, companions: n, companion_names: companionNames });
    toast(`补录成功，已签票：${r.ticket.code}`, "success");
    await Promise.all([loadBatchView(), loadBatches(), loadStats()]);
  } catch (e) { toast(e.message, "error"); }
};

window.syncBfNames = () => {
  const n = Math.max(0, parseInt($("bf-comp").value, 10) || 0);
  const wrap = $("bf-names");
  const old = [...wrap.querySelectorAll("input")].map(i => i.value);
  wrap.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const inp = document.createElement("input");
    inp.placeholder = `同行人 ${i + 1} 姓名`;
    inp.value = old[i] || "";
    wrap.appendChild(inp);
  }
};

// ---------------- 按人 ----------------
async function loadPeople(q) {
  const { people } = await adminApi("GET", `/api/admin/people${q !== undefined && q !== null ? `?q=${encodeURIComponent(q)}` : ""}`);
  $("p-list").innerHTML = people.length ? `<table><thead><tr>
    <th>人员</th><th>总票数</th><th>当前可用</th><th>已核销</th><th>已作废</th><th>已过期</th><th></th>
    </tr></thead><tbody>` + people.map(p => `<tr>
      <td class="mono">${esc(p.person_id)}</td><td>${p.total}</td>
      <td><b style="color:#4ade80">${p.valid}</b></td><td>${p.redeemed}</td>
      <td>${p.revoked}</td><td>${p.expired}</td>
      <td><button class="btn small" onclick="loadPerson('${esc(p.person_id)}')">详情</button></td>
    </tr>`).join("") + "</tbody></table>"
    : `<p class="muted">没有找到人员。</p>`;
};
window.loadPerson = async id => {
  try {
    const v = await adminApi("GET", `/api/admin/people/${encodeURIComponent(id)}`);
    const ticketRows = v.tickets.map(t => {
      const overlap = t.effective_status === "ACTIVE";
      return `<tr>
        <td class="mono code-cell">${esc(t.code)}</td>
        <td>${badge(t.effective_status)}</td>
        <td>${fmtTime(t.valid_from)}<br><span class="muted">至 ${fmtTime(t.valid_until)}</span></td>
        <td>${t.redeemed_gate ? esc(t.redeemed_gate) + "<br><span class='muted'>" + fmtTime(t.redeemed_at) + "</span>" : "—"}</td>
        <td>${t.revoked_reason ? esc(t.revoked_reason) + "<br><span class='muted'>" + fmtTime(t.revoked_at) + "</span>" : "—"}</td>
        <td class="mono">${(t.zones || []).map(esc).join(", ") || "—"}</td>
        <td>${t.note ? esc(t.note) : ""}</td>
      </tr>`;
    }).join("");
    const eventRows = v.events.map(e => `<tr>
      <td class="mono">#${e.version}</td><td>${fmtTime(e.ts)}</td>
      <td>${esc(e.type.replace("TICKET_", ""))}</td>
      <td class="mono">${esc(e.ticket_code || "")}</td>
      <td>${e.gate_id ? esc(e.gate_id) : ""}</td><td>${esc(e.reason || "")}</td>
    </tr>`).join("");
    const attemptRows = v.scan_attempts.map(a => `<tr>
      <td>${fmtTime(a.at)}</td><td>${esc(a.gate_name || a.gate_id)}</td>
      <td><span class="badge ${a.ok ? "ok" : "fail"}">${a.ok ? "成功" : "拒绝"}</span></td>
      <td>${esc(a.status)}</td>
    </tr>`).join("");
    $("p-detail").innerHTML =
      `<h2>${esc(id)} · 当前可用 <b style="color:#4ade80">${v.valid_tickets.length}</b> 张（多张可时间重叠，按票号独立核销）</h2>
       <table><thead><tr><th>票号</th><th>状态</th><th>有效期</th><th>核销</th><th>作废</th><th>分区</th><th>备注</th></tr></thead>
       <tbody>${ticketRows}</tbody></table>
       <div class="section"><h2>状态变化原因（append-only 事件）</h2>
       <table><thead><tr><th>版本</th><th>时间</th><th>变化</th><th>票号</th><th>门点</th><th>原因</th></tr></thead>
       <tbody>${eventRows || '<tr><td colspan=6 class=muted>无</td></tr>'}</tbody></table></div>
       <div class="section"><h2>门点扫码记录</h2>
       <table><thead><tr><th>时间</th><th>门点</th><th>结果</th><th>状态</th></tr></thead>
       <tbody>${attemptRows || '<tr><td colspan=4 class=muted>无</td></tr>'}</tbody></table></div>`;
  } catch (e) { toast(e.message, "error"); }
};
$("btn-ps").onclick = () => { try { requireToken(); loadPeople($("p-q").value.trim()); } catch (_) {} };
$("btn-p-all").onclick = () => { try { requireToken(); $("p-q").value = ""; loadPeople(""); } catch (_) {} };

// ---------------- 按票 ----------------
$("btn-ticket").onclick = async () => {
  try {
    requireToken();
    const code = $("t-code").value.trim();
    if (!code) return;
    const t = await adminApi("GET", `/api/admin/tickets/${encodeURIComponent(code)}`);
    const evs = await adminApi("GET", `/api/admin/events?code=${encodeURIComponent(code)}`);
    const rows = evs.events.map(e => `<tr><td class="mono">#${e.version}</td><td>${fmtTime(e.ts)}</td>
      <td>${esc(e.type)}</td><td>${e.gate_id ? esc(e.gate_id) : ""}</td><td>${esc(e.reason || "")}</td></tr>`).join("");
    $("t-detail").innerHTML =
      `<table><tbody>
        <tr><th>票号</th><td class="mono big-code">${esc(t.code)}</td></tr>
        <tr><th>持票人</th><td class="mono">${esc(t.person_id)}</td></tr>
        <tr><th>状态</th><td>${badge(t.status)}</td></tr>
        <tr><th>有效期</th><td>${fmtTime(t.valid_from)} ～ ${fmtTime(t.valid_until)}</td></tr>
        <tr><th>核销</th><td>${t.redeemed_gate ? esc(t.redeemed_gate) + " @ " + fmtTime(t.redeemed_at) : "—"}</td></tr>
        <tr><th>作废</th><td>${t.revoked_reason ? esc(t.revoked_reason) + " @ " + fmtTime(t.revoked_at) : "—"}</td></tr>
       </tbody></table>
       <div class="section"><h2>该票状态流水</h2><table><thead><tr><th>版本</th><th>时间</th><th>事件</th><th>门点</th><th>原因</th></tr></thead>
       <tbody>${rows || '<tr><td colspan=5 class=muted>无</td></tr>'}</tbody></table></div>`;
  } catch (e) { toast(e.message, "error"); }
};

// ---------------- 流水 / 记录 ----------------
$("btn-events").onclick = async () => {
  try {
    requireToken();
    const f = $("e-filter").value.trim();
    let url = "/api/admin/events?limit=300";
    if (f.startsWith("T-")) url += `&code=${encodeURIComponent(f)}`;
    else if (f) url += `&person_id=${encodeURIComponent(f)}`;
    const { events } = await adminApi("GET", url);
    $("e-table").querySelector("tbody").innerHTML = events.map(e => {
      let badgeHtml;
      if (e.type === "POLICY_LOCK" || e.type === "BATCH_CLOSED"
          || e.type === "APPLICATION_CANCELLED" || e.type === "APPLICATION_REJECTED"
          || e.type === "APPLICATION_CHANGE_REJECTED"
          || e.type === "APPLICATION_CHANGE_CANCELLED"
          || e.type === "APPLICATION_CHANGE_EXPIRED") {
        badgeHtml = '<span class="badge LOCKED">' + esc(e.type) + "</span>";
      } else if (e.type === "TICKET_REDEEMED") {
        badgeHtml = badge("REDEEMED") + esc(e.type.replace("TICKET_", ""));
      } else if (e.type === "TICKET_REVOKED") {
        badgeHtml = badge("REVOKED") + esc(e.type.replace("TICKET_", ""));
      } else if (e.type === "TICKET_EXPIRED" || e.type === "APPLICATION_EXPIRED") {
        badgeHtml = badge("EXPIRED") + esc(e.type);
      } else if (e.type === "POLICY_UNLOCK") {
        badgeHtml = '<span class="badge OPEN">UNLOCK</span>解锁';
      } else if (e.type === "APPLICATION_CHANGE_SUBMITTED") {
        badgeHtml = '<span class="badge offline">变更提交</span>';
      } else if (e.type === "APPLICATION_CHANGE_APPROVED") {
        badgeHtml = '<span class="badge ACTIVE">变更通过·换票</span>';
      } else {
        badgeHtml = badge("ACTIVE") + esc(e.type.replace("TICKET_", ""));
      }
      const ref2 = e.application_id || (e.payload && e.payload.rule_id) || "";
      return `<tr>
      <td class="mono">#${e.version}</td><td>${fmtTime(e.ts)}</td>
      <td>${badgeHtml}</td>
      <td class="mono">${esc(e.ticket_code || "")}</td>
      <td class="mono">${esc(e.person_id || ref2 || (e.batch_id || (e.type.startsWith("POLICY") ? "全局" : "")))}</td>
      <td>${esc(e.gate_id || "")}</td><td>${esc(e.reason || "")}</td>
    </tr>`; }).join("") || '<tr><td colspan=7 class=muted>无事件</td></tr>';
  } catch (e) { toast(e.message, "error"); }
};

$("btn-attempts").onclick = async () => {
  try {
    requireToken();
    const g = $("a-gate").value;
    const { attempts } = await adminApi("GET",
      `/api/admin/attempts?limit=300${g ? `&gate_id=${encodeURIComponent(g)}` : ""}`);
    $("a-table").querySelector("tbody").innerHTML = attempts.map(a => `<tr>
      <td>${fmtTime(a.at)}</td><td>${esc(a.gate_name || a.gate_id)}</td>
      <td class="mono">${esc(a.code)}</td>
      <td><span class="badge ${a.ok ? "ok" : "fail"}">${a.ok ? "成功" : "拒绝"}</span></td>
      <td>${esc(a.status)}</td><td>${a.http_status}</td>
      <td class="mono muted">${esc(a.attempt_id)}</td>
    </tr>`).join("") || '<tr><td colspan=7 class=muted>无记录</td></tr>';
  } catch (e) { toast(e.message, "error"); }
};

// ---------------- 统计与定时刷新 ----------------
async function loadStats() {
  try {
    requireToken();
    const s = await adminApi("GET", "/api/admin/stats");
    const t = s.tickets;
    $("stats").innerHTML = [
      ["当前可用", t.ACTIVE, "#4ade80"],
      ["已核销", t.REDEEMED, "#93c5fd"],
      ["已作废", t.REVOKED, "#fca5a5"],
      ["已过期", t.EXPIRED, "#fcd34d"],
      ["事件版本", "#" + s.last_version, "#c4b5fd"],
      ["门点", `${s.gates}（停用 ${s.gates_revoked}）`, "#cbd5e1"],
      ["分区", `${s.zones}（封锁 ${s.zones_locked}）`, s.zones_locked ? "#fca5a5" : "#cbd5e1"],
      ["策略版本", "#" + s.policy_version, "#c4b5fd"],
      ["预约批次", `${s.batches}（开放 ${s.batches_open}）`, "#7dd3fc"],
      ["待审/候补", `${s.applications.PENDING} / ${s.applications.WAITLISTED}`, "#fcd34d"],
      ["已通过申请", s.applications.APPROVED, "#4ade80"],
      ["待审变更", (s.changes ? s.changes.PENDING : 0), "#fca5a5"],
      ["在场（组/人）", `${s.onsite_groups} / ${s.onsite_people}`, "#4ade80"],
      ["清点快照", s.rollcalls || 0, "#c4b5fd"],
      ["路线（进行/完成/违规）",
        `${s.route_in_progress || 0} / ${s.route_completed || 0} / ${s.route_violated || 0}`,
        s.route_violated ? "#fca5a5" : "#93c5fd"],
      ["路线冲突待处理", s.route_open_conflicts || 0,
        s.route_open_conflicts ? "#fca5a5" : "#cbd5e1"],
    ].map(([k, v, c]) => `<div class="stat"><b style="color:${c}">${v}</b>${k}</div>`).join("");
    $("clock").textContent = "服务器时间 " + fmtTime(s.now);
  } catch (_) {}
}

function refreshAll() {
  loadStats();
  loadZones().then(() => {
    loadGates(); loadZoneView();
    loadBatches().then(() => { loadBatchView(); });
  });
  loadRules();
  try { if (token) { loadRouteCatalog(); loadRouteMonitor(); loadRouteConflicts(); } } catch (_) {}
  try { loadPeople(""); } catch (_) {}
  try { loadPresence(); } catch (_) {}
  $("btn-events").click();
  $("btn-attempts").click();
}

setInterval(loadStats, 5000);
setInterval(() => { if (!$("tab-events").hidden) $("btn-events").click(); }, 8000);
setInterval(() => { if (!$("tab-routes").hidden) { loadRouteMonitor(); loadRouteConflicts(); } }, 5000);
if (token) refreshAll();

// ================= 访客路线：编排 + 监控 =================

const ROUTE_STATUS_BADGE = {
  IN_PROGRESS: "ACTIVE", COMPLETED: "REDEEMED", VIOLATED: "REVOKED",
};
const ROUTE_STATUS_TEXT = {
  IN_PROGRESS: "进行中", COMPLETED: "已完成", VIOLATED: "已违规",
};
const VIOLATION_TEXT = {
  SKIPPED_CHECKPOINT: "跳过检查点",
  CHECKPOINT_CLOSED: "进入已关闭检查点",
  DWELL_TIMEOUT: "停留超时",
  ADMIN_MARK_VIOLATED: "管理员判定违规",
};
const CONFLICT_TEXT = {
  GAP_PENDING: "中间检查点缺失",
  LATE_EVENT: "迟到的旧事件",
  ALREADY_TERMINAL: "终态后到达",
  INVALID_TIMESTAMP: "事件时间异常",
  NO_ROUTE: "路线未开始/未绑定",
  TICKET_STATE: "票面状态异常",
};

let rtvCheckpoints = [];

async function loadRouteCatalog() {
  try {
    requireToken();
    const [{ routes }, { gates }, { zones }, { batches }] = await Promise.all([
      adminApi("GET", "/api/admin/routes"),
      adminApi("GET", "/api/admin/gates"),
      adminApi("GET", "/api/admin/zones"),
      adminApi("GET", "/api/admin/batches"),
    ]);
    window._routeCatalog = routes;
    // 编排卡：分区下拉
    $("rt-zone").innerHTML = zones.zones.length
      ? zones.zones.map(z => `<option value="${esc(z.id)}">${esc(z.id)} · ${esc(z.name)}</option>`).join("")
      : '<option value="">（先建分区）</option>';
    // 路线选择下拉
    const routeOpts = '<option value="">选择路线…</option>' + routes.filter(r => !r.paused).map(r =>
      `<option value="${esc(r.id)}">${esc(r.id)} v${r.current_version} · ${esc(r.name)}</option>`).join("");
    const routeOptsAll = '<option value="">不绑定路线</option>' + routes.map(r =>
      `<option value="${esc(r.id)}">${esc(r.id)} v${r.current_version} · ${esc(r.name)}`
      + `${r.paused ? "（已暂停）" : ""}</option>`).join("");
    $("f-route").innerHTML = routeOptsAll;
    $("b-route").innerHTML = routeOptsAll;
    for (const id of ["rtv-route", "rta-route", "rm-route"]) $(id).innerHTML = routeOpts;
    $("rm-zone").innerHTML = '<option value="">全部分区</option>' +
      zones.zones.map(z => `<option value="${esc(z.id)}">${esc(z.id)}</option>`).join("");
    $("rm-batch").innerHTML = '<option value="">全部批次</option>' +
      (batches.batches || []).map(x =>
        `<option value="${esc(x.id)}">${esc(x.id)}${x.name ? " · " + esc(x.name) : ""}</option>`).join("");
    renderRouteList(routes);
    renderCheckpointEditor(gates.gates);
    return { routes, gates: gates.gates };
  } catch (e) { toast(e.message, "error"); return { routes: [], gates: [] }; }
}

function renderRouteList(routes) {
  $("rt-list").innerHTML = routes.length ? `<table><thead><tr>
    <th>路线</th><th>分区</th><th>版本</th><th>状态</th><th>检查点</th>
    <th>进行/完成/违规</th><th></th></tr></thead><tbody>` +
    routes.map(r => `<tr>
      <td class="mono">${esc(r.id)}<div class="muted" style="font-size:11px">${esc(r.name)}</div></td>
      <td>${esc(r.zone_id)}</td>
      <td>v${r.current_version}</td>
      <td>${r.paused ? '<span class="badge LOCKED">已暂停</span>' : '<span class="badge ACTIVE">使用中</span>'}</td>
      <td>${r.checkpoint_count}</td>
      <td>${r.in_progress} / ${r.completed} / <b style="color:${r.violated ? "#fca5a5" : ""}">${r.violated}</b></td>
      <td><button class="btn small" onclick="showRouteDetail('${esc(r.id)}')">查看</button></td>
    </tr>`).join("") + "</tbody></table>"
    : '<p class="muted">尚未编排路线。</p>';
}

function renderCheckpointEditor(gates) {
  const wrap = $("rtv-cps");
  if (!rtvCheckpoints.length) rtvCheckpoints = [{ gate_id: "", max_stay_seconds: "" }];
  const opts = '<option value="">选择门点…</option>' + gates.map(g =>
    `<option value="${esc(g.id)}">${esc(g.id)} · ${esc(g.name)}（${esc(g.zone_id || "未配置分区")}）</option>`).join("");
  wrap.innerHTML = rtvCheckpoints.map((cp, i) => `<div class="row" style="margin-bottom:6px">
    <b style="min-width:24px">#${i + 1}</b>
    <select data-i="${i}" class="rtv-gate" style="flex:1">${
      opts.replace(`value="${esc(cp.gate_id)}"`, `value="${esc(cp.gate_id)}" selected`)
    }</select>
    <input data-i="${i}" class="rtv-stay" type="number" min="1" placeholder="最长停留秒(空=不限)"
           style="max-width:170px" value="${cp.max_stay_seconds ?? ""}">
    <button class="btn small danger" data-i="${i}" data-act="del">删</button>
  </div>`).join("");
  wrap.querySelectorAll(".rtv-gate").forEach(sel =>
    sel.onchange = () => { rtvCheckpoints[+sel.dataset.i].gate_id = sel.value; });
  wrap.querySelectorAll(".rtv-stay").forEach(inp =>
    inp.oninput = () => { rtvCheckpoints[+inp.dataset.i].max_stay_seconds = inp.value; });
  wrap.querySelectorAll("[data-act=del]").forEach(btn =>
    btn.onclick = () => {
      rtvCheckpoints.splice(+btn.dataset.i, 1);
      renderCheckpointEditor(gates);
    });
}

$("btn-route-create").onclick = async () => {
  try {
    requireToken();
    const body = {
      zone_id: $("rt-zone").value,
      name: $("rt-name").value.trim() || ("路线-" + Date.now()),
    };
    if ($("rt-id").value.trim()) body.id = $("rt-id").value.trim();
    const r = await adminApi("POST", "/api/admin/routes", body);
    toast(`路线已创建：${r.id}（请发布检查点版本）`, "success");
    $("rtv-route").value = r.id;
    await loadRouteCatalog();
  } catch (e) { toast(e.message, "error"); }
};

$("btn-rtv-add").onclick = async () => {
  const { gates } = await loadRouteCatalog();
  rtvCheckpoints.push({ gate_id: "", max_stay_seconds: "" });
  renderCheckpointEditor(gates);
};

$("btn-rtv-publish").onclick = async () => {
  try {
    requireToken();
    const rid = $("rtv-route").value;
    if (!rid) return toast("请选择路线", "error");
    const gates = [...document.querySelectorAll("#rtv-cps .rtv-gate")].map(s => s.value);
    const stays = [...document.querySelectorAll("#rtv-cps .rtv-stay")].map(s => s.value);
    if (gates.some(g => !g)) return toast("每个检查点都要选择门点", "error");
    const checkpoints = gates.map((g, i) => ({
      gate_id: g,
      name: `检查点 ${i + 1}`,
      ...(stays[i] ? { max_stay_seconds: parseInt(stays[i], 10) } : {}),
    }));
    const r = await adminApi("POST", `/api/admin/routes/${rid}/versions`,
      { checkpoints, note: $("rtv-note").value.trim() || null });
    toast(`已发布 ${rid} v${r.published_version}（仅影响之后开始的访客）`, "success");
    rtvCheckpoints = [];
    await loadRouteCatalog();
    showRouteDetail(rid);
  } catch (e) { toast(e.message, "error"); }
};

window.showRouteDetail = async rid => {
  try {
    const d = await adminApi("GET", `/api/admin/routes/${rid}`);
    const versions = d.versions.map(v => `<details><summary>v${v.version} · ${v.checkpoints.length} 点 · ${fmtTime(v.created_at)}</summary>
      <table><thead><tr><th>#</th><th>门点</th><th>名称</th><th>最长停留</th><th>状态</th></tr></thead><tbody>
      ${v.checkpoints.map(c => `<tr><td>${c.seq}</td><td class="mono">${esc(c.gate_id)}</td>
        <td>${esc(c.name)}</td><td>${c.max_stay_seconds ? c.max_stay_seconds + " 秒" : "不限"}</td>
        <td>${c.closed ? '<span class="badge LOCKED">已关闭</span>' : '<span class="badge ACTIVE">开放</span>'}</td></tr>`).join("")}
      </tbody></table></details>`).join("");
    $("rtv-result").innerHTML =
      `<h2>${esc(rid)} <span class="badge ${d.route.status === "PAUSED" ? "LOCKED" : "ACTIVE"}">`
      + `${d.route.status === "PAUSED" ? "已暂停" : "使用中"}</span> v${d.route.current_version}</h2>
       <p class="hint">目录版本 #${d.catalog_version} · 绑定 ${d.bindings.length} 次 · 执行 ${d.progress.length} 条</p>
       ${versions}`;
    $("rta-route").value = rid; $("rtv-route").value = rid;
  } catch (e) { toast(e.message, "error"); }
};

$("btn-rta-close").onclick = async () => {
  const r = await routeCheckpointState(true);
  if (r) toast(`检查点 #${r.checkpoint.seq} 已关闭（在途访客进入即判违规）`, "success");
};
$("btn-rta-open").onclick = async () => {
  const r = await routeCheckpointState(false);
  if (r) toast(`检查点 #${r.checkpoint.seq} 已开放`, "success");
};
async function routeCheckpointState(closed) {
  try {
    requireToken();
    const rid = $("rta-route").value;
    const seq = parseInt($("rta-seq").value, 10);
    if (!rid || !seq) { toast("请选择路线并填写检查点顺序", "error"); return null; }
    const body = { seq, closed };
    const v = parseInt($("rta-version").value, 10);
    if (v) body.version = v;
    const r = await adminApi("PUT", `/api/admin/routes/${rid}/checkpoints`, body);
    await loadRouteCatalog();
    return r;
  } catch (e) { toast(e.message, "error"); return null; }
}

$("btn-rta-pause").onclick = async () => {
  try {
    requireToken();
    const rid = $("rta-route").value;
    if (!rid) return toast("请选择路线", "error");
    await adminApi("POST", `/api/admin/routes/${rid}/pause`, { reason: "admin" });
    toast("路线已暂停：不再接受新的绑定/开始；在途访客继续走原版本", "success");
    await loadRouteCatalog();
  } catch (e) { toast(e.message, "error"); }
};
$("btn-rta-resume").onclick = async () => {
  try {
    requireToken();
    const rid = $("rta-route").value;
    if (!rid) return toast("请选择路线", "error");
    await adminApi("POST", `/api/admin/routes/${rid}/resume`, { reason: "admin" });
    toast("路线已恢复", "success");
    await loadRouteCatalog();
  } catch (e) { toast(e.message, "error"); }
};

$("btn-rta-bind").onclick = async () => {
  try {
    requireToken();
    const rid = $("rta-route").value;
    const target = $("rta-target").value.trim();
    const scope = $("rta-scope").value;
    if (!rid || !target) return toast("请选择路线并填写票号/批次", "error");
    const body = { route_id: rid, scope };
    body[scope === "TICKET" ? "code" : "batch_id"] = target;
    const r = await adminApi("POST", "/api/admin/routes/bindings", body);
    toast(`已把 ${scope === "TICKET" ? r.ticket_code : r.batch_id} 绑定到 ${rid} v${r.route_version}`, "success");
    await loadRouteCatalog();
  } catch (e) { toast(e.message, "error"); }
};

// ---------------- 路线监控 ----------------

async function loadRouteMonitor() {
  try {
    requireToken();
    const params = new URLSearchParams();
    if ($("rm-status").value) params.set("status", $("rm-status").value);
    if ($("rm-route").value) params.set("route_id", $("rm-route").value);
    if ($("rm-zone").value) params.set("zone_id", $("rm-zone").value);
    if ($("rm-batch").value) params.set("batch_id", $("rm-batch").value);
    if ($("rm-person").value.trim()) params.set("person_id", $("rm-person").value.trim());
    if ($("rm-overdue").checked) params.set("overdue", "true");
    const [v, summ] = await Promise.all([
      adminApi("GET", "/api/admin/route-progress?" + params.toString()),
      adminApi("GET", "/api/admin/routes"),
    ]);
    const s = summ.summary;
    $("rt-summary").innerHTML = [
      ["进行中", s.in_progress, "#93c5fd"],
      ["已完成", s.completed, "#4ade80"],
      ["违规", s.violated, "#fca5a5"],
      ["超时停留", s.overdue, "#fcd34d"],
      ["待处理冲突", s.open_conflicts, s.open_conflicts ? "#fca5a5" : "#cbd5e1"],
    ].map(([k, val, c]) => `<div class="stat"><b style="color:${c}">${val}</b>${k}</div>`).join("");
    $("rm-list").innerHTML = v.progress.length ? `<table><thead><tr>
      <th>票号</th><th>人员</th><th>路线/版本</th><th>状态</th><th>当前检查点</th>
      <th>停留</th><th>批次</th><th></th></tr></thead><tbody>` +
      v.progress.map(p => {
        const cp = p.current_checkpoint;
        const dwell = p.dwell_seconds != null
          ? `${p.dwell_seconds}s${cp && cp.max_stay_seconds ? " / " + cp.max_stay_seconds + "s" : ""}`
          : "—";
        return `<tr>
        <td class="mono code-cell">${esc(p.ticket_code)}</td>
        <td>${esc(p.applicant_name || p.person_id)}</td>
        <td class="mono">${esc(p.route_id)}<div class=muted>v${p.route_version}</div></td>
        <td><span class="badge ${ROUTE_STATUS_BADGE[p.status]}">${ROUTE_STATUS_TEXT[p.status] || p.status}</span>
            ${p.violation_kind ? `<div class="muted" style="font-size:11px">${esc(VIOLATION_TEXT[p.violation_kind] || p.violation_kind)}</div>` : ""}</td>
        <td>${cp ? `#${cp.seq} ${esc(cp.name || cp.gate_id)}` : "—"}
            ${cp && cp.closed ? ' <span class="badge LOCKED">关闭</span>' : ""}</td>
        <td>${dwell}${p.overdue ? ' <span class="badge fail">超时</span>' : ""}</td>
        <td class="muted">${esc(p.batch_id || "")}</td>
        <td><button class="btn small" onclick="loadProgressDetail('${esc(p.ticket_code)}')">轨迹</button></td>
      </tr>`;
      }).join("") + "</tbody></table>"
      : '<p class="muted">无符合条件的路线执行。</p>';
  } catch (e) { /* 静默：定时刷新时令牌可能为空 */ }
}

window.loadProgressDetail = async code => {
  try {
    const d = await adminApi("GET", `/api/admin/route-progress/${encodeURIComponent(code)}`);
    const p = d.progress;
    const cpRows = (d.checkpoints || []).map(c => {
      const reached = p && c.seq <= (p.status === "COMPLETED" ? d.checkpoints.length : (p.current_seq || 0));
      return `<tr><td>#${c.seq}</td><td class="mono">${esc(c.gate_id)} ${c.gate_name ? esc(c.gate_name) : ""}</td>
        <td>${esc(c.name)}</td><td>${c.max_stay_seconds ? c.max_stay_seconds + " 秒" : "不限"}</td>
        <td>${c.closed ? '<span class="badge LOCKED">关闭</span>' : "开放"}</td>
        <td>${reached ? "✅" : ""}</td></tr>`;
    }).join("");
    const checkRows = (d.checks || []).map(x => `<tr>
      <td>${fmtTime(x.at)}</td><td class="mono">${esc(x.gate_id)}</td>
      <td>${x.checkpoint_seq ?? "—"}</td>
      <td><span class="badge ${x.decision === "REJECTED" || x.decision === "VIOLATED" || x.decision === "CONFLICT" ? "fail" : "ok"}">${esc(x.decision)}</span></td>
      <td>${esc(x.result || "")}${x.reason ? " · " + esc(x.reason) : ""}</td>
      <td>${x.offline ? "离线" : ""}${x.replayed ? "重放" : ""}</td></tr>`).join("");
    $("rm-detail").innerHTML =
      `<h2>${esc(code)} ${p ? `<span class="badge ${ROUTE_STATUS_BADGE[p.status]}">${ROUTE_STATUS_TEXT[p.status] || p.status}</span>` : '<span class=muted>未开始</span>'}</h2>
       ${p && p.violation_kind ? `<p class="hint">违规：<b>${esc(VIOLATION_TEXT[p.violation_kind] || p.violation_kind)}</b>${p.violation_reason ? " · " + esc(p.violation_reason) : ""}</p>` : ""}
       <table><thead><tr><th>#</th><th>门点</th><th>名称</th><th>最长停留</th><th>状态</th><th>已到</th></tr></thead>
       <tbody>${cpRows}</tbody></table>
       <div class="section"><h2>门点检查记录（票/人员/版本/顺序/门点）</h2>
       <table><thead><tr><th>时间</th><th>门点</th><th>检查点</th><th>结论</th><th>原因</th><th>标记</th></tr></thead>
       <tbody>${checkRows || '<tr><td colspan=6 class=muted>无</td></tr>'}</tbody></table></div>`;
  } catch (e) { toast(e.message, "error"); }
};

$("btn-rm").onclick = () => { try { requireToken(); loadRouteMonitor(); } catch (_) {} };

// ---------------- 离线冲突队列 ----------------

async function loadRouteConflicts() {
  try {
    requireToken();
    const status = $("rc-status").value;
    const { conflicts } = await adminApi("GET",
      `/api/admin/route-conflicts?status=${encodeURIComponent(status)}`);
    $("rc-list").innerHTML = conflicts.length ? conflicts.map(c => `<div class="section" style="border:1px solid #334155;padding:10px">
      <div><b>#${c.id}</b> <span class="badge fail">${esc(CONFLICT_TEXT[c.kind] || c.kind)}</span>
        <span class="badge ${c.status === "OPEN" ? "LOCKED" : ""}">${c.status === "OPEN" ? "待处理" : "已处理 · " + esc(c.resolution || "")}</span></div>
      <div class="muted" style="font-size:12px;margin:4px 0">
        票 <span class="mono">${esc(c.ticket_code)}</span> · 门点 ${esc(c.gate_id)} ·
        检查点 #${c.checkpoint_seq ?? "—"} · 路线 ${esc(c.route_id || "")} v${c.route_version ?? "—"}<br>
        事件时间 ${fmtTime(c.event_ts)} · 发现于 ${fmtTime(c.detected_at)}
        ${c.resolve_reason ? "<br>处理原因：" + esc(c.resolve_reason) + "（" + esc(c.resolved_by || "") + "）" : ""}
      </div>
      ${c.status === "OPEN" ? `<div class="row">
        <button class="btn primary small" onclick="resolveConflict(${c.id},'APPLIED')">按现场补推进</button>
        <button class="btn danger small" onclick="resolveConflict(${c.id},'MARK_VIOLATED')">判违规</button>
        <button class="btn small" onclick="resolveConflict(${c.id},'DISMISSED')">忽略</button>
      </div>` : ""}
    </div>`).join("") : '<p class="muted">无冲突记录。</p>';
  } catch (_) {}
}

window.resolveConflict = async (id, action) => {
  const reason = prompt(`处理冲突 #${id}（${action}）的原因：`, action === "DISMISSED" ? "确认忽略" : "管理员处理");
  if (reason === null) return;
  try {
    await adminApi("POST", `/api/admin/route-conflicts/${id}/resolve`,
      { action, reason: reason || "admin" });
    toast(`冲突 #${id} 已处理（${action}），原始冲突记录保留`, "success");
    await Promise.all([loadRouteConflicts(), loadRouteMonitor(), loadStats()]);
  } catch (e) { toast(e.message, "error"); }
};

$("btn-rc").onclick = () => { try { requireToken(); loadRouteConflicts(); } catch (_) {} };

