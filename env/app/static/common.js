// 公共：令牌存取、API 封装、提示、时间格式化
const TOKEN_KEY = "shortpass_admin_token";
const GATE_TOKEN_KEY = "shortpass_gate_token";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("zh-CN", { hour12: false });
}
function badge(status) {
  return `<span class="badge ${esc(status)}">${esc(status)}</span>`;
}
function toast(msg, kind = "info") {
  const box = document.getElementById("toast");
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

async function api(method, path, body, token) {
  const opts = {
    method,
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) {
    const detail = data && data.detail ? data.detail : `HTTP ${r.status}`;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data || {};
}
