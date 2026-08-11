/* 現場終端機 —— 純 JavaScript，無需建置流程。

   跟看板最大的差別是輸入方式：這裡的主要輸入裝置是掃描槍。
   掃描槍等同一個打字很快、結尾按 Enter 的鍵盤，所以做法是
   讓掃描框「永遠保持聚焦」，操作員拿起槍就掃，不必先點畫面。 */
"use strict";

const state = {
  token: localStorage.getItem("mes_token") || "",
  user: null,
  eqId: localStorage.getItem("mes_terminal_eq") || "",
  station: null,
  lot: null,
  numTarget: null,
};

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
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v) => (v ?? 0).toLocaleString("zh-TW");
const mmss = (sec) => {
  const s = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} 時 ${m} 分` : `${m} 分`;
};

function show(screen) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(`#${screen}`).classList.add("active");
  if (screen === "work") focusScan();
  if (screen === "pick") $("#pick-scan").focus();
}

function openModal(id) { $(`#${id}`).classList.add("active"); }
function closeModal(id) { $(`#${id}`).classList.remove("active"); focusScan(); }

/** 大字回饋。現場站著看，錯誤訊息要跟成功一樣大聲。 */
let bannerSeq = 0;

function banner(kind, title, detail = "") {
  const node = $("#banner");
  const token = ++bannerSeq;
  node.dataset.token = String(token);
  node.className = `banner show ${kind}`;
  node.innerHTML = `<div class="title">${esc(title)}</div>` +
    (detail ? `<div class="detail">${detail}</div>` : "");
  // 成功訊息自動淡出，但只能關掉「自己」——
  // 否則前一則的計時器會把後來才出現的訊息清掉，現場就看不到回饋了
  if (kind === "ok") {
    setTimeout(() => { if (node.dataset.token === String(token)) clearBanner(); }, 6000);
  }
}

function clearBanner() {
  const node = $("#banner");
  node.className = "banner";
  node.innerHTML = "";
  delete node.dataset.token;
}

/** 掃描框永遠保持聚焦 —— 操作員拿起槍就掃，不必先點畫面。 */
function focusScan() {
  const box = $("#scan");
  if (box && $("#work").classList.contains("active") && !document.querySelector(".modal.active")) {
    box.focus();
  }
}
document.addEventListener("click", () => setTimeout(focusScan, 0));

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
    $("#p").value = "";
    await afterLogin();
  } catch (err) {
    $("#login-hint").className = "hint err";
    $("#login-hint").textContent = err.message;
  }
});

// 掃工號條碼會直接送出 Enter，此時把游標移到密碼欄而不是提交空密碼
$("#u").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("#p").value) {
    event.preventDefault();
    $("#p").focus();
  }
});

function logout() {
  state.token = "";
  state.user = null;
  localStorage.removeItem("mes_token");
  show("login");
  $("#u").focus();
}
$("#pick-logout").addEventListener("click", logout);
$("#work-logout").addEventListener("click", logout);

async function afterLogin() {
  const name = state.user.full_name || state.user.username;
  $("#pick-who").textContent = name;
  $("#work-who").textContent = name;
  if (state.eqId) {
    try { await loadStation(); return; } catch { state.eqId = ""; }
  }
  await loadStations();
}

/* ── 選機台 ──────────────────────────────────────────── */
$("#change-eq").addEventListener("click", () => { loadStations(); });

async function loadStations() {
  show("pick");
  const list = await api("/api/terminal/stations");
  $("#station-grid").innerHTML = list.map((s) => `
    <div class="station-card" data-eq="${esc(s.eq_id)}">
      <div class="sid">${esc(s.eq_id)}</div>
      <div class="sname">${esc(s.name)}</div>
      <span class="tag ${esc(s.current_state)}">${esc(s.current_state)}</span>
    </div>`).join("");
  $("#station-grid").querySelectorAll("[data-eq]").forEach((card) => {
    card.addEventListener("click", () => selectStation(card.dataset.eq));
  });
}

$("#pick-scan").addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  const code = $("#pick-scan").value.trim();
  $("#pick-scan").value = "";
  if (code) await selectStation(code);
});

async function selectStation(eqId) {
  try {
    state.eqId = eqId;
    localStorage.setItem("mes_terminal_eq", eqId);
    await loadStation();
  } catch (err) {
    state.eqId = "";
    alert(err.message);
  }
}

