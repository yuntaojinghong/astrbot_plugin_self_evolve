/* 自进化 · 配置面板
 * 只做「查看 / 审批 / 回滚」——不提供直接修改策略分数的入口，
 * 因为分数来自学习。要人工干预请用经验条目与版本回滚。 */

const state = {
  groups: [],
  current: null,
  detail: null,
  tab: "learned",
  busy: false,
};

/* ============================ 基础设施 ============================ */

function $(sel) {
  return document.querySelector(sel);
}

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, v);
    }
  }
  for (const c of [].concat(children || [])) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/* AstrBot 的页面 bridge：没有它就无法与后端通信 */
function bridge() {
  if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  if (window.parent && window.parent.AstrBotPluginPage) return window.parent.AstrBotPluginPage;
  return null;
}

async function apiGet(endpoint, params) {
  const b = bridge();
  if (!b) throw new Error("未检测到 AstrBot 页面通信接口，请从插件管理页面打开本面板。");
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  return b.apiGet(endpoint + qs);
}

async function apiPost(endpoint, body) {
  const b = bridge();
  if (!b) throw new Error("未检测到 AstrBot 页面通信接口，请从插件管理页面打开本面板。");
  return b.apiPost(endpoint, body || {});
}

function showError(msg) {
  const box = $("#error");
  if (!msg) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = msg;
}

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function pct(v) {
  return `${(Number(v || 0) * 100).toFixed(0)}%`;
}

const SIGNAL_LABEL = {
  praise: "明确肯定",
  thanks: "礼貌收尾",
  criticism: "明确否定",
  correction: "用户纠正",
  reask: "追问（隐式）",
  continue: "顺着聊（隐式）",
  stop: "放弃（隐式）",
};

/* ============================ 加载 ============================ */

async function loadAll() {
  showError("");
  try {
    const boot = await apiGet("bootstrap");
    if (boot && boot.version) {
      const vb = $("#versionBadge");
      vb.textContent = "v" + String(boot.version).replace(/^v/, "");
      vb.hidden = false;
    }
    state.boot = boot || {};
    applyPauseState();
    const data = await apiGet("overview");
    state.groups = (data && data.groups) || [];
    renderGroups();
    if (state.current) {
      const still = state.groups.some((g) => g.group_id === state.current);
      if (still) await selectGroup(state.current);
      else renderPlaceholder();
    } else {
      renderPlaceholder();
    }
  } catch (e) {
    showError(String(e.message || e));
  }
}

function applyPauseState() {
  const paused = !!(state.boot && state.boot.paused);
  const btn = $("#btnPause");
  btn.textContent = paused ? "恢复学习" : "暂停学习";
  btn.classList.toggle("primary", paused);
  const off = state.boot && state.boot.enabled === false;
  if (off) {
    btn.textContent = "总开关已关闭";
    btn.disabled = true;
  } else {
    btn.disabled = false;
  }
}

function renderGroups() {
  const box = $("#groupList");
  box.innerHTML = "";
  if (!state.groups.length) {
    box.append(el("p", { class: "empty-hint", text: "还没有任何学习痕迹。\n等群里和机器人聊过几轮后再来看。" }));
    return;
  }
  for (const g of state.groups) {
    const active = g.group_id === state.current;
    box.append(el("button", {
      class: "group-item" + (active ? " active" : ""),
      onClick: () => selectGroup(g.group_id),
    }, [
      el("div", { class: "group-line" }, [
        el("span", { class: "group-id", text: g.group_id }),
        g.pending ? el("span", { class: "tag warn", text: `${g.pending} 待批` }) : null,
      ]),
      el("div", { class: "group-stats" }, [
        el("span", { text: `经验 ${g.entries_alive}` }),
        el("span", { text: `反馈 ${g.feedback}` }),
        el("span", { text: `注入 ${g.injections}` }),
      ]),
    ]));
  }
}

function renderPlaceholder() {
  const box = $("#content");
  box.innerHTML = "";
  box.append(el("div", { class: "placeholder" }, [
    el("div", { class: "placeholder-icon", text: "🧠" }),
    el("p", { text: "从左侧选择一个群，查看它学到了什么。" }),
  ]));
}

async function selectGroup(gid) {
  state.current = gid;
  renderGroups();
  try {
    state.detail = await apiGet("group", { group_id: gid });
    renderDetail();
  } catch (e) {
    showError(String(e.message || e));
  }
}

