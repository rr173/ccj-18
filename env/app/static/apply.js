// 访客预约申请页（公开，无 Bearer 令牌；鉴权靠链接里的一次性 apply token）
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const applyToken = params.get("k") || "";
const myAppsKey = () => `shortpass_myapps_${applyToken}`;
// request_id 只在表单“一次提交动作”内复用：网络抖动/双击重发同一请求天然幂等
let submittingId = null;

function uuid() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    "r-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}
function loadMyApps() {
  try { return JSON.parse(localStorage.getItem(myAppsKey()) || "[]"); }
  catch (_) { return []; }
}
function saveMyApp(manageToken) {
  const list = loadMyApps().filter(t => t !== manageToken);
  list.unshift(manageToken);
  localStorage.setItem(myAppsKey(), JSON.stringify(list.slice(0, 10)));
}

function statusBadge(s) {
  const cls = ({ APPROVED: "ACTIVE", PENDING: "online", WAITLISTED: "offline",
                 CANCELLED: "REVOKED", REJECTED: "REVOKED", EXPIRED: "EXPIRED" })[s] || "UNKNOWN";
  const text = ({ APPROVED: "已通过", PENDING: "待审核（已占名额）", WAITLISTED: "候补中",
                  CANCELLED: "已取消", REJECTED: "已拒绝", EXPIRED: "已过期" })[s] || s;
  return `<span class="badge ${cls}">${text}</span>`;
}

function changeStatusBadge(s) {
  const map = {
    PENDING: ["offline", "变更待审核"], APPROVED: ["ACTIVE", "变更已通过"],
    REJECTED: ["REVOKED", "变更被拒绝"], CANCELLED: ["REVOKED", "变更已撤回"],
    EXPIRED: ["EXPIRED", "变更已过期"],
  };
  const [cls, text] = map[s] || ["UNKNOWN", s];
  return `<span class="badge ${cls}">${text}</span>`;
}

// 根据同行人数动态生成姓名输入框
function syncCompanionInputs() {
  const n = Math.max(0, parseInt($("f-companions").value, 10) || 0);
  const wrap = $("f-names-wrap");
  const prev = wrap.querySelectorAll("input").length;
  if (n === prev) return;
  const old = [...wrap.querySelectorAll("input")].map(i => i.value);
  wrap.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const label = document.createElement("label");
    label.textContent = `同行人 ${i + 1} 姓名`;
    const inp = document.createElement("input");
    inp.maxLength = 128;
    inp.value = old[i] || "";
    inp.placeholder = `同行人 ${i + 1} 姓名（门点核对整组访客）`;
    wrap.appendChild(label);
    wrap.appendChild(inp);
  }
}
$("f-companions").addEventListener("input", syncCompanionInputs);

function renderBatch(b) {
  $("sub").textContent = `${b.visit_date} · ${fmtTime(b.start_at)} ～ ${fmtTime(b.end_at)} · 分区 ${b.zone_id}`;
  const state = !b.accepting
    ? '<span class="badge REVOKED">已关闭/已结束</span>'
    : b.remaining > 0
      ? '<span class="badge ACTIVE">申请中</span>'
      : '<span class="badge EXPIRED">满额 · 候补中</span>';
  $("batch-info").innerHTML = `<table><tbody>
    <tr><th>批次</th><td class="mono">${esc(b.id)}${b.name ? " · " + esc(b.name) : ""}</td></tr>
    <tr><th>状态</th><td>${state}</td></tr>
    <tr><th>名额</th><td>容量 ${b.capacity} · 已占 <b>${b.used}</b> · 剩余 <b>${b.remaining}</b>
        · 候补 ${b.waitlist_count} 组</td></tr>
  </tbody></table>`;
  if (!b.accepting) {
    document.querySelectorAll("#apply-form input, #apply-form button").forEach(el => el.disabled = true);
  }
}

async function loadBatch() {
  if (!applyToken) {
    $("sub").textContent = "链接缺少申请令牌 (k=...)，请向管理员索取完整申请链接。";
    return;
  }
  try {
    const b = await api("GET", `/api/public/batches/${encodeURIComponent(applyToken)}`);
    renderBatch(b);
    refreshMyApps();
  } catch (e) {
    $("sub").textContent = "申请链接无效或批次不存在。";
    $("batch-info").innerHTML = `<div class="result-box fail">${esc(e.message)}</div>`;
  }
}

