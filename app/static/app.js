/* Git regression localization console - vanilla JS, no external deps. */
"use strict";

const LANE = 16;
const PAD = 11;

const STATE_LABEL = {
  "known-bad": "已知失败",
  "known-good": "已知通过",
  "test-fail": "实测失败",
  "test-pass": "实测通过",
  "deduced-fail": "推导失败",
  "deduced-pass": "推导通过",
  running: "实测中",
  skip: "跳过·未知",
  error: "出错·未知",
  pending: "待补测",
  outside: "区间外",
  unknown: "未知",
  "conflict-deduced": "推导冲突",
};

let historyData = null;
let bySha = new Map();
let parentMap = new Map();
let session = null;
let sessionId = null;
let pollTimer = null;
let expanded = new Set();

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function short(sha) { return sha ? sha.slice(0, 7) : "?"; }

function ancestorSet(sha) {
  const out = new Set();
  const stack = [sha];
  while (stack.length) {
    const cur = stack.pop();
    if (out.has(cur)) continue;
    out.add(cur);
    (parentMap.get(cur) || []).forEach((p) => stack.push(p));
  }
  return out;
}

/* ------------------------------------------------------------- health */
async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const j = await r.json();
    $("#health").textContent = j.ok ? "● 服务正常" : "异常";
    $("#health").classList.toggle("bad", !j.ok);
  } catch (e) {
    $("#health").textContent = "● 无法连接服务";
    $("#health").classList.add("bad");
  }
}

/* ------------------------------------------------------------ history */
async function loadHistory() {
  const r = await fetch("/api/history");
  historyData = await r.json();
  bySha = new Map(historyData.commits.map((c) => [c.sha, c]));
  parentMap = new Map(historyData.commits.map((c) => [c.sha, c.parents || []]));

  renderPresets();
  populateSelects();
  renderGraph();

  // Default selection from first preset.
  const p0 = historyData.presets[0];
  if (p0) {
    setSelectToRef("#bad-select", p0.bad);
    setSelectToRef("#good-select", p0.good);
    filterGoodOptions();
    markActivePreset(p0.id);
  }
}

function resolveRef(ref) {
  if (bySha.has(ref)) return ref;
  const hit = historyData.commits.find(
    (c) => c.sha.startsWith(ref) || c.short === ref ||
      (c.refs || []).some((x) => x === ref || x === "tag:" + ref)
  );
  return hit ? hit.sha : "";
}

function setSelectToRef(sel, ref) {
  const sha = resolveRef(ref);
  if (sha) $(sel).value = sha;
}

function optionLabel(c) {
  const refs = (c.refs || []).map((r) => `[${r}]`).join(" ");
  return `${c.short}  ${c.subject}  ${refs}`.trim();
}

function populateSelects() {
  const bad = $("#bad-select");
  const good = $("#good-select");
  bad.innerHTML = "";
  good.innerHTML = "";
  historyData.commits.forEach((c) => {
    bad.add(new Option(optionLabel(c), c.sha));
    good.add(new Option(optionLabel(c), c.sha));
  });
  bad.addEventListener("change", () => { filterGoodOptions(); markActivePreset(null); });
  good.addEventListener("change", () => markActivePreset(null));
}

function filterGoodOptions() {
  const badSha = $("#bad-select").value;
  const anc = badSha ? ancestorSet(badSha) : null;
  const good = $("#good-select");
  Array.from(good.options).forEach((op) => {
    const ok = !anc || anc.has(op.value);
    op.disabled = !ok;
    op.textContent = optionLabel(bySha.get(op.value)) + (ok ? "" : "（非祖先）");
  });
  // If the current good is no longer a valid ancestor, pick the baseline
  // when possible, otherwise the oldest enabled ancestor in the list.
  const currentBad = good.selectedOptions[0] && good.selectedOptions[0].disabled;
  if (!good.value || currentBad) {
    const baseline = resolveRef("baseline-good");
    if (baseline && (!anc || anc.has(baseline))) {
      good.value = baseline;
    } else {
      const enabled = Array.from(good.options).filter((o) => !o.disabled);
      if (enabled.length) good.value = enabled[enabled.length - 1].value;
    }
  }
}