/* ── 工作台 ──────────────────────────────────────────── */
async function loadStation() {
  const data = await api(`/api/terminal/station/${encodeURIComponent(state.eqId)}`);
  state.station = data;
  show("work");

  const eq = data.equipment;
  $("#eq-name").textContent = `${eq.eq_id} ${eq.name}`;
  $("#eq-state").textContent = eq.current_state;
  $("#eq-state").className = `tag ${eq.current_state}`;

  renderQueue(data.queue, data.queue_total, data.queue_truncated);
  renderEqStatus(data);

  // 機台上已經有料時直接帶出來，操作員一走近就知道現在在跑什麼。
  // 但如果他自己掃了「別的」批號在看，就不要動 —— 這支函式每 30 秒會自己跑一次，
  // 在他眼前把批號換掉，等於讓按鈕在手指底下改變身分，很容易對錯批下指令。
  const viewingOther = state.lot && data.current_lot
    && state.lot.lot_id !== data.current_lot.lot_id;
  if (data.current_lot && !viewingOther) {
    state.lot = data.current_lot;
    renderLot(data.current_lot);
  } else if (!state.lot) {
    clearLotPanel();
  }
}

/** 批號面板有三塊，清空要一起清 —— 分開寫過一次就會漏掉其中一塊。 */
function clearLotPanel() {
  $("#lot-body").innerHTML = '<div class="empty-hint">尚未掃描批號</div>';
  $("#lot-checks").innerHTML = "";
  $("#lot-actions").innerHTML = "";
}

function renderQueue(queue, total, truncated) {
  // 只列前幾批，但標題要說出真正的總數 ——
  // 螢幕上寫「12 批」而實際有 40 批在等，現場會低估自己落後多少
  const all = Number.isFinite(total) ? total : queue.length;
  $("#queue-count").textContent = all
    ? truncated ? `（顯示 ${queue.length} / 共 ${all} 批）` : `（${all} 批）`
    : "";
  if (!queue.length) {
    $("#queue").innerHTML = '<div class="empty-hint">目前沒有待進站的批號</div>';
    return;
  }
  $("#queue").innerHTML = queue.map((r) => `
    <div class="queue-item ${esc(r.urgency)}" data-lot="${esc(r.lot_id)}">
      <div>
        <div class="qid">${esc(r.lot_id)}</div>
        <div class="qmeta" style="margin:4px 0 0;text-align:left">${esc(r.device_id)} · ${esc(r.op_code)}</div>
      </div>
      <div class="qmeta">
        ${num(r.qty)} ${esc(r.unit_type)}<br>
        ${r.qtime_remaining_min != null
          ? (r.qtime_expired
              ? `<span style="color:var(--bad)">Q-Time 逾時 ${Math.abs(r.qtime_remaining_min).toFixed(0)} 分</span>`
              : `Q-Time 剩 ${r.qtime_remaining_min.toFixed(0)} 分`)
          : `等待 ${r.waiting_minutes.toFixed(0)} 分`}
      </div>
    </div>`).join("");
  $("#queue").querySelectorAll("[data-lot]").forEach((item) => {
    item.addEventListener("click", () => handleScan(item.dataset.lot));
  });
}

function renderEqStatus(data) {
  const tools = data.tools || [];
  const recipe = data.loaded_recipe;
  $("#eq-status").innerHTML = `
    <div class="row"><span>可執行站別</span><b>${esc((data.equipment.op_codes || []).join(", ") || "-")}</b></div>
    <div class="row"><span>載入配方</span><b>${esc(recipe?.ppid || "未載入")}</b></div>
    <div class="row"><span>治具</span><b>${tools.length ? tools.map((t) =>
      `${esc(t.tool_id)}（${(t.usage_ratio * 100).toFixed(0)}%）`).join("、") : "無"}</b></div>
    ${data.tools_need_change.length
      ? `<div class="row" style="color:var(--warn)"><span>待更換治具</span><b>${esc(data.tools_need_change.join(", "))}</b></div>`
      : ""}
    ${data.pending_sops.length
      ? `<div class="row" style="color:var(--warn)"><span>待簽認 SOP</span><b>${
          data.pending_sops.map((s) => esc(`${s.sop_code} v${s.version}`)).join("、")}</b></div>`
      : ""}`;
}

