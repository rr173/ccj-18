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
document.querySelectorAll(".tabs button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    ["person", "ticket", "zone", "events", "attempts"].forEach(t =>
      $(`tab-${t}`).hidden = t !== btn.dataset.tab);
    if (btn.dataset.tab === "zone") loadZoneView();
  };
});

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
       允许分区：${t.zones && t.zones.length ? t.zones.map(esc).join("、") : "（无 · 所有门点拒绝）"}</div></div>`;
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
    // 规则目标分区
    $("pr-zone").innerHTML = `<option value="">全局（所有分区）</option>` +
      zones.map(z => `<option value="${esc(z.id)}">${esc(z.id)} · ${esc(z.name)}</option>`).join("");
    // 分区视图选择
    $("zv-zone").innerHTML = zones.map(z =>
      `<option value="${esc(z.id)}">${esc(z.id)} · ${esc(z.name)}</option>`).join("");
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
    $("e-table").querySelector("tbody").innerHTML = events.map(e => `<tr>
      <td class="mono">#${e.version}</td><td>${fmtTime(e.ts)}</td>
      <td>${e.type === "POLICY_LOCK" ? '<span class="badge LOCKED">LOCK</span>封锁'
        : e.type === "POLICY_UNLOCK" ? '<span class="badge OPEN">UNLOCK</span>解锁'
        : badge(e.type === "TICKET_REDEEMED" ? "REDEEMED"
        : e.type === "TICKET_REVOKED" ? "REVOKED"
        : e.type === "TICKET_EXPIRED" ? "EXPIRED" : "ACTIVE") + esc(e.type.replace("TICKET_", ""))}</td>
      <td class="mono">${esc(e.ticket_code || (e.payload && e.payload.rule_id) || "")}</td>
      <td class="mono">${esc(e.person_id || (e.payload && e.payload.zone_id) || (e.type.startsWith("POLICY") ? "全局" : ""))}</td>
      <td>${esc(e.gate_id || "")}</td><td>${esc(e.reason || "")}</td>
    </tr>`).join("") || '<tr><td colspan=7 class=muted>无事件</td></tr>';
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
    ].map(([k, v, c]) => `<div class="stat"><b style="color:${c}">${v}</b>${k}</div>`).join("");
    $("clock").textContent = "服务器时间 " + fmtTime(s.now);
  } catch (_) {}
}

function refreshAll() {
  loadStats();
  loadZones().then(() => { loadGates(); loadZoneView(); });
  loadRules();
  try { loadPeople(""); } catch (_) {}
  $("btn-events").click();
  $("btn-attempts").click();
}

setInterval(loadStats, 5000);
setInterval(() => { if (!$("tab-events").hidden) $("btn-events").click(); }, 8000);
if (token) refreshAll();
