/* OSAT MES 前端 —— 純 JavaScript，無需建置流程。 */
"use strict";

const state = { token: localStorage.getItem("mes_token") || "", user: null };

/* ── API ─────────────────────────────────────────────── */
async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options };
  if (state.token) opts.headers.Authorization = `Bearer ${state.token}`;
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);

  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }

  if (res.status === 401) { logout(); throw new Error("登入逾期，請重新登入"); }
  if (!res.ok) {
    const msg = data?.message || data?.detail || `HTTP ${res.status}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ── 小工具 ──────────────────────────────────────────── */
const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, html = "") => {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
  if (html) node.innerHTML = html;
  return node;
};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => (v ?? 0).toLocaleString("zh-TW");
const pct = (v) => `${((v ?? 0) * 100).toFixed(2)}%`;
const when = (v) => (v ? new Date(v).toLocaleString("zh-TW", { hour12: false }) : "-");
const hhmm = (sec) => {
  if (!sec) return "0 分";
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h} 時 ${m} 分` : `${m} 分`;
};

function toast(msg, kind = "") {
  const node = el("div", { class: `toast ${kind}` }, esc(msg));
  $("#toast").appendChild(node);
  setTimeout(() => node.remove(), 5200);
}

/** 依表頭與列資料產生表格。 */
function renderTable(target, columns, rows, emptyText = "查無資料") {
  const table = typeof target === "string" ? $(target) : target;
  table.innerHTML = "";
  if (!rows || rows.length === 0) {
    table.innerHTML = `<tbody><tr><td class="empty">${esc(emptyText)}</td></tr></tbody>`;
    return;
  }
  const thead = el("thead");
  thead.appendChild(el("tr", {}, columns.map((c) =>
    `<th class="${c.num ? "num" : ""}">${esc(c.title)}</th>`).join("")));
  const tbody = el("tbody");
  rows.forEach((row) => {
    const tr = el("tr");
    columns.forEach((c) => {
      const td = el("td", c.num ? { class: "num" } : {});
      td.innerHTML = c.render ? c.render(row) : esc(row[c.key]);
      tr.appendChild(td);
    });
    if (columns.onClick) tr.style.cursor = "pointer";
    tbody.appendChild(tr);
  });
  table.append(thead, tbody);
}

const barCell = (ratio, kind = "") =>
  `<div style="display:flex;align-items:center;gap:8px;justify-content:flex-end">
     <span>${pct(ratio)}</span>
     <div class="bar ${kind}" style="width:70px"><i style="width:${Math.min(100, (ratio || 0) * 100).toFixed(1)}%"></i></div>
   </div>`;

/** 單站良率的門檻。 */
const yieldKind = (y) => (y >= 0.98 ? "ok" : y >= 0.95 ? "warn" : "bad");
/** 累計良率跨十幾站連乘，門檻自然低得多，另訂一組。 */
const cumulativeKind = (y) => (y >= 0.9 ? "ok" : y >= 0.85 ? "warn" : "bad");
/** 晶圓 CP 良率是晶圓廠的成績，九成多屬正常，不能套封裝站的門檻。 */
const cpYieldKind = (y) => (y >= 0.95 ? "ok" : y >= 0.9 ? "warn" : "bad");

/* ── 登入 ────────────────────────────────────────────── */
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: { username: $("#u").value.trim(), password: $("#p").value },
    });
    state.token = data.access_token;
    state.user = data.user;
    localStorage.setItem("mes_token", state.token);
    enterApp();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#logout").addEventListener("click", logout);

function logout() {
  stopLiveDashboard();
  state.token = "";
  state.user = null;
  localStorage.removeItem("mes_token");
  $("#app").style.display = "none";
  $("#login").style.display = "grid";
}

async function enterApp() {
  $("#login").style.display = "none";
  $("#app").style.display = "block";
  $("#who").textContent = `${state.user.full_name || state.user.username}（${state.user.roles.join(", ")}）`;
  loadDashboard();
}

/* ── 主題（淺色為預設，夜班可切深色）───────────────────── */
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("mes_theme", theme);
  $("#theme-toggle").textContent = theme === "dark" ? "淺色" : "深色";
}
applyTheme(localStorage.getItem("mes_theme") || "light");
$("#theme-toggle").addEventListener("click", () => {
  applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
});

/* ── 導覽 ────────────────────────────────────────────── */
document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $(`#view-${btn.dataset.view}`).classList.add("active");
    const loaders = {
      dashboard: loadDashboard, dispatch: loadDispatch, lots: loadLots,
      shopfloor: loadMeasurementItems, spc: loadSpc, equipment: loadOee,
      tools: loadTools, quality: loadQuality, orders: loadWorkOrders,
      handover: loadHandover, wafermap: loadWaferMaps, sop: loadSops,
      erp: loadErp, secs: loadSecs, sampling: loadSampling,
      recipes: loadRecipes, rma: loadRma,
    };
    loaders[btn.dataset.view]?.();
  });
});

/* ── 戰情看板 ────────────────────────────────────────── */
$("#dash-refresh").addEventListener("click", loadDashboard);
$("#dash-hours").addEventListener("change", () => {
  loadDashboard();
  if (liveStream) startLiveDashboard();  // 換區間要重開推播
});

/** 掛在牆上的看板應該要自己動，而不是等人去按重新整理。 */
let liveStream = null;

$("#dash-live").addEventListener("change", (event) => {
  if (event.target.checked) startLiveDashboard();
  else stopLiveDashboard();
});

function stopLiveDashboard() {
  if (liveStream) { liveStream.abort(); liveStream = null; }
}

async function startLiveDashboard() {
  stopLiveDashboard();
  const hours = $("#dash-hours").value;
  const controller = new AbortController();
  liveStream = controller;
  try {
    // EventSource 帶不了 Authorization 標頭，因此自己解析 SSE 串流
    const res = await fetch(`/api/reports/dashboard/stream?hours=${hours}&interval=15`, {
      headers: { Authorization: `Bearer ${state.token}` },
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`自動更新失敗（HTTP ${res.status}）`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop();
      chunks.forEach((chunk) => {
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) return;
        try { renderDashboard(JSON.parse(line.slice(6)), hours); } catch { /* 忽略壞掉的單筆 */ }
      });
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      toast(err.message, "err");
      $("#dash-live").checked = false;
      liveStream = null;
    }
  }
}

async function loadDashboard() {
  try {
    const hours = $("#dash-hours").value;
    renderDashboard(await api(`/api/reports/dashboard?hours=${hours}`), hours);
  } catch (err) { toast(err.message, "err"); }
}

function renderDashboard(d, hours) {
  try {
    $("#dash-time").textContent =
      `資料時間 ${when(d.generated_at)}${liveStream ? "（自動更新中）" : ""}`;

    const k = d.kpi;
    const cards = [
      { label: "在製批號", value: num(k.wip_lots), foot: `${num(k.wip_qty)} 顆/片` },
      { label: "在線工單", value: num(k.open_work_orders), foot: "已下達 / 生產中" },
      { label: "扣留中批號", value: num(k.lots_on_hold), foot: "待品保判定", kind: k.lots_on_hold > 0 ? "bad" : "ok" },
      { label: "產出移動數", value: num(k.moves), foot: `最近 ${hours} 小時` },
      { label: "良品產出", value: num(k.output_good), foot: `不良 ${num(k.output_reject)}` },
      { label: "整體良率", value: pct(k.overall_yield), foot: "各站加權", kind: yieldKind(k.overall_yield) },
      {
        label: "累計良率（最差流程）",
        value: k.worst_route_yield == null ? "—" : pct(k.worst_route_yield),
        foot: d.final_yield_by_route?.[0]?.route_code || "尚無資料",
        kind: k.worst_route_yield == null ? "" : cumulativeKind(k.worst_route_yield),
      },
      { label: "設備可用率", value: pct(k.equipment_uptime_ratio), foot: "E10 Uptime", kind: yieldKind(k.equipment_uptime_ratio) },
      { label: "Q-Time 逾時", value: num(k.qtime_violations), foot: "需品保放行", kind: k.qtime_violations > 0 ? "warn" : "ok" },
      { label: "Q-Time 待處理", value: num(k.qtime_at_risk), foot: "即將或已逾時", kind: k.qtime_at_risk > 0 ? "bad" : "ok" },
      { label: "SPC 判異", value: num(k.spc_violations), foot: "製程異常訊號", kind: k.spc_violations > 0 ? "warn" : "ok" },
      { label: "治具待更換", value: num(k.tools_to_change), foot: "到期或接近壽命", kind: k.tools_to_change > 0 ? "warn" : "ok" },
    ];
    $("#kpis").innerHTML = cards.map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${c.value}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    const maxWip = Math.max(1, ...d.wip_by_operation.map((r) => r.qty));
    renderTable("#t-wip", [
      { title: "站序", key: "seq", num: true },
      { title: "站別", render: (r) => `${esc(r.op_code)}<br><span class="muted" style="font-size:12px">${esc(r.op_name)}</span>` },
      { title: "批數", key: "lots", num: true },
      { title: "數量", num: true, render: (r) => num(r.qty) },
      { title: "分佈", render: (r) => `<div class="bar" style="width:100px"><i style="width:${(r.qty / maxWip * 100).toFixed(1)}%"></i></div>` },
      { title: "待/跑/扣", num: true, render: (r) => `${r.waiting}/${r.running}/${r.hold}` },
    ], d.wip_by_operation, "目前沒有在製批號");

    renderTable("#t-yield", [
      { title: "站別", key: "op_code" },
      { title: "應產出", num: true, render: (r) => num(r.qty_expected) },
      { title: "良品", num: true, render: (r) => num(r.qty_good) },
      { title: "站良率", num: true, render: (r) => barCell(r.step_yield, yieldKind(r.step_yield)) },
      {
        title: "累計良率", num: true,
        render: (r) => (r.cumulative_yield == null
          ? '<span class="muted" title="多條流程混合，累計良率不可直接連乘">—</span>'
          : pct(r.cumulative_yield)),
      },
    ], d.yield_by_operation, "此區間尚無出站紀錄");

    renderTable("#t-route-yield", [
      { title: "製程流程", key: "route_code" },
      { title: "站數", key: "steps", num: true },
      { title: "累計良率", num: true, render: (r) => barCell(r.final_yield, cumulativeKind(r.final_yield)) },
    ], d.final_yield_by_route, "此區間尚無出站紀錄");

    renderTable("#t-pareto", [
      { title: "不良代碼", render: (r) => `${esc(r.defect_code)}<br><span class="muted" style="font-size:12px">${esc(r.defect_name)}</span>` },
      { title: "數量", num: true, render: (r) => num(r.qty) },
      { title: "佔比", num: true, render: (r) => barCell(r.ratio, "bad") },
      { title: "累計", num: true, render: (r) => pct(r.cumulative_ratio) },
    ], d.defect_pareto, "此區間沒有不良紀錄");

    const states = d.equipment_states.by_state || {};
    $("#eq-states").innerHTML = Object.entries(states).map(([name, info]) => `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">
        <span class="tag ${esc(name)}" style="min-width:130px;text-align:center">${esc(stateLabel(name))}</span>
        <div class="bar ${name === "UNSCHEDULED_DOWN" ? "bad" : name === "PRODUCTIVE" ? "ok" : ""}" style="flex:1">
          <i style="width:${(info.count / Math.max(1, d.equipment_states.total) * 100).toFixed(1)}%"></i>
        </div>
        <b style="min-width:32px;text-align:right">${info.count}</b>
      </div>`).join("") || '<div class="empty">尚無設備資料</div>';

    renderTable("#t-moves", [
      { title: "班別", key: "key" },
      { title: "移動數", key: "moves", num: true },
      { title: "良品", num: true, render: (r) => num(r.qty_good) },
      { title: "良率", num: true, render: (r) => pct(r.yield) },
    ], d.throughput_by_shift, "此區間尚無產出");

    renderTable("#t-qtime", QTIME_COLUMNS, d.qtime_watch, "目前沒有 Q-Time 風險");

    renderTable("#t-spc-alert", [
      { title: "量測項目", render: (r) => `${esc(r.item_code)} <span class="muted">${esc(r.item_name)}</span>` },
      { title: "站別", key: "op_code" },
      { title: "判異次數", key: "violations", num: true },
      { title: "影響批號", num: true, render: (r) => r.affected_lots.length },
    ], d.spc_violations, "此區間沒有 SPC 異常");

    renderTable("#t-tool-alert", TOOL_ALERT_COLUMNS, d.tools_to_change, "目前沒有治具需要更換");
  } catch (err) {
    toast(err.message, "err");
  }
}

