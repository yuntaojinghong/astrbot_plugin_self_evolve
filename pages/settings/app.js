/* 自进化 · 配置面板
 *
 * 三件事都在这一个页面里完成，不必再跳到 AstrBot 原生配置页：
 *   1. 全局设置：分组表单 + 一键预设 + 风险提示 + 改动预览，直接写回配置
 *   2. 学习结果：每个群学到了什么、为什么（含注入内容预览）
 *   3. 审批与回滚：反思候选审批、经验删除、版本回退
 *
 * 刻意**不提供**直接修改策略分数的入口——分数来自学习，手改会破坏"可解释"。
 * 要人工干预请用经验条目与版本回滚。 */

const state = {
  groups: [],
  current: null,
  detail: null,
  tab: "learned",
  busy: false,
  settings: null,    // /config 返回的描述（分组/预设/当前值）
  draft: {},         // 未保存的改动
  probe: "",         // 预览用的探测语句
  preview: null,
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

/* AstrBot 的页面 bridge。
 *
 * 面板跑在 AstrBot 的受限 iframe 里，SDK 会以全局 `window.AstrBotPluginPage`
 * 的形式**注入到本页面自己的 window** 上——**不是** window.parent 上的属性。
 *
 * 千万不要去读 window.parent.*：面板 iframe 是 sandbox 出来的（origin 为 "null"），
 * 跨源读父窗口属性会直接抛 SecurityError，SDK 会把它报成
 * 「Blocked a frame with origin "null" from accessing a cross-origin frame」，
 * 整个面板就此打不开。所以这里只碰自己 window 上的这个键。
 *
 * 每次都重新取，不缓存：脚本可能在 SDK 注入之前就执行完了。
 */
function bridge() {
  try {
    return window.AstrBotPluginPage || null;
  } catch (e) {
    return null;
  }
}

const NO_BRIDGE =
  "未检测到 AstrBot 页面通信接口。请从「插件管理」里点开本插件页面，不要直接访问该地址。";

async function apiGet(endpoint, params) {
  const b = bridge();
  if (!b) throw new Error(NO_BRIDGE);
  return b.apiGet(endpoint, params || {});
}

async function apiPost(endpoint, body) {
  const b = bridge();
  if (!b) throw new Error(NO_BRIDGE);
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
  const b = bridge();
  if (!b) {
    showError(NO_BRIDGE);
    renderPlaceholder();
    return;
  }
  try {
    // bridge 就绪后能拿到宿主上下文（主题等）。SDK 版本不一，这些方法都可能有也可能没有，
    // 因此逐个判存在再调；失败也不影响数据加载。
    try {
      if (typeof b.ready === "function") {
        applyTheme(await b.ready());
      }
      if (typeof b.onContext === "function") {
        b.onContext((ctx) => applyTheme(ctx));
      }
    } catch (e) {
      /* 忽略：主题拿不到不影响功能 */
    }

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

function applyTheme(context) {
  const isDark = context && context.isDark;
  if (isDark === undefined) return;
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
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
    // 注意：外层不能是 <button>，否则删除按钮会嵌套在按钮里（HTML 非法且点击会串）。
    // 所以用 div 容器 + 内部两个按钮。
    box.append(el("div", { class: "group-item" + (active ? " active" : "") }, [
      el("button", {
        class: "group-main",
        title: `查看 ${g.group_id} 的学习详情`,
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
      ]),
      el("button", {
        class: "group-del",
        title: "删除这个会话的学习数据",
        text: "✕",
        onClick: (ev) => {
          if (ev && ev.stopPropagation) ev.stopPropagation();
          doDeleteGroup(g.group_id);
        },
      }),
    ]));
  }
}

function renderPlaceholder() {
  const box = $("#content");
  box.innerHTML = "";

  const boot = state.boot || {};
  const cfg = boot.config || {};
  const minSamples = Number(cfg.min_samples ?? 5) || 5;
  const groups = state.groups || [];

  // 汇总所有群的情况，让「还没选群」这一屏也有信息量，
  // 而不是只有一个脑花图标加一行提示。
  let feedback = 0;
  let entries = 0;
  let pending = 0;
  let injections = 0;
  for (const g of groups) {
    feedback += Number(g.feedback || 0);
    entries += Number(g.entries_alive || 0);
    pending += Number(g.pending || 0);
    injections += Number(g.injections || 0);
  }

  const wrap = el("div", { class: "overview-wrap" });

  wrap.append(el("div", { class: "panel-head" }, [
    el("div", {}, [
      el("h2", { text: "学习概览" }),
      el("p", {
        class: "sub",
        text: groups.length
          ? `${groups.length} 个群有学习痕迹 · 共 ${entries} 条生效经验 · ${feedback} 次反馈归因`
          : "还没有任何学习痕迹。右下角的「全局设置」里可以先确认插件已开启。",
      }),
    ]),
    el("button", {
      class: "btn primary",
      text: "全局设置",
      onClick: () => openSettings(),
    }),
  ]));

  // 四个统计块
  const tiles = el("div", { class: "tiles" });
  const items = [
    ["活跃群", String(groups.length), "有学习痕迹的群数量"],
    ["生效经验", String(entries), "会参与注入的条目数"],
    ["反馈归因", String(feedback), "用户对上一次回复做出反应的次数"],
    ["注入次数", String(injections), "学到的内容被拼进提示词的次数"],
  ];
  for (const [label, value, hint] of items) {
    tiles.append(el("div", { class: "tile", title: hint }, [
      el("div", { class: "tile-label", text: label }),
      el("div", { class: "tile-value", text: value }),
      el("div", { class: "tile-hint", text: hint }),
    ]));
  }
  wrap.append(tiles);

  // 门槛提示：这是「学了半天看不出变化」最常见的原因
  wrap.append(el("div", { class: "notice" }, [
    el("div", { class: "notice-title", text: "为什么风格还没变化？" }),
    el("p", {
      text: `每个风格档位各自需要累积至少 ${minSamples} 次反馈才会影响行为`
        + `（当前 ${feedback} 次）。在此之前插件照常记录，但机器人保持默认风格。`,
    }),
    el("p", {
      class: "notice-sub",
      text: "想先看它在学什么，可以在左侧选一个群；想调门槛，去「全局设置 → 学习力度」。",
    }),
  ]));

  // 群一览：点一下直接进详情，比在左栏找更方便
  if (groups.length) {
    wrap.append(el("div", { class: "notice" }, [
      el("div", { class: "notice-title", text: "各群情况" }),
      el("div", { class: "mini-list" }, groups.map((g) => el("button", {
        class: "mini-row",
        onClick: () => selectGroup(g.group_id),
      }, [
        el("span", { class: "mini-id", text: g.group_id }),
        el("span", { class: "mini-stat", text: `经验 ${g.entries_alive || 0}` }),
        el("span", { class: "mini-stat", text: `反馈 ${g.feedback || 0}` }),
        el("span", { class: "mini-stat", text: `注入 ${g.injections || 0}` }),
        g.pending
          ? el("span", { class: "tag warn", text: `${g.pending} 待批` })
          : el("span", { class: "mini-stat dim", text: "无待批" }),
      ]))),
    ]));
  }

  if (pending) {
    wrap.append(el("div", { class: "notice" }, [
      el("div", { class: "notice-title", text: "有候选等待审批" }),
      el("p", { text: `共 ${pending} 条候选。反思产出的内容需要你确认后才会生效。` }),
    ]));
  }

  box.append(wrap);
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

/* ============================ 全局设置 ============================ */
/* 配置直接写在面板里：分组表单 + 一键预设 + 风险提示 + 改动预览。
 * 后端用 _conf_schema.json 当白名单与类型约束，所以这里只管展示与收集。 */

async function openSettings() {
  state.current = null;
  state.tab = "settings";
  renderGroups();
  try {
    state.settings = await apiGet("config");
    state.draft = {};
    renderSettings();
  } catch (e) {
    showError(String(e.message || e));
  }
}

function draftValue(field) {
  return Object.prototype.hasOwnProperty.call(state.draft, field.key)
    ? state.draft[field.key]
    : field.value;
}

function isDirty(field) {
  return Object.prototype.hasOwnProperty.call(state.draft, field.key)
    && JSON.stringify(state.draft[field.key]) !== JSON.stringify(field.value);
}

function renderSettings() {
  const s = state.settings;
  const box = $("#content");
  box.innerHTML = "";
  if (!s) return;

  const dirtyCount = allFields().filter(isDirty).length;

  box.append(el("div", { class: "panel-head" }, [
    el("div", {}, [
      el("h2", { text: "⚙ 全局设置" }),
      el("p", {
        class: "sub",
        text: dirtyCount
          ? `${dirtyCount} 项已改动，尚未保存`
          : "改完点右下角「保存」即可生效，不必重载插件",
      }),
    ]),
    el("button", { class: "btn", text: "返回列表", onClick: () => { state.tab = "learned"; renderPlaceholder(); } }),
  ]));

  if (!s.writable) {
    box.append(el("div", { class: "alert info" }, [
      el("span", { text: "⚠ 读不到 _conf_schema.json，本页只能查看当前值、无法保存。请确认插件文件完整。" }),
    ]));
  }

  // ---- 一键预设 ----
  const presets = el("div", { class: "preset-grid" });
  for (const p of s.presets || []) {
    presets.append(el("button", {
      class: "preset",
      onClick: () => applyPreset(p),
    }, [
      el("strong", { text: p.label }),
      el("span", { text: p.desc }),
    ]));
  }
  box.append(el("div", { class: "card" }, [
    el("div", { class: "card-head" }, [
      el("strong", { text: "一键预设" }),
      el("span", { class: "grow" }),
      el("span", { class: "card-meta", text: "不改文件、只填入表单，确认后再保存" }),
    ]),
    presets,
  ]));

  // ---- 分组表单 ----
  for (const group of s.groups || []) {
    const body = el("div", { class: "fields" });
    for (const field of group.fields) body.append(renderField(field));
    const groupDirty = group.fields.filter(isDirty).length;
    box.append(el("div", { class: "card" }, [
      el("div", { class: "card-head" }, [
        el("strong", { text: group.label }),
        groupDirty ? el("span", { class: "tag warn", text: `${groupDirty} 项改动` }) : null,
        el("span", { class: "grow" }),
        el("span", { class: "card-meta", text: group.desc || "" }),
      ]),
      body,
    ]));
  }

  // ---- 底部操作条 ----
  box.append(el("div", { class: "sticky-actions" }, [
    el("span", { class: "card-meta", text: dirtyCount ? `${dirtyCount} 项待保存` : "没有改动" }),
    el("span", { class: "grow" }),
    el("button", {
      class: "btn", text: "丢弃改动",
      onClick: () => { state.draft = {}; renderSettings(); },
    }),
    el("button", {
      class: "btn primary", text: "保存",
      onClick: doSaveConfig,
    }),
  ]));

  renderPreviewCard(box);
}

function allFields() {
  const out = [];
  for (const g of (state.settings && state.settings.groups) || []) {
    for (const f of g.fields) out.push(f);
  }
  return out;
}

function applyPreset(preset) {
  const byKey = {};
  for (const f of allFields()) byKey[f.key] = f;
  for (const [key, value] of Object.entries(preset.values || {})) {
    if (byKey[key]) state.draft[key] = value;
  }
  renderSettings();
  flash(`已套用预设「${preset.label}」，确认无误后点保存`);
}

function renderField(field) {
  const value = draftValue(field);
  const dirty = isDirty(field);
  const row = el("div", { class: "field" + (dirty ? " dirty" : "") });

  const head = el("div", { class: "field-head" }, [
    el("strong", { text: field.label }),
    el("code", { class: "key", text: field.key }),
    field.risk === "caution" ? el("span", { class: "tag warn", text: "需谨慎" }) : null,
    field.min !== undefined && field.min !== null
      ? el("span", { class: "card-meta", text: `${field.min} ~ ${field.max}` }) : null,
    el("span", { class: "grow" }),
    dirty ? el("button", {
      class: "btn tiny", text: "还原",
      onClick: () => { delete state.draft[field.key]; renderSettings(); },
    }) : null,
  ]);
  row.append(head);

  const input = el("div", { class: "field-input" });
  if (field.type === "bool") {
    input.append(el("label", { class: "switch" }, [
      el("input", {
        type: "checkbox",
        checked: value ? true : false,
        onChange: (e) => setDraft(field, e.target.checked),
      }),
      el("span", { text: value ? "开启" : "关闭" }),
    ]));
  } else if (field.type === "int" || field.type === "float") {
    const num = el("input", {
      type: "number",
      class: "input",
      value: value === undefined || value === null ? "" : String(value),
      step: field.type === "float" ? "0.01" : "1",
      onInput: (e) => {
        const raw = e.target.value;
        setDraft(field, raw === "" ? "" : Number(raw), true);
      },
    });
    if (field.min !== undefined && field.min !== null) num.setAttribute("min", field.min);
    if (field.max !== undefined && field.max !== null) num.setAttribute("max", field.max);
    input.append(num);
  } else {
    input.append(el("input", {
      type: "text", class: "input", value: value === undefined || value === null ? "" : String(value),
      onInput: (e) => setDraft(field, e.target.value, true),
    }));
  }
  row.append(input);

  if (field.hint) row.append(el("p", { class: "hint", text: field.hint }));
  return row;
}

function setDraft(field, value, rerenderHead) {
  state.draft[field.key] = value;
  if (rerenderHead) {
    // 输入过程中不整体重绘，否则光标会跳；只更新样式与计数
    const dirtyCount = allFields().filter(isDirty).length;
    const sub = document.querySelector(".panel-head .sub");
    if (sub) sub.textContent = dirtyCount ? `${dirtyCount} 项已改动，尚未保存` : "改完点右下角「保存」即可生效，不必重载插件";
    const bar = document.querySelector(".sticky-actions .card-meta");
    if (bar) bar.textContent = dirtyCount ? `${dirtyCount} 项待保存` : "没有改动";
  } else {
    renderSettings();
  }
}

async function doSaveConfig() {
  const dirty = {};
  for (const f of allFields()) if (isDirty(f)) dirty[f.key] = state.draft[f.key];
  if (!Object.keys(dirty).length) {
    flash("没有需要保存的改动");
    return;
  }
  return withBusy(async () => {
    const r = await apiPost("config", { values: dirty });
    state.settings = await apiGet("config");
    state.draft = {};
    renderSettings();
    const parts = [r.message || "已保存"];
    if (r.rejected && r.rejected.length) parts.push(`被拒绝：${r.rejected.join("；")}`);
    flash(parts.join(" · "));
  });
}

/* ---------------- 注入预览：面板最实用的一块 ---------------- */

function renderPreviewCard(box) {
  const gid = (state.groups[0] && state.groups[0].group_id) || state.probeGroup || "";
  const card = el("div", { class: "card" });
  card.append(el("div", { class: "card-head" }, [
    el("strong", { text: "🔍 注入预览" }),
    el("span", { class: "grow" }),
    el("span", { class: "card-meta", text: "看看学习结果最终以什么样子进入对话" }),
  ]));

  const gsel = el("select", { class: "input" });
  for (const g of state.groups) {
    gsel.append(el("option", { value: g.group_id, text: g.group_id, selected: g.group_id === gid }));
  }
  if (!state.groups.length) gsel.append(el("option", { value: "", text: "（还没有群数据）" }));

  const probe = el("input", {
    type: "text", class: "input", placeholder: "输入一句群友可能会说的话（可留空）",
    value: state.probe,
    onInput: (e) => { state.probe = e.target.value; },
  });

  card.append(el("div", { class: "preview-controls" }, [
    gsel, probe,
    el("button", { class: "btn", text: "预览", onClick: doPreview }),
  ]));

  if (state.preview) {
    const p = state.preview;
    if (!p.active) {
      card.append(el("div", { class: "alert info" }, [
        el("span", { text: "当前未启用注入（总开关或「启用注入」是关闭的），所以实际不会注入任何内容。" }),
      ]));
    }
    card.append(el("p", { class: "card-meta", text: `当前策略：${p.choice_desc}${p.fallback ? "（证据不足，走默认档）" : ""}` }));
    if (p.style_notes && p.style_notes.length) {
      card.append(el("p", { class: "card-meta", text: `风格指令：${p.style_notes.join("；")}` }));
    }
    if (p.blocked && p.blocked.length) {
      card.append(el("div", { class: "alert info" }, [
        el("span", { text: `🛡 有 ${p.blocked.length} 条经验因疑似指令性内容被拦截，未注入` }),
      ]));
    }
    card.append(el("pre", { class: "preview-box", text: p.text || "（本次没有可注入的内容）" }));
  }
  box.append(card);
}

async function doPreview() {
  const gsel = document.querySelector(".preview-controls select");
  const gid = gsel ? gsel.value : "";
  if (!gid) {
    flash("还没有群数据，先让机器人在群里聊几轮");
    return;
  }
  return withBusy(async () => {
    state.preview = await apiPost("preview", { group_id: gid, message: state.probe });
    renderSettings();
  });
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

/**
 * 页面内自绘确认框，返回 Promise<boolean>。
 *
 * 为什么不用原生 confirm()：面板跑在 AstrBot 的受限 iframe 里，宿主若没授予
 * `allow-modals`，confirm() 会被**直接拦掉**（返回 false 且不报错），
 * 表现就是「点了没反应」且日志里查不出任何原因。自绘框不依赖该权限。
 *
 * Esc / 点遮罩 = 取消；Enter = 确认。
 */
function confirmDialog({ title, message, confirmText = "确定", cancelText = "取消", danger = false } = {}) {
  return new Promise((resolve) => {
    let done = false;
    let overlay = null;
    const finish = (val) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      if (overlay && overlay.remove) overlay.remove();
      resolve(val);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
      else if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    };

    const confirmBtn = el("button", {
      class: "btn" + (danger ? " danger" : " primary"),
      text: confirmText,
      onClick: () => finish(true),
    });
    overlay = el("div", {
      class: "modal-overlay",
      onClick: (ev) => { if (ev.target === overlay) finish(false); },
    }, [
      el("div", { class: "modal-card", role: "dialog", "aria-modal": "true" }, [
        el("div", { class: "modal-title", text: title || "确认" }),
        el("div", { class: "modal-msg", text: message || "" }),
        el("div", { class: "modal-actions" }, [
          el("button", { class: "btn", text: cancelText, onClick: () => finish(false) }),
          confirmBtn,
        ]),
      ]),
    ]);

    document.addEventListener("keydown", onKey, true);
    document.body.append(overlay);
    try { confirmBtn.focus(); } catch (e) { /* 忽略 */ }
  });
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

/**
 * 删除某个会话的全部学习数据。
 *
 * 之前面板**没有这个能力**（只有单条经验删除），所以退出的群会一直留在左侧
 * 列表里且点不掉。后端 /reset_group 会把经验、策略、快照、审计、待批、
 * 统计一并清掉——统计也必须清，否则列表刷新后这一行会回来。
 */
function doDeleteGroup(gid) {
  const target = gid || state.current;
  if (!target) return Promise.resolve();
  return withBusy(async () => {
    if (typeof confirmDialog === "function") {
      const ok = await confirmDialog({
        title: "删除这个会话的学习数据？",
        message: `群 ${target} 学到的经验、策略、快照与记录都会被清空，且不可撤销。\n（不会影响群本身，也不会退群。）`,
        confirmText: "删除",
        danger: true,
      });
      if (!ok) return;
    }
    const r = await apiPost("reset_group", { group_id: target });
    // 删的是当前选中的群时，清空右侧详情，避免停留在已删除的数据上
    if (target === state.current) {
      state.current = "";
      state.detail = null;
    }
    await reloadDetail((r && r.message) || "已删除该会话");
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

$("#btnSettings").addEventListener("click", () => withBusy(openSettings));

$("#btnPause").addEventListener("click", () => withBusy(async () => {
  const paused = !!(state.boot && state.boot.paused);
  const r = await apiPost("switch", { action: paused ? "resume" : "pause" });
  state.boot.paused = !!(r && r.paused);
  applyPauseState();
  flash((r && r.message) || "已切换");
}));

/* ============================ 启动 ============================ */

/* AstrBot 把页面 SDK 的 <script> 注入在 </body> **之前**，而本文件也在 </body> 前，
 * 所以本脚本执行时 window.AstrBotPluginPage 往往**还没定义**——直接初始化会
 * 误报"未检测到通信接口"（真实踩过的坑）。
 *
 * 因此等页面所有脚本都执行完再启动；万一 SDK 仍未出现（旧版本不注入、
 * 或请求失败），再重试几次后给出明确提示，并说清是"没等到"还是"没注入"。
 */
async function waitForBridge(attempts = 20, intervalMs = 50) {
  for (let i = 0; i < attempts; i += 1) {
    const b = bridge();
    if (b) return b;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return null;
}

function whenDocumentReady() {
  if (document.readyState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    window.addEventListener("load", () => resolve(), { once: true });
  });
}

async function bootstrap() {
  await whenDocumentReady();
  const b = await waitForBridge();
  if (!b) {
    // 到这一步说明 SDK 确实没有出现（不是"还没加载"）。
    showError(
      NO_BRIDGE +
      "\n（若你是从插件管理里打开的，请确认 AstrBot 版本 >= 4.24.2 —— " +
      "更早的版本不会为插件页面注入通信接口。）"
    );
    renderPlaceholder();
    return;
  }
  await loadAll();
}

bootstrap();
