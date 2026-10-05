/* 自进化面板的 DOM 级验证。
 *
 * 覆盖三类线上反馈：
 *   1. 「点反思没反应」——必须发出 POST 并把结果显示出来
 *   2. 「学习倾向要能拉动进度条」——有范围的数值项必须是滑杆且能改
 *   3. 「每个群能单独设人设」——人设页能切换模式、保存、删除
 *
 * 跑法：REPO=<插件目录> node panel.test.js
 * 依赖 jsdom（面板是浏览器代码，只能靠 DOM 环境验证）。
 */
const fs = require("fs");
const path = require("path");

const REPO = process.env.REPO || process.cwd();
const HTML = fs.readFileSync(path.join(REPO, "pages/settings/index.html"), "utf8");
const APP = fs.readFileSync(path.join(REPO, "pages/settings/app.js"), "utf8");

const { JSDOM } = require("jsdom");

let pass = 0, fail = 0;
const results = [];
function check(label, cond, extra) {
  if (cond) { pass++; results.push(`  OK   ${label}`); }
  else { fail++; results.push(`  FAIL ${label}${extra !== undefined ? "   " + JSON.stringify(extra) : ""}`); }
}

const dom = new JSDOM(HTML, { runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.HTMLCanvasElement.prototype.getContext = () => null;

const GROUP = "1077250302";
const calls = [];

// 配置项：含一个带范围的 float（应当渲染成滑杆）
const FIELDS = [
  { key: "learning_rate", label: "学习率", type: "float",
    min: 0.01, max: 0.5, step: 0.01, value: 0.12, default: 0.12,
    hint: "越大越快，也越容易被一次风波带偏" },
  { key: "min_samples", label: "生效门槛", type: "int",
    min: 1, max: 50, step: 1, value: 5, default: 5, hint: "样本不足时保持默认风格" },
  { key: "inject_enabled", label: "启用注入", type: "bool",
    value: true, default: true, hint: "关掉后只学习不注入" },
  { key: "reflect_provider_id", label: "反思模型", type: "string",
    value: "", default: "", hint: "留空则用第一个可用模型" },
];

window.AstrBotPluginPage = {
  apiGet: async (endpoint, params) => {
    calls.push(["GET", endpoint, params]);
    if (endpoint === "bootstrap") {
      return { groups: [{ group_id: GROUP }], paused: false, registered: true,
               config: { min_samples: 5 } };
    }
    if (endpoint === "group") {
      return { group_id: GROUP,
               stats: { alive: 3, total: 5, feedback: 9, injections: 4 },
               entries: [], pending: [], audit: [], snapshots: [], dimensions: [],
               choice: {} };
    }
    if (endpoint === "overview") return { groups: [{ group_id: GROUP }] };
    if (endpoint === "config") {
      return { groups: [{ key: "learn", label: "学习", desc: "", fields: FIELDS }],
               presets: [], current: {}, defaults: {}, writable: true };
    }
    if (endpoint === "persona") {
      return { group_id: GROUP, mode: "follow", text: "" };
    }
    return {};
  },
  apiPost: async (endpoint, body) => {
    calls.push(["POST", endpoint, body]);
    if (endpoint === "reflect") {
      return { message: "🧪 反思完成（结果未生效，需审批）\n\n1. [偏好] 群友喜欢短句（置信 0.80）" };
    }
    if (endpoint === "persona/save") {
      return { group_id: GROUP, mode: body.mode, text: body.text || "",
               message: body.mode === "follow" ? "已切换为跟随 AstrBot 全局人设" : "已保存本群专属人设" };
    }
    return { ok: true };
  },
};

window.eval(APP);

const $ = (s) => window.document.querySelector(s);
const $$ = (s) => [...window.document.querySelectorAll(s)];
const settle = () => new Promise((r) => setTimeout(r, 60));
const click = (node) => node.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));

function findByText(sel, text) {
  return $$(sel).find((n) => (n.textContent || "").includes(text));
}

