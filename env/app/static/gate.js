// 门点核销台：核销 + 失败重试队列 + 离线重连按版本补齐
const $ = id => document.getElementById(id);

const gateId = () => localStorage.getItem("shortpass_gate_id") || "";
const gateToken = () => localStorage.getItem("shortpass_gate_token") || "";
const cursorKey = () => `shortpass_cursor_${gateId()}`;
const queueKey = () => `shortpass_queue_${gateId()}`;
const histKey = () => `shortpass_hist_${gateId()}`;

const getCursor = () => parseInt(localStorage.getItem(cursorKey()) || "0", 10);
const setCursor = v => localStorage.setItem(cursorKey(), String(v));
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
  $("queue-n").textContent = getQueue().length;
  renderHistory();
}

function renderHistory() {
  const h = getHist();
  $("history").innerHTML = h.map(x => `<tr>
    <td>${fmtTime(x.ts)}</td>
    <td class="mono">${esc(x.code)}</td>
    <td><span class="badge ${x.ok ? "ok" : "fail"}">${x.ok ? "放行" : "拒绝"}</span></td>
    <td>${esc(x.detail || x.status || "")}${x.replayed ? " <span class='muted'>(幂等重放)</span>" : ""}</td>
  </tr>`).join("") || '<tr><td colspan=4 class="muted">暂无</td></tr>';
}

function showResult(r, httpStatus, replayed) {
  const ok = r.ok;
  const detail = r.reason_text || r.status;
  $("result").innerHTML = `<div class="result-box ${ok ? "ok" : "fail"}">
    <div>${ok ? "✅ 核销成功 · 放行" : "⛔ 拒绝核销"} ${httpStatus === 409 ? "（该票已使用）" : ""}</div>
    <div class="big-code">${esc(r.code || "")}</div>
    <div class="detail">
      ${r.person_id ? "持票人 " + esc(r.person_id) + " · " : ""}状态 ${esc(r.status)} · ${esc(detail)}
      ${r.redeemed_gate ? "<br>已由门点 <b>" + esc(r.redeemed_gate) + "</b> 于 " + fmtTime(r.redeemed_at) + " 核销" : ""}
      ${r.revoked_reason ? "<br>作废原因：" + esc(r.revoked_reason) : ""}
      ${replayed ? "<br>⚠ 本次为同一次扫码的幂等重放，结果以首次为准" : ""}
    </div>
  </div>`;
}

async function doRedeem(code, attemptId) {
  try {
    const r = await gateApi("POST", "/api/gate/redeem", {
      gate_id: gateId(), code, attempt_id: attemptId,
    });
    return r; // fetch 非 2xx 会抛错
  } catch (e) {
    // 4xx 是服务器的明确业务结论（已使用/已作废/已过期/不存在），同样是终态结果
    if (e.data && (e.status === 409 || e.status === 410 || e.status === 403 || e.status === 404)) {
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
  while (queue.length) {
    const item = queue[0]; // FIFO
    try {
      const r = await doRedeem(item.code, item.attempt_id);
      const h = getHist();
      h.unshift({
        ts: r.ts || new Date().toISOString(),
        code: r.code || item.code,
        ok: !!r.ok,
        status: r.status,
        detail: r.reason_text || "",
        replayed: !!r.replayed,
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

// 离线重连：按版本号顺序补齐撤销 / 核销 / 过期结果
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
        lines.push(`#${e.version} ${fmtTime(e.ts)} ${e.type}` +
          (e.ticket_code ? ` ${e.ticket_code}` : "") +
          (e.gate_id ? ` @${e.gate_id}` : "") +
          (e.reason ? ` reason=${e.reason}` : ""));
        setCursor(e.version);
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