function renderSubmitResult(r, replayed) {
  const wl = r.status === "WAITLISTED";
  $("result").innerHTML = `<div class="result-box ${wl ? "fail" : "ok"}">
    <div>${wl ? "⏳ 名额已满，已进入候补队列" : "✅ 申请已提交"}${replayed ? "（幂等重放，未重复申请）" : ""}</div>
    <div class="detail">
      申请编号 <span class="mono">${esc(r.id)}</span> · ${statusBadge(r.status)}
      ${wl ? `<br>候补名次：<b>第 ${r.waitlist_position} 位</b>（有名额释放时按提交顺序自动晋级）` : ""}
      ${!wl ? "<br>待管理员审核；通过后本页将显示通行票号。" : ""}
      ${r.manage_token ? `<br><span class="muted">状态查询令牌已保存在本浏览器。</span>` : ""}
    </div>
  </div>`;
}

$("apply-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const name = $("f-name").value.trim();
  const contact = $("f-contact").value.trim();
  const companions = parseInt($("f-companions").value, 10);
  if (!name || !contact) return toast("姓名与联系方式必填", "error");
  if (isNaN(companions) || companions < 0) return toast("同行人数无效", "error");
  const companionNames = [...$("f-names-wrap").querySelectorAll("input")].map(i => i.value.trim());

  // 同一次提交动作（双击/网络重试）复用同一个 request_id
  if (!submittingId) submittingId = uuid();
  const btn = document.querySelector("#apply-form button");
  btn.disabled = true;
  try {
    const r = await api("POST",
      `/api/public/batches/${encodeURIComponent(applyToken)}/applications`,
      { request_id: submittingId, name, contact, companions,
        companion_names: companionNames });
    renderSubmitResult(r, !!r.replayed);
    if (r.manage_token) { saveMyApp(r.manage_token); $("my-apps-section").hidden = false; }
    loadBatch();
    refreshMyApps();
    submittingId = null;
    $("f-name").value = $("f-contact").value = "";
    $("f-companions").value = "0";
    syncCompanionInputs();
  } catch (e) {
    // 409/410 等明确结论展示出来；网络错误保留同一 request_id 以便重试
    $("result").innerHTML = `<div class="result-box fail">提交失败：${esc(e.message)}</div>`;
    toast("提交失败：" + e.message + "（可直接重试，不会重复申请）", "error");
  } finally {
    btn.disabled = false;
  }
};

async function refreshMyApps() {
  const tokens = loadMyApps();
  if (!tokens.length) { $("my-apps-section").hidden = true; return; }
  $("my-apps-section").hidden = false;
  currentTokens.length = 0;
  currentTokens.push(...tokens);
  const views = [];
  for (const t of tokens) {
    try { views.push(await api("GET", `/api/public/applications/${encodeURIComponent(t)}`)); }
    catch (_) { /* 令牌失效则忽略 */ }
  }
  $("my-apps").innerHTML = views.map(v => {
    const names = (v.companion_names || []).filter(Boolean).join("、");
    const pc = v.pending_change;
    const changeBox = pc ? `<div style="margin-top:6px;border-top:1px dashed #334155;padding-top:6px">
        ${changeStatusBadge(pc.status)}
        变更为：${esc(pc.new_name)} · 共 ${pc.new_party_size} 人
        ${(pc.new_companion_names || []).filter(Boolean).length ? "（" + pc.new_companion_names.filter(Boolean).map(esc).join("、") + "）" : ""}
        <button class="btn small danger" style="margin-left:8px"
          onclick="cancelChange('${encodeURIComponent(currentTokens[views.indexOf(v)])}','${encodeURIComponent(pc.id)}')">撤回变更</button>
      </div>` : "";
    const canChange = ["PENDING", "WAITLISTED", "APPROVED"].includes(v.status) && !pc;
    return `<div style="border:1px solid #334155;border-radius:8px;padding:10px;margin-bottom:8px">
    <div>${statusBadge(v.status)} <span class="mono">${esc(v.id)}</span>
      ${v.waitlist_position ? `· 候补第 <b>${v.waitlist_position}</b> 位` : ""}
      · <span class="muted">资料版本 v${v.change_version ?? 0}</span></div>
    <div class="muted" style="font-size:12px;margin-top:4px">
      ${esc(v.name)} · 共 ${v.party_size} 人
      ${names ? "（同行：" + esc(names) + "）" : ""}
      · 提交于 ${fmtTime(v.created_at)}
      ${v.promoted_at ? "<br>已于 " + fmtTime(v.promoted_at) + " 候补晋级" : ""}
      ${v.ticket ? `<br>通行票 <span class="mono">${esc(v.ticket.code)}</span>
         · ${badge(v.ticket.status)} · 有效至 ${fmtTime(v.ticket.valid_until)}
         ${v.ticket.replaced_code ? "<br>♻ 本票为变更后换发，旧票 " + esc(v.ticket.replaced_code) + " 已作废" : ""}` : ""}
    </div>
    ${changeBox}
    ${canChange ? `<button class="btn small" style="margin-top:6px"
      onclick="toggleChangeForm('${encodeURIComponent(currentTokens[views.indexOf(v)])}')">申请变更（姓名/联系方式/同行人数）</button>
      <div id="cf-${views.indexOf(v)}" hidden style="margin-top:8px">
        <input data-k="name" maxlength="128" value="${esc(v.name)}" placeholder="新姓名" style="margin-bottom:4px">
        <input data-k="contact" maxlength="128" value="${esc(v.contact || "")}" placeholder="新联系方式" style="margin-bottom:4px">
        <label>新同行人数（不含本人）</label>
        <input data-k="companions" type="number" min="0" max="1000"
          value="${v.companions ?? (v.party_size - 1)}"
          oninput="buildChangeNames(this, ${views.indexOf(v)})">
        <div data-k="names"></div>
        <button class="btn small primary" onclick="submitChange('${encodeURIComponent(currentTokens[views.indexOf(v)])}', ${views.indexOf(v)})">提交变更申请</button>
      </div>` : ""}
  </div>`; }).join("") || '<p class="muted">暂无。</p>';
}