const QTIME_COLUMNS = [
  { title: "批號", key: "lot_id" },
  { title: "站別", render: (r) => `${r.seq} ${esc(r.op_code)}` },
  {
    title: "剩餘", num: true,
    render: (r) => (r.qtime_expired
      ? `<span class="tag CRITICAL">逾時 ${Math.abs(r.qtime_remaining_min).toFixed(0)} 分</span>`
      : `<span class="tag URGENT">${r.qtime_remaining_min.toFixed(0)} 分</span>`),
  },
  { title: "已等待", num: true, render: (r) => hhmm(r.waiting_minutes * 60) },
  { title: "可用機台", render: (r) => esc(r.available_equipments.join(", ") || "無") },
];

const TOOL_ALERT_COLUMNS = [
  { title: "治具", render: (r) => `${esc(r.tool_id)} <span class="muted">${esc(r.tool_type)}</span>` },
  { title: "設備", render: (r) => esc(r.eq_id || "-") },
  { title: "使用", num: true, render: (r) => `${num(r.used_count)} / ${num(r.life_limit)}` },
  { title: "使用率", num: true, render: (r) => barCell(r.usage_ratio, r.usage_ratio >= 1 ? "bad" : "warn") },
  { title: "狀態", render: (r) => `<span class="tag ${esc(r.status)}">${esc(TOOL_STATUS_LABELS[r.status] || r.status)}</span>` },
];

const TOOL_STATUS_LABELS = {
  IDLE: "在庫", MOUNTED: "已上機", EXPIRED: "壽命到期", SCRAPPED: "已報廢",
};
const URGENCY_LABELS = {
  CRITICAL: "已逾時", URGENT: "緊急", HIGH: "優先", BLOCKED: "待機台", NORMAL: "一般",
};

const STATE_LABELS = {
  PRODUCTIVE: "生產中", STANDBY: "待機", ENGINEERING: "工程試作",
  SCHEDULED_DOWN: "計畫停機", UNSCHEDULED_DOWN: "非計畫停機", NON_SCHEDULED: "非排程",
};
const stateLabel = (s) => STATE_LABELS[s] || s;

const LOT_STATUS_LABELS = {
  WAITING: "待進站", RUNNING: "加工中", HOLD: "扣留中", COMPLETED: "已完工",
  SHIPPED: "已出貨", SCRAPPED: "已報廢", MERGED: "已併批", SPLIT: "已拆批",
};

/* ── 在製批號 ────────────────────────────────────────── */
$("#lot-search").addEventListener("click", loadLots);
$("#lot-q").addEventListener("keydown", (e) => e.key === "Enter" && loadLots());

async function loadLots() {
  try {
    const lotId = $("#lot-q").value.trim();
    if (lotId) {
      const lot = await api(`/api/lots/${encodeURIComponent(lotId)}`);
      renderLotTable([lot]);
      showLotDetail(lotId);
      return;
    }
    const params = new URLSearchParams({ limit: "100" });
    if ($("#lot-status").value) params.set("status", $("#lot-status").value);
    if ($("#lot-op").value.trim()) params.set("op_code", $("#lot-op").value.trim());
    if ($("#lot-device").value.trim()) params.set("device_id", $("#lot-device").value.trim());
    const data = await api(`/api/lots?${params}`);
    $("#lot-count").textContent = `共 ${data.total} 批`;
    renderLotTable(data.items);
  } catch (err) {
    toast(err.message, "err");
  }
}

function renderLotTable(items) {
  renderTable("#t-lots", [
    { title: "批號", render: (r) => `<a href="#" data-lot="${esc(r.lot_id)}" style="color:var(--accent)">${esc(r.lot_id)}</a>` },
    { title: "料號", key: "device_id" },
    { title: "站別", render: (r) => `${r.current_seq} ${esc(r.current_op)}` },
    { title: "狀態", render: (r) => `<span class="tag ${esc(r.status)}">${esc(LOT_STATUS_LABELS[r.status] || r.status)}</span>` },
    { title: "數量", num: true, render: (r) => `${num(r.qty)} <span class="muted">${esc(r.unit_type)}</span>` },
    { title: "設備", render: (r) => esc(r.eq_id || "-") },
  ], items, "查無批號");

  document.querySelectorAll("#t-lots a[data-lot]").forEach((a) => {
    a.addEventListener("click", (e) => { e.preventDefault(); showLotDetail(a.dataset.lot); });
  });
}