/* ============================ 详情 ============================ */

const TABS = [
  ["learned", "学到的倾向"],
  ["entries", "经验条目"],
  ["pending", "待批候选"],
  ["audit", "变更依据"],
  ["history", "版本与回滚"],
];

function renderDetail() {
  const d = state.detail;
  const box = $("#content");
  box.innerHTML = "";
  if (!d) return;

  const st = d.stats || {};
  box.append(el("div", { class: "panel-head" }, [
    el("div", {}, [
      el("h2", { text: `群 ${d.group_id}` }),
      el("p", { class: "sub", text: `经验 ${st.alive || 0} 条生效 / 共 ${st.total || 0} 条 · 已归因 ${st.feedback || 0} 次反馈 · 注入 ${st.injections || 0} 次` }),
    ]),
    el("button", {
      class: "btn",
      text: "立即反思",
      onClick: doReflect,
    }),
  ]));

  if (st.blocked_injections) {
    box.append(el("div", { class: "alert info" }, [
      el("span", { text: `🛡 已拦截 ${st.blocked_injections} 条疑似指令性内容，未注入（详情见经验条目里的"已失效"项）` }),
    ]));
  }
  const dims = d.dimensions || [];
  const learning = dims.some((r) => r.chosen !== null);
  if (!learning) {
    box.append(el("div", { class: "alert info" }, [
      el("span", { text: `⏳ 证据还在积累：每个风格档位需要至少 ${(state.boot && state.boot.config && state.boot.config.min_samples) || 5} 次反馈才会影响行为。在此之前机器人保持默认风格。` }),
    ]));
  }

  const tabs = el("div", { class: "tabs" });
  for (const [key, label] of TABS) {
    const count = key === "pending" ? (d.pending || []).length
      : key === "entries" ? (d.entries || []).length
        : key === "audit" ? (d.audit || []).length
          : key === "history" ? (d.history || []).length : 0;
    tabs.append(el("button", {
      class: "tab" + (state.tab === key ? " active" : ""),
      onClick: () => { state.tab = key; renderDetail(); },
    }, [
      el("span", { text: label }),
      count ? el("span", { class: "tab-count", text: String(count) }) : null,
    ]));
  }
  box.append(tabs);

  const body = el("div", { class: "tab-body" });
  if (state.tab === "learned") body.append(renderLearned(d));
  else if (state.tab === "entries") body.append(renderEntries(d));
  else if (state.tab === "pending") body.append(renderPending(d));
  else if (state.tab === "audit") body.append(renderAudit(d));
  else body.append(renderHistory(d));
  box.append(body);
}

function renderLearned(d) {
  const wrap = el("div", { class: "dims" });
  const meta = {};
  for (const m of (state.boot && state.boot.dimensions) || []) meta[m.key] = m;

  for (const row of d.dimensions || []) {
    const m = meta[row.dimension] || {};
    const levels = m.levels || [];
    const maxAbs = Math.max(1e-6, ...(row.cells || []).map((c) => Math.abs(c.score)));

    const cells = (row.cells || []).map((c) => {
      const w = Math.round((Math.abs(c.score) / maxAbs) * 100);
      const isChosen = row.chosen === c.level;
      const isBase = c.level === row.baseline;
      return el("div", {
        class: "cell" + (isChosen ? " chosen" : "") + (isBase ? " base" : "") + (c.eligible ? "" : " weak"),
        title: `${c.label}｜分数 ${c.score}｜被选中 ${c.pulls} 次${c.eligible ? "" : "（证据不足）"}`,
      }, [
        el("div", { class: "bar-wrap" }, [
          el("div", {
            class: "bar" + (c.score < 0 ? " neg" : ""),
            style: `width:${Math.max(2, w)}%`,
          }),
        ]),
        el("div", { class: "cell-label" }, [
          el("span", { text: c.label }),
          el("span", { class: "cell-pulls", text: `${c.pulls}次` }),
        ]),
      ]);
    });

    wrap.append(el("div", { class: "dim" }, [
      el("div", { class: "dim-head" }, [
        el("strong", { text: (m.label || row.dimension) }),
        row.fallback
          ? el("span", { class: "tag", text: "证据不足 · 用默认档" })
          : el("span", { class: "tag ok", text: row.offset ? `偏移 ${(row.offset * 100).toFixed(1)}%` : "默认档" }),
      ]),
      el("div", { class: "cells" }, cells),
      m.hints && row.chosen !== null && m.hints[row.chosen]
        ? el("p", { class: "hint", text: `实际注入给模型的指令：${m.hints[row.chosen]}` })
        : null,
    ]));
  }

  wrap.append(el("p", { class: "foot-note", text: "分数来自真实反馈；条越长表示该档位越被这个群接受。灰色条表示被选中次数还没达到门槛，暂不影响行为。" }));
  return wrap;
}

