/* 用 jsdom 真实加载自进化面板前端，验证：
 *   1. 在 sandbox 风格的环境里能跑起来（不会因跨源读父窗口而炸）
 *   2. 调用的接口名与参数和后端 pages_api.py 的约定一致
 *   3. 返回数据能正确渲染出群列表
 *
 * 这是对「面板打不开」那次事故的端到端回归。
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const REPO = process.env.REPO || path.resolve(__dirname, "..");const HTML = fs.readFileSync(path.join(REPO, "pages/settings/index.html"), "utf-8");
const APP = fs.readFileSync(path.join(REPO, "pages/settings/app.js"), "utf-8");

const calls = [];

// 模拟后端返回（与 pages_api.py 的形状一致）
const RESPONSES = {
  bootstrap: {
    version: "0.3.0",
    enabled: true,
    inject_enabled: true,
    paused: false,
    config: { min_samples: 5, max_offset: 0.2, admin_only: true },
    dimensions: [
      { key: "length", label: "长度", levels: ["很短", "短", "适中", "长", "很长"], hints: [], baseline: 2 },
    ],
  },
  overview: {
    groups: [
      { group_id: "1001", entries_alive: 2, entries_total: 3, pending: 1,
        feedback: 7, injections: 4, replies: 9, snapshots: 1, blocked: 0, last_audit: 1790000000 },
    ],
  },
  group: {
    group_id: "1001",
    stats: { alive: 2, total: 3, feedback: 7, injections: 4, blocked_injections: 0 },
    dimensions: [
      { dimension: "length", chosen: 2, baseline: 2, fallback: true, offset: 0.0,
        cells: [
          { level: 0, label: "很短", score: 0, pulls: 0, eligible: false },
          { level: 1, label: "短", score: 0, pulls: 0, eligible: false },
          { level: 2, label: "适中", score: -0.2, pulls: 3, eligible: false },
        ] },
    ],
    entries: [
      { eid: "g:fact:x", content: "本群把版主叫扫地僧", kind: "fact", kind_label: "事实",
        confidence: 0.8, effective_confidence: 0.77, evidence: 3, source: "user",
        last_seen: 1790000000, pinned: false, alive: true },
    ],
    pending: [
      { pid: "p1", kind: "entry", content: "周五晚上开黑", entry_kind: "fact", kind_label: "事实",
        confidence: 0.7, reason: "反思候选", created: 1790000000 },
    ],
    audit: [
      { aid: "a1", created: 1790000000, signal: "criticism", polarity: -1, weight: 0.8,
        user_message: "不对，你说错了", bot_reply: "抱歉", evidence: "否定词", correction: "",
        note: "", choice: { picks: { length: 2 } }, updates: [{ dimension: "length", level: 2, before: 0, after: -0.2 }] },
    ],
    history: [{ sid: "s1", reason: "初始", created: 1790000000, summary: { memory: "+2" } }],
    paused: false,
  },
};

const dom = new JSDOM(HTML, {
  runScripts: "outside-only",
  url: "https://example.invalid/plugin-page/astrbot_plugin_self_evolve/settings",
  pretendToBeVisual: true,
});

const { window } = dom;

// 真实 AstrBot 里 SDK 是以 <script> 注入到本页面 window 上的全局对象，
// 且注入位置在 </body> **之前** —— 本文件也在 </body> 前，所以
// **本脚本先执行、SDK 后定义**。这里精确复现该时序：延迟一小会儿再定义。
// 若前端在脚本执行那一刻就初始化，就会误报"未检测到通信接口"（真实踩过的坑）。
const SDK_DELAY_MS = 60;
setTimeout(() => {
  Object.defineProperty(window, "AstrBotPluginPage", {
    configurable: true,
    value: {
      async ready() { calls.push(["ready"]); return { isDark: true }; },
      onContext() {},
      async apiGet(endpoint, params) { calls.push(["GET", endpoint, params || {}]); return RESPONSES[endpoint]; },
      async apiPost(endpoint, body) { calls.push(["POST", endpoint, body || {}]); return { message: "ok" }; },
    },
  });
}, SDK_DELAY_MS);

// 把 window.parent 做成「一读就抛 SecurityError」，精确复现 origin 为 "null"
// 的 sandbox iframe —— 若前端还去读它，就会在这里暴露。
let securityThrown = false;
Object.defineProperty(window, "parent", {
  configurable: true,
  get() {
    securityThrown = true;
    const e = new Error(
      "Failed to read a named property 'AstrBotPluginPage' from 'Window': " +
      "Blocked a frame with origin \"null\" from accessing a cross-origin frame."
    );
    e.name = "SecurityError";
    throw e;
  },
});

const failures = [];
function assert(label, cond, detail) {
  console.log(`  ${cond ? "OK  " : "FAIL"} ${label}${cond ? "" : "   " + (detail ?? "")}`);
  if (!cond) failures.push(label);
}

(async () => {
  window.eval(APP);
  // 等 loadAll 的 Promise 链跑完
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 25));
    if (calls.some((c) => c[0] === "GET" && c[1] === "overview")) break;
  }
  await new Promise((r) => setTimeout(r, 120));

  console.log("SDK 调用序列：");
  for (const c of calls) console.log("   ", JSON.stringify(c));

  console.log("\n断言：");
  assert("没有去读跨源的 window.parent", !securityThrown);
  assert("调用了 bridge.ready()", calls.some((c) => c[0] === "ready"));
  assert("请求了 bootstrap", calls.some((c) => c[1] === "bootstrap"));
  assert("请求了 overview", calls.some((c) => c[1] === "overview"));

  const get = calls.find((c) => c[1] === "group");
  assert("查询参数交给 SDK 传递（未自行拼进路径）",
    !get || (typeof get[2] === "object" && get[2].group_id === "1001"),
    get ? JSON.stringify(get) : "尚未请求 group");

  const err = window.document.querySelector("#error");
  assert("错误条没有显示（面板正常加载）", !err || err.hidden, err ? err.textContent : "");

  const list = window.document.querySelector("#groupList");
  assert("渲染出了群列表", !!list && list.textContent.includes("1001"),
    list ? list.textContent.trim().slice(0, 80) : "(无)");

  const badge = window.document.querySelector("#versionBadge");
  assert("显示版本号 v0.3.0", !!badge && badge.textContent.includes("0.3.0"),
    badge ? badge.textContent : "(无)");

  const pause = window.document.querySelector("#btnPause");
  assert("暂停按钮文案正确", !!pause && pause.textContent.includes("暂停"), pause ? pause.textContent : "");

  console.log("\n结论:", failures.length ? `失败 ${failures.length} 项` : "面板前端加载与接线均正确");
  process.exit(failures.length ? 1 : 0);
})();