async function showLotDetail(lotId) {
  try {
    const [lot, history] = await Promise.all([
      api(`/api/lots/${encodeURIComponent(lotId)}`),
      api(`/api/lots/${encodeURIComponent(lotId)}/history`),
    ]);
    $("#lot-detail-id").textContent = lotId;
    const info = `
      <div class="form-grid" style="margin-bottom:14px">
        <div><div class="muted">產品料號</div><b>${esc(lot.device_id)}</b></div>
        <div><div class="muted">工單</div><b>${esc(lot.wo_no)}</b></div>
        <div><div class="muted">目前站別</div><b>${lot.current_seq} ${esc(lot.current_op)}</b></div>
        <div><div class="muted">狀態</div><span class="tag ${esc(lot.status)}">${esc(LOT_STATUS_LABELS[lot.status] || lot.status)}</span></div>
        <div><div class="muted">現有量</div><b>${num(lot.qty)} ${esc(lot.unit_type)}</b></div>
        <div><div class="muted">累計不良</div><b>${num(lot.scrap_qty)}</b></div>
        <div><div class="muted">投入量</div><b>${num(lot.initial_qty)} 片</b></div>
        <div><div class="muted">Q-Time 逾時</div><b>${num(lot.qtime_violations)} 次</b></div>
      </div>`;

    const items = history.map((h) => {
      const detail = h.action === "TRACK_OUT"
        ? `應產出 ${num(h.qty_expected)}｜良品 ${num(h.qty_good)}｜不良 ${num(h.qty_reject)}｜良率 ${pct(h.step_yield)}｜加工 ${hhmm(h.process_sec)}`
        : h.action === "TRACK_IN"
          ? `設備 ${esc(h.eq_id || "-")}｜等待 ${hhmm(h.queue_sec)}${h.qtime_violation ? ' <span class="tag HOLD">Q-Time 逾時</span>' : ""}`
          : esc(h.remark || "");
      return `<div class="item">
        <div class="when">${when(h.timestamp)}｜${esc(h.shift || "")}｜${esc(h.operator || "")}</div>
        <div><b>${esc(h.op_code || "")} · ${esc(h.action)}</b></div>
        <div class="muted">${detail}</div>
      </div>`;
    }).join("");

    $("#lot-detail").innerHTML = info + (items ? `<div class="timeline">${items}</div>` : '<div class="empty">尚無履歷</div>');
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ── 現場作業 ────────────────────────────────────────── */
const showResult = (data) => { $("#shopfloor-out").textContent = JSON.stringify(data, null, 2); };

$("#btn-track-in").addEventListener("click", async () => {
  try {
    const data = await api("/api/lots/track-in", {
      method: "POST",
      body: { lot_id: $("#ti-lot").value.trim(), eq_id: $("#ti-eq").value.trim() },
    });
    showResult(data);
    toast(`批號 ${data.lot_id} 已於 ${data.current_op} 進站`, "ok");
  } catch (err) { showResult({ error: err.message }); toast(err.message, "err"); }
});

$("#btn-track-out").addEventListener("click", async () => {
  try {
    const reject = Number($("#to-reject").value || 0);
    const body = {
      lot_id: $("#to-lot").value.trim(),
      good_qty: Number($("#to-good").value || 0),
      reject_qty: reject,
      defects: reject > 0 && $("#to-defect").value.trim()
        ? [{ defect_code: $("#to-defect").value.trim(), qty: reject }] : [],
    };
    const data = await api("/api/lots/track-out", { method: "POST", body });
    showResult(data);
    toast(`批號 ${data.lot_id} 出站完成，站良率 ${pct(data.last_step.step_yield)}`, "ok");
  } catch (err) { showResult({ error: err.message }); toast(err.message, "err"); }
});

$("#btn-hold").addEventListener("click", async () => {
  try {
    const data = await api("/api/lots/hold", {
      method: "POST",
      body: { lot_id: $("#hd-lot").value.trim(), reason: $("#hd-reason").value, remark: $("#hd-remark").value },
    });
    showResult(data); toast(`批號 ${data.lot_id} 已扣留`, "ok");
  } catch (err) { showResult({ error: err.message }); toast(err.message, "err"); }
});

$("#btn-release").addEventListener("click", async () => {
  try {
    const data = await api("/api/lots/release", {
      method: "POST",
      body: { lot_id: $("#hd-lot").value.trim(), remark: $("#hd-remark").value },
    });
    showResult(data); toast(`批號 ${data.lot_id} 已放行`, "ok");
  } catch (err) { showResult({ error: err.message }); toast(err.message, "err"); }
});

/* ── 設備 OEE ────────────────────────────────────────── */
$("#eq-refresh").addEventListener("click", loadOee);

async function loadOee() {
  try {
    const hours = $("#oee-hours").value;
    const [rows, list] = await Promise.all([
      api(`/api/equipments/oee?hours=${hours}`),
      api("/api/equipments?limit=200"),
    ]);
    const byId = Object.fromEntries(list.items.map((e) => [e.eq_id, e]));
    renderTable("#t-oee", [
      { title: "設備", render: (r) => `${esc(r.eq_id)}<br><span class="muted" style="font-size:12px">${esc(r.name)}</span>` },
      { title: "目前狀態", render: (r) => {
          const s = byId[r.eq_id]?.current_state || "-";
          return `<span class="tag ${esc(s)}">${esc(stateLabel(s))}</span>`;
        } },
      { title: "生產時間", num: true, render: (r) => hhmm(r.productive_sec) },
      { title: "產出", num: true, render: (r) => num(r.units_processed) },
      { title: "稼動率", num: true, render: (r) => barCell(r.availability) },
      { title: "效能", num: true, render: (r) => barCell(r.performance) },
      { title: "良率", num: true, render: (r) => barCell(r.quality, yieldKind(r.quality)) },
      { title: "OEE", num: true, render: (r) => barCell(r.oee, r.oee >= 0.6 ? "ok" : r.oee >= 0.35 ? "warn" : "bad") },
    ], rows, "尚無設備資料");
  } catch (err) { toast(err.message, "err"); }
}

/* ── 品質 ────────────────────────────────────────────── */
async function loadQuality() {
  try {
    const [holds, bins, defects] = await Promise.all([
      api("/api/quality/holds?status=OPEN"),
      api("/api/quality/bins?hours=168"),
      api("/api/quality/defects?limit=200"),
    ]);
    renderTable("#t-holds", [
      { title: "批號", key: "lot_id" },
      { title: "站別", key: "op_code" },
      { title: "原因", key: "reason" },
      { title: "說明", render: (r) => esc(r.remark || "-") },
      { title: "扣留時間", render: (r) => when(r.held_at) },
      { title: "已扣留", num: true, render: (r) => `${r.hold_hours} 小時` },
    ], holds, "目前沒有扣留中的批號");

    renderTable("#t-bins", [
      { title: "Bin", render: (r) => `Bin ${esc(r.bin)}${r.bin === "1" ? "（良品）" : ""}` },
      { title: "數量", num: true, render: (r) => num(r.qty) },
      { title: "佔比", num: true, render: (r) => barCell(r.ratio, r.bin === "1" ? "ok" : "bad") },
    ], bins.bins, "此區間尚無測試資料");

    renderTable("#t-defects", [
      { title: "時間", render: (r) => when(r.timestamp) },
      { title: "批號", key: "lot_id" },
      { title: "站別", key: "op_code" },
      { title: "不良代碼", render: (r) => `${esc(r.defect_code)} <span class="muted">${esc(r.defect_name)}</span>` },
      { title: "數量", num: true, render: (r) => num(r.qty) },
      { title: "處置", key: "disposition" },
      { title: "設備", render: (r) => esc(r.eq_id || "-") },
    ], defects, "尚無不良紀錄");
  } catch (err) { toast(err.message, "err"); }
}

/* ── 追溯 ────────────────────────────────────────────── */
$("#trace-go").addEventListener("click", loadTrace);
$("#trace-key").addEventListener("keydown", (e) => e.key === "Enter" && loadTrace());

async function loadTrace() {
  const key = $("#trace-key").value.trim();
  if (!key) return toast("請輸入查詢條件", "err");
  const type = $("#trace-type").value;
  const urls = {
    backward: `/api/trace/lots/${encodeURIComponent(key)}/backward`,
    forward: `/api/trace/wafers/${encodeURIComponent(key)}/forward`,
    genealogy: `/api/trace/lots/${encodeURIComponent(key)}/genealogy`,
    "where-used": `/api/trace/materials/${encodeURIComponent(key)}/where-used`,
  };
  try {
    const data = await api(urls[type]);
    const summary = {
      backward: () => `來源晶圓 ${data.source_wafers.length} 片｜加工紀錄 ${data.process_history.length} 筆｜使用設備 ${data.equipments_used.length} 台｜作業員 ${data.operators_involved.length} 人`,
      forward: () => `影響 ${data.impacted_lot_count} 個批號｜出貨單 ${data.shipments.length} 張｜客戶 ${data.shipped_customers.join(", ") || "無"}`,
      genealogy: () => `上游 ${data.ancestors.length} 批｜下游 ${data.descendants.length} 批`,
      "where-used": () => `影響 ${data.impacted_lot_count} 個批號｜異動 ${data.transactions.length} 筆`,
    }[type]();
    $("#trace-summary").textContent = summary;
    $("#trace-out").textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    $("#trace-summary").textContent = "";
    $("#trace-out").textContent = err.message;
    toast(err.message, "err");
  }
}

/* ── 工單 ────────────────────────────────────────────── */
$("#wo-refresh").addEventListener("click", loadWorkOrders);

async function loadWorkOrders() {
  try {
    const params = new URLSearchParams({ limit: "100" });
    if ($("#wo-status").value) params.set("status", $("#wo-status").value);
    const data = await api(`/api/work-orders?${params}`);
    renderTable("#t-wos", [
      { title: "工單", key: "wo_no" },
      { title: "料號", key: "device_id" },
      { title: "客戶", key: "customer_code" },
      { title: "狀態", render: (r) => `<span class="tag">${esc(r.status)}</span>` },
      { title: "計畫量", num: true, render: (r) => num(r.plan_qty) },
      { title: "已投料", num: true, render: (r) => num(r.released_qty) },
      { title: "投料率", num: true, render: (r) => barCell((r.released_qty || 0) / Math.max(1, r.plan_qty)) },
      { title: "批數", key: "lot_count", num: true },
      { title: "交期", render: (r) => when(r.due_date) },
      { title: "優先", key: "priority", num: true },
    ], data.items, "查無工單");
  } catch (err) { toast(err.message, "err"); }
}

/* ── 派工看板 ────────────────────────────────────────── */
$("#dispatch-refresh").addEventListener("click", loadDispatch);
$("#dispatch-op").addEventListener("keydown", (e) => e.key === "Enter" && loadDispatch());

async function loadDispatch() {
  try {
    const op = $("#dispatch-op").value.trim();
    const data = await api(`/api/dispatch?limit=200${op ? `&op_code=${encodeURIComponent(op)}` : ""}`);
    $("#dispatch-note").textContent = `共 ${data.total} 批待進站${data.truncated ? "（僅顯示前 200 筆）" : ""}`;
    data.items.forEach((row, index) => { row.rank = index + 1; });
    renderTable("#t-dispatch", [
      { title: "順序", key: "rank", num: true },
      { title: "緊急度", render: (r) => `<span class="tag ${esc(r.urgency)}">${esc(URGENCY_LABELS[r.urgency] || r.urgency)}</span>` },
      { title: "排序理由", render: (r) => esc(r.reason) },
      { title: "批號", key: "lot_id" },
      { title: "料號", key: "device_id" },
      { title: "站別", render: (r) => `${r.seq} ${esc(r.op_code)}<br><span class="muted" style="font-size:12px">${esc(r.op_name)}</span>` },
      { title: "數量", num: true, render: (r) => `${num(r.qty)} <span class="muted">${esc(r.unit_type)}</span>` },
      { title: "優先序", key: "priority", num: true },
      { title: "已等待", num: true, render: (r) => hhmm(r.waiting_minutes * 60) },
      {
        title: "Q-Time", num: true,
        render: (r) => (r.qtime_remaining_min == null
          ? '<span class="muted">不管制</span>'
          : `${r.qtime_remaining_min.toFixed(0)} / ${r.qtime_limit_min} 分`),
      },
      { title: "交期", num: true, render: (r) => (r.days_to_due == null ? "-" : `${r.days_to_due.toFixed(1)} 天`) },
      { title: "可用機台", render: (r) => esc(r.available_equipments.join(", ") || "—") },
    ], data.items, "目前沒有待進站的批號");
  } catch (err) { toast(err.message, "err"); }
}

/* ── SPC ─────────────────────────────────────────────── */
let measurementItems = [];

async function loadMeasurementItems() {
  if (measurementItems.length) return measurementItems;
  try {
    const data = await api("/api/spc/items?active=true");
    measurementItems = data.items;
    const options = measurementItems
      .map((i) => `<option value="${esc(i.item_code)}">${esc(i.item_code)}｜${esc(i.name)}（${esc(i.op_code)}）</option>`)
      .join("");
    $("#ms-item").innerHTML = options;
    $("#spc-item").innerHTML = options;
  } catch (err) { toast(err.message, "err"); }
  return measurementItems;
}

$("#btn-measure").addEventListener("click", async () => {
  try {
    const values = $("#ms-values").value.split(",").map((v) => Number(v.trim())).filter((v) => !Number.isNaN(v));
    const data = await api("/api/spc/measurements", {
      method: "POST",
      body: { item_code: $("#ms-item").value, lot_id: $("#ms-lot").value.trim(), values },
    });
    showResult(data);
    if (data.violations.length) {
      toast(`判異：${data.violations.join(", ")}${data.lot_held ? "，批號已自動扣留" : ""}`, "err");
    } else {
      toast(`量測已記錄，平均 ${data.mean}`, "ok");
    }
  } catch (err) { showResult({ error: err.message }); toast(err.message, "err"); }
});

$("#spc-refresh").addEventListener("click", loadSpc);
$("#spc-item").addEventListener("change", loadSpc);

async function loadSpc() {
  await loadMeasurementItems();
  const item = $("#spc-item").value;
  if (!item) return;
  const hours = $("#spc-hours").value;
  try {
    const [chart, cap, measurements, violations] = await Promise.all([
      api(`/api/spc/items/${encodeURIComponent(item)}/chart?hours=${hours}`),
      api(`/api/spc/items/${encodeURIComponent(item)}/capability?hours=${hours}`),
      api(`/api/spc/measurements?item_code=${encodeURIComponent(item)}&limit=100`),
      api(`/api/spc/violations?hours=${hours}`),
    ]);

    const capKind = (v) => (v == null ? "" : v >= 1.33 ? "ok" : v >= 1.0 ? "warn" : "bad");
    $("#spc-kpis").innerHTML = [
      { label: "量測筆數", value: num(cap.sample_count), foot: `${num(cap.subgroup_count)} 組子群` },
      { label: "平均值", value: cap.mean ?? "—", foot: `規格 ${cap.lsl ?? "—"} ~ ${cap.usl ?? "—"} ${cap.unit}` },
      { label: "標準差 σ", value: cap.stdev ?? "—", foot: "個別值" },
      { label: "Cp", value: cap.cp ?? "—", foot: "只看變異", kind: capKind(cap.cp) },
      { label: "Cpk", value: cap.cpk ?? "—", foot: cap.grade, kind: capKind(cap.cpk) },
      { label: "超規筆數", value: num(cap.out_of_spec_count), foot: "超出規格上下限", kind: cap.out_of_spec_count > 0 ? "bad" : "ok" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot || "")}</div>
      </div>`).join("");

    $("#spc-chart-note").textContent = chart.control_limits
      ? `管制界限來源：${chart.limits_source}｜${chart.points.length} 組子群`
      : (chart.note || "");
    drawControlChart($("#spc-chart"), chart);

    renderTable("#t-measurements", [
      { title: "時間", render: (r) => when(r.timestamp) },
      { title: "批號", key: "lot_id" },
      { title: "設備", render: (r) => esc(r.eq_id || "-") },
      { title: "平均", key: "mean", num: true },
      { title: "全距", key: "range", num: true },
      {
        title: "判定", render: (r) => (r.violations?.length
          ? `<span class="tag CRITICAL">${esc(r.violations.join(", "))}</span>`
          : '<span class="tag RUNNING">正常</span>'),
      },
    ], measurements, "尚無量測資料");

    renderTable("#t-violations", [
      { title: "量測項目", render: (r) => `${esc(r.item_code)} <span class="muted">${esc(r.item_name)}</span>` },
      { title: "站別", key: "op_code" },
      { title: "判異次數", key: "violations", num: true },
      { title: "影響批號", render: (r) => esc(r.affected_lots.slice(0, 4).join(", ")) + (r.affected_lots.length > 4 ? " …" : "") },
    ], violations, "此區間沒有判異");
  } catch (err) { toast(err.message, "err"); }
}

/** 以原生 SVG 畫 X-bar 管制圖，不依賴任何圖表函式庫。 */
function drawControlChart(svg, data) {
  const W = 900, H = 260, padL = 58, padR = 14, padT = 14, padB = 26;
  svg.innerHTML = "";
  const points = data.points || [];
  if (!points.length) {
    svg.innerHTML = `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">此區間沒有量測資料</text>`;
    return;
  }

  const limits = data.control_limits || {};
  const spec = data.item || {};
  const candidates = points.map((p) => p.mean).concat(
    [limits.x_ucl, limits.x_lcl, limits.x_bar_bar, spec.usl, spec.lsl].filter((v) => v != null)
  );
  let lo = Math.min(...candidates), hi = Math.max(...candidates);
  const margin = (hi - lo || 1) * 0.12;
  lo -= margin; hi += margin;

  const x = (i) => padL + (points.length === 1 ? (W - padL - padR) / 2
    : (i * (W - padL - padR)) / (points.length - 1));
  const y = (v) => padT + (H - padT - padB) * (1 - (v - lo) / (hi - lo));
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs, text) => {
    const node = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
    if (text != null) node.textContent = text;
    svg.appendChild(node);
    return node;
  };

  // Y 軸刻度
  for (let i = 0; i <= 4; i++) {
    const value = lo + ((hi - lo) * i) / 4;
    add("line", { class: "grid-line", x1: padL, x2: W - padR, y1: y(value), y2: y(value) });
    add("text", { x: padL - 6, y: y(value) + 3, "text-anchor": "end" }, value.toFixed(2));
  }
  add("line", { class: "axis", x1: padL, x2: padL, y1: padT, y2: H - padB });
  add("line", { class: "axis", x1: padL, x2: W - padR, y1: H - padB, y2: H - padB });

  const rule = (value, cls, label) => {
    if (value == null || value < lo || value > hi) return;
    add("line", { class: cls, x1: padL, x2: W - padR, y1: y(value), y2: y(value) });
    add("text", { x: W - padR - 2, y: y(value) - 3, "text-anchor": "end" }, label);
  };
  rule(spec.usl, "spec", `USL ${spec.usl}`);
  rule(spec.lsl, "spec", `LSL ${spec.lsl}`);
  rule(limits.x_ucl, "limit", `UCL ${limits.x_ucl?.toFixed(3)}`);
  rule(limits.x_lcl, "limit", `LCL ${limits.x_lcl?.toFixed(3)}`);
  rule(limits.x_bar_bar, "center", `CL ${limits.x_bar_bar?.toFixed(3)}`);

  add("polyline", { class: "series", points: points.map((p, i) => `${x(i)},${y(p.mean)}`).join(" ") });
  points.forEach((p, i) => {
    const bad = p.out_of_spec || (p.violations && p.violations.length);
    const dot = add("circle", { class: `dot ${bad ? "bad" : ""}`, cx: x(i), cy: y(p.mean), r: bad ? 4.5 : 3 });
    const tip = document.createElementNS(ns, "title");
    tip.textContent = `${when(p.timestamp)}\n${p.lot_id}｜${p.eq_id || "-"}\n平均 ${p.mean}｜全距 ${p.range}`
      + (bad ? `\n異常：${(p.violations || []).join(", ") || "超規"}` : "");
    dot.appendChild(tip);
  });
}

/* ── 治具 ────────────────────────────────────────────── */
$("#tool-refresh").addEventListener("click", loadTools);

async function loadTools() {
  try {
    const status = $("#tool-status").value;
    const [tools, attention] = await Promise.all([
      api(`/api/tools?limit=300${status ? `&status=${status}` : ""}`),
      api("/api/tools/attention"),
    ]);
    renderTable("#t-tool-attention", TOOL_ALERT_COLUMNS, attention, "目前沒有治具需要更換");
    renderTable("#t-tools", [
      { title: "治具", render: (r) => `${esc(r.tool_id)}<br><span class="muted" style="font-size:12px">${esc(r.name)}</span>` },
      { title: "類型", key: "tool_type" },
      { title: "適用站別", render: (r) => esc((r.op_codes || []).join(", ")) },
      { title: "設備", render: (r) => esc(r.eq_id || "-") },
      { title: "狀態", render: (r) => `<span class="tag ${esc(r.status)}">${esc(TOOL_STATUS_LABELS[r.status] || r.status)}</span>` },
      { title: "已用 / 壽命", num: true, render: (r) => `${num(r.used_count)} / ${num(r.life_limit)}` },
      { title: "剩餘", num: true, render: (r) => num(r.remaining) },
      { title: "使用率", num: true, render: (r) => barCell(r.usage_ratio, r.usage_ratio >= 1 ? "bad" : r.usage_ratio >= 0.85 ? "warn" : "ok") },
      { title: "上機次數", key: "mount_count", num: true },
    ], tools, "尚無治具資料");
  } catch (err) { toast(err.message, "err"); }
}

$("#btn-tool-replace").addEventListener("click", async () => {
  try {
    const params = new URLSearchParams({
      old_tool_id: $("#tool-old").value.trim(),
      new_tool_id: $("#tool-new").value.trim(),
    });
    const data = await api(`/api/tools/replace?${params}`, { method: "POST" });
    $("#tool-out").textContent = JSON.stringify(data, null, 2);
    toast(`${data.equipment} 已換上 ${data.installed.tool_id}`, "ok");
    loadTools();
  } catch (err) { $("#tool-out").textContent = err.message; toast(err.message, "err"); }
});

/* ── 交接班 ──────────────────────────────────────────── */
$("#handover-refresh").addEventListener("click", loadHandover);

async function loadHandover() {
  try {
    const shift = $("#handover-shift").value.trim();
    const d = await api(`/api/reports/shift-handover${shift ? `?shift=${encodeURIComponent(shift)}` : ""}`);
    $("#handover-window").textContent = `${d.shift}｜${when(d.window.start)} ~ ${when(d.window.end)}`;

    const o = d.output;
    $("#handover-kpis").innerHTML = [
      { label: "移動數", value: num(o.moves), foot: `${num(o.lots_processed)} 個批號` },
      { label: "良品產出", value: num(o.qty_good), foot: `不良 ${num(o.qty_reject)}` },
      { label: "本班良率", value: pct(o.yield), foot: "良品 / 總產出", kind: yieldKind(o.yield) },
      { label: "新增扣留", value: num(d.new_holds.length), foot: "本班發生", kind: d.new_holds.length ? "warn" : "ok" },
      { label: "Q-Time 逾時", value: num(d.qtime_watch.expired), foot: `另有 ${d.qtime_watch.at_risk} 批接近`, kind: d.qtime_watch.expired ? "bad" : "ok" },
      { label: "治具待換", value: num(d.tools_to_change.length), foot: "交接注意", kind: d.tools_to_change.length ? "warn" : "ok" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    renderTable("#t-ho-holds", [
      { title: "批號", key: "lot_id" },
      { title: "站別", key: "op_code" },
      { title: "原因", key: "reason" },
      { title: "說明", render: (r) => esc(r.remark || "-") },
      { title: "狀態", render: (r) => `<span class="tag ${r.status === "OPEN" ? "HOLD" : "COMPLETED"}">${r.status === "OPEN" ? "未結" : "已放行"}</span>` },
      { title: "時間", render: (r) => when(r.held_at) },
    ], d.new_holds, "本班沒有新增扣留");

    renderTable("#t-ho-down", [
      { title: "設備", key: "eq_id" },
      { title: "類型", render: (r) => `<span class="tag ${esc(r.state)}">${esc(stateLabel(r.state))}</span>` },
      { title: "次數", key: "events", num: true },
      { title: "停機時間", num: true, render: (r) => `${r.minutes} 分` },
      { title: "原因", render: (r) => esc(r.reasons.join(", ") || "-") },
    ], d.equipment_downtime, "本班沒有停機");

    renderTable("#t-ho-next", [
      { title: "緊急度", render: (r) => `<span class="tag ${esc(r.urgency)}">${esc(URGENCY_LABELS[r.urgency] || r.urgency)}</span>` },
      { title: "批號", key: "lot_id" },
      { title: "站別", render: (r) => `${r.seq} ${esc(r.op_code)}` },
      { title: "理由", render: (r) => esc(r.reason) },
    ], d.next_up, "沒有待進站的批號");

    renderTable("#t-ho-spc", [
      { title: "量測項目", render: (r) => `${esc(r.item_code)} <span class="muted">${esc(r.item_name)}</span>` },
      { title: "站別", key: "op_code" },
      { title: "判異次數", key: "violations", num: true },
    ], d.spc_violations, "本班沒有 SPC 異常");

    renderTable("#t-ho-tools", TOOL_ALERT_COLUMNS, d.tools_to_change, "沒有治具需要更換");
  } catch (err) { toast(err.message, "err"); }
}

/* ── 晶圓 Map ────────────────────────────────────────── */
/** Bin 配色：良品綠、晶圓外留白，其餘依 Bin 編號循環取色。 */
const BIN_COLORS = ["#d94b4b", "#e08a2e", "#c9a227", "#8e5fd0", "#2e9bb5", "#b5548e", "#5f7fd0"];
const binColor = (bin, passBins) =>
  passBins.includes(bin) ? "#3f9a5a" : BIN_COLORS[Math.abs(bin) % BIN_COLORS.length];

$("#wm-refresh").addEventListener("click", loadWaferMaps);

async function loadWaferMaps() {
  try {
    const lot = $("#wm-lot").value.trim();
    const maps = await api(`/api/wafer/maps${lot ? `?wafer_lot_id=${encodeURIComponent(lot)}` : ""}`);
    $("#wm-count").textContent = `共 ${maps.length} 張`;
    renderTable("#t-wafermaps", [
      { title: "晶圓", render: (r) => `<a href="#" data-wafer="${esc(r.wafer_id)}">${esc(r.wafer_id)}</a>` },
      { title: "母批", key: "wafer_lot_id" },
      { title: "料號", key: "device_id" },
      { title: "晶粒", num: true, render: (r) => num(r.die_count) },
      { title: "良品", num: true, render: (r) => num(r.pass_count) },
      { title: "CP 良率", num: true, render: (r) => barCell(Number(r.yield), cpYieldKind(Number(r.yield))) },
      { title: "投入批號", render: (r) => esc(r.assembly_lot_id || "-") },
    ], maps, "尚無晶圓 Map，可由工程師上傳");

    $("#t-wafermaps").querySelectorAll("a[data-wafer]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        showWaferMap(link.dataset.wafer);
      });
    });
    if (maps.length) showWaferMap(maps[0].wafer_id);
  } catch (err) { toast(err.message, "err"); }
}

