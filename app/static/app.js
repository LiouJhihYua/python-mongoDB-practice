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

/* ── 導覽 ────────────────────────────────────────────── */
document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $(`#view-${btn.dataset.view}`).classList.add("active");
    const loaders = {
      dashboard: loadDashboard, lots: loadLots, equipment: loadOee,
      quality: loadQuality, orders: loadWorkOrders,
    };
    loaders[btn.dataset.view]?.();
  });
});

/* ── 戰情看板 ────────────────────────────────────────── */
$("#dash-refresh").addEventListener("click", loadDashboard);
$("#dash-hours").addEventListener("change", loadDashboard);

async function loadDashboard() {
  try {
    const hours = $("#dash-hours").value;
    const d = await api(`/api/reports/dashboard?hours=${hours}`);
    $("#dash-time").textContent = `資料時間 ${when(d.generated_at)}`;

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
  } catch (err) {
    toast(err.message, "err");
  }
}

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