(async () => {
  await settle();

  // ---------- 进入某个群的详情 ----------
  const mini = $$(".mini-row");
  if (mini.length) click(mini[0]);
  else {
    const gm = $$(".group-main");
    if (gm.length) click(gm[0]);
  }
  await settle();

  // ================= 1. 反思按钮 =================
  const btn = findByText("button", "立即反思");
  check("详情页有「立即反思」按钮", !!btn,
        $$("button").map((b) => b.textContent.trim()));

  if (btn) {
    click(btn);
    await settle();
    const posted = calls.filter((c) => c[0] === "POST" && c[1] === "reflect");
    check("点击后发出 POST /reflect", posted.length >= 1);
    check("请求体带 group_id", !!(posted[0] && posted[0][2].group_id === GROUP));
    const box = $("#error");
    check("结果被展示出来（不是静默无反应）",
          !!box && !box.hidden && (box.textContent || "").includes("反思完成"),
          box ? { hidden: box.hidden, text: box.textContent } : null);
  }

  // ================= 2. 滑杆 =================
  const setBtn = findByText("button", "全局设置");
  if (setBtn) { click(setBtn); await settle(80); await settle(); }

  const ranges = $$('input[type="range"]');
  check("带范围的数值项渲染成滑杆", ranges.length >= 2, { rangeCount: ranges.length });

  const lr = ranges.find((r) => r.getAttribute("min") === "0.01");
  check("学习率滑杆的上下限来自 schema",
        !!lr && lr.getAttribute("max") === "0.5", lr ? {
          min: lr.getAttribute("min"), max: lr.getAttribute("max"),
        } : null);

  if (lr) {
    // 拖动 = 改 value 并触发 input
    lr.value = "0.3";
    lr.dispatchEvent(new window.Event("input", { bubbles: true }));
    await settle();

    const readout = lr.parentElement
      && lr.parentElement.querySelector(".slider-val");
    check("拖动后读数实时更新", !!readout && readout.textContent === "0.3",
          readout ? readout.textContent : null);
    check("轨道进度变量跟着更新（否则颜色不跟随）",
          (lr.style.getPropertyValue("--pct") || "") !== "",
          lr.style.getPropertyValue("--pct"));
    check("改动被计入未保存状态", (() => {
      const sub = $(".panel-head .sub");
      return !!sub && /已改动/.test(sub.textContent || "");
    })(), $(".panel-head .sub") ? $(".panel-head .sub").textContent : null);
  }

  // 没有范围的字符串项不能变成滑杆
  check("无范围的字段仍是文本输入",
        $$('input[type="text"]').length >= 1,
        { textInputs: $$('input[type="text"]').length });

  // ================= 3. 按群人设 =================
  const back = findByText("button", "返回列表");
  if (back) { click(back); await settle(); }
  const mini2 = $$(".mini-row");
  if (mini2.length) { click(mini2[0]); await settle(); }

  const personaTab = findByText(".tab", "本群人设");
  check("存在「本群人设」标签页", !!personaTab,
        $$(".tab").map((t) => t.textContent.trim()));

  if (personaTab) {
    click(personaTab);
    await settle();
    await settle();

    check("拉取了人设接口",
          calls.some((c) => c[0] === "GET" && c[1] === "persona"));

    const followTab = findByText(".tab", "跟随");
    check("有「跟随全局」选项", !!followTab);

    // 默认 follow：不该出现文本框
    check("follow 模式下不显示人设输入框",
          $$("textarea").length === 0, { textareas: $$("textarea").length });

    // 切到 custom
    const customTab = findByText(".tab", "本群专属人设");
    if (customTab) {
      click(customTab);
      await settle();
      const area = $("textarea");
      check("切到专属人设后出现输入框", !!area);

      if (area) {
        area.value = "你是这个群的老朋友，说话简短。";
        area.dispatchEvent(new window.Event("input", { bubbles: true }));
        await settle();

        const saveBtn = findByText("button", "保存");
        check("有保存按钮", !!saveBtn);
        if (saveBtn) {
          click(saveBtn);
          await settle();
          const post = calls.filter((c) => c[0] === "POST" && c[1] === "persona/save").pop();
          check("保存时发出 POST /persona/save", !!post, post);
          check("带上 mode=custom 与人设正文",
                !!post && post[2].mode === "custom"
                && String(post[2].text).includes("老朋友"), post ? post[2] : null);
          check("保存后给出反馈",
                (() => { const b = $("#error"); return !!b && !b.hidden; })(),
                $("#error") ? $("#error").textContent : null);
        }
      }
    }
  }

  console.log(results.join("\n"));
  console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
