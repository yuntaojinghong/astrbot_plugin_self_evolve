/* 场景 B：SDK 尚未注入（或页面被直接打开）时，面板必须给出友好提示而不是崩掉。
 *
 * 复现用户遇到的情形：`window.AstrBotPluginPage` 取不到，
 * 于是代码退而去读 `window.parent.AstrBotPluginPage` ——
 * 而 iframe 是 sandbox 出来的（origin "null"），这一读直接抛 SecurityError。
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const REPO = process.env.REPO || path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(REPO, "pages/settings/index.html"), "utf-8");
const APP = fs.readFileSync(path.join(REPO, "pages/settings/app.js"), "utf-8");

const dom = new JSDOM(HTML, {
  runScripts: "outside-only",
  url: "https://example.invalid/plugin-page/astrbot_plugin_self_evolve/settings",
  pretendToBeVisual: true,
});
const { window } = dom;

// 关键：**不注入** SDK 全局。真实场景里 SDK 会注入，但注入前脚本可能已执行，
// 或者页面被直接打开。
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
  let crashed = null;
  try {
    window.eval(APP);
    // 前端会先等页面脚本加载完、再给 SDK 一点出现的时间（约 1 秒），
    // 之后才判定"确实没有通信接口"。所以这里要等够，不能提前断言。
    await new Promise((r) => setTimeout(r, 1600));
  } catch (e) {
    crashed = e;
  }

  const err = window.document.querySelector("#error");
  console.log("  错误条内容:", err ? JSON.stringify(err.textContent.slice(0, 120)) : "(无)");
  console.log("  错误条可见:", err ? !err.hidden : false);

  console.log("\n断言：");
  assert("脚本没有抛异常崩掉", !crashed, crashed ? String(crashed.message).slice(0, 120) : "");
  assert("没有去读跨源的 window.parent", !securityThrown);
  assert("给出了可读的提示（不是 SecurityError 原文）",
    !!err && !err.hidden && err.textContent.includes("插件管理") && !err.textContent.includes("cross-origin"),
    err ? err.textContent.slice(0, 140) : "(无错误条)");

  console.log("\n结论:", failures.length ? `失败 ${failures.length} 项` : "无 SDK 时优雅降级");
  process.exit(failures.length ? 1 : 0);
})();
