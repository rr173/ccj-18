// 门点核销台：核销 + 失败重试队列 + 离线重连按版本补齐（含封锁策略事件）
const $ = id => document.getElementById(id);

const gateId = () => localStorage.getItem("shortpass_gate_id") || "";
const gateToken = () => localStorage.getItem("shortpass_gate_token") || "";
const cursorKey = () => `shortpass_cursor_${gateId()}`;
const queueKey = () => `shortpass_queue_${gateId()}`;
const histKey = () => `shortpass_hist_${gateId()}`;
const pvKey = () => `shortpass_pv_${gateId()}`;

const getCursor = () => parseInt(localStorage.getItem(cursorKey()) || "0", 10);
const setCursor = v => localStorage.setItem(cursorKey(), String(v));
const getPV = () => parseInt(localStorage.getItem(pvKey()) || "0", 10);
const setPV = v => { if (v > getPV()) localStorage.setItem(pvKey(), String(v)); };
const getQueue = () => JSON.parse(localStorage.getItem(queueKey()) || "[]");
const setQueue = q => localStorage.setItem(queueKey(), JSON.stringify(q));
const getHist = () => JSON.parse(localStorage.getItem(histKey()) || "[]");
const setHist = h => localStorage.setItem(histKey(), JSON.stringify(h.slice(0, 50)));

let syncing = false;
let flushing = false;
let online = false;

const gateApi = (m, p, b) => api(m, p, b, gateToken());

function uuid() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    "a-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

$("saveCfg").onclick = () => {
  localStorage.setItem("shortpass_gate_id", $("gateId").value.trim());
  localStorage.setItem("shortpass_gate_token", $("gateToken").value.trim());
  toast("配置已保存", "success");
  renderBase();
};
$("gateId").value = gateId();
$("gateToken").value = gateToken();

function setNet(on, text) {
  online = on;
  $("net-dot").className = "queue-dot " + (on ? "on" : "off");
  $("net-text").textContent = text;
}

function renderBase() {
  $("cursor").textContent = "#" + getCursor();
  $("pv").textContent = "#" + getPV();
  $("queue-n").textContent = getQueue().length;
  renderHistory();
}

function renderHistory() {
  const h = getHist();
  $("history").innerHTML = h.map(x => `<tr>
    <td>${fmtTime(x.ts)}</td>
    <td class="mono">${esc(x.code)}${x.party_size ? `<div class="muted" style="font-size:11px">${esc(x.applicant || "")} · 共${x.party_size}人 · v${x.change_version ?? 0}</div>` : ""}</td>
    <td><span class="badge ${x.ok ? "ok" : "fail"}">${x.ok ? "放行" : "拒绝"}</span></td>
    <td>${esc(x.detail || x.status || "")}${x.replayed ? " <span class='muted'>(幂等重放)</span>" : ""}</td>
  </tr>`).join("") || '<tr><td colspan=4 class="muted">暂无</td></tr>';
}

function showResult(r, httpStatus, replayed) {
  const ok = r.ok;
  const detail = r.reason_text || r.status;
  const a = r.appointment;
  $("result").innerHTML = `<div class="result-box ${ok ? "ok" : "fail"}">
    <div>${ok ? "✅ 核销成功 · 放行" : "⛔ 拒绝核销"} ${httpStatus === 409 && r.reason === "already_redeemed" ? "（该票已使用）" : ""}</div>
    <div class="big-code">${esc(r.code || "")}</div>
    <div class="detail">
      ${r.person_id ? "持票人 " + esc(r.person_id) + " · " : ""}状态 ${esc(r.status)} · ${esc(detail)}
      ${a ? `<br>🏷️ 批次 <b>${esc(a.batch_id)}</b>${a.batch_name ? "（" + esc(a.batch_name) + "）" : ""}
             ${a.visit_date ? " · " + esc(a.visit_date) : ""}
             · 访客 <b>${esc(a.applicant_name || "")}</b>
             · <b>同行共 ${a.party_size} 人</b>
             · 资料版本 <b>v${a.change_version ?? 0}</b>
             ${(a.companion_names || []).filter(Boolean).length
               ? "<br>同行人名单：" + a.companion_names.filter(Boolean).map(esc).join("、")
               : ""}
             ${a.replaced_code ? `<br>♻ 本票为变更后换发（替换旧票 <span class="mono">${esc(a.replaced_code)}</span>）：${esc(a.replacement_reason || "")}` : ""}
             ${a.replaced_by_code ? `<br>♻ 本票已因申请变更被替换，请改扫新票 <span class="mono">${esc(a.replaced_by_code)}</span>` : ""}` : ""}
      ${r.redeemed_gate ? "<br>已由门点 <b>" + esc(r.redeemed_gate) + "</b> 于 " + fmtTime(r.redeemed_at) + " 核销" : ""}
      ${r.revoked_reason ? "<br>作废原因：" + esc(r.revoked_reason) : ""}
      ${r.replaced_by_code && !a ? "<br>请改扫新票 <span class='mono'>" + esc(r.replaced_by_code) + "</span>" : ""}
      ${r.reason === "zone_mismatch" ? "<br>本门点分区 <b>" + esc(r.zone_id || "") + "</b>，票据允许分区：" + esc((r.ticket_zones || []).join("、") || "（无）") : ""}
      ${r.lock_rule ? "<br>封锁规则 <b>" + esc(r.lock_rule.rule_id) + "</b>（版本 #" + r.lock_rule.version + "）：" + esc(r.lock_rule.reason || "") : ""}
      ${r.reason === "stale_policy" ? "<br>本门点策略版本 #" + r.policy_version + "，服务器已到 #" + r.current_policy_version + "，正在自动补齐…" : ""}
      ${replayed ? "<br>⚠ 本次为同一次扫码的幂等重放，结果以首次为准" : ""}
    </div>
  </div>`;
}

