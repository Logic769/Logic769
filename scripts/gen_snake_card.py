#!/usr/bin/env python3
"""GitHub contribution snake card (activity snake).

The snake planner below (`plan_snake_route`) is taken from Acfufu/token-stats.py's
snake section. Everything else -- grid, palettes and rendering -- is written for
this repo and matches the look of the retired Platane/snk card: viewBox
"-16 -32 880 192", 53 columns x 7 rows, 12px cells on a 16px pitch, plus the
bottom level-progress strip.

Data: GitHub public contribution calendar, reused from gen_cards.collect_contributions().
Output: assets/activity-snake-dark.svg and assets/activity-snake-light.svg
"""

import datetime
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

import gen_cards

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
OUT_DIR = ROOT / "assets"
README_PATH = ROOT / "README.md"

FILES = (("activity-snake-dark.svg", "dark"), ("activity-snake-light.svg", "light"))
STAMP_RE = re.compile(r"(activity-snake-(?:dark|light)\.svg\?v=)(\d+)")

COLS, ROWS = 53, 7
CELL, PITCH, X0, Y0 = 12, 16, 2, 2
BAR_Y, BAR_H, BAR_W = 144, 12, 848
BASE_LEN = 3
SPS = 10.0
PAUSE = 26

PALETTES = {
    "dark": {
        "empty": "#161b22",
        "levels": ("#0e4429", "#006d32", "#26a641", "#39d353"),
        "snake": ("#c9e4ff", "#79c0ff", "#58a6ff"),
        "snake_tail": ("#4485d4", "#3a6cb0"),
        "acc": "#f59e0b",
        "bg": "#0d1117",
        "ghost_k": 0.45,
        "track": "#161b22",
    },
    "light": {
        "empty": "#ebedf0",
        "levels": ("#9be9a8", "#40c463", "#30a14e", "#216e39"),
        "snake": ("#a5b4fc", "#7c93ff", "#4d6bfe"),
        "snake_tail": ("#3b5bf5", "#2a45d8"),
        "acc": "#b45309",
        "bg": "#ffffff",
        "ghost_k": 0.20,
        "track": "#ebedf0",
    },
}


# ============ 贪吃蛇路径规划（取自 Acfufu/token-stats.py）============
def plan_snake_route(cells, base_len=3, caps=(40, 36, 32, 28, 24, 20, 16, 12)):
    """觅食式路径：像玩家操纵的蛇逐格狩猎，而不是固定扫描线。

    cells: {(col,row): 亮度级别}，只含真实渲染的格子，级别>0 视为食物。
    每轮选目标：离蛇头最近者优先、同距偏好更暗的格子（亮度进排序键是 +2*级别
    惩罚——亮格留到最后被吃，与热力图衰减层次一致）；对前 24 个候选逐个
    BFS 最短路（蛇身为障碍），走过去途中每吃一格身体长一节。每步落子前
    检查"走完后尾巴仍可达"（BFS 头→尾）防自困；被自己围死时改追尾巴
    腾空间。上限 cap 从大到小搜索，取还能吃完全盘的最大身长。

    返回 (route, eats, growth, pops, left)：route 为逐格坐标序列；eats 为
    [(route 下标, 格子)]；growth 为触发长节的 route 下标；pops[s] 为第 s 步
    弹出的尾节数（0/1，踩尾原步与普通挪动都会弹尾，渲染器据此重放蛇身）；
    left 为没吃到的食物数（0 = 全清）。棋盘无食物时返回 None。
    """
    food = {cr for cr, lev in cells.items() if lev > 0}
    if not food:
        return None
    cmax = max(c for c, _ in cells)

    def neighbors(cr):
        c, r = cr
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = (c + dc, r + dr)
            if 0 <= nb[0] <= cmax and 0 <= nb[1] <= 6 and nb in cells:
                yield nb

    def bfs(src, goal, blocked):
        if src == goal:
            return [src]
        q, seen = deque([(src, [src])]), {src}
        while q:
            cur, path = q.popleft()
            for nb in neighbors(cur):
                if nb in seen or (nb in blocked and nb != goal):
                    continue
                if nb == goal:
                    return path + [nb]
                seen.add(nb)
                q.append((nb, path + [nb]))
        return None

    def solve(cap):
        start = min(food, key=lambda cr: (cr[0], abs(cr[1] - 3)))
        remaining = set(food)
        body = deque([start])
        occupied = {start}
        route, eats, growth = [start], [], []
        pops = [0]

        def eat(cr):
            remaining.discard(cr)
            eats.append((len(route) - 1, cr))
            if base_len + len(growth) < cap:
                growth.append(len(route) - 1)

        def step_to(cr):
            removed = 0
            if cr == body[0] and cr not in remaining:
                occupied.discard(body.popleft())
                removed = 1
            route.append(cr)
            pops.append(0)
            body.append(cr)
            occupied.add(cr)
            if cr in remaining:
                eat(cr)
            while len(body) > base_len + len(growth):
                occupied.discard(body.popleft())
                removed = 1
            pops[len(pops) - 1] = removed

        eat(start)

        def safe_step(cr):
            if len(body) < 8:
                return True
            new_occ = set(occupied) | {cr}
            new_tail = body[0]
            if cr not in remaining:
                new_occ.discard(body[0])
                new_tail = body[1] if len(body) > 1 else cr
            return bfs(cr, new_tail, new_occ - {new_tail, cr}) is not None

        stuck = 0
        while remaining and len(route) < 4000:
            head = body[-1]
            blocked = occupied - {head}
            path = None
            for cand in sorted(
                remaining,
                key=lambda cr: abs(cr[0] - head[0]) + abs(cr[1] - head[1]) + cells[cr] * 2,
            )[:24]:
                p_ = bfs(head, cand, blocked)
                if p_ and len(p_) > 1:
                    path = p_
                    break
            if path:
                aborted = False
                for cell in path[1:]:
                    if not safe_step(cell):
                        aborted = True
                        break
                    step_to(cell)
                if not aborted:
                    stuck = 0
                    continue
            stuck += 1
            if stuck > 400:
                break
            head = body[-1]
            tail = body[0]
            tpath = bfs(head, tail, (occupied - {head}) - {tail})
            nxt = None
            if tpath and len(tpath) > 1 and (tpath[1] == tail or tpath[1] not in occupied) and safe_step(tpath[1]):
                nxt = tpath[1]
            else:
                free = [nb for nb in neighbors(head) if nb not in occupied or nb == tail]
                safe = [nb for nb in free if safe_step(nb)]
                pool = safe or free
                if pool:
                    nxt = max(pool, key=lambda cr: sum(1 for n2 in neighbors(cr) if n2 not in occupied))
            if nxt is None:
                break
            step_to(nxt)

        return route, eats, growth, pops, len(remaining)

    for cap in caps:
        route, eats, growth, pops, left = solve(cap)
        if left == 0:
            break
    return route, eats, growth, pops, left