async function showWaferMap(waferId) {
  try {
    const [map, analysis] = await Promise.all([
      api(`/api/wafer/maps/${encodeURIComponent(waferId)}/grid?max_size=160`),
      api(`/api/wafer/maps/${encodeURIComponent(waferId)}/analysis`),
    ]);
    $("#wm-title").textContent =
      `${waferId}｜${map.source}｜${map.display_rows}×${map.display_cols}` +
      (map.downsample_step > 1 ? `（每 ${map.downsample_step} 顆縮為 1 格）` : "");
    drawWaferMap(map);

    const e = analysis.edge;
    $("#wm-kpis").innerHTML = [
      { label: "CP 良率", value: pct(analysis.yield), foot: `${num(analysis.pass_count)} / ${num(analysis.die_count)}`, kind: cpYieldKind(analysis.yield) },
      { label: "邊緣良率", value: pct(e.edge_yield), foot: `外 ${e.rings} 圈共 ${num(e.edge_die)} 顆`, kind: cpYieldKind(e.edge_yield) },
      { label: "中心良率", value: pct(e.center_yield), foot: `${num(e.center_die)} 顆`, kind: cpYieldKind(e.center_yield) },
      { label: "邊緣落差", value: pct(e.gap), foot: "中心 − 邊緣", kind: e.gap >= 0.05 ? "bad" : "ok" },
      { label: "群聚不良", value: num(analysis.clusters.clustered_die), foot: `最大一群 ${num(analysis.clusters.largest_cluster)} 顆`, kind: analysis.clusters.cluster_count ? "warn" : "ok" },
      { label: "不良總數", value: num(analysis.fail_count), foot: `${analysis.clusters.cluster_count} 個群聚` },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    $("#wm-findings").innerHTML = analysis.findings.map((f) => `<li>${esc(f)}</li>`).join("");
    renderTable("#t-wm-pareto", [
      { title: "Bin", key: "bin" },
      { title: "顆數", num: true, render: (r) => num(r.qty) },
      { title: "占全片", num: true, render: (r) => barCell(r.ratio, "bad") },
    ], analysis.bin_pareto.slice(0, 8), "全片無不良");
  } catch (err) { toast(err.message, "err"); }
}

function drawWaferMap(map) {
  const canvas = $("#wm-canvas");
  const ctx = canvas.getContext("2d");
  const rows = map.grid.length, cols = map.grid[0]?.length || 1;
  const cell = Math.max(1, Math.floor(Math.min(canvas.width / cols, canvas.height / rows)));
  const offsetX = Math.floor((canvas.width - cell * cols) / 2);
  const offsetY = Math.floor((canvas.height - cell * rows) / 2);

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const seen = new Map();
  map.grid.forEach((row, y) => row.forEach((bin, x) => {
    if (bin === map.null_bin) return;
    const color = binColor(bin, map.pass_bins);
    seen.set(bin, color);
    ctx.fillStyle = color;
    ctx.fillRect(offsetX + x * cell, offsetY + y * cell, cell, cell);
  }));

  $("#wm-legend").innerHTML = [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([bin, color]) =>
    `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px">
       <i style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${color}"></i>
       Bin ${esc(bin)}${map.pass_bins.includes(bin) ? "（良品）" : ""}
     </span>`).join("");
}

$("#wm-die-go").addEventListener("click", async () => {
  const lotId = $("#wm-die-lot").value.trim();
  const seq = $("#wm-die-seq").value.trim();
  if (!lotId) { toast("請輸入批號", "err"); return; }
  try {
    const summary = await api(`/api/wafer/lots/${encodeURIComponent(lotId)}/die-summary`);
    renderTable("#t-wm-dies", [
      { title: "來源晶圓", key: "wafer_id" },
      { title: "綁定顆數", num: true, render: (r) => num(r.total) },
      { title: "測試 PASS", num: true, render: (r) => num(r.PASS || 0) },
      { title: "測試 FAIL", num: true, render: (r) => num(r.FAIL || 0) },
    ], summary.by_wafer, "此批號尚未綁定晶粒");

    if (!seq) {
      $("#wm-die-out").textContent = JSON.stringify(summary, null, 2);
      return;
    }
    const die = await api(`/api/wafer/lots/${encodeURIComponent(lotId)}/units/${encodeURIComponent(seq)}`);
    $("#wm-die-out").textContent =
      `成品 ${lotId} #${seq}\n` +
      `→ 晶圓 ${die.die.wafer_id} 座標 (${die.die.die_x}, ${die.die.die_y})\n` +
      `→ CP Bin ${die.die.cp_bin}｜FT Bin ${die.die.ft_bin ?? "未測"}｜狀態 ${die.die.status}\n` +
      `→ 晶圓母批 ${die.wafer?.wafer_lot_id ?? "-"}／晶圓廠 ${die.wafer?.fab ?? "-"}\n\n` +
      `相鄰晶粒（判斷是否為群聚不良）：\n${JSON.stringify(die.neighbour_dies, null, 2)}`;
  } catch (err) { $("#wm-die-out").textContent = err.message; toast(err.message, "err"); }
});

/* ── e-SOP ───────────────────────────────────────────── */
const SOP_STATUS_TAG = { RELEASED: "RUNNING", DRAFT: "WAITING", OBSOLETE: "NON_SCHEDULED" };
const SOP_STATUS_LABEL = { RELEASED: "生效中", DRAFT: "草稿", OBSOLETE: "已作廢" };

$("#sop-refresh").addEventListener("click", loadSops);

async function loadSops() {
  try {
    if ($("#sop-op").options.length <= 1) {
      const ops = await api("/api/master/operations?limit=200");
      ops.items.forEach((op) =>
        $("#sop-op").appendChild(el("option", { value: op.op_code }, `${op.op_code} ${op.name}`)));
    }
    const params = new URLSearchParams();
    if ($("#sop-op").value) params.set("op_code", $("#sop-op").value);
    if ($("#sop-status").value) params.set("status", $("#sop-status").value);

    const [sops, pending, compliance] = await Promise.all([
      api(`/api/sops?${params}`),
      api("/api/sops/pending"),
      api("/api/sops/compliance"),
    ]);

    renderTable("#t-sops", [
      { title: "代碼", render: (r) => `<a href="#" data-sop="${esc(r.sop_code)}" data-ver="${r.version}">${esc(r.sop_code)}</a>` },
      { title: "版本", num: true, render: (r) => `v${r.version}` },
      { title: "標題", key: "title" },
      { title: "站別", key: "op_code" },
      { title: "狀態", render: (r) => `<span class="tag ${SOP_STATUS_TAG[r.status] || ""}">${esc(SOP_STATUS_LABEL[r.status] || r.status)}</span>` },
      { title: "步驟", key: "step_count", num: true },
      { title: "生效", render: (r) => when(r.effective_from) },
    ], sops, "尚無作業指導書");

    $("#t-sops").querySelectorAll("a[data-sop]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        showSop(link.dataset.sop, link.dataset.ver);
      });
    });
    if (sops.length) showSop(sops[0].sop_code, sops[0].version);

    renderTable("#t-sop-pending", [
      { title: "代碼", key: "sop_code" },
      { title: "版本", num: true, render: (r) => `v${r.version}` },
      { title: "標題", key: "title" },
      { title: "站別", key: "op_code" },
      { title: "", render: (r) => `<button class="btn small" data-ack="${esc(r.sop_code)}" data-ver="${r.version}">簽認</button>` },
    ], pending, "已全部簽認完畢");
    bindAckButtons("#t-sop-pending");

    renderTable("#t-sop-compliance", [
      { title: "代碼", key: "sop_code" },
      { title: "版本", num: true, render: (r) => `v${r.version}` },
      { title: "站別", key: "op_code" },
      { title: "已簽認", num: true, render: (r) => `${num(r.acknowledged)} / ${num(r.target_headcount)}` },
      { title: "簽認率", num: true, render: (r) => barCell(r.rate, r.rate >= 1 ? "ok" : r.rate >= 0.6 ? "warn" : "bad") },
    ], compliance, "沒有需要簽認的 SOP");
  } catch (err) { toast(err.message, "err"); }
}