function renderEntries(d) {
  const wrap = el("div", {});
  const items = d.entries || [];
  if (!items.length) {
    wrap.append(el("p", { class: "empty-hint", text: "还没有沉淀出经验条目。\n反复出现的信息才会入库；用户明确纠正也会立即入库。" }));
    return wrap;
  }
  for (const e of items) {
    wrap.append(el("div", { class: "card" + (e.alive ? "" : " dead") }, [
      el("div", { class: "card-head" }, [
        el("span", { class: "tag", text: e.kind_label }),
        e.pinned ? el("span", { class: "tag ok", text: "已固定" }) : null,
        e.alive ? null : el("span", { class: "tag warn", text: "已停止注入" }),
        el("span", { class: "grow" }),
        el("button", {
          class: "btn tiny danger",
          text: "删除",
          onClick: () => doForget(e.eid),
        }),
      ]),
      el("p", { class: "card-body", text: e.content }),
      el("p", { class: "card-meta", text: `置信 ${e.effective_confidence}（记录值 ${e.confidence}）· 印证 ${e.evidence} 次 · 来源 ${e.source} · 最近印证 ${fmtTime(e.last_seen)}` }),
    ]));
  }
  return wrap;
}

function renderPending(d) {
  const wrap = el("div", {});
  const items = d.pending || [];
  if (!items.length) {
    wrap.append(el("p", { class: "empty-hint", text: "待批区是空的。\n点右上角「立即反思」让模型复盘最近的互动。" }));
    return wrap;
  }
  wrap.append(el("div", { class: "row-actions" }, [
    el("button", { class: "btn primary", text: "全部采纳", onClick: () => doApprove("all") }),
    el("button", { class: "btn", text: "全部驳回", onClick: () => doReject("all") }),
  ]));
  for (const p of items) {
    wrap.append(el("div", { class: "card pending" }, [
      el("div", { class: "card-head" }, [
        el("span", { class: "tag", text: p.kind_label || p.entry_kind || p.kind }),
        p.confidence !== null && p.confidence !== undefined
          ? el("span", { class: "tag", text: `置信 ${p.confidence}` }) : null,
        el("span", { class: "grow" }),
        el("button", { class: "btn tiny primary", text: "采纳", onClick: () => doApprove([p.pid]) }),
        el("button", { class: "btn tiny", text: "驳回", onClick: () => doReject([p.pid]) }),
      ]),
      el("p", { class: "card-body", text: p.content }),
      el("p", { class: "card-meta", text: `${p.reason || ""} · ${fmtTime(p.created)}` }),
    ]));
  }
  wrap.append(el("p", { class: "foot-note", text: "反思产出不会自动生效——模型总结的内容可能把玩笑当群规、把个人偏好当全群偏好，因此必须人工确认。" }));
  return wrap;
}