/* ── 掃描 ────────────────────────────────────────────── */
$("#scan").addEventListener("keydown", async (event) => {
  if (event.key !== "Enter") return;
  const code = $("#scan").value.trim();
  $("#scan").value = "";
  if (code) await handleScan(code);
});
$("#scan-clear").addEventListener("click", () => {
  state.lot = null;
  clearLotPanel();
  clearBanner();
  focusScan();
});

async function handleScan(code) {
  clearBanner();
  try {
    const result = await api("/api/terminal/scan", {
      method: "POST", body: { code, eq_id: state.eqId },
    });

    if (result.kind === "EQUIPMENT") {
      await selectStation(result.equipment.eq_id);
      banner("ok", `已切換到 ${result.equipment.eq_id}`, esc(result.equipment.name));
      return;
    }
    if (result.kind === "LOT") {
      state.lot = result.preflight;
      renderLot(result.preflight);
      return;
    }
    if (result.kind === "TOOL") {
      const t = result.tool;
      banner(t.needs_attention ? "warn" : "ok", `治具 ${t.tool_id}`,
        `${esc(t.name)}｜已用 ${(t.usage_ratio * 100).toFixed(0)}%（${num(t.used_count)} / ${num(t.life_limit)}）` +
        `${t.needs_attention ? "<br><b>已達壽命，請更換</b>" : ""}`);
      return;
    }
    if (result.kind === "CARRIER") {
      const c = result.carrier;
      banner("ok", `載具 ${c.carrier_id}`,
        `${esc(c.carrier_type)}｜狀態 ${esc(c.status)}` +
        (c.current_lot_id ? `｜掛載 ${esc(c.current_lot_id)}` : "｜目前是空的"));
      return;
    }
    if (result.kind === "WAFER") {
      const w = result.wafer;
      banner("ok", `晶圓 ${w.wafer_id}`,
        `${esc(w.device_id)}｜母批 ${esc(w.wafer_lot_id)}｜` +
        (w.consumed ? `已投入 ${esc(w.assembly_lot_id || "")}` : "尚未投入"));
      return;
    }
    banner("warn", `掃到 ${esc(code)}`, "系統認得這個編號，但目前沒有對應的作業");
  } catch (err) {
    banner("err", "無法辨識", esc(err.message));
  }
}

/* ── 批號面板 ────────────────────────────────────────── */
function renderLot(check) {
  const info = check.info || {};
  const op = check.operation || {};
  $("#lot-body").innerHTML = `
    <div class="lot-id">${esc(check.lot_id)}</div>
    <div class="lot-meta">
      <div><span>狀態</span><br><span class="tag ${esc(check.status)}">${esc(check.status)}</span></div>
      <div><span>站別</span><br><b>${esc(op.seq ?? "")} ${esc(op.op_code || "")}</b></div>
      <div><span>料號</span><br><b>${esc(check.device_id)}</b></div>
      <div><span>數量</span><br><b>${num(check.qty)} ${esc(check.unit_type)}</b></div>
      ${info.elapsed_sec != null
        ? `<div><span>已加工</span><br><b>${mmss(info.elapsed_sec)}</b></div>` : ""}
      ${info.qtime_remaining_min != null
        ? `<div><span>Q-Time</span><br><b style="color:${info.qtime_remaining_min < 0 ? "var(--bad)" : "inherit"}">${
            info.qtime_remaining_min < 0
              ? `逾時 ${Math.abs(info.qtime_remaining_min).toFixed(0)} 分`
              : `剩 ${info.qtime_remaining_min.toFixed(0)} 分`}</b></div>` : ""}
    </div>`;

  // 阻擋原因刻意跟按鈕放在一起，不寫進上面會捲動的詳情區 ——
  // 「按鈕是灰的但看不到為什麼」正是這頁要消滅的情況
  $("#lot-checks").innerHTML =
    (check.blockers || []).map((b) =>
      `<div class="check block"><span class="icon">✕</span><span>${esc(b)}</span></div>`).join("") +
    (check.warnings || []).map((w) =>
      `<div class="check warn"><span class="icon">!</span><span>${esc(w)}</span></div>`).join("") +
    (!(check.blockers || []).length && !(check.warnings || []).length
      ? '<div class="check ok"><span class="icon">✓</span><span>所有前置檢查都通過</span></div>' : "");

  $("#lot-actions").innerHTML = (check.actions || []).map((a) => {
    const cls = a.action === "TRACK_IN" || a.action === "TRACK_OUT" || a.action === "ACK_SOP"
      ? "primary" : a.action === "HOLD" ? "danger" : "";
    return `<button class="${cls}" data-action="${esc(a.action)}" ${a.enabled ? "" : "disabled"}
              title="${esc(a.reason)}">${esc(a.label)}</button>`;
  }).join("");

  $("#lot-actions").querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => doAction(btn.dataset.action, check));
  });
}