async function showSop(sopCode, version) {
  try {
    const sop = await api(`/api/sops/${encodeURIComponent(sopCode)}?version=${version}`);
    $("#sop-title").textContent = `${sop.sop_code} v${sop.version}｜${sop.op_code}`;
    const steps = (sop.steps || []).map((s) => `
      <li><b>${esc(s.instruction)}</b>
        ${s.detail ? `<div class="muted">${esc(s.detail)}</div>` : ""}
        ${s.checkpoint ? `<div class="muted">檢查點：${esc(s.checkpoint)}</div>` : ""}
      </li>`).join("");
    $("#sop-detail").innerHTML = `
      <div style="margin-bottom:10px">${esc(sop.summary || "")}</div>
      <ol style="margin-left:18px;line-height:1.9">${steps || "<li class='muted'>尚無步驟</li>"}</ol>
      ${sop.hazards ? `<div style="margin-top:12px"><b>風險：</b>${esc(sop.hazards)}</div>` : ""}
      ${(sop.ppe || []).length ? `<div style="margin-top:6px"><b>防護具：</b>${sop.ppe.map((p) => `<span class="tag">${esc(p)}</span>`).join(" ")}</div>` : ""}
      <div style="margin-top:14px">
        <button class="btn primary" data-ack="${esc(sop.sop_code)}" data-ver="${sop.version}">我已閱讀並簽認</button>
      </div>`;
    bindAckButtons("#sop-detail");
  } catch (err) { toast(err.message, "err"); }
}