function renderAudit(d) {
  const wrap = el("div", {});
  const items = d.audit || [];
  if (!items.length) {
    wrap.append(el("p", { class: "empty-hint", text: "还没有反馈记录。\n归因发生在「用户对机器人上一条回复做出反应」时。" }));
    return wrap;
  }
  for (const a of items) {
    const arrow = a.polarity > 0 ? "👍" : (a.polarity < 0 ? "👎" : "•");
    const picks = (a.choice && a.choice.picks) || {};
    const pickTxt = Object.entries(picks).map(([k, v]) => `${k}=${v}`).join(" ");
    const upd = (a.updates || []).slice(0, 2).map(
      (u) => `${u.dimension}: ${u.before}→${u.after}`
    ).join(" · ");
    wrap.append(el("div", { class: "card audit" }, [
      el("div", { class: "card-head" }, [
        el("span", { class: "signal" + (a.polarity > 0 ? " pos" : a.polarity < 0 ? " neg" : ""), text: arrow }),
        el("span", { class: "tag", text: SIGNAL_LABEL[a.signal] || a.signal }),
        el("span", { class: "tag", text: `权重 ${a.weight}` }),
        el("span", { class: "grow" }),
        el("span", { class: "card-meta", text: fmtTime(a.created) }),
      ]),
      a.user_message ? el("p", { class: "card-body", text: `用户：${a.user_message}` }) : null,
      a.bot_reply ? el("p", { class: "card-meta", text: `机器人当时回复：${a.bot_reply}` }) : null,
      a.evidence ? el("p", { class: "card-meta", text: `判定依据：${a.evidence}` }) : null,
      a.correction ? el("p", { class: "card-meta", text: `抽取到的正确内容：${a.correction}` }) : null,
      pickTxt ? el("p", { class: "card-meta", text: `当时策略：${pickTxt}` }) : null,
      upd ? el("p", { class: "card-meta", text: `分数变化：${upd}` }) : null,
      a.note ? el("p", { class: "card-meta dim", text: a.note }) : null,
    ]));
  }
  return wrap;
}

function renderHistory(d) {
  const wrap = el("div", {});
  const items = d.history || [];
  if (!items.length) {
    wrap.append(el("p", { class: "empty-hint", text: "还没有版本记录。\n每次学习生效后会自动保存一个可回滚的快照。" }));
    return wrap;
  }
  for (const h of items) {
    const sum = Object.entries(h.summary || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
    wrap.append(el("div", { class: "card" }, [
      el("div", { class: "card-head" }, [
        el("span", { class: "tag", text: fmtTime(h.created) }),
        el("span", { class: "grow" }),
        el("button", {
          class: "btn tiny", text: "回滚到此处",
          onClick: () => doRollback(h.sid),
        }),
      ]),
      el("p", { class: "card-body", text: h.reason || "（无说明）" }),
      sum ? el("p", { class: "card-meta", text: sum }) : null,
    ]));
  }
  wrap.append(el("p", { class: "foot-note", text: "回滚会覆盖该群当前的学习状态（经验条目与策略分数），并作为一次变更记入审计。" }));
  return wrap;
}

/* ============================ 动作 ============================ */

async function withBusy(fn) {
  if (state.busy) return;
  state.busy = true;
  try {
    await fn();
  } catch (e) {
    showError(String(e.message || e));
  } finally {
    state.busy = false;
  }
}

async function reloadDetail(message) {
  if (message) showError("");
  state.detail = await apiGet("group", { group_id: state.current });
  renderDetail();
  const data = await apiGet("overview");
  state.groups = (data && data.groups) || [];
  renderGroups();
  if (message) flash(message);
}

function flash(msg) {
  const box = $("#error");
  box.hidden = false;
  box.className = "alert info";
  box.textContent = msg;
  setTimeout(() => {
    box.className = "alert error";
    box.hidden = true;
  }, 4000);
}

function doApprove(ids) {
  return withBusy(async () => {
    const r = await apiPost("approve", { group_id: state.current, ids });
    await reloadDetail((r && r.message) || "已采纳");
  });
}

function doReject(ids) {
  return withBusy(async () => {
    const r = await apiPost("reject", { group_id: state.current, ids });
    await reloadDetail((r && r.message) || "已驳回");
  });
}

function doForget(eid) {
  return withBusy(async () => {
    const r = await apiPost("forget", { group_id: state.current, eid });
    await reloadDetail((r && r.message) || "已删除");
  });
}

function doRollback(sid) {
  return withBusy(async () => {
    const r = await apiPost("rollback", { group_id: state.current, sid });
    await reloadDetail((r && r.message) || "已回滚");
  });
}

function doReflect() {
  return withBusy(async () => {
    const r = await apiPost("reflect", { group_id: state.current });
    state.tab = "pending";
    await reloadDetail((r && r.message) || "反思完成");
  });
}

/* ============================ 事件绑定 ============================ */

$("#btnReload").addEventListener("click", () => withBusy(loadAll));

$("#btnPause").addEventListener("click", () => withBusy(async () => {
  const paused = !!(state.boot && state.boot.paused);
  const r = await apiPost("switch", { action: paused ? "resume" : "pause" });
  state.boot.paused = !!(r && r.paused);
  applyPauseState();
  flash((r && r.message) || "已切换");
}));

loadAll();