# ============ 网格与渲染 ============
def _level_fn(values):
    pos = sorted(v for v in values if v > 0)
    if not pos:
        return lambda v: 0
    q1 = pos[len(pos) // 4]
    q2 = pos[len(pos) // 2]
    q3 = pos[(len(pos) * 3) // 4]

    def lvl(v):
        if v <= 0:
            return 0
        if v <= q1:
            return 1
        if v <= q2:
            return 2
        if v <= q3:
            return 3
        return 4

    return lvl


def build_grid(daily):
    """53 列 x 7 行（周日开头）；日历范围外没有数据的日期按 0 级补全。"""
    days = sorted(daily)
    if not days:
        return None, {}
    last = datetime.date.fromisoformat(days[-1])
    week_end = last - datetime.timedelta(days=(last.weekday() + 1) % 7)
    start = week_end - datetime.timedelta(days=(COLS - 1) * 7)
    lvl = _level_fn(daily.values())
    grid = {}
    for c in range(COLS):
        for r in range(ROWS):
            dd = start + datetime.timedelta(days=c * 7 + r)
            grid[(c, r)] = lvl(daily.get(dd.isoformat(), 0))
    return start, grid


def _pct(s, total):
    return round(s / total * 100.0, 3)


def render(grid, plan, daily, dark=True):
    pal = PALETTES["dark" if dark else "light"]
    route, eats, growth, pops = plan[:4] if plan else ([], [], [], [])
    n = len(route)
    total = n + PAUSE
    dur = total / SPS
    eps = 0.05

    intervals, open_iv = {}, {}
    body_len = []
    body, prev = deque(), set()
    for s in range(n):
        body.append(route[s])
        if pops[s]:
            body.popleft()
        body_len.append(len(body))
        cur = set(body)
        for cr in cur - prev:
            open_iv[cr] = s
        for cr in prev - cur:
            intervals.setdefault(cr, []).append((open_iv.pop(cr), s))
        prev = cur
    for cr, s0 in open_iv.items():
        intervals.setdefault(cr, []).append((s0, total))

    eaten_at = {cr: s for s, cr in eats}
    snk_head, snk_flash, snk_body = pal["snake"]
    snk_old1, snk_old2 = pal["snake_tail"]
    bg_hex = pal["bg"]

    def _ghost(c, k=0.45):
        return "#" + "".join(
            "{0:02x}".format(round(int(c[i:i + 2], 16) * (1 - k) + int(bg_hex[i:i + 2], 16) * k))
            for i in (1, 3, 5))

    ghost_k = pal.get("ghost_k", 0.45)

    lmax = max(body_len) if body_len else 1

    def _ramp_color(age, ghost=False):
        # Keep most of the body in the bright cyan range; only the last
        # quarter fades through the two darker blues.  The earlier 8/38/75
        # split made a 70+ segment snake look almost uniformly dark.
        frac = age / float(max(lmax - 1, 1))
        if frac < 0.62:
            col = snk_flash
        elif frac < 0.85:
            col = snk_body
        elif frac < 0.95:
            col = snk_old1
        else:
            col = snk_old2
        return _ghost(col, ghost_k) if ghost else col

    swaps = []
    body_sim = deque()
    for s in range(n):
        if body_sim and route[s] == body_sim[0]:
            swaps.append(s)
        body_sim.append(route[s])
        if pops[s]:
            body_sim.popleft()
    runs = []
    for s in swaps:
        if runs and s - runs[-1][1] <= 3:
            runs[-1][1] = s
        else:
            runs.append([s, s])
    windows = [(max(0, a - 4), min(n, b + 4)) for a, b in runs if b - a <= 10]

    def _in_escape(a, b):
        return any(a < wb and b > wa for wa, wb in windows)

    def _add_ramp(stops, born, end):
        last_col = None
        for s in range(born + 1, min(end, n)):
            col = _ramp_color(s - born, _in_escape(s, s + 1))
            if col != last_col:
                stops.append((_pct(s, total), col))
                last_col = col

    css = ["@media (prefers-reduced-motion: reduce) { * { animation: none !important; } "
           ".u1, .u2, .u3, .u4 { transform: scale(1, 1) !important; } }"]

    # 蛇身按“格子被占用”上色：进格闪头色，随后按入队年龄转暗（头亮尾暗）。
    for (c, r), ivs in sorted(intervals.items()):
        lev = grid[(c, r)]
        base = pal["levels"][lev - 1] if lev else pal["empty"]
        cls = "m{0}_{1}".format(c, r)
        stops = [(0.0, base)]
        swap_hits = [s for s in swaps if route[s] == (c, r)]
        for a, b in ivs:
            post = pal["track"] if eaten_at.get((c, r), n) <= b else base
            pa = _pct(a, total)
            if pa > 0:
                stops.append((max(pa - eps, 0.0), None))
            stops.append((pa, snk_head))
            _add_ramp(stops, a, b)
            for s in [x for x in swap_hits if a <= x < b]:
                ps = _pct(s, total)
                if ps > 0:
                    stops.append((max(ps - eps, 0.0), None))
                stops.append((ps, snk_head))
                _add_ramp(stops, s, b)
            if b < total:
                pb = _pct(b, total)
                stops.append((max(pb - eps, 0.0), None))
                stops.append((pb, post))
        ordered = sorted(stops, key=lambda x: (x[0], 0 if x[1] is None else 1))
        frames, last = [], base
        for p, col in ordered:
            if col is None:
                frames.append("{0:.3f}% {{ fill: {1}; }}".format(p, last))
            else:
                frames.append("{0:.3f}% {{ fill: {1}; }}".format(p, col))
                last = col
        frames.append("100% {{ fill: {0}; }}".format(last))
        anim = "k{0} {1:.1f}s linear infinite".format(cls, dur)
        if swap_hits:
            pts = [(0.0, "transparent")]
            for s in swap_hits:
                pts.append((max(_pct(s, total) - 0.10, 0.0), "transparent"))
                pts.append((_pct(s, total), "{0}; stroke-width: 2".format(pal["acc"])))
                pts.append((min(_pct(s, total) + 0.12, 99.99), "transparent"))
            pts.append((100.0, "transparent"))
            kf = " ".join("{0:.3f}% {{ stroke: {1}; }}".format(p, c2) for p, c2 in pts)
            css.append("@keyframes s{0} {{ {1} }}".format(cls, kf))
            anim += ", s{0} {1:.1f}s linear infinite".format(cls, dur)
        css.append("@keyframes k{0} {{ {1} }}\n.{0} {{ animation: {2}; }}".format(
            cls, " ".join(frames), anim))

    per_level = [0, 0, 0, 0]
    for lev in grid.values():
        if lev:
            per_level[lev - 1] += 1
    food_total = sum(per_level)
    eats_by_level = {1: [], 2: [], 3: [], 4: []}
    for s, cr in eats:
        eats_by_level[grid[cr]].append(s)

    bars = []
    bx = 0.0
    for lev in range(1, 5):
        cnt = per_level[lev - 1]
        if not cnt:
            continue
        w = BAR_W * cnt / max(food_total, 1)
        steps = sorted(eats_by_level[lev])
        frames = ["0% { transform: scale(0, 1); }"]
        for k, s in enumerate(steps):
            if s == 0:
                continue
            frames.append("{0:.3f}% {{ transform: scale({1:.4f}, 1); }}".format(
                _pct(s, total), (k + 1) / cnt))
        frames.append("100% {{ transform: scale({0:.4f}, 1); }}".format(min(len(steps) / cnt, 1.0)))
        css.append("@keyframes u{0} {{ {1} }}\n"
                   ".u{0} {{ transform-box: fill-box; transform-origin: left center; "
                   "animation: u{0} {2:.1f}s linear infinite; }}".format(lev, " ".join(frames), dur))
        bars.append('<rect x="{0:.1f}" y="{1}" width="{2:.1f}" height="{3}" rx="2" '
                    'fill="{4}" class="u{5}"/>'.format(bx, BAR_Y, w, BAR_H, pal["levels"][lev - 1], lev))
        bx += w

    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="-16 -32 880 192" width="880" '
           'height="192" role="img" aria-label="GitHub contribution snake">',
           '<title>GitHub contribution snake</title>',
           '<desc>Contribution cells eaten by a snake, from the GitHub contribution calendar.</desc>',
           '<style>{0}</style>'.format("".join(css))]

    rects = []
    for (c, r) in sorted(grid):
        lev = grid[(c, r)]
        x, y = X0 + c * PITCH, Y0 + r * PITCH
        fill = pal["empty"] if lev == 0 else pal["levels"][lev - 1]
        cls = ' class="m{0}_{1}"'.format(c, r) if (c, r) in intervals else ""
        rects.append('<rect x="{0}" y="{1}" width="{2}" height="{2}" rx="2" fill="{3}"{4}/>'.format(
            x, y, CELL, fill, cls))
    out.extend(rects)
    out.extend(bars)
    out.append("</svg>")
    stats = {"steps": n, "eaten": len(eats), "left": (plan[4] if plan else 0),
             "duration": round(dur, 1), "cells": len(grid), "food": food_total}
    return "".join(out), stats


def _bump_readme_stamp(text, stamp):
    return STAMP_RE.sub(lambda m: m.group(1) + str(stamp), text)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    no_stamp = "--no-stamp" in argv
    out_dir = OUT_DIR
    if "--out" in argv:
        out_dir = Path(argv[argv.index("--out") + 1]).resolve()

    daily, total = gen_cards.collect_contributions()
    if not daily:
        print("error: no contribution data", file=sys.stderr)
        return 1
    start, grid = build_grid(daily)
    food = sum(1 for v in grid.values() if v > 0)
    caps = tuple([BASE_LEN + max(food, 1)] + [40, 36, 32, 28, 24, 20, 16, 12])
    plan = plan_snake_route({cr: lev for cr, lev in grid.items()}, base_len=BASE_LEN, caps=caps)
    if plan is None:
        print("warn: no contribution cells to eat, rendering static grid", file=sys.stderr)

    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {}
    changed = False
    for fname, theme in FILES:
        svg, st = render(grid, plan, daily, dark=(theme == "dark"))
        path = out_dir / fname
        if not path.exists() or path.read_text(encoding="utf-8") != svg:
            changed = True
        tmp = path.with_suffix(".svg.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(svg)
        os.replace(tmp, path)
        st["bytes"] = len(svg.encode("utf-8"))
        stats[theme] = st
    stats["calendar"] = {"cells": len(daily), "total": total,
                         "first": min(daily), "last": max(daily), "start": str(start)}
    if plan:
        stats["route"] = {"steps": len(plan[0]), "eaten": len(plan[1]), "left": plan[4]}

    if as_json:
        import json
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    if changed and not no_stamp and out_dir == OUT_DIR and README_PATH.exists():
        text = README_PATH.read_text(encoding="utf-8")
        new = _bump_readme_stamp(text, int(time.time()))
        if new != text:
            with open(README_PATH, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
            print("readme: cache-bust stamp updated")
    for theme in ("dark", "light"):
        st = stats[theme]
        print("{0}: {1} bytes, {2} steps, {3} eaten, left={4}, {5}s".format(
            theme, st["bytes"], st["steps"], st["eaten"], st["left"], st["duration"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