function renderPresets() {
  const box = $("#presets");
  box.innerHTML = "";
  historyData.presets.forEach((p) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "preset";
    b.dataset.testid = "preset-" + p.id;
    b.innerHTML = `<b>${esc(p.label)}</b><span>${esc(p.description)}</span>`;
    b.addEventListener("click", () => {
      setSelectToRef("#bad-select", p.bad);
      setSelectToRef("#good-select", p.good);
      filterGoodOptions();
      markActivePreset(p.id);
      startSession(p.bad, p.good);
    });
    box.appendChild(b);
  });
}

function markActivePreset(id) {
  document.querySelectorAll(".preset").forEach((el, i) => {
    el.classList.toggle("active", historyData.presets[i] && historyData.presets[i].id === id);
  });
}

/* -------------------------------------------------------------- start */
async function startSession(badRef, goodRef) {
  const bad = badRef || $("#bad-select").value;
  const good = goodRef || $("#good-select").value;
  const errBox = $("#form-error");
  errBox.classList.add("hidden");
  $("#start-btn").disabled = true;
  try {
    const r = await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bad, good }),
    });
    const data = await r.json();
    if (!r.ok) {
      errBox.textContent = data.error || "启动失败";
      errBox.classList.remove("hidden");
      $("#start-btn").disabled = false;
      return;
    }
    sessionId = data.id;
    session = data;
    expanded = new Set();
    try { localStorage.setItem("gsbSessionId", data.id); } catch (e) { /* private mode */ }
    $("#new-round-btn").classList.remove("hidden");
    renderAll();
    schedulePoll(true);
  } catch (e) {
    errBox.textContent = "请求失败：" + e;
    errBox.classList.remove("hidden");
    $("#start-btn").disabled = false;
  }
}

function schedulePoll(immediate) {
  if (pollTimer) clearInterval(pollTimer);
  const tick = async () => {
    if (!sessionId) return;
    try {
      const r = await fetch(`/api/sessions/${sessionId}`);
      if (!r.ok) return;
      session = await r.json();
      renderAll();
      if (session.status === "complete" || session.status === "conflict") {
        clearInterval(pollTimer);
        pollTimer = null;
        $("#start-btn").disabled = false;
      }
    } catch (e) { /* transient; keep polling */ }
  };
  pollTimer = setInterval(tick, 1000);
  if (immediate) /* state already rendered from POST */;
}

/* -------------------------------------------------------------- graph */
const STATE_HEX = {
  "known-bad": "#d92d20",
  "test-fail": "#d92d20",
  "known-good": "#12805c",
  "test-pass": "#12805c",
  "deduced-fail": "#f0948b",
  "deduced-pass": "#7ac7a9",
  running: "#2e6be6",
  skip: "#b54708",
  error: "#b54708",
  pending: "#98a2b3",
  outside: "#d0d5dd",
  idle: "#98a2b3",
};

function laneWidth() {
  const total = (historyData && historyData.commits[0] &&
    historyData.commits[0].layout && historyData.commits[0].layout.lanes) || 1;
  return PAD * 2 + Math.max(0, total - 1) * LANE + 4;
}

function xLane(i) { return PAD + i * LANE; }

/* One absolutely-positioned SVG measured from the real row positions, so
 * edges always meet their dots even if a row wraps or is expanded. */
