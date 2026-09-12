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

  // 同一次提交动作（双击/网络重试）复用同一个 request_id
  if (!submittingId) submittingId = uuid();
  const btn = document.querySelector("#apply-form button");
  btn.disabled = true;
  try {
    const r = await api("POST",
      `/api/public/batches/${encodeURIComponent(applyToken)}/applications`,
      { request_id: submittingId, name, contact, companions });
    renderSubmitResult(r, !!r.replayed);
    if (r.manage_token) { saveMyApp(r.manage_token); $("my-apps-section").hidden = false; }
    loadBatch();
    refreshMyApps();
    submittingId = null;
    $("f-name").value = $("f-contact").value = "";
    $("f-companions").value = "0";
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
  const views = [];
  for (const t of tokens) {
    try { views.push(await api("GET", `/api/public/applications/${encodeURIComponent(t)}`)); }
    catch (_) { /* 令牌失效则忽略 */ }
  }
  $("my-apps").innerHTML = views.map(v => `<div style="border:1px solid #334155;border-radius:8px;padding:10px;margin-bottom:8px">
    <div>${statusBadge(v.status)} <span class="mono">${esc(v.id)}</span>
      ${v.waitlist_position ? `· 候补第 <b>${v.waitlist_position}</b> 位` : ""}</div>
    <div class="muted" style="font-size:12px;margin-top:4px">
      ${esc(v.name)} · 共 ${v.party_size} 人 · 提交于 ${fmtTime(v.created_at)}
      ${v.promoted_at ? "<br>已于 " + fmtTime(v.promoted_at) + " 候补晋级" : ""}
      ${v.ticket ? `<br>通行票 <span class="mono">${esc(v.ticket.code)}</span>
         · ${badge(v.ticket.status)} · 有效至 ${fmtTime(v.ticket.valid_until)}` : ""}
    </div>
  </div>`).join("") || '<p class="muted">暂无。</p>';
}

loadBatch();