function bindAckButtons(scope) {
  $(scope).querySelectorAll("button[data-ack]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const data = await api("/api/sops/acknowledge", {
          method: "POST",
          body: { sop_code: btn.dataset.ack, version: Number(btn.dataset.ver) },
        });
        toast(data.already_acknowledged ? "此版本先前已簽認" : `已簽認 ${data.sop_code} v${data.version}`, "ok");
        loadSops();
      } catch (err) { toast(err.message, "err"); }
    });
  });
}

/* ── ERP 介接 ────────────────────────────────────────── */
const ERP_STATUS_TAG = {
  PENDING: "WAITING", PROCESSED: "RUNNING", ACKED: "RUNNING",
  SENT: "SCHEDULED_DOWN", FAILED: "HOLD",
};
const ERP_DOC_LABEL = {
  CUSTOMER: "客戶主檔", DEVICE: "產品料號", MATERIAL: "材料主檔", WORK_ORDER: "生產訂單",
  PRODUCTION_REPORT: "完工回報", MATERIAL_ISSUE: "材料領用", SHIPMENT: "出貨", SCRAP: "報廢",
};

$("#erp-refresh").addEventListener("click", loadErp);

$("#erp-process").addEventListener("click", async () => {
  try {
    const data = await api("/api/erp/inbound/process", { method: "POST", body: { limit: 100 } });
    toast(`處理 ${data.picked} 筆：成功 ${data.processed}、失敗 ${data.failed}`, data.failed ? "warn" : "ok");
    loadErp();
  } catch (err) { toast(err.message, "err"); }
});

$("#erp-build").addEventListener("click", async () => {
  try {
    const data = await api("/api/erp/outbound/build", { method: "POST", body: { hours: 24 } });
    toast(`已彙整 ${data.total} 張待送單據`, "ok");
    loadErp();
  } catch (err) { toast(err.message, "err"); }
});

$("#erp-doc-type").addEventListener("change", loadErp);

$("#erp-export").addEventListener("click", async () => {
  const docType = $("#erp-doc-type").value;
  if (!docType) { toast("請先選擇要匯出的單據類型", "err"); return; }
  try {
    // CSV 端點需要 Bearer 標頭，不能直接開新視窗下載
    const res = await fetch(`/api/erp/outbound/export.csv?doc_type=${docType}`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!res.ok) throw new Error(`匯出失敗（HTTP ${res.status}）`);
    const url = URL.createObjectURL(await res.blob());
    const link = el("a", { href: url, download: `${docType.toLowerCase()}.csv` });
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (err) { toast(err.message, "err"); }
});

async function loadErp() {
  try {
    const docType = $("#erp-doc-type").value;
    const [summary, inbound, outbound] = await Promise.all([
      api("/api/erp/summary"),
      api("/api/erp/inbound?limit=100"),
      api(`/api/erp/outbound?limit=100${docType ? `&doc_type=${docType}` : ""}`),
    ]);
    $("#erp-kpis").innerHTML = [
      { label: "下行待處理", value: num(summary.inbound_pending), foot: "等待套用到 MES", kind: summary.inbound_pending ? "warn" : "ok" },
      { label: "下行失敗", value: num(summary.inbound_failed), foot: "需人工處理", kind: summary.inbound_failed ? "bad" : "ok" },
      { label: "上行待送", value: num(summary.outbound_pending), foot: "等待 ERP 取件", kind: summary.outbound_pending ? "warn" : "ok" },
      { label: "本頁上行單據", value: num(outbound.length), foot: docType ? ERP_DOC_LABEL[docType] : "全部類型（最多 100 筆）" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    renderTable("#t-erp-in", [
      { title: "ERP 單號", key: "external_id" },
      { title: "類型", render: (r) => esc(ERP_DOC_LABEL[r.doc_type] || r.doc_type) },
      { title: "通道", key: "source" },
      { title: "狀態", render: (r) => `<span class="tag ${ERP_STATUS_TAG[r.status] || ""}">${esc(r.status)}</span>` },
      { title: "重試", key: "attempts", num: true },
      { title: "訊息", render: (r) => esc(r.error || r.result?.key || "-") },
      { title: "收單時間", render: (r) => when(r.received_at) },
      { title: "", render: (r) => (r.status === "FAILED" ? `<button class="btn small" data-retry="${r.id}">重試</button>` : "") },
    ], inbound, "沒有下行單據");

    $("#t-erp-in").querySelectorAll("button[data-retry]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/erp/inbound/${btn.dataset.retry}/retry`, { method: "POST" });
          toast("已排入重試", "ok");
          loadErp();
        } catch (err) { toast(err.message, "err"); }
      });
    });

    renderTable("#t-erp-out", [
      { title: "類型", render: (r) => esc(ERP_DOC_LABEL[r.doc_type] || r.doc_type) },
      { title: "來源單號", key: "reference" },
      { title: "狀態", render: (r) => `<span class="tag ${ERP_STATUS_TAG[r.status] || ""}">${esc(r.status)}</span>` },
      { title: "建立時間", render: (r) => when(r.created_at) },
      { title: "送出", render: (r) => when(r.sent_at) },
      { title: "回覆", render: (r) => when(r.acked_at) },
    ], outbound, "沒有上行單據");
  } catch (err) { toast(err.message, "err"); }
}

/* ── SECS/GEM ────────────────────────────────────────── */
const SECS_STATE_TAG = {
  SELECTED: "RUNNING", CONNECTED: "WAITING",
  NOT_CONNECTED: "NON_SCHEDULED", DISCONNECTED: "HOLD",
};
const SECS_ACTION_LABEL = {
  EQ_STATE: "更新設備狀態", TRACK_OUT_READY: "提示可出站",
  ALARM: "轉非計畫停機", LOG_ONLY: "只留紀錄",
};

$("#secs-refresh").addEventListener("click", loadSecs);

$("#secs-simulate").addEventListener("click", async () => {
  const eqId = $("#secs-eq").value;
  const ceid = $("#secs-ceid").value.trim();
  if (!eqId || !ceid) { toast("請選擇設備並輸入 CEID", "err"); return; }
  try {
    const data = await api(`/api/secs/links/${encodeURIComponent(eqId)}/simulate-event`, {
      method: "POST", body: { ceid: Number(ceid), data_id: 0, reports: [] },
    });
    $("#secs-out").textContent = JSON.stringify(data, null, 2);
    const matched = data.actions?.matched;
    toast(matched ? `已套用規則：${data.actions.rule}` : "查無對應規則，僅留下紀錄", matched ? "ok" : "warn");
    loadSecs();
  } catch (err) { toast(err.message, "err"); }
});

$("#secs-decode").addEventListener("click", async () => {
  try {
    const data = await api("/api/secs/decode", { method: "POST", body: { hex: $("#secs-hex").value.trim() } });
    $("#secs-out").textContent = `${data.name}\n\n${data.sml}\n\n${JSON.stringify(data.python, null, 2)}`;
  } catch (err) { $("#secs-out").textContent = err.message; toast(err.message, "err"); }
});

async function loadSecs() {
  try {
    const [status, rules, messages] = await Promise.all([
      api("/api/secs/status"),
      api("/api/secs/rules"),
      api("/api/secs/messages?limit=100"),
    ]);

    if ($("#secs-eq").options.length <= 1) {
      status.links.forEach((l) =>
        $("#secs-eq").appendChild(el("option", { value: l.eq_id }, `${l.eq_id} ${l.eq_name || ""}`)));
    }

    $("#secs-kpis").innerHTML = [
      { label: "已設定設備", value: num(status.total), foot: "HSMS 連線" },
      { label: "已連線", value: num(status.selected), foot: "SELECTED 狀態", kind: status.selected ? "ok" : "" },
      { label: "未連線", value: num(status.disconnected), foot: "含停用中", kind: status.disconnected ? "warn" : "ok" },
      { label: "近一小時訊息", value: num(status.links.reduce((s, l) => s + l.messages_last_hour, 0)), foot: "收發合計" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    renderTable("#t-secs-links", [
      { title: "設備", render: (r) => `${esc(r.eq_id)}<br><span class="muted" style="font-size:12px">${esc(r.eq_name || "")}</span>` },
      { title: "位址", render: (r) => `${esc(r.host)}:${r.port}` },
      { title: "連線狀態", render: (r) => `<span class="tag ${SECS_STATE_TAG[r.connection_state] || ""}">${esc(r.connection_state)}</span>` },
      { title: "啟用", render: (r) => (r.enabled ? "是" : "否") },
      { title: "近一小時訊息", key: "messages_last_hour", num: true },
      { title: "最後訊息", render: (r) => when(r.last_message_at) },
      { title: "", render: (r) => `<button class="btn small" data-conn="${esc(r.eq_id)}" data-on="${r.connection_state === "SELECTED" ? 0 : 1}">${r.connection_state === "SELECTED" ? "中斷" : "連線"}</button>` },
    ], status.links, "尚未設定任何設備連線");

    $("#t-secs-links").querySelectorAll("button[data-conn]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const action = btn.dataset.on === "1" ? "connect" : "disconnect";
        try {
          await api(`/api/secs/links/${encodeURIComponent(btn.dataset.conn)}/${action}`, { method: "POST" });
          toast(action === "connect" ? "已開始連線，請稍候重新整理" : "已中斷連線", "ok");
          loadSecs();
        } catch (err) { toast(err.message, "err"); }
      });
    });

    renderTable("#t-secs-rules", [
      { title: "CEID", key: "ceid", num: true },
      { title: "名稱", key: "name" },
      { title: "適用設備", render: (r) => esc(r.eq_id || "全部") },
      { title: "動作", render: (r) => esc(SECS_ACTION_LABEL[r.action] || r.action) },
      { title: "參數", render: (r) => `<code>${esc(JSON.stringify(r.params || {}))}</code>` },
      { title: "啟用", render: (r) => (r.enabled ? "是" : "否") },
    ], rules, "尚未設定事件規則");

    renderTable("#t-secs-msgs", [
      { title: "時間", render: (r) => when(r.timestamp) },
      { title: "設備", key: "eq_id" },
      { title: "方向", render: (r) => `<span class="tag ${r.direction === "RECV" ? "WAITING" : "COMPLETED"}">${r.direction === "RECV" ? "收" : "送"}</span>` },
      { title: "訊息", render: (r) => `<a href="#" data-msg="${r.id}">S${r.stream}F${r.function}${r.w_bit ? " W" : ""}</a>` },
      { title: "說明", key: "description" },
    ], messages, "尚無 SECS 訊息");

    const byId = Object.fromEntries(messages.map((m) => [String(m.id), m]));
    $("#t-secs-msgs").querySelectorAll("a[data-msg]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        const msg = byId[link.dataset.msg];
        $("#secs-out").textContent =
          `S${msg.stream}F${msg.function}${msg.w_bit ? " W" : ""}｜${msg.direction}｜${when(msg.timestamp)}\n\n` +
          `${msg.sml || "（無內容）"}`;
      });
    });
  } catch (err) { toast(err.message, "err"); }
}

