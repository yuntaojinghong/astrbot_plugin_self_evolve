"""生成自进化插件的图标资源（logo.png / docs/logo.png / docs/favicon.png）。

设计：一枚「迭代环 + 累积节点」的标记。
- 外圈是一段带箭头的环，代表「反馈 → 调整 → 再反馈」的闭环。
- 环上的三个节点代表一次次学到的经验；节点渐次变大/变亮，表示在积累。
- 配色走「青蓝 → 靛紫」，与既有插件区分，也贴合「冷静、可解释」的定位。

全部图形由本脚本的常量生成（纯 Pillow 绘制，不依赖字体文件），
因此 logo 与 favicon 不会出现渲染漂移。重新生成：
    <bundled-python> scripts/make_logo.py
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))   # <repo>/scripts
REPO = os.path.dirname(HERE)                        # <repo>
DOCS = os.path.join(REPO, "docs")

# ---------------------------------------------------------------- 设计常量
SS = 4                        # 超采样倍率（先大后缩，得到平滑边缘）
SIZE = 512
BG_TOP = (26, 34, 52)
BG_BOTTOM = (13, 17, 26)
RING_A = (106, 169, 255)      # 青蓝（起点）
RING_B = (158, 124, 255)      # 靛紫（终点）
NODE = (255, 255, 255)
TRACK = (44, 56, 80)          # 未走完的环（暗轨）


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _ring_pt(cx, cy, r, ang):
    return (cx + r * math.cos(ang), cy + r * math.sin(ang))


def make_logo(size: int = SIZE) -> Image.Image:
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ---------- 背景：竖向渐变 + 圆角 ----------
    bg = Image.new("RGBA", (S, S))
    bd = ImageDraw.Draw(bg)
    for y in range(S):
        bd.line([(0, y), (S, y)], fill=lerp(BG_TOP, BG_BOTTOM, y / S) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255
    )
    img.paste(bg, (0, 0), mask)

    cx = cy = S * 0.5
    r = S * 0.315
    lw = max(2, int(S * 0.052))          # 环线宽

    # ---------- 暗轨：完整一圈，暗示"还会继续转" ----------
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=TRACK + (255,), width=lw)

    # ---------- 明环：约 282° 的一圈，颜色由靛紫渐变到青蓝 ----------
    # 注意：**不能**用「沿圆周打点、逐段连线」的办法上色——线段长度只有几个像素，
    # 而线宽有二十多像素，圆头/粗线会让每段都变成一根放射状的"刺"。
    # 正确做法：把渐变画成一张图，再用一段圆环当遮罩抠出来。
    a0_deg = -90.0                       # 从正上方开始
    span_deg = 282.0                     # 留缺口，表示仍在迭代

    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    gh = int(S * 0.18)                   # 渐变条高度，够覆盖整个圆的 y 范围
    for y in range(S):
        t = min(1.0, max(0.0, (y - (cy - r)) / (2 * r)))
        gd.line([(0, y), (S, y)], fill=lerp(RING_B, RING_A, t) + (255,))

    arc_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(arc_mask).arc(
        [cx - r, cy - r, cx + r, cy + r],
        start=a0_deg, end=a0_deg + span_deg, fill=255, width=lw,
    )
    ring_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ring_layer.paste(grad, (0, 0), arc_mask)
    img.alpha_composite(ring_layer)

    # ---------- 箭头：明环末端，指向"下一次迭代" ----------
    ang_end = math.radians(a0_deg + span_deg)
    tip = _ring_pt(cx, cy, r, ang_end + math.radians(6))
    ax, ay = _ring_pt(cx, cy, r, ang_end)
    head = lw * 1.5
    for sgn in (1, -1):
        a2 = ang_end + math.pi + sgn * math.radians(64)
        side = (ax + head * math.cos(a2), ay + head * math.sin(a2))
        if sgn == 1:
            side_a = side
        else:
            side_b = side
    d.polygon([tip, side_a, side_b], fill=RING_A + (255,))

    # ---------- 节点：学到的经验，逐步变大变亮 ----------
    # 尺寸按画布比例直接给；发光必须小且淡，否则会在暗底上糊成一大团白饼。
    # 颜色只往白色混 35%~55%，保留自身的渐变色调，避免三个大白圆喧宾夺主。
    for frac, rr_ratio, mix in (
        (0.16, 0.026, 0.55),
        (0.46, 0.032, 0.68),
        (0.76, 0.039, 0.82),
    ):
        ang = math.radians(a0_deg + span_deg * frac)
        x, y = _ring_pt(cx, cy, r, ang)
        rr = S * rr_ratio
        base = lerp(RING_B, RING_A, frac)
        col = lerp(base, NODE, mix)
        # 外发光：范围小（最多 1.9 倍半径）、强度低
        for k in range(6, 0, -1):
            gr = rr * (1 + k * 0.15)
            d.ellipse([x - gr, y - gr, x + gr, y + gr],
                      fill=col + (int(9 * (1 - k / 6) * mix),))
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=col + (255,))
        # 高光：偏到左上，读起来是"点"而非"饼"
        hr = rr * 0.30
        hx, hy = x - rr * 0.28, y - rr * 0.28
        d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=(255, 255, 255, 235))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(DOCS, exist_ok=True)

    logo = make_logo(SIZE)
    logo.save(os.path.join(REPO, "logo.png"))
    logo.resize((256, 256), Image.LANCZOS).save(os.path.join(DOCS, "logo.png"))
    logo.resize((64, 64), Image.LANCZOS).save(os.path.join(DOCS, "favicon.png"))

    # GitHub Pages：告诉 Jekyll 不要处理站点文件
    open(os.path.join(DOCS, ".nojekyll"), "w").close()

    for name in ("logo.png", "docs/logo.png", "docs/favicon.png"):
        p = os.path.join(REPO, name)
        print(f"  {name:24s} {os.path.getsize(p) / 1024:7.1f} KB")


if __name__ == "__main__":
    main()
