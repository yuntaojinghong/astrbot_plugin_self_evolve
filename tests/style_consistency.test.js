/* 样式与前端的一致性检查。
 *
 * 起因：重写 style.css 时漏掉了 18 个 JS 仍在使用的类
 * （.bar-wrap / .cell / .cell-label / .dims / .preview-box …），
 * 其中"学到的倾向"整块因此不可见——而**没有任何测试发现**，
 * 因为原有测试只断言"页面能加载""不访问 window.parent"。
 *
 * 这类问题不该靠人眼复查。这里把两件事固定下来：
 *   1. app.js 里出现的每个 class 名，style.css 里必须有一条规则；
 *   2. 倾向条的结构不能搞反（.bar 是填充条、.bar-wrap 是轨道）。
 *
 * 跑法：REPO=<插件目录> node tests/style_consistency.test.js
 */
const fs = require("fs");
const path = require("path");

const REPO = process.env.REPO || process.cwd();
const APP = fs.readFileSync(path.join(REPO, "pages/settings/app.js"), "utf8");
const CSS = fs.readFileSync(path.join(REPO, "pages/settings/style.css"), "utf8");
const HTML = fs.readFileSync(path.join(REPO, "pages/settings/index.html"), "utf8");

let pass = 0, fail = 0;
const out = [];
function check(label, cond, extra) {
  if (cond) { pass++; out.push(`  OK   ${label}`); }
  else { fail++; out.push(`  FAIL ${label}${extra !== undefined ? "   " + JSON.stringify(extra) : ""}`); }
}

/* ---------- 1. 收集 JS 里用到的类名 ---------- */
const used = new Set();
for (const m of APP.matchAll(/class:\s*"([^"]+)"/g)) {
  for (const c of m[1].split(/\s+/)) {
    // 跳过模板拼接（class: "bar" + (…) 这类只取到字面量部分）
    if (c && !c.includes("${")) used.add(c);
  }
}
// HTML 里的静态类名也要算上
for (const m of HTML.matchAll(/class="([^"]+)"/g)) {
  for (const c of m[1].split(/\s+/)) if (c) used.add(c);
}

/* ---------- 2. 判断 CSS 里是否有对应规则 ---------- */
function hasRule(name) {
  const esc = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  // 匹配 .name 后面跟 空白 , { : . > 或行尾，避免 .bar 误匹配 .bar-wrap
  return new RegExp("\\." + esc + "(?=[\\s,{:.>]|$)").test(CSS);
}

const missing = [...used].filter((c) => !hasRule(c)).sort();
check(`JS/HTML 用到的每个类都有 CSS 规则（共 ${used.size} 个）`,
      missing.length === 0, missing);

/* ---------- 3. 关键结构不能搞反 ---------- */
// .bar 是填充条：必须有背景色/渐变，且允许被行内 width 控制
const barBlock = CSS.match(/\.bar\s*\{[^}]*\}/);
check(".bar 有背景（它是填充条，不能只有轨道样式）",
      !!barBlock && /background/.test(barBlock[0]),
      barBlock ? barBlock[0].slice(0, 60) : null);

// .bar-wrap 是轨道：要有底色且比填充条暗
const wrapBlock = CSS.match(/\.bar-wrap\s*\{[^}]*\}/);
check(".bar-wrap 有底色（它是轨道）",
      !!wrapBlock && /background/.test(wrapBlock[0]),
      wrapBlock ? wrapBlock[0].slice(0, 60) : null);

// 负分要有区分色，否则正负看不出来
check(".bar.neg 有独立配色（正负分要能区分）", /\.bar\.neg\s*\{/.test(CSS));

/* ---------- 4. 轨道对比度：未填充段不能和卡片底色太接近 ---------- */
function hexToRgb(h) {
  const s = h.replace("#", "");
  const v = s.length === 3 ? s.split("").map((c) => c + c).join("") : s;
  return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
}
function lum([r, g, b]) {
  const f = (c) => { const x = c / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}
function contrast(a, b) {
  const la = lum(a), lb = lum(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

const cardVar = CSS.match(/--bg-card:\s*(#[0-9a-fA-F]{3,6})/);
const card = cardVar ? hexToRgb(cardVar[1]) : null;

/** 从某个选择器的规则块里取背景色（第一个 #rrggbb）。 */
function bgOf(selectorRe) {
  const m = CSS.match(selectorRe);
  if (!m) return null;
  const hex = m[0].match(/#[0-9a-fA-F]{6}/);
  return hex ? hex[0] : null;
}

// 倾向条轨道：.bar-wrap 的底色
const wrapBg = bgOf(/\.bar-wrap\s*\{[^}]*\}/);
// 滑杆未填充段：range 轨道里那个较深的颜色（第二段渐变色）
const rangeTrack = CSS.match(/::-webkit-slider-runnable-track\s*\{[^}]*\}/);
let rangeBg = null;
if (rangeTrack) {
  const hexes = [...rangeTrack[0].matchAll(/#[0-9a-fA-F]{6}/g)].map((m) => m[0]);
  // 取较暗的那个 = 未填充段
  if (hexes.length) {
    rangeBg = hexes.sort((a, b) => lum(hexToRgb(a)) - lum(hexToRgb(b)))[0];
  }
}

if (card) {
  for (const [label, hex] of [["倾向条轨道 .bar-wrap", wrapBg],
                              ["滑杆未填充段", rangeBg]]) {
    if (!hex) {
      check(`能取到${label}的颜色`, false, { selector: label });
      continue;
    }
    const ratio = contrast(hexToRgb(hex), card);
    check(`${label} 与卡片底色可区分（${hex} vs 卡片 ${cardVar[1]}，对比度 ${ratio.toFixed(2)}:1，需 ≥2.0）`,
          ratio >= 2.0, { track: hex, card: cardVar[1] });
  }
} else {
  check("能取到 --bg-card（对比度检查的前提）", false);
}

/* ---------- 5. 旧教训：滑块不能太细 ---------- */
const trackH = CSS.match(/::-webkit-slider-runnable-track\s*\{[^}]*height:\s*(\d+)px/);
check("滑杆轨道至少 8px 高（过去 5px 太细看不清）",
      !!trackH && Number(trackH[1]) >= 8, trackH ? trackH[1] : null);

console.log(out.join("\n"));
console.log(`\n结果: ${pass} 通过, ${fail} 失败`);
process.exit(fail ? 1 : 0);
