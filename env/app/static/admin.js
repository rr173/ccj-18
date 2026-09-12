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
    ["person", "ticket", "events", "attempts"].forEach(t =>
      $(`tab-${t}`).hidden = t !== btn.dataset.tab);
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
    const payload = { person_id: person, ttl_seconds: minutes * 60 };
    if ($("f-from").value) {
      delete payload.ttl_seconds;
      payload.valid_from = new Date($("f-from").value).toISOString();
      payload.valid_until = new Date(new Date($("f-from").value).getTime() + minutes * 60000).toISOString();
    }
    if ($("f-note").value.trim()) payload.note = $("f-note").value.trim();
    const t = await adminApi("POST", "/api/admin/tickets", payload);
    $("issue-result").innerHTML =
      `<div class="result-box ok">已签发 <span class="big-code mono">${esc(t.code)}</span>
       <div class="detail">有效期至 ${fmtTime(t.valid_until)} · 版本 #${t.version}</div></div>`;
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
    $("gates").innerHTML = `<table><thead><tr><th>ID</th><th>名称</th><th>状态</th><th>同步水位</th><th></th></tr></thead><tbody>` +
      gates.map(g => `<tr>
        <td class="mono">${esc(g.id)}</td><td>${esc(g.name)}</td>
        <td>${g.revoked ? '<span class="badge REVOKED">已停用</span>' : '<span class="badge ACTIVE">启用中</span>'}</td>
        <td class="mono">#${g.last_version ?? 0}</td>
        <td>${g.revoked ? "" : `<button class="btn small danger" onclick="revokeGate('${esc(g.id)}')">停用</button>`}</td>
      </tr>`).join("") + "</tbody></table>" +
      `<p class="hint">所有门点共用令牌 PASSPORT_GATE_TOKEN，门点 ID 必须先在此注册。</p>`;
    const sel = $("a-gate");
    sel.innerHTML = `<option value="">全部门点</option>` +
      gates.map(g => `<option value="${esc(g.id)}">${esc(g.id)} · ${esc(g.name)}</option>`).join("");
  } catch (e) { /* 未填令牌时静默 */ }
}
window.revokeGate = async id => {
  if (!confirm(`停将门点 ${id}？停用后该门点核销与同步都会被拒绝。`)) return;
  try { await adminApi("DELETE", `/api/admin/gates/${id}`); toast("门点已停用", "success"); loadGates(); }
  catch (e) { toast(e.message, "error"); }
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
       <table><thead><tr><th>票号</th><th>状态</th><th>有效期</th><th>核销</th><th>作废</th><th>备注</th></tr></thead>
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
      <td>${badge(e.type === "TICKET_REDEEMED" ? "REDEEMED"
        : e.type === "TICKET_REVOKED" ? "REVOKED"
        : e.type === "TICKET_EXPIRED" ? "EXPIRED" : "ACTIVE")}${esc(e.type.replace("TICKET_", ""))}</td>
      <td class="mono">${esc(e.ticket_code || "")}</td>
      <td class="mono">${esc(e.person_id || "")}</td>
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
    ].map(([k, v, c]) => `<div class="stat"><b style="color:${c}">${v}</b>${k}</div>`).join("");
    $("clock").textContent = "服务器时间 " + fmtTime(s.now);
  } catch (_) {}
}

function refreshAll() {
  loadStats();
  loadGates();
  try { loadPeople(""); } catch (_) {}
  $("btn-events").click();
  $("btn-attempts").click();
}

setInterval(loadStats, 5000);
setInterval(() => { if (!$("tab-events").hidden) $("btn-events").click(); }, 8000);
if (token) refreshAll();