/* ── 抽樣檢驗與屬性管制圖 ───────────────────────────── */
const INSPECT_TAG = { PENDING: "WAITING", ACCEPT: "RUNNING", REJECT: "HOLD", NOT_REQUIRED: "NON_SCHEDULED" };
const DECISION_LABEL = { FULL: "全檢", SAMPLED: "抽檢", SKIPPED: "跳批" };

$("#samp-refresh").addEventListener("click", loadSampling);
$("#samp-chart").addEventListener("change", loadSampling);
$("#samp-op").addEventListener("change", loadSampling);

$("#samp-judge").addEventListener("click", async () => {
  const lotId = $("#samp-lot").value.trim();
  const found = $("#samp-found").value.trim();
  if (!lotId || found === "") { toast("請輸入批號與檢出不良數", "err"); return; }
  try {
    const data = await api("/api/sampling/inspections/judge", {
      method: "POST", body: { lot_id: lotId, defect_found: Number(found), defects: [] },
    });
    toast(data.message, data.accepted ? "ok" : "err");
    $("#samp-lot").value = ""; $("#samp-found").value = "";
    loadSampling();
  } catch (err) { toast(err.message, "err"); }
});

async function loadSampling() {
  try {
    const [pending, plans, summary, overview] = await Promise.all([
      api("/api/sampling/inspections/pending"),
      api("/api/sampling/plans"),
      api("/api/sampling/summary?hours=168"),
      api("/api/spc/attribute/overview?hours=168"),
    ]);

    if ($("#samp-op").options.length === 0) {
      overview.forEach((r) => $("#samp-op").appendChild(el("option", { value: r.op_code }, r.op_code)));
      if (!overview.length) $("#samp-op").appendChild(el("option", { value: "" }, "（尚無生產資料）"));
    }

    const rejected = summary.total_rejected;
    $("#samp-kpis").innerHTML = [
      { label: "待判定", value: num(pending.length), foot: "等品保回報", kind: pending.length ? "warn" : "ok" },
      { label: "已檢驗批數", value: num(summary.total_inspected), foot: "最近 7 天" },
      { label: "跳批批數", value: num(summary.total_skipped), foot: "依抽樣計畫輪替" },
      { label: "拒收批數", value: num(rejected), foot: "需開立扣留處置", kind: rejected ? "bad" : "ok" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    renderTable("#t-samp-pending", [
      { title: "批號", key: "lot_id" },
      { title: "站別", key: "op_code" },
      { title: "方式", render: (r) => esc(DECISION_LABEL[r.decision] || r.decision) },
      { title: "批量", num: true, render: (r) => num(r.lot_size) },
      { title: "抽樣數", num: true, render: (r) => num(r.sample_size) },
      { title: "允收/拒收", num: true, render: (r) => `${r.accept_number ?? "-"} / ${r.reject_number ?? "-"}` },
      { title: "時間", render: (r) => when(r.timestamp) },
    ], pending, "沒有待判定的檢驗");

    renderTable("#t-samp-overview", [
      { title: "站別", key: "op_code" },
      { title: "批數", key: "lots", num: true },
      { title: "檢驗數", num: true, render: (r) => num(r.units) },
      { title: "不良率", num: true, render: (r) => barCell(r.defect_rate, r.defect_rate > 0.02 ? "bad" : "ok") },
      { title: "判異點數", num: true, render: (r) => (r.violations ? `<span class="tag HOLD">${r.violations}</span>` : "0") },
    ], overview, "資料點不足，尚無法建立管制圖");

    renderTable("#t-samp-plans", [
      { title: "計畫", key: "plan_code" },
      { title: "站別", key: "op_code" },
      { title: "方式", key: "plan_type" },
      { title: "AQL", key: "aql", num: true },
      { title: "跳批", num: true, render: (r) => `每 ${r.lot_interval} 批` },
      { title: "級距數", key: "level_count", num: true },
      { title: "啟用", render: (r) => (r.active ? "是" : "否") },
    ], plans, "尚未建立抽樣計畫");

    await drawAttributeChart();
  } catch (err) { toast(err.message, "err"); }
}

async function drawAttributeChart() {
  const opCode = $("#samp-op").value;
  const chartType = $("#samp-chart").value;
  const svg = $("#samp-chart-svg");
  if (!opCode) { svg.innerHTML = ""; $("#samp-chart-note").textContent = "尚無生產資料"; return; }

  const data = await api(`/api/spc/attribute/${encodeURIComponent(opCode)}?chart=${chartType}&hours=168`);
  $("#samp-chart-title").textContent = `${opCode}｜${chartType.toUpperCase()} 圖`;
  if (!data.ready) {
    svg.innerHTML = "";
    $("#samp-chart-note").textContent = data.message || "資料點不足";
    return;
  }
  $("#samp-chart-note").textContent =
    `中心線 ${Number(data.center).toFixed(6)}｜${data.points.length} 點｜判異 ${data.violations} 點`;
  drawControlPoints(svg, data.points);
}

/** 通用的管制圖繪製：逐點的 UCL/LCL 都畫得出來（p 圖與 u 圖需要）。 */
function drawControlPoints(svg, points) {
  const W = 760, H = 300, pad = { top: 16, right: 16, bottom: 28, left: 56 };
  const values = points.flatMap((p) => [p.value, p.ucl, p.lcl]);
  const min = Math.min(...values), max = Math.max(...values);
  const span = (max - min) || 1;
  const lo = min - span * 0.1, hi = max + span * 0.1;
  const x = (i) => pad.left + (i * (W - pad.left - pad.right)) / Math.max(1, points.length - 1);
  const y = (v) => H - pad.bottom - ((v - lo) / (hi - lo)) * (H - pad.top - pad.bottom);

  const path = (key, color, dash = "") =>
    `<polyline fill="none" stroke="${color}" stroke-width="1.5" ${dash ? `stroke-dasharray="${dash}"` : ""}
      points="${points.map((p, i) => `${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" ")}" />`;

  const dots = points.map((p, i) =>
    `<circle cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="${p.out_of_control ? 4.5 : 3}"
       fill="${p.out_of_control ? "#d94b4b" : "#0b6fbd"}" />`).join("");

  svg.innerHTML = `
    <rect x="0" y="0" width="${W}" height="${H}" fill="none" />
    <line x1="${pad.left}" y1="${H - pad.bottom}" x2="${W - pad.right}" y2="${H - pad.bottom}"
          stroke="currentColor" stroke-opacity="0.25" />
    <line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${H - pad.bottom}"
          stroke="currentColor" stroke-opacity="0.25" />
    ${path("ucl", "#d94b4b", "4 3")}
    ${path("lcl", "#d94b4b", "4 3")}
    ${path("value", "#0b6fbd")}
    ${dots}
    <text x="4" y="${y(hi) + 12}" font-size="11" fill="currentColor" opacity="0.6">${hi.toFixed(4)}</text>
    <text x="4" y="${H - pad.bottom}" font-size="11" fill="currentColor" opacity="0.6">${lo.toFixed(4)}</text>`;
}

/* ── 配方管理 ────────────────────────────────────────── */
const RECIPE_STATUS_TAG = { RELEASED: "RUNNING", DRAFT: "WAITING", OBSOLETE: "NON_SCHEDULED" };
const RECIPE_STATUS_LABEL = { RELEASED: "已發行", DRAFT: "草稿", OBSOLETE: "已作廢" };

$("#rcp-refresh").addEventListener("click", loadRecipes);
$("#rcp-op").addEventListener("change", loadRecipes);
$("#rcp-status").addEventListener("change", loadRecipes);

async function loadRecipes() {
  try {
    if ($("#rcp-op").options.length <= 1) {
      const ops = await api("/api/master/operations?limit=200");
      ops.items.forEach((op) =>
        $("#rcp-op").appendChild(el("option", { value: op.op_code }, `${op.op_code} ${op.name}`)));
    }
    const params = new URLSearchParams();
    if ($("#rcp-op").value) params.set("op_code", $("#rcp-op").value);
    if ($("#rcp-status").value) params.set("status", $("#rcp-status").value);

    const [overview, list, checks] = await Promise.all([
      api("/api/recipes/status"),
      api(`/api/recipes?${params}`),
      api("/api/recipes/checks?limit=100"),
    ]);

    const failed = checks.filter((c) => !c.passed).length;
    $("#rcp-kpis").innerHTML = [
      { label: "已發行配方", value: num(overview.released_recipes), foot: "核可可用" },
      { label: "已載入機台", value: num(overview.loaded), foot: `未載入 ${overview.unloaded} 台` },
      { label: "未核可配方", value: num(overview.unknown_recipe), foot: "機台載了不認識的配方", kind: overview.unknown_recipe ? "bad" : "ok" },
      { label: "比對失敗", value: num(failed), foot: "最近 100 次進站比對", kind: failed ? "bad" : "ok" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    renderTable("#t-rcp-equipments", [
      { title: "設備", render: (r) => `${esc(r.eq_id)}<br><span class="muted" style="font-size:12px">${esc(r.eq_name || "")}</span>` },
      { title: "機型", key: "model" },
      { title: "載入配方", render: (r) => (r.ppid
          ? `<span class="tag ${r.recognised ? "RUNNING" : "HOLD"}">${esc(r.ppid)}</span>`
          : '<span class="muted">未載入</span>') },
      { title: "來源", render: (r) => esc(r.source || "-") },
      { title: "載入時間", render: (r) => when(r.loaded_at) },
    ], overview.equipments, "尚無設備");

    renderTable("#t-recipes", [
      { title: "PPID", render: (r) => `<a href="#" data-ppid="${esc(r.ppid)}" data-ver="${r.version}">${esc(r.ppid)}</a>` },
      { title: "版本", num: true, render: (r) => `v${r.version}` },
      { title: "站別", key: "op_code" },
      { title: "料號", render: (r) => esc(r.device_id || "全部") },
      { title: "狀態", render: (r) => `<span class="tag ${RECIPE_STATUS_TAG[r.status] || ""}">${esc(RECIPE_STATUS_LABEL[r.status] || r.status)}</span>` },
      { title: "參數數", key: "param_count", num: true },
    ], list, "尚無配方");

    $("#t-recipes").querySelectorAll("a[data-ppid]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        showRecipe(link.dataset.ppid, link.dataset.ver);
      });
    });
    if (list.length) showRecipe(list[0].ppid, list[0].version);

    renderTable("#t-rcp-checks", [
      { title: "時間", render: (r) => when(r.timestamp) },
      { title: "批號", key: "lot_id" },
      { title: "設備", key: "eq_id" },
      { title: "站別", key: "op_code" },
      { title: "機台配方", render: (r) => esc(r.loaded_ppid || "-") },
      { title: "結果", render: (r) => `<span class="tag ${r.passed ? "RUNNING" : "HOLD"}">${r.passed ? "通過" : "不符"}</span>` },
      { title: "說明", key: "reason" },
    ], checks, "尚無比對紀錄");
  } catch (err) { toast(err.message, "err"); }
}