function drawLaneOverlay() {
  const box = document.querySelector("#graph");
  if (!box || !historyData) return;
  const rowsWrap = box.querySelector(".rows");
  const svg = box.querySelector(".lane-overlay");
  if (!rowsWrap || !svg) return;

  const rr = rowsWrap.getBoundingClientRect();
  const w = Math.max(rr.width, laneWidth());
  const h = Math.max(rr.height, 1);
  svg.setAttribute("width", w);
  svg.setAttribute("height", h);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);

  const yBySha = {};
  rowsWrap.querySelectorAll(".commit-main[data-sha]").forEach((el) => {
    const r = el.getBoundingClientRect();
    yBySha[el.dataset.sha] = r.top - rr.top + r.height / 2;
  });

  let paths = "";
  let halos = "";
  let dots = "";

  historyData.commits.forEach((c) => {
    const L = c.layout;
    const yc = yBySha[c.sha];
    if (!L || yc == null) return;
    const xc = xLane(L.lane);

    Object.entries(L.parent_lanes || {}).forEach(([p, pl]) => {
      const yp = yBySha[p];
      if (yp == null) return;
      const xp = xLane(pl);
      const bend = Math.max(8, (yp - yc) / 2);
      paths += `<path d="M ${xc} ${yc} C ${xc} ${yc + bend}, ${xp} ${yp - bend}, ${xp} ${yp}"
                 fill="none" stroke="#c3cadb" stroke-width="2"/>`;
    });

    const node = session ? session.nodes[c.sha] : null;
    const state = node ? node.state : "idle";
    const color = STATE_HEX[state] || "#98a2b3";

    if (node && node.is_conflict) {
      halos += `<circle cx="${xc}" cy="${yc}" r="10" fill="none"
                 stroke="#c01048" stroke-width="2.5"/>`;
    } else if (node && node.is_candidate) {
      halos += `<circle cx="${xc}" cy="${yc}" r="9.5" fill="none"
                 stroke="#b54708" stroke-width="2" stroke-dasharray="3 2"/>`;
    }

    if (state === "running") {
      dots += `<circle cx="${xc}" cy="${yc}" r="6" fill="none" stroke="${color}" stroke-width="2">
          <animate attributeName="r" values="6;13;6" dur="1.2s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values="0.8;0;0.8" dur="1.2s" repeatCount="indefinite"/>
        </circle>`;
    }
    dots += `<circle cx="${xc}" cy="${yc}" r="6" fill="${color}" stroke="#ffffff" stroke-width="2"/>`;
  });

  svg.innerHTML = paths + halos + dots;
}