async function doRedeem(code, attemptId) {
  try {
    const r = await gateApi("POST", "/api/gate/redeem", {
      gate_id: gateId(), code, attempt_id: attemptId, policy_version: getPV(),
    });
    return r; // fetch 非 2xx 会抛错
  } catch (e) {
    // 4xx 是服务器的明确业务结论（已使用/已作废/已过期/分区不符/封锁中等），同样是终态结果
    if (e.data && (e.status === 409 || e.status === 410 || e.status === 403
                   || e.status === 404 || e.status === 423)) {
      return e.data;
    }
    throw e; // 网络错误/5xx：留在队列稍后重试
  }
}

// 一次物理扫码 = 一个新的 attempt_id（同一张票第二次扫应得到“已使用”）
async function scan() {
  if (!gateId() || !gateToken()) return toast("请先配置门点 ID 和令牌", "error");
  const code = $("code").value.trim().toUpperCase();
  if (!code) return;
  $("code").value = "";
  const item = { attempt_id: uuid(), code, ts: new Date().toISOString() };
  const queue = getQueue();
  queue.push(item);
  setQueue(queue);
  renderBase();
  await flushQueue();
  $("code").focus();
}

async function flushQueue() {
  if (flushing || !gateId()) return;
  flushing = true;
  let queue = getQueue();
  let staleRetries = 0;
  while (queue.length) {
    const item = queue[0]; // FIFO
    try {
      const r = await doRedeem(item.code, item.attempt_id);
      if (r.reason === "stale_policy") {
        // 规则版本过旧：不算终态结论，先按版本补齐策略事件再重试同一条
        if (++staleRetries > 3) {
          setNet(false, "策略版本过旧且同步失败，稍候自动重试");
          break;
        }
        toast("封锁规则版本过旧，正在补齐后重试…", "info");
        await sync();
        continue; // 不移出队列，用同一 attempt_id 重试（服务端未记录该次判定）
      }
      staleRetries = 0;
      if (r.policy_version) setPV(r.policy_version);
      const h = getHist();
      h.unshift({
        ts: r.ts || new Date().toISOString(),
        code: r.code || item.code,
        ok: !!r.ok,
        status: r.status,
        detail: r.reason_text || "",
        replayed: !!r.replayed,
        party_size: r.appointment ? r.appointment.party_size : null,
        applicant: r.appointment ? r.appointment.applicant_name : null,
        change_version: r.appointment ? r.appointment.change_version : null,
      });
      setHist(h);
      if (!r.replayed) showResult(r, r.ok ? 200 : (r.reason === "already_redeemed" ? 409 : 410), false);
      queue.shift(); // 拿到明确结论才移出队列
      setQueue(queue);
      setNet(true, "在线 · " + gateId());
    } catch (e) {
      setNet(false, "离线/连接失败，结果将在恢复后补提交");
      toast("网络错误，已保留在离线队列", "error");
      break;
    }
  }
  flushing = false;
  renderBase();
}

// 离线重连：按版本号顺序补齐撤销 / 核销 / 过期 / 封锁策略事件
async function sync() {
  if (syncing || !gateId() || !gateToken()) return;
  syncing = true;
  let pages = 0;
  const logEl = $("synclog");
  const lines = [];
  try {
    do {
      const data = await gateApi("POST", "/api/gate/sync", {
        gate_id: gateId(), since_version: getCursor(),
      });
      for (const e of data.events) {
        const target = e.ticket_code
          ? ` ${e.ticket_code}`
          : (e.application_id ? ` ${e.application_id}` + (e.batch_id ? ` @${e.batch_id}` : "") : (e.batch_id ? ` @${e.batch_id}` : ""));
        lines.push(`#${e.version} ${fmtTime(e.ts)} ${e.type}` +
          target +
          (e.gate_id ? ` @${e.gate_id}` : "") +
          (e.reason ? ` reason=${e.reason}` : ""));
        setCursor(e.version);
        if (e.type === "POLICY_LOCK" || e.type === "POLICY_UNLOCK") setPV(e.version);
      }
      pages++;
      if (!data.events.length || !data.has_more || pages >= 50) break;
    } while (true);
    setNet(true, "在线 · " + gateId());
    if (lines.length) {
      logEl.textContent = lines.join("\n") + "\n" + logEl.textContent;
      toast(`已补齐 ${lines.length} 条状态变化`, "success");
    }
    // 重连成功后先补提交离线扫码
    if (getQueue().length) await flushQueue();
  } catch (e) {
    setNet(false, "同步失败（门点未注册/已停用 或 离线）：" + e.message);
  } finally {
    syncing = false;
    renderBase();
  }
}

$("btn-redeem").onclick = scan;
$("code").addEventListener("keydown", e => { if (e.key === "Enter") scan(); });

if (gateId()) {
  setNet(false, "正在连接…");
  sync();
  flushQueue();
}
setInterval(sync, 5000);
setInterval(flushQueue, 5000);
setInterval(renderBase, 2000);