const currentTokens = [];

window.toggleChangeForm = (mtEnc) => {
  // 通过 token 反查索引
  const mt = decodeURIComponent(mtEnc);
  const idx = currentTokens.indexOf(mt);
  const el = $("cf-" + idx);
  if (el) {
    el.hidden = !el.hidden;
    if (!el.hidden) buildChangeNames(el.querySelector('[data-k="companions"]'), idx);
  }
};

function buildChangeNames(compInput, idx) {
  const n = Math.max(0, parseInt(compInput.value, 10) || 0);
  const wrap = compInput.parentElement.querySelector('[data-k="names"]');
  const prev = [...wrap.querySelectorAll("input")].map(i => i.value);
  wrap.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const inp = document.createElement("input");
    inp.maxLength = 128;
    inp.placeholder = `同行人 ${i + 1} 姓名`;
    inp.value = prev[i] || "";
    wrap.appendChild(inp);
  }
}

window.submitChange = async (mtEnc, idx) => {
  const mt = decodeURIComponent(mtEnc);
  const root = $("cf-" + idx);
  const name = root.querySelector('[data-k="name"]').value.trim();
  const contact = root.querySelector('[data-k="contact"]').value.trim();
  const companions = parseInt(root.querySelector('[data-k="companions"]').value, 10);
  const companionNames = [...root.querySelectorAll('[data-k="names"] input')].map(i => i.value.trim());
  if (!name || !contact) return toast("姓名与联系方式必填", "error");
  try {
    const r = await api("POST", `/api/public/applications/${encodeURIComponent(mt)}/changes`,
      { request_id: uuid(), name, contact, companions, companion_names: companionNames });
    if (r.noop) return toast("变更内容与当前申请一致，无需提交", "info");
    toast(r.replayed ? "变更请求已存在（幂等重放）" : "变更申请已提交，等待管理员审核", "success");
    await refreshMyApps();
  } catch (e) { toast("变更失败：" + e.message, "error"); }
};

window.cancelChange = async (mtEnc, changeIdEnc) => {
  const mt = decodeURIComponent(mtEnc);
  const changeId = decodeURIComponent(changeIdEnc);
  if (!confirm("撤回该变更申请？")) return;
  try {
    await api("POST", `/api/public/changes/${encodeURIComponent(changeId)}/cancel`,
      { manage_token: mt });
    toast("变更已撤回", "success");
    await refreshMyApps();
  } catch (e) { toast("撤回失败：" + e.message, "error"); }
};

loadBatch();