async function doAction(action, check) {
  try {
    if (action === "TRACK_IN") {
      const result = await api("/api/lots/track-in", {
        method: "POST", body: { lot_id: check.lot_id, eq_id: state.eqId, remark: "" },
      });
      let detail = `${esc(check.lot_id)} 已在 ${esc(state.eqId)} 開始加工`;
      if (result.sampling) {
        const s = result.sampling;
        detail += s.decision === "SKIPPED"
          ? "<br>抽檢：本批跳批免驗"
          : `<br>抽檢：${s.decision === "FULL" ? "全檢" : `抽 ${s.sample_size} 顆`}` +
            (s.reject_number ? `，${s.reject_number} 顆不良即拒收` : "");
      }
      banner("ok", "進站成功", detail);
      await refresh(check.lot_id);
      return;
    }
    if (action === "TRACK_OUT") { openTrackOut(check); return; }
    if (action === "ACK_SOP") { await openSop(check); return; }
    if (action === "HOLD") {
      const remark = prompt("扣留原因？");
      if (remark === null) return;
      await api("/api/lots/hold", {
        method: "POST", body: { lot_id: check.lot_id, reason: "QUALITY", remark },
      });
      banner("warn", "已扣留", esc(check.lot_id));
      await refresh(check.lot_id);
      return;
    }
    if (action === "RELEASE") {
      await api("/api/lots/release", { method: "POST", body: { lot_id: check.lot_id, remark: "終端機放行" } });
      banner("ok", "已放行", esc(check.lot_id));
      await refresh(check.lot_id);
    }
  } catch (err) {
    banner("err", "動作被擋下", esc(err.message));
    await refresh(check.lot_id);
  }
}

async function refresh(lotId) {
  await loadStation();
  if (!lotId) return;
  try {
    const check = await api(
      `/api/terminal/lots/${encodeURIComponent(lotId)}/preflight?eq_id=${encodeURIComponent(state.eqId)}`);
    state.lot = check;
    renderLot(check);
  } catch { /* 批號可能已完工離站，維持目前畫面 */ }
}

/* ── 出站 ────────────────────────────────────────────── */
let trackOutLot = null;

function openTrackOut(check) {
  trackOutLot = check;
  const info = check.info || {};
  const expected = info.expected_output ?? check.qty;
  $("#to-lot").textContent = check.lot_id;
  $("#to-summary").innerHTML =
    `進站 ${num(check.qty)} ${esc(check.unit_type)}　→　應產出 <b>${num(expected)} ${esc(info.output_unit || "")}</b>` +
    `<br>良品 + 不良必須等於應產出量`;
  $("#to-good").value = String(expected);
  $("#to-reject").value = "0";
  setNumTarget($("#to-good"));
  loadDefectCodes(info.op_code);
  openModal("trackout");
}

async function loadDefectCodes(opCode) {
  try {
    const codes = await api("/api/master/defect-codes?limit=200");
    const items = (codes.items || codes).filter(
      (c) => !c.op_codes?.length || c.op_codes.includes(opCode));
    $("#to-defect").innerHTML = items.map((c) =>
      `<option value="${esc(c.code)}">${esc(c.code)} ${esc(c.name)}</option>`).join("");
  } catch { $("#to-defect").innerHTML = ""; }
}

$("#to-reject").addEventListener("input", syncDefectVisibility);
function syncDefectVisibility() {
  $("#to-defect-wrap").hidden = Number($("#to-reject").value || 0) <= 0;
}

[$("#to-good"), $("#to-reject")].forEach((input) => {
  input.addEventListener("focus", () => setNumTarget(input));
});
function setNumTarget(input) {
  state.numTarget = input;
  [$("#to-good"), $("#to-reject")].forEach((i) => i.classList.toggle("target", i === input));
}