function renderGraph() {
  const box = $("#graph");
  if (!historyData) return;
  const nodes = session ? session.nodes : {};
  box.innerHTML = `<div class="rows">
      <svg class="lane-overlay" data-testid="lane-overlay" aria-hidden="true"></svg>
      ${historyData.commits.map((c) => {
    const n = nodes[c.sha];
    const state = n ? n.state : "idle";
    const isCand = n && n.is_candidate;
    const isConflict = n && n.is_conflict;
    const isOpen = expanded.has(c.sha);
    const rowCls = [
      "commit-row", `state-${state}`,
      isCand ? "candidate-row" : "",
      isConflict ? "conflict-row" : "",
      isOpen ? "expanded" : "",
    ].join(" ");

    const refs = (c.refs || []).map((r) => {
      const cls = r.startsWith("tag:") ? "ref-chip tag"
        : (r === "main" || r === "conflict-line") ? "ref-chip badref" : "ref-chip";
      return `<span class="${cls}">${esc(r)}</span>`;
    }).join("");

    let badges = "";
    if (n) {
      badges += `<span class="badge ${state}">${
        state === "running" ? '<span class="spinner"></span>' : ""
      }${esc(STATE_LABEL[state] || state)}</span>`;
      if (isCand) badges += `<span class="badge candidate-badge">可能最早失败</span>`;
      if (isConflict) badges += `<span class="badge conflict-badge">冲突提交</span>`;
    }

    let why = "";
    if (n && n.deduced_from) {
      why = `<span class="why">由 ${short(n.deduced_from)} 的${
        state === "deduced-pass" ? "通过（祖先全部通过）" : "失败（后代全部失败）"
      }推导</span>`;
    }
    if (n && n.fact && n.fact.kind === "skip") {
      why += `<span class="why">跳过按未知处理，不产生推导</span>`;
    }

    const detail = isOpen ? renderDetail(c, n) : "";

    return `
      <div class="${rowCls}" data-testid="commit-row" data-sha="${c.sha}">
        <div class="commit-main" data-action="toggle" data-sha="${c.sha}">
          <div class="lane-cell" style="width:${laneWidth()}px"></div>
          <div class="commit-body">
            <div class="commit-line1">
              <span class="sha">${esc(c.short)}</span>
              <span class="subject" title="${esc(c.subject)}">${esc(c.subject)}</span>
              ${refs}
              ${badges}
            </div>
            <div class="commit-line2">
              <span>${esc(c.author || "")}</span>
              ${why}
              ${n && n.attempts > 1 ? `<span>实测次数：${n.attempts}</span>` : ""}
              ${n && n.last_job && n.last_job.duration ? `<span>实测耗时 ${n.last_job.duration}s</span>` : ""}
            </div>
          </div>
        </div>
        ${detail}
      </div>`;
  }).join("")}
    </div>`;

  // Synchronous draw: getBoundingClientRect forces layout, and painting has
  // not happened yet, so the very first frame already contains the lanes.
  drawLaneOverlay();
  requestAnimationFrame(drawLaneOverlay);

  box.querySelectorAll('[data-action="toggle"]').forEach((el) => {
    el.addEventListener("click", () => {
      const sha = el.dataset.sha;
      if (expanded.has(sha)) expanded.delete(sha); else expanded.add(sha);
      renderGraph();
    });
  });
  box.querySelectorAll("[data-retry]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      await retrySha(btn.dataset.retry);
    });
  });
  box.querySelectorAll("[data-set-role]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const sha = btn.dataset.sha;
      if (btn.dataset.setRole === "bad") {
        $("#bad-select").value = sha;
        filterGoodOptions();
      } else {
        $("#good-select").value = sha;
      }
      markActivePreset(null);
      document.querySelector(".setup-card").scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
}

function renderDetail(c, n) {
  const output = (n && n.fact && n.fact.output) || (n && n.last_job && n.last_job.output) || "";
  const files = (n && n.fact && n.fact.read_files) ||
    (n && n.last_job && n.last_job.read_files) || [];
  const canRetry = session && session.status === "running" &&
    n.fact && (n.fact.kind === "skip" || n.fact.kind === "error");
  const parentText = (c.parents || []).map((p) => short(p)).join("、") || "（根提交）";
  return `
    <div class="commit-detail" data-testid="commit-detail" onclick="event.stopPropagation()">
      <div class="detail-meta">
        <span>完整 SHA：<code>${esc(c.sha)}</code></span>
        <span>父提交：${esc(parentText)}</span>
        <span>${esc(c.date || "")}</span>
      </div>
      <div class="detail-actions">
        <button class="btn tiny" data-set-role="bad" data-sha="${c.sha}">设为已知失败端点</button>
        <button class="btn tiny" data-set-role="good" data-sha="${c.sha}">设为已通过祖先</button>
      </div>
      ${files.length ? `<div class="readfiles">该实测读取了提交内文件：` +
          files.map((f) => `<code>${esc(f.path)}</code>`).join("") + `</div>` : ""}
      ${output ? `<pre class="output">${esc(output)}</pre>` : ""}
      ${canRetry ? `<button class="btn tiny" data-retry="${c.sha}">重新实测此提交</button>` : ""}
    </div>`;
}

async function retrySha(sha) {
  if (!sessionId) return;
  const r = await fetch(`/api/sessions/${sessionId}/retry`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sha }),
  });
  const data = await r.json();
  if (!r.ok) { alert(data.error || "无法重测"); return; }
  session = data;
  renderAll();
  schedulePoll(false);
}