async function showRecipe(ppid, version) {
  try {
    const recipe = await api(`/api/recipes/${encodeURIComponent(ppid)}?version=${version}`);
    $("#rcp-title").textContent = `${recipe.ppid} v${recipe.version}｜${recipe.op_code}`;
    const rows = Object.entries(recipe.parameters || {}).map(([k, v]) => ({ name: k, value: v }));
    renderTable("#t-rcp-params", [
      { title: "參數", key: "name" },
      { title: "設定值", num: true, render: (r) => esc(String(r.value)) },
    ], rows, "此版本尚無參數");
  } catch (err) { toast(err.message, "err"); }
}

/* ── 客訴與載具 ──────────────────────────────────────── */
const COMPLAINT_TAG = {
  OPEN: "WAITING", INVESTIGATING: "SCHEDULED_DOWN", ACTION: "SCHEDULED_DOWN",
  CLOSED: "RUNNING", REJECTED: "NON_SCHEDULED",
};
const COMPLAINT_LABEL = {
  OPEN: "受理中", INVESTIGATING: "調查中", ACTION: "對策執行", CLOSED: "已結案", REJECTED: "不成立",
};
const SEVERITY_TAG = { CRITICAL: "HOLD", MAJOR: "SCHEDULED_DOWN", MINOR: "WAITING" };
const CARRIER_TAG = {
  EMPTY: "WAITING", IN_USE: "RUNNING", DIRTY: "SCHEDULED_DOWN",
  MAINTENANCE: "SCHEDULED_DOWN", SCRAPPED: "NON_SCHEDULED",
};

$("#rma-refresh").addEventListener("click", loadRma);
$("#rma-status").addEventListener("change", loadRma);

async function loadRma() {
  try {
    const status = $("#rma-status").value;
    const [complaints, summary, carriers, carrierOverview] = await Promise.all([
      api(`/api/quality-ops/complaints${status ? `?status=${status}` : ""}`),
      api("/api/quality-ops/complaints/summary?hours=2160"),
      api("/api/quality-ops/carriers?limit=200"),
      api("/api/quality-ops/carriers/overview"),
    ]);

    $("#rma-kpis").innerHTML = [
      { label: "未結案客訴", value: num(summary.open), foot: `最近 90 天共 ${summary.total} 件`, kind: summary.open ? "warn" : "ok" },
      { label: "嚴重客訴", value: num(summary.by_severity?.CRITICAL || 0), foot: "CRITICAL", kind: summary.by_severity?.CRITICAL ? "bad" : "ok" },
      { label: "逾期未結", value: num(summary.overdue.length), foot: "已過承諾日", kind: summary.overdue.length ? "bad" : "ok" },
      { label: "平均結案天數", value: summary.avg_close_days ?? "—", foot: "已結案客訴" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    renderTable("#t-complaints", [
      { title: "單號", render: (r) => `<a href="#" data-cm="${esc(r.complaint_no)}">${esc(r.complaint_no)}</a>` },
      { title: "客戶", key: "customer_code" },
      { title: "料號", key: "device_id" },
      { title: "嚴重度", render: (r) => `<span class="tag ${SEVERITY_TAG[r.severity] || ""}">${esc(r.severity)}</span>` },
      { title: "狀態", render: (r) => `<span class="tag ${COMPLAINT_TAG[r.status] || ""}">${esc(COMPLAINT_LABEL[r.status] || r.status)}</span>` },
      { title: "影響批數", key: "impacted_lot_count", num: true },
      { title: "收件", render: (r) => when(r.received_at) },
    ], complaints, "目前沒有客訴");

    $("#t-complaints").querySelectorAll("a[data-cm]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        showComplaint(link.dataset.cm);
      });
    });
    if (complaints.length) showComplaint(complaints[0].complaint_no);

    const o = carrierOverview;
    $("#carrier-kpis").innerHTML = [
      { label: "載具總數", value: num(o.total), foot: `${o.by_type.length} 種類型` },
      { label: "使用中", value: num(o.in_use), foot: "已掛批號" },
      { label: "可用", value: num(o.available), foot: "空的且已清洗", kind: o.available ? "ok" : "warn" },
      { label: "待清洗", value: num(o.needs_cleaning.length), foot: "達清洗週期", kind: o.needs_cleaning.length ? "warn" : "ok" },
    ].map((c) => `
      <div class="kpi">
        <div class="label">${esc(c.label)}</div>
        <div class="value ${c.kind || ""}">${esc(String(c.value))}</div>
        <div class="foot">${esc(c.foot)}</div>
      </div>`).join("");

    renderTable("#t-carriers", [
      { title: "載具", key: "carrier_id" },
      { title: "類型", key: "carrier_type" },
      { title: "狀態", render: (r) => `<span class="tag ${CARRIER_TAG[r.status] || ""}">${esc(r.status)}</span>` },
      { title: "掛載批號", render: (r) => esc(r.current_lot_id || "-") },
      { title: "使用次數", num: true, render: (r) => `${num(r.use_count)}${r.clean_interval ? ` / ${r.clean_interval}` : ""}` },
    ], carriers.filter((c) => c.status === "IN_USE").slice(0, 50), "目前沒有使用中的載具");

    renderTable("#t-carriers-dirty", [
      { title: "載具", key: "carrier_id" },
      { title: "類型", key: "carrier_type" },
      { title: "使用次數", num: true, render: (r) => `${num(r.use_count)} / ${num(r.clean_interval)}` },
      { title: "上次清洗", render: (r) => when(r.last_cleaned_at) },
      { title: "", render: (r) => `<button class="btn small" data-clean="${esc(r.carrier_id)}">完成清洗</button>` },
    ], o.needs_cleaning, "沒有載具需要清洗");

    $("#t-carriers-dirty").querySelectorAll("button[data-clean]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await api(`/api/quality-ops/carriers/${encodeURIComponent(btn.dataset.clean)}/clean`, { method: "POST" });
          toast(`${btn.dataset.clean} 已完成清洗`, "ok");
          loadRma();
        } catch (err) { toast(err.message, "err"); }
      });
    });
  } catch (err) { toast(err.message, "err"); }
}

async function showComplaint(complaintNo) {
  try {
    const cm = await api(`/api/quality-ops/complaints/${encodeURIComponent(complaintNo)}`);
    $("#rma-title").textContent = `${cm.complaint_no}｜${cm.customer_code}｜${COMPLAINT_LABEL[cm.status] || cm.status}`;

    const steps = Object.entries(cm.d8_steps).map(([code, title]) => {
      const filled = (cm.d8 || {})[code];
      return { code, title, content: filled?.content || "", completed: !!filled?.completed,
               updated_at: filled?.updated_at, owner: filled?.owner || "" };
    });
    renderTable("#t-d8", [
      { title: "步驟", render: (r) => `${esc(r.code)} ${esc(r.title)}` },
      { title: "內容", render: (r) => esc(r.content || "—") },
      { title: "狀態", render: (r) => (r.completed
          ? '<span class="tag RUNNING">已完成</span>'
          : '<span class="tag WAITING">未填</span>') },
      { title: "更新", render: (r) => (r.updated_at ? when(r.updated_at) : "-") },
    ], steps);

    const impact = cm.impact || {};
    $("#rma-impact").innerHTML = `
      <div>申告批號：${(impact.reported_lots || []).map((l) => `<span class="tag">${esc(l)}</span>`).join(" ") || "—"}</div>
      <div style="margin-top:6px">來源晶圓：${num((impact.source_wafers || []).length)} 片</div>
      <div style="margin-top:6px">受影響批號：<b>${num(impact.impacted_lot_count || 0)}</b> 個</div>
      <div style="margin-top:6px">已出貨客戶：${(impact.affected_customers || []).map((c) => `<span class="tag HOLD">${esc(c)}</span>`).join(" ") || "—"}</div>
      <div style="margin-top:6px">退回品座標：${(impact.returned_dies || []).map((d) =>
        `<span class="tag">${esc(d.wafer_id)} (${d.die_x}, ${d.die_y})</span>`).join(" ") || "—"}</div>`;
  } catch (err) { toast(err.message, "err"); }
}

/* ── 啟動 ────────────────────────────────────────────── */
(async function boot() {
  if (!state.token) return;
  try {
    state.user = await api("/api/auth/me");
    enterApp();
  } catch {
    logout();
  }
})();