// 觸控螢幕多半沒有實體鍵盤，也不想叫出會蓋住畫面的系統小鍵盤
$("#numpad").innerHTML = ["7", "8", "9", "4", "5", "6", "1", "2", "3", "清除", "0", "⌫"]
  .map((k) => `<button data-key="${k}">${k}</button>`).join("");
$("#numpad").querySelectorAll("[data-key]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = state.numTarget || $("#to-good");
    const key = btn.dataset.key;
    if (key === "清除") target.value = "";
    else if (key === "⌫") target.value = target.value.slice(0, -1);
    else target.value = (target.value === "0" ? "" : target.value) + key;
    syncDefectVisibility();
  });
});

$("#to-cancel").addEventListener("click", () => closeModal("trackout"));
$("#to-submit").addEventListener("click", async () => {
  const good = Number($("#to-good").value || 0);
  const reject = Number($("#to-reject").value || 0);
  const body = {
    lot_id: trackOutLot.lot_id, good_qty: good, reject_qty: reject,
    defects: reject > 0 ? [{ defect_code: $("#to-defect").value, qty: reject, remark: "" }] : [],
    materials: [], bin_map: null, remark: "",
  };
  try {
    const result = await api("/api/lots/track-out", { method: "POST", body });
    closeModal("trackout");
    const step = result.last_step || {};
    banner("ok", "出站成功",
      `${esc(trackOutLot.lot_id)}｜良品 ${num(step.qty_good)}／不良 ${num(step.qty_reject)}` +
      `｜站良率 ${((step.step_yield || 0) * 100).toFixed(2)}%` +
      (result.tool_alerts?.length
        ? `<br><b>治具需更換：${esc(result.tool_alerts.map((t) => t.tool_id).join(", "))}</b>` : ""));
    await refresh(trackOutLot.lot_id);
  } catch (err) {
    banner("err", "出站被擋下", esc(err.message));
  }
});

/* ── SOP 簽認 ────────────────────────────────────────── */
let sopDoc = null;

async function openSop(check) {
  const meta = check.info?.sop;
  if (!meta) { banner("warn", "沒有需要簽認的 SOP"); return; }
  try {
    const doc = await api(`/api/sops/${encodeURIComponent(meta.sop_code)}?version=${meta.version}`);
    sopDoc = doc;
    $("#sop-title").textContent = `${doc.sop_code} v${doc.version}｜${doc.title}`;
    $("#sop-body").innerHTML = `
      <p>${esc(doc.summary || "")}</p>
      <ol>${(doc.steps || []).map((s) => `
        <li><b>${esc(s.instruction)}</b>
          ${s.checkpoint ? `<div class="cp">檢查點：${esc(s.checkpoint)}</div>` : ""}
        </li>`).join("")}</ol>
      ${doc.hazards ? `<p><b>風險：</b>${esc(doc.hazards)}</p>` : ""}
      ${(doc.ppe || []).length ? `<p><b>防護具：</b>${esc(doc.ppe.join("、"))}</p>` : ""}`;
    openModal("sop");
  } catch (err) { banner("err", "無法讀取 SOP", esc(err.message)); }
}

$("#sop-cancel").addEventListener("click", () => closeModal("sop"));
$("#sop-ack").addEventListener("click", async () => {
  try {
    await api("/api/sops/acknowledge", {
      method: "POST", body: { sop_code: sopDoc.sop_code, version: sopDoc.version },
    });
    closeModal("sop");
    banner("ok", "已簽認", `${esc(sopDoc.sop_code)} v${sopDoc.version}`);
    if (state.lot) await refresh(state.lot.lot_id);
  } catch (err) { banner("err", "簽認失敗", esc(err.message)); }
});

/* ── 時鐘與自動更新 ──────────────────────────────────── */
setInterval(() => {
  $("#clock").textContent = new Date().toLocaleTimeString("zh-TW", { hour12: false });
}, 1000);

// 佇列與機台狀態會被別人改動，定期回來對一次
setInterval(() => {
  if ($("#work").classList.contains("active") && !document.querySelector(".modal.active")) {
    loadStation().catch(() => { /* 網路不穩時安靜重試，不要洗版 */ });
  }
}, 30000);

/* ── 啟動 ────────────────────────────────────────────── */
(async function boot() {
  if (!state.token) { show("login"); return; }
  try {
    state.user = await api("/api/auth/me");
    await afterLogin();
  } catch {
    logout();
  }
})();