/* -------------------------------------------------------------- panels */
function renderStatus() {
  const el = $("#session-status");
  if (!session) {
    el.innerHTML = `<span class="status-pill idle">尚未启动</span>`;
    return;
  }
  const map = {
    running: ["running", session.draining ? "冲突核对中…" : "● 实测定位进行中"],
    complete: ["complete", "✓ 定位结束（无冲突）"],
    conflict: ["conflict", "⛔ 以冲突结束本轮"],
  };
  const [cls, text] = map[session.status] || ["idle", session.status];
  el.innerHTML = `
    <span class="status-pill ${cls}" data-testid="status-pill">${text}</span>
    <span class="session-meta">${esc(session.id)} · 用时 ${session.elapsed}s</span>`;
  $("#draining-note").classList.toggle("hidden", !(session.draining));
  $("#slot-count").textContent = `${session.active_count} / ${session.concurrency}`;
}

function renderActiveJobs() {
  const box = $("#active-jobs");
  if (!session) { box.innerHTML = `<div class="no-job">启动后最多同时运行 2 项实测</div>`; return; }
  if (!session.running_jobs.length) {
    box.innerHTML = `<div class="no-job">当前没有运行中的实测（空位 ${session.concurrency}/${session.concurrency}）</div>`;
    return;
  }
  box.innerHTML = session.running_jobs.map((j) => {
    const c = bySha.get(j.sha);
    const files = (j.read_files || []).map((f) => f.path).join(", ");
    return `
      <div class="job-card" data-testid="active-job" data-sha="${j.sha}">
        <div class="jtop">
          <span class="spinner" style="width:11px;height:11px;border:2px solid var(--run);border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite"></span>
          <span class="jsha">${esc(short(j.sha))}</span>
          <span>${esc(c ? c.subject : "")}</span>
          <span class="jelapsed">${j.elapsed}s</span>
        </div>
        <div class="jfiles">正在解出 ${esc(j.sha)} 的文件并执行 qa/run_check.py… 独立临时目录</div>
        ${files ? `<div class="jfiles">已读取：${esc(files)}</div>` : ""}
      </div>`;
  }).join("");
}

function renderStats() {
  const c = session ? session.counts : null;
  const set = (id, v) => { $(id).textContent = c ? v : "–"; };
  set("#stat-tested", c && c.tested);
  set("#stat-pass", c && c.passed);
  set("#stat-fail", c && c.failed);
  set("#stat-skip", c ? c.skipped + c.errors : null);
  set("#stat-dpass", c && c.deduced_pass);
  set("#stat-dfail", c && c.deduced_fail);
}

function renderCandidates() {
  const box = $("#candidates");
  if (!session) { box.innerHTML = `<div class="empty">启动后在这里列出候选。</div>`; return; }
  if (!session.candidates.length) {
    box.innerHTML = `<div class="empty">当前没有候选（可能已全部排除）。</div>`;
    return;
  }
  box.innerHTML = session.candidates.map((sha, i) => {
    const c = bySha.get(sha) || {};
    const n = session.nodes[sha] || {};
    let reason;
    if (n.state === "known-bad" || n.state === "test-fail") {
      reason = "已确认失败，且它的所有祖先都没有失败事实 —— 它就是最早失败点之一。";
    } else if (n.state === "skip") {
      reason = "该提交实测被跳过（按未知处理），祖先中无失败事实：它或其直接后代都可能是最早失败点。";
    } else if (n.state === "error") {
      reason = "该提交实测出错（按未知处理），仍可能是最早失败点，可展开后手动重测。";
    } else if (n.state === "running") {
      reason = "正在实测；在结果出来之前仍属于可能的最早失败提交。";
    } else {
      reason = "尚无结果且无失败祖先，理论上仍可能是最早失败提交。";
    }
    return `
      <div class="cand ${n.state === "skip" || n.state === "error" ? "skip-cand" : ""}"
           data-testid="candidate-item" data-sha="${sha}">
        <div class="idx">${i + 1}</div>
        <div class="cbody">
          <div class="cline1">
            <span class="sha">${esc(short(sha))}</span>
            <span class="csubj">${esc(c.subject || "")}</span>
            <span class="badge ${n.state}">${esc(STATE_LABEL[n.state] || n.state || "")}</span>
          </div>
          <div class="creason">${esc(reason)}</div>
        </div>
      </div>`;
  }).join("");
}

function renderConflicts() {
  const panel = $("#conflict-panel");
  if (!session || !session.conflicts.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("#conflict-list").innerHTML = session.conflicts.map((p) => {
    const f = bySha.get(p.fail) || {};
    const ps = bySha.get(p.pass) || {};
    return `
      <div class="conflict-pair" data-testid="conflict-pair"
           data-fail="${p.fail}" data-pass="${p.pass}">
        <div class="pair-row">
          <span class="badge test-fail">✗ 祖先失败 ${esc(short(p.fail))}</span>
          <span>${esc(f.subject || "")}</span>
        </div>
        <div class="pair-row" style="margin-top:5px">
          <span class="pair-arrow">↓ 是 … 的祖先，而 …</span>
        </div>
        <div class="pair-row">
          <span class="badge test-pass">✓ 后代通过 ${esc(short(p.pass))}</span>
          <span>${esc(ps.subject || "")}</span>
        </div>
        <div class="pair-note">单调推导失效：按要求标记这两个提交，并以冲突结束本轮。</div>
      </div>`;
  }).join("");
}

function renderEvents() {
  const box = $("#events");
  if (!session) { box.innerHTML = ""; return; }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = session.events.map((e) => `
    <div class="event ${esc(e.level)}">
      <span class="t">${new Date(e.t * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</span>
      <span class="text">${esc(e.text)}</span>
    </div>`).join("");
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderAll() {
  renderControls();
  renderStatus();
  renderActiveJobs();
  renderStats();
  renderCandidates();
  renderConflicts();
  renderEvents();
  renderGraph();
}

function renderControls() {
  const busy = !!(session && session.status === "running");
  const startBtn = $("#start-btn");
  startBtn.disabled = busy;
  startBtn.textContent = busy ? "测试进行中…" : "▶ 启动测试";
  document.querySelectorAll(".preset").forEach((b) => { b.disabled = busy; });
  const nr = $("#new-round-btn");
  if (session) {
    nr.classList.remove("hidden");
    nr.disabled = busy;
    nr.textContent = busy ? "本轮实测未结束，请稍候" : "发起新一轮";
    nr.title = busy ? "已开跑的测试必须等待实测完成并计入结论" : "清空视图并选择新的区间";
  } else {
    nr.classList.add("hidden");
  }
}

/* -------------------------------------------------------------- boot */
$("#start-btn").addEventListener("click", () => startSession());
$("#new-round-btn").addEventListener("click", () => {
  if ($("#new-round-btn").disabled) return;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  session = null;
  sessionId = null;
  expanded = new Set();
  try { localStorage.removeItem("gsbSessionId"); } catch (e) { /* ignore */ }
  renderAll();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

async function reattach() {
  let saved = null;
  try { saved = localStorage.getItem("gsbSessionId"); } catch (e) { /* ignore */ }
  if (!saved) return;
  try {
    const r = await fetch(`/api/sessions/${saved}`);
    if (!r.ok) {
      try { localStorage.removeItem("gsbSessionId"); } catch (e) { /* ignore */ }
      return;
    }
    session = await r.json();
    sessionId = session.id;
    $("#new-round-btn").classList.remove("hidden");
    renderAll();
    if (session.status === "running") schedulePoll(true);
  } catch (e) { /* server not reachable yet */ }
}

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(drawLaneOverlay, 120);
});

checkHealth();
setInterval(checkHealth, 15000);
loadHistory()
  .then(reattach)
  .catch((e) => {
    $("#graph").innerHTML = `<div class="empty">读取历史失败：${esc(e)}<br>请确认服务已启动并已构建内置仓库。</div>`;
  });
