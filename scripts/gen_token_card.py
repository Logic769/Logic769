#!/usr/bin/env python3
"""生成 GitHub 主页用的 Token 统计卡（中文 / 暗亮双主题）。

数据源：~/.tokentracker/tracker/queue.jsonl（TokenTracker 逐小时用量记录）
定价：scripts/token_card/pricing-snapshot.json（复制自 TokenTracker 的 curated 价表，
      MIT 许可）+ ~/.tokentracker/cache/pricing.json（LiteLLM 兜底）
口径：按 (source, model, hour_start) 三元组 last-wins 去重；成本按
      input/output/cached/cache_creation/reasoning 五列计费，total_tokens 只作展示分母；
      DeepSeek 四款模型按 UTC 高峰（01-04、06-10 时）与北京周末规则打五折。

用法：
    python scripts/gen_token_card.py            # 写 assets/token-stats-zh-{dark,light}.svg
    python scripts/gen_token_card.py --json     # 只打印聚合结果
    python scripts/gen_token_card.py --verify <row-costs.jsonl>   # 逐行比对定价
"""

from __future__ import annotations

from collections import deque
from datetime import date
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
QUEUE = HOME / ".tokentracker" / "tracker" / "queue.jsonl"
LITELLM_CACHE = HOME / ".tokentracker" / "cache" / "pricing.json"
HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "token_card" / "pricing-snapshot.json"
ROOT = HERE.parent
OUT_DIR = ROOT / "assets"

# ---------------------------------------------------------------- 定价常量

DEEPSEEK_TIME_PRICED = ("deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-flash", "deepseek-v4-pro")
DEEPSEEK_WEEKEND_FROM = datetime(2026, 8, 22, 16, 0, 0, tzinfo=timezone.utc)
LOCAL_INFERENCE_SOURCES = {"lmstudio"}
PI_SUBSCRIPTION_SOURCES = {"pi-github-copilot", "pi-copilot", "prime-agent-github-copilot", "prime-agent-copilot"}
AUTHORITATIVE_COST_SOURCES = {"grok", "cline"}
REASONING_FOLD_SOURCES = {"codex", "acode", "every-code", "omo", "cline", "muse"}
ZERO = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def _round10(x: float) -> float:
    return math.floor(x * 1e10 + 0.5) / 1e10 if x >= 0 else -math.floor(-x * 1e10 + 0.5) / 1e10


def _load_snapshot() -> dict:
    with open(SNAPSHOT, encoding="utf-8") as fh:
        return json.load(fh)


def _load_litellm() -> dict:
    """LiteLLM 原始 per-token 表 -> 每百万 token 美元。"""
    try:
        with open(LITELLM_CACHE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for name, entry in raw.items():
        if name.startswith("_") or not isinstance(entry, dict):
            continue
        conv = {}
        for src, dst in (
            ("input_cost_per_token", "input"),
            ("output_cost_per_token", "output"),
            ("cache_read_input_token_cost", "cache_read"),
            ("cache_creation_input_token_cost", "cache_write"),
        ):
            val = entry.get(src)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                conv[dst] = _round10(val * 1_000_000)
        if conv:
            out[name] = conv
    return out


def _exact_ci(table: dict, model: str):
    if not table or not model:
        return None
    if model in table:
        return table[model]
    low = model.lower()
    for key, val in table.items():
        if key.lower() == low:
            return val
    return None


def _contained_ci(table: dict, model: str):
    if not table or not model:
        return None
    low = model.lower()
    for key in sorted(table, key=len, reverse=True):
        start = low.find(key.lower())
        if start < 0:
            continue
        suffix = low[start + len(key):]
        if suffix[:1].isdigit() or suffix[:1] == ".":
            continue
        if _SKU_MARKER.search(suffix):
            continue
        return table[key]
    return None


import re  # noqa: E402  (放在使用处附近，保持上面的表结构清晰)

_SKU_MARKER = re.compile(r"(?:^|-)(?:preview|next|exp|experimental|beta|alpha)(?:-|$)")
_SUFFIX_STRIP = [re.compile(p) for p in (
    r"-xhigh-fast$", r"-high-fast$", r"-medium-fast$", r"-low-fast$",
    r"-xhigh$", r"-high$", r"-medium$", r"-low$", r"-fast$",
)]


def _dot_restored(model: str) -> str:
    low = model.lower()
    restored = re.sub(r"(\d+)-(\d+)", r"\1.\2", low)
    return "" if restored == low else restored


def lookup_pricing(model: str, source: str, curated: dict, litellm: dict):
    """复刻 TokenTracker matcher 的解析顺序，返回 (rates, source_tag)。"""
    if not model:
        return None, "empty"
    low = model.lower()
    dot = _dot_restored(model)
    src = (source or "").lower()

    if src == "workbuddy-ai" and low.strip() == "auto":
        return None, "miss"
    if src == "cline" and low.endswith(":free"):
        return dict(ZERO), "curated:cline-free-suffix"
    if src == "acode" and (low == "auto" or low.endswith("-auto")):
        return None, "miss"

    src_table = (curated.get("source_exact") or {}).get(src)
    if src_table:
        hit = _exact_ci(src_table, model)
        if hit:
            return hit, "curated:source-exact"

    exact = curated.get("exact") or {}
    if model in exact:
        return exact[model], "curated:exact"
    hit = _exact_ci(exact, dot) if dot else None
    if hit:
        return hit, "curated:exact-dot"

    if re.match(r"(?:^|/)qwen3[.-]8-flash(?:-\d{4}-\d{2}-\d{2})?$", low.strip()) and "qwen3.8-flash" in exact:
        return exact["qwen3.8-flash"], "curated:exact"
    if re.match(r"(?:^|/)qwen3[.-]8-max(?:-0902|-\d{4}-\d{2}-\d{2})?$", low.strip()) and "qwen3.8-max" in exact:
        return exact["qwen3.8-max"], "curated:exact"

    if litellm.get(model):
        return litellm[model], "litellm:exact"
    hit = _exact_ci(litellm, dot) if dot else None
    if hit:
        return hit, "litellm:exact-dot"

    if low.endswith("-free"):
        return dict(ZERO), "curated:opencode-free-suffix" if src == "opencode" else "curated:free-suffix"

    if re.search(r"(?:^|/)(?:kimicoding(?:/k2\.8)?|kimi-k2\.8|k2\.8)(?:-|\.|$)", low.strip()) or low.strip() == "kimicoding":
        return None, "miss"

    hit = _contained_ci(exact, dot) if dot else None
    if hit:
        return hit, "curated:exact-dot"

    alias = curated.get("alias") or {}
    target = alias.get(model)
    if target and target in exact:
        return exact[target], "curated:alias"

    for item in curated.get("fuzzy") or []:
        needle = (item.get("match") or "").lower()
        ref = item.get("ref") or ""
        if not needle or ref not in exact:
            continue
        if needle in low or (dot and needle in dot):
            return exact[ref], "curated:fuzzy"

    for pat in _SUFFIX_STRIP:
        if pat.search(model):
            stripped = pat.sub("", model)
            if stripped != model and litellm.get(stripped):
                return litellm[stripped], "litellm:strip"

    suffix = "/" + low
    best = None
    for key in litellm:
        if len(key) > len(suffix) and key.lower().endswith(suffix):
            if best is None or key < best:
                best = key
    if best:
        return litellm[best], "litellm:prefix-strip"

    for key in sorted(litellm, key=len, reverse=True):
        klow = key.lower()
        if klow in low or (dot and klow in dot):
            return litellm[key], "litellm:fuzzy"
    return None, "miss"


def _is_time_priced(model: str) -> bool:
    low = (model or "").lower()
    return any(name in low for name in DEEPSEEK_TIME_PRICED)


def _parse_hour_start(value: str) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _off_peak(row: dict) -> bool:
    tier = row.get("pricing_tier")
    if tier == "off_peak":
        return True
    if tier == "peak":
        return False
    dt = _parse_hour_start(row.get("hour_start", ""))
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.astimezone(timezone.utc)
    if ts >= DEEPSEEK_WEEKEND_FROM:
        beijing_day = (ts + timedelta(hours=8)).weekday()  # 周一=0
        if (beijing_day + 1) % 7 in (0, 6):  # 转成 JS getUTCDay 口径：周日=0
            return True
    return not (1 <= ts.hour < 4 or 6 <= ts.hour < 10)


def _norm_rates(rates) -> dict:
    """统一成四个键（缺项补 0），避免调用处 KeyError。"""
    if not rates:
        return dict(ZERO)
    return {k: float(rates.get(k) or 0) for k in ZERO}


def row_pricing(row: dict, curated: dict, litellm: dict) -> dict:
    rates, _tag = lookup_pricing(row.get("model") or "", row.get("source") or "", curated, litellm)
    rates = _norm_rates(rates)
    if (row.get("source") or "").lower() == "acode":
        return rates
    if not _is_time_priced(row.get("model") or "") or not _off_peak(row):
        return rates
    return {k: (rates.get(k) or 0) * 0.5 for k in ZERO}


def compute_row_cost(row: dict, curated: dict, litellm: dict) -> float:
    src = (row.get("source") or "").lower()
    if src in LOCAL_INFERENCE_SOURCES or src in PI_SUBSCRIPTION_SOURCES:
        return 0.0
    reported = row.get("total_cost_usd")
    if src in AUTHORITATIVE_COST_SOURCES and isinstance(reported, (int, float)) and reported > 0:
        return float(reported)
    rates = row_pricing(row, curated, litellm)
    folded = (row.get("source") or "") in REASONING_FOLD_SOURCES
    reasoning_cost = 0.0 if folded else (row.get("reasoning_output_tokens") or 0) * rates["output"]
    return (
        (row.get("input_tokens") or 0) * rates["input"]
        + (row.get("output_tokens") or 0) * rates["output"]
        + (row.get("cached_input_tokens") or 0) * rates["cache_read"]
        + (row.get("cache_creation_input_tokens") or 0) * rates["cache_write"]
        + reasoning_cost
    ) / 1_000_000
# ---------------------------------------------------------------- 数据聚合


def load_rows(path: Path = QUEUE) -> tuple[list[dict], dict]:
    """按 (source, model, hour_start) last-wins 去重，返回 (rows, counts)。"""
    counts = {"raw": 0, "blank": 0, "parse_errors": 0, "duplicates": 0}
    order: dict[tuple, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                counts["blank"] += 1
                continue
            counts["raw"] += 1
            try:
                row = json.loads(line)
            except ValueError:
                counts["parse_errors"] += 1
                continue
            key = (row.get("source") or "", row.get("model") or "", row.get("hour_start") or "")
            if key in order:
                counts["duplicates"] += 1
            order[key] = row
    return list(order.values()), counts


def aggregate(rows: list[dict], curated: dict, litellm: dict) -> dict:
    totals = {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "conversations": 0,
        "cost_usd": 0.0,
    }
    by_model: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    status_cache: dict[tuple, str] = {}

    for row in rows:
        tokens = int(row.get("total_tokens") or 0)
        cost = compute_row_cost(row, curated, litellm)
        conv = int(row.get("conversation_count") or 0)
        model = row.get("model") or "unknown"
        source = row.get("source") or "unknown"
        day = str(row.get("hour_start") or "")[:10]

        totals["total_tokens"] += tokens
        totals["input_tokens"] += int(row.get("input_tokens") or 0)
        totals["output_tokens"] += int(row.get("output_tokens") or 0)
        totals["cached_input_tokens"] += int(row.get("cached_input_tokens") or 0)
        totals["cache_creation_input_tokens"] += int(row.get("cache_creation_input_tokens") or 0)
        totals["reasoning_output_tokens"] += int(row.get("reasoning_output_tokens") or 0)
        totals["conversations"] += conv
        totals["cost_usd"] += cost

        m = by_model.setdefault(model, {"model": model, "tokens": 0, "cost_usd": 0.0, "rows": 0, "conversations": 0, "statuses": set()})
        m["tokens"] += tokens
        m["cost_usd"] += cost
        m["rows"] += 1
        m["conversations"] += conv
        key = ((row.get("source") or "").lower(), model)
        if key not in status_cache:
            rates, _tag = lookup_pricing(model, row.get("source") or "", curated, litellm)
            if rates is None:
                status_cache[key] = "unpriced"
            elif any((rates.get(k) or 0) > 0 for k in ZERO):
                status_cache[key] = "priced"
            else:
                status_cache[key] = "free"
        m["statuses"].add(status_cache[key])

        s = by_source.setdefault(source, {"source": source, "tokens": 0, "cost_usd": 0.0, "rows": 0, "conversations": 0})
        s["tokens"] += tokens
        s["cost_usd"] += cost
        s["rows"] += 1
        s["conversations"] += conv

        if day:
            d = daily.setdefault(day, {"date": day, "tokens": 0, "cost_usd": 0.0, "rows": 0})
            d["tokens"] += tokens
            d["cost_usd"] += cost
            d["rows"] += 1

    models = []
    for info in by_model.values():
        statuses = info.pop("statuses")
        info["status"] = "priced" if statuses == {"priced"} else ("free" if statuses == {"free"} else "mixed")
        models.append(info)
    models.sort(key=lambda v: -v["tokens"])

    unpriced_tokens = sum(v["tokens"] for v in models if v["status"] != "priced")
    unpriced_models = sorted(
        ((v["model"], v["tokens"], v["status"]) for v in models if v["status"] != "priced"),
        key=lambda x: -x[1],
    )

    days = sorted(daily)
    return {
        "totals": totals,
        "by_model": models,
        "by_source": sorted(by_source.values(), key=lambda v: -v["tokens"]),
        "daily": [daily[d] for d in days],
        "date_range": (days[0], days[-1]) if days else ("", ""),
        "rows": len(rows),
        "unpriced_tokens": unpriced_tokens,
        "unpriced_models": unpriced_models,
    }


def verify_row_costs(path: Path, curated: dict, litellm: dict) -> int:
    """与 TokenTracker 原生实现导出的逐行成本做逐行断言。"""
    rows, counts = load_rows()
    index = {(r.get("source"), r.get("model"), r.get("hour_start")): r for r in rows}
    bad = 0
    checked = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ref = json.loads(line)
            key = (ref["source"], ref["model"], ref["hour_start"])
            row = index.get(key)
            if row is None:
                print("MISSING", key)
                bad += 1
                continue
            got = compute_row_cost(row, curated, litellm)
            want = float(ref["cost_usd"])
            checked += 1
            if abs(got - want) > 1e-9:
                bad += 1
                if bad <= 10:
                    print(f"DIFF {key} got={got!r} want={want!r}")
    print(f"verify: checked={checked} mismatches={bad} dedup={counts}")
    return bad


# ---------------------------------------------------------------- 格式化

def fmt_tokens(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return "%.1f" % (n / 1_000_000_000) + "B"
    if n >= 1_000_000:
        return "%.1f" % (n / 1_000_000) + "M"
    if n >= 1_000:
        return "%.1f" % (n / 1_000) + "K"
    return str(n)


def fmt_cost(usd: float) -> str:
    usd = float(usd or 0)
    if usd >= 1000:
        return "$" + "%.1f" % (usd / 1000) + "K"
    if usd >= 100:
        return "$" + "%.0f" % usd
    if usd >= 1:
        return "$" + "%.1f" % usd
    return "$" + "%.2f" % usd


def fmt_conversations(n) -> str:
    n = int(n or 0)
    if n >= 10000:
        return "%.1f" % (n / 10000) + " 万次对话"
    return str(n) + " 次对话"


def esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_width(text: str, size: float) -> float:
    width = 0.0
    for ch in str(text):
        width += size if ord(ch) > 0x2E80 else size * 0.56
    return width


# ---------------------------------------------------------------- 主题

THEMES = {
    "dark": {
        "bg": "#0a0e14", "card": "#0a0e14", "titlebar": "#111826", "border": "#22406a",
        "track": "#161b22", "text": "#e6edf3", "text2": "#c9d1d9", "muted": "#6e7f95",
        "faint": "#57606a", "user": "#58a6ff", "bios": "#79c0ff",
        "acc": "#f59e0b", "acc_hi": "#fbbf24",
        "snake": ("#c9e4ff", "#79c0ff", "#58a6ff"),
        "tail": ("#4485d4", "#3a6cb0"),
        "hm": ["#161b22", "#4a3a12", "#7d5a17", "#b9821d", "#fbbf24"],
        "split": ["#fbbf24", "#f59e0b", "#fde68a", "#8b949e"],
        "grid": "rgba(110,127,149,0.18)",
    },
    "light": {
        "bg": "#ffffff", "card": "#ffffff", "titlebar": "#f6f8fa", "border": "#a9c4e4",
        "track": "#ebedf0", "text": "#1f2328", "text2": "#424a53", "muted": "#8c9bb0",
        "faint": "#8c959f", "user": "#0969da", "bios": "#0a5cc2",
        "acc": "#b45309", "acc_hi": "#d97706",
        "snake": ("#54aeff", "#0a5cc2", "#0969da"),
        "tail": ("#3d8ae0", "#79b3ec"),
        "hm": ["#ebedf0", "#f5e0b0", "#e8c26a", "#d69b2f", "#b45309"],
        "split": ["#d97706", "#b45309", "#f59e0b", "#8c959f"],
        "grid": "rgba(140,149,159,0.2)",
    },
}

FONT = "ui-monospace,'SF Mono',Menlo,Consolas,'PingFang SC','Microsoft YaHei',monospace"

PALETTE_TOOLS = ["#e64980", "#6c63ff", "#22b8cf", "#51cf66", "#e3b341", "#8b949e"]
PALETTE_MODELS = ["#f59e0b", "#58a6ff", "#51cf66", "#e64980", "#22b8cf", "#a371f7", "#e3b341", "#8b949e"]


# ---------------------------------------------------------------- 热力图 / 贪吃蛇

def build_grid(daily: list, today: str, max_cols: int = 62) -> dict:
    """把按日聚合的 token 序列排成贡献墙（周日为第一行，列=周）。"""
    by_day = {d["date"]: int(d["tokens"] or 0) for d in daily if d.get("date")}
    if not by_day:
        return {"cols": 0, "rows": 7, "cells": {}, "levels": {}, "months": [],
                "start": "", "end": "", "today": 0, "thresholds": []}

    first = date.fromisoformat(min(by_day))
    last = date.fromisoformat(max(by_day))
    start = first - timedelta(days=(first.weekday() + 1) % 7)

    cols = ((last - start).days // 7) + 1
    if cols > max_cols:
        start = start + timedelta(days=(cols - max_cols) * 7)
        cols = max_cols
    cells = {}
    months = []
    seen_month = None
    for c in range(cols):
        for r in range(7):
            day = start + timedelta(days=c * 7 + r)
            if day > last:
                continue
            cells[(r, c)] = by_day.get(day.isoformat(), 0)
            if r == 0 and day >= first and day.month != seen_month:
                seen_month = day.month
                months.append((c, str(day.month) + " 月"))

    values = sorted(v for v in cells.values() if v > 0)
    thresholds = []
    if values:
        for q in (0.4, 0.7, 0.9):
            thresholds.append(values[min(len(values) - 1, int(len(values) * q))])

    def level(v):
        if v <= 0:
            return 0
        return 1 + sum(1 for t in thresholds if v > t)

    return {
        "cols": cols, "rows": 7, "cells": cells,
        "levels": {k: level(v) for k, v in cells.items()},
        "months": months, "start": start.isoformat(), "end": last.isoformat(),
        "today": int(by_day.get(today, 0)), "thresholds": thresholds,
    }




def plan_route(grid: dict, base_len: int = 3, cap: int = 12) -> dict:
    """蛇沿热力图蛇形巡游（逐列上下往返），吃掉途中每一个有 token 的格子。"""
    rows, cols = grid["rows"], grid["cols"]
    cells = grid["cells"]
    if not cells:
        return {"route": [], "eaten": [], "total": 0, "lens": []}

    order = []
    for c in range(cols):
        rng = range(rows) if c % 2 == 0 else range(rows - 1, -1, -1)
        for r in rng:
            if (r, c) in cells:
                order.append((r, c))

    lens = []
    grown = base_len
    for pos in order:
        if cells.get(pos, 0) > 0:
            grown = min(cap, grown + 1)
        lens.append(grown)
    route = [(p, cells.get(p, 0) > 0) for p in order]
    eaten = [p for p in order if cells.get(p, 0) > 0]
    return {"route": route, "eaten": eaten, "total": len(eaten), "lens": lens}


# ---------------------------------------------------------------- 渲染：常量与工具

from datetime import date

W = 960
CARD_X = 4
CARD_W = 952
PAD = 36
RIGHT = W - PAD
TB_H = 38
CELL = 10
PITCH = 13
SPS = 10.0
PAUSE = 2.6

MODEL_LABELS = {
    "deepseek": "DeepSeek", "gpt": "GPT", "glm": "GLM", "qwen": "Qwen",
    "claude": "Claude", "gemini": "Gemini", "codex": "Codex", "sol": "Sol",
    "luna": "Luna", "flash": "Flash", "pro": "Pro", "mini": "Mini",
    "never": "NeverStuck", "stuck": "", "auto": "Auto", "rewsz": "",
}
SOURCE_LABELS = {
    "dsh": "DSH", "codex": "Codex", "zcode": "ZCode", "omp": "OMP",
    "acode": "Acode", "claude": "Claude", "cline": "Cline", "grok": "Grok",
    "muse": "Muse", "omo": "Omo", "pi": "Pi", "lmstudio": "LM Studio",
}


def _f(x) -> str:
    if isinstance(x, int):
        return str(x)
    s = "%.2f" % float(x)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def txt(x, y, s, size, fill, anchor=None, weight=None, ls=None, opacity=None, cls=None, style=None):
    out = '<text x="' + _f(x) + '" y="' + _f(y) + '" font-size="' + _f(size) + '" fill="' + fill + '"'
    if anchor:
        out += ' text-anchor="' + anchor + '"'
    if weight:
        out += ' font-weight="' + weight + '"'
    if ls:
        out += ' letter-spacing="' + ls + '"'
    if opacity is not None:
        out += ' opacity="' + _f(opacity) + '"'
    if cls:
        out += ' class="' + cls + '"'
    if style:
        out += ' style="' + style + '"'
    return out + ">" + esc(s) + "</text>"


def rect(x, y, w, h, fill, rx=None, cls=None, style=None, opacity=None):
    out = '<rect x="' + _f(x) + '" y="' + _f(y) + '" width="' + _f(w) + '" height="' + _f(h) + '"'
    if rx is not None:
        out += ' rx="' + _f(rx) + '"'
    out += ' fill="' + fill + '"'
    if opacity is not None:
        out += ' opacity="' + _f(opacity) + '"'
    if cls:
        out += ' class="' + cls + '"'
    if style:
        out += ' style="' + style + '"'
    return out + "/>"


def pretty_model(name: str) -> str:
    low = (name or "unknown").lower()
    if low in MODEL_LABELS:
        return MODEL_LABELS[low] or "Unknown"
    out = []
    for p in low.split("-"):
        if p in MODEL_LABELS:
            if MODEL_LABELS[p]:
                out.append(MODEL_LABELS[p])
        elif p[:1].isdigit():
            out.append(p.upper())
        else:
            out.append(p[:1].upper() + p[1:])
    return " ".join(out) or "Unknown"


def pretty_source(name: str) -> str:
    low = (name or "unknown").lower()
    if low in SOURCE_LABELS:
        return SOURCE_LABELS[low]
    return low[:1].upper() + low[1:]


def pct(a: float, b: float) -> str:
    if not b:
        return "0%"
    v = a / b * 100
    if v >= 10:
        return "%.1f%%" % v
    if v >= 0.1:
        return "%.2f%%" % v
    return "<0.1%"


def legend_rows(items, width, size=10.5, gap=18):
    rows, cur, cur_x = [], [], 0.0
    for label, value, color in items:
        w = 14 + text_width(label, size) + 6 + text_width(value, size)
        if cur and cur_x + w > width:
            rows.append(cur)
            cur, cur_x = [], 0.0
        cur.append((cur_x, label, value, color))
        cur_x += w + gap
    if cur:
        rows.append(cur)
    return rows


def _kf(name, stops):
    merged = []
    for p, decls in sorted(stops, key=lambda s: s[0]):
        key = round(max(0.0, min(100.0, p)), 3)
        if merged and merged[-1][0] == key:
            merged[-1][1].extend(decls)
        else:
            merged.append([key, list(decls)])
    body = "".join(_f(k) + "%{" + ";".join(d) + "}" for k, d in merged)
    return "@keyframes " + name + "{" + body + "}"


def snake_color(pal, age):
    if age < 1:
        return pal["snake"][0]
    if age < 5:
        return pal["snake"][1]
    if age < 13:
        return pal["snake"][2]
    return pal["tail"][0]


# ---------------------------------------------------------------- 渲染：卡片

def svg_card(data: dict, theme_name: str = "dark", login: str = "Logic769",
             host: str = "github", stamp: str = "") -> str:
    pal = THEMES[theme_name]
    tot = data["totals"]
    daily = data["daily"]
    today = stamp or date.today().isoformat()
    grid = build_grid(daily, today)
    plan = plan_route(grid)
    body = []
    css = []

    def add(s):
        body.append(s)

    def divider(y):
        add('<line x1="' + _f(PAD) + '" y1="' + _f(y) + '" x2="' + _f(RIGHT) + '" y2="' + _f(y)
            + '" stroke="' + pal["grid"] + '" stroke-width="1"/>')

    # ---- 标题栏
    add(rect(CARD_X, 0, CARD_W, TB_H, pal["titlebar"], rx=12))
    add(rect(CARD_X, TB_H - 12, CARD_W, 12, pal["titlebar"]))
    for i, col in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        add('<circle cx="' + _f(26 + i * 16) + '" cy="19" r="5.5" fill="' + col + '"/>')
    add(txt(86, 25, login + "@" + host + " ~ % tail -f ~/.tokens", 15, pal["text2"]))
    add(txt(RIGHT, 25, "TokenTracker · 实时", 15, pal["muted"], anchor="end"))

    # ---- 英雄区
    add(txt(PAD, 78, "AI TOKEN 使用量", 11, pal["muted"], ls="1.4"))
    add(txt(RIGHT, 78, "估算成本", 11, pal["muted"], anchor="end", ls="1.4"))
    big = fmt_tokens(tot["total_tokens"])
    add(txt(PAD, 122, big, 42, pal["text"], weight="700"))
    add(txt(PAD + text_width(big, 42) + 12, 122, "tokens", 13, pal["muted"]))
    start_day = data["date_range"][0] or today
    sd = date.fromisoformat(start_day)
    add(txt(PAD, 148, fmt_conversations(tot["conversations"]) + " · 自 "
            + str(sd.year) + " 年 " + str(sd.month) + " 月起", 11.5, pal["muted"]))
    add(txt(RIGHT, 118, fmt_cost(tot["cost_usd"]), 30, pal["acc_hi"], anchor="end", weight="700"))
    add(txt(RIGHT, 148, "官方定价 · 分时折扣", 11.5, pal["muted"], anchor="end"))
    divider(170)

    # ---- 热力图
    add(txt(PAD, 194, "TOKEN 活跃热力图", 11, pal["muted"], ls="1.4"))
    cols = grid["cols"]
    gw = cols * PITCH - (PITCH - CELL) if cols else 0
    block_w = 26 + gw + 34 + 92
    bx = PAD + max(0.0, (RIGHT - PAD - block_w) / 2.0)
    gx = bx + 26
    gy = 220
    grid_h = 6 * PITCH + CELL
    n_steps = len(plan["route"])
    dur = round(n_steps / SPS + PAUSE, 2) if n_steps else 0.0
    eps = (100.0 / n_steps) * 0.3 if n_steps else 0.0
    style_of = (lambda name: "animation:" + name + " " + _f(dur) + "s linear infinite") if n_steps else None
    if n_steps:
        css.append("@media (prefers-reduced-motion:reduce){*{animation:none!important}}")

    eat_step = {}
    for i, (pos, ate) in enumerate(plan["route"]):
        if ate:
            eat_step[pos] = i

    for (r, c), v in sorted(grid["cells"].items()):
        lvl = grid["levels"][(r, c)]
        x = gx + c * PITCH
        y = gy + r * PITCH
        if (r, c) in eat_step:
            pe = eat_step[(r, c)] / n_steps * 100
            name = "ce" + str(r) + "_" + str(c)
            css.append(_kf(name, [
                (0, ["fill:" + pal["hm"][lvl]]),
                (pe - eps, ["fill:" + pal["hm"][lvl]]),
                (pe + eps, ["fill:" + pal["track"]]),
                (100, ["fill:" + pal["hm"][lvl]]),
            ]))
            add(rect(x, y, CELL, CELL, pal["hm"][lvl], rx=2, cls=name, style=style_of(name)))
        else:
            add(rect(x, y, CELL, CELL, pal["hm"][lvl], rx=2))

    last_x = -999.0
    for c, label in grid["months"]:
        x = gx + c * PITCH
        if x - last_x < 36:
            continue
        last_x = x
        add(txt(x, 212, label, 9.5, pal["faint"]))
    for r, label in ((1, "一"), (3, "三"), (5, "五")):
        add(txt(gx - 10, gy + r * PITCH + 8, label, 9.5, pal["faint"], anchor="end"))
    add(txt(gx + gw + 34, gy + 18, "今日", 10, pal["muted"]))
    add(txt(gx + gw + 34, gy + 38, fmt_tokens(grid["today"]), 13, pal["text"], weight="600"))

    # 吃掉的 token 计数
    y_foot = gy + grid_h + 24
    add(txt(bx, y_foot, "$ 吃掉的 token：", 9.5, pal["muted"]))
    lx = bx + text_width("$ 吃掉的 token：", 9.5) + 10
    events = []
    cum = 0
    for i, (pos, ate) in enumerate(plan["route"]):
        if ate:
            cum += grid["cells"].get(pos, 0)
            events.append((i, cum))
    if events:
        for k, (step, val) in enumerate(events):
            p0 = step / n_steps * 100
            p1 = events[k + 1][0] / n_steps * 100 if k + 1 < len(events) else 100
            name = "ct" + str(k)
            css.append(_kf(name, [
                (0, ["opacity:0"]),
                (p0 - eps, ["opacity:0"]),
                (p0 + eps, ["opacity:1"]),
                (p1 - eps, ["opacity:1"]),
                (p1, ["opacity:0"]),
                (100, ["opacity:0"]),
            ]))
            add(txt(lx, y_foot, fmt_tokens(val) + " · " + pct(val, tot["total_tokens"]), 9.5,
                    pal["bios"], opacity=0, cls=name, style=style_of(name)))
    else:
        add(txt(lx, y_foot, fmt_tokens(0), 9.5, pal["bios"]))

    # 图例（贴住热力图块右端，避免与左侧计数脱节）
    leg_r = bx + block_w
    add(txt(leg_r - 96, y_foot, "少", 9.5, pal["faint"]))
    for i in range(4):
        add(rect(leg_r - 88 + i * 12, y_foot - 9, CELL, CELL, pal["hm"][i + 1], rx=2))
    add(txt(leg_r, y_foot, "多", 9.5, pal["faint"], anchor="end"))

    # ---- 蛇
    if n_steps:
        lens = plan["lens"]
        for i, (pos, ate) in enumerate(plan["route"]):
            r, c = pos
            L = lens[i]
            p0 = i / n_steps * 100
            p1 = (i + L) / n_steps * 100
            stops = [(0, ["opacity:0", "fill:" + snake_color(pal, 0)]),
                     (p0 - eps, ["opacity:0"]),
                     (p0 + eps, ["opacity:1", "fill:" + snake_color(pal, 0)])]
            for age in (1, 5, 13, 26):
                if age >= L:
                    continue
                stops.append((p0 + age * (100.0 / n_steps) + eps,
                              ["fill:" + snake_color(pal, age)]))
            stops.append((p1 - eps, ["opacity:1"]))
            stops.append((p1, ["opacity:0"]))
            stops.append((100, ["opacity:0", "fill:" + snake_color(pal, 0)]))
            name = "sg" + str(i)
            css.append(_kf(name, stops))
            add(rect(gx + c * PITCH, gy + r * PITCH, CELL, CELL, snake_color(pal, 0), rx=3,
                     cls=name, style=style_of(name), opacity=0))

    # ---- 趋势
    divider(350)
    add(txt(PAD, 374, "使用趋势", 11, pal["muted"], ls="1.4"))
    days = daily[-30:]
    chart_top, chart_h = 402, 96
    base_y = chart_top + chart_h
    usable = chart_h - 14
    vals_all = [d["tokens"] for d in daily]
    ma_all = []
    for i in range(len(vals_all)):
        lo = max(0, i - 6)
        ma_all.append(sum(vals_all[lo:i + 1]) / (i - lo + 1))
    ma = ma_all[-30:]
    n = len(days)
    vmax = max((d["tokens"] for d in days), default=0) or 1
    cw_all = RIGHT - PAD
    slot = cw_all / n if n else cw_all
    bw = min(16.0, slot * 0.66) if n else 16
    peak_i = max(range(n), key=lambda i: days[i]["tokens"]) if n else 0
    for gi in range(1, 4):
        gy_line = base_y - usable * gi / 4.0
        add('<line x1="' + _f(PAD) + '" y1="' + _f(gy_line) + '" x2="' + _f(RIGHT) + '" y2="' + _f(gy_line)
            + '" stroke="' + pal["grid"] + '" stroke-width="1" stroke-dasharray="3 5"/>')
    add('<line x1="' + _f(PAD) + '" y1="' + _f(base_y) + '" x2="' + _f(RIGHT) + '" y2="' + _f(base_y)
        + '" stroke="' + pal["grid"] + '" stroke-width="1"/>')
    for i, d in enumerate(days):
        h = max(1.0, (d["tokens"] / vmax) * usable) if d["tokens"] else 0.0
        x = PAD + i * slot + (slot - bw) / 2.0
        if i == peak_i:
            add(rect(x, base_y - h, bw, h, pal["acc_hi"], rx=1.5))
            add('<rect x="' + _f(x) + '" y="' + _f(base_y - h) + '" width="' + _f(bw) + '" height="'
                + _f(h) + '" rx="1.5" fill="none" stroke="' + pal["acc_hi"] + '" stroke-width="1.2"/>')
        else:
            add(rect(x, base_y - h, bw, h, pal["acc"], rx=1.5, opacity=0.7))
    if ma:
        pts = []
        for i, v in enumerate(ma):
            px = PAD + i * slot + slot / 2.0
            py = base_y - (v / vmax) * usable
            pts.append(_f(px) + "," + _f(py))
        add('<polyline points="' + " ".join(pts) + '" fill="none" stroke="' + pal["acc_hi"]
            + '" stroke-width="1.8" stroke-linecap="round" opacity="0.95"/>')
    lx2 = RIGHT
    for label, col in (("峰值", pal["acc_hi"]), ("7 日均线", pal["acc_hi"]), ("每日用量", pal["acc"])):
        w = 14 + text_width(label, 10.5)
        add(rect(lx2 - w, 370, 7, 7, col, rx=1.5))
        add(txt(lx2 - w + 12, 377, label, 10.5, pal["muted"]))
        lx2 -= w + 14
    if n:
        pd = date.fromisoformat(days[peak_i]["date"])
        add(txt(RIGHT, chart_top - 8, "峰值 " + str(pd.month) + "/" + str(pd.day) + " · "
                + fmt_tokens(vmax), 9.5, pal["faint"], anchor="end"))
        step = max(1, n // 4)
        for i in range(0, n, step):
            d = date.fromisoformat(days[i]["date"])
            add(txt(PAD + i * slot + slot / 2.0, base_y + 16, str(d.month) + "/" + str(d.day),
                    9, pal["faint"], anchor="middle"))

    # ---- 四列
    y_cl, y_cv = 544, 570
    parts = [
        ("输入", tot["input_tokens"], pal["split"][0]),
        ("缓存读", tot["cached_input_tokens"], pal["split"][1]),
        ("输出", tot["output_tokens"], pal["split"][2]),
        ("缓存写", tot["cache_creation_input_tokens"], pal["split"][3]),
    ]
    for i, (label, val, col) in enumerate(parts):
        x = PAD + i * 222
        add(rect(x, y_cl - 8, 6, 6, col, rx=1.5))
        add(txt(x + 12, y_cl, label, 10.5, pal["muted"]))
        vs = fmt_tokens(val)
        add(txt(x + 12, y_cv, vs, 15, pal["text"], weight="600"))
        add(txt(x + 12 + text_width(vs, 15) + 8, y_cv, pct(val, tot["total_tokens"]), 10.5, pal["faint"]))

    # ---- 按工具
    srcs = data["by_source"]
    total_tok = sum(s["tokens"] for s in srcs) or 1
    bar_w = RIGHT - PAD
    y_t = 600
    add(txt(PAD, y_t, "按工具", 11, pal["muted"], ls="1.4"))
    add(txt(RIGHT, y_t, "共 " + str(len(srcs)) + " 个来源", 10, pal["faint"], anchor="end"))
    bar_y = y_t + 10
    segs = [(pretty_source(s["source"]), s["tokens"], PALETTE_TOOLS[i]) for i, s in enumerate(srcs[:5])]
    if len(srcs) > 5:
        segs.append(("其他", sum(s["tokens"] for s in srcs[5:]), PALETTE_TOOLS[5]))
    add(rect(PAD, bar_y, bar_w, 10, pal["track"], rx=3))
    cx = PAD
    for label, val, col in segs:
        w = max(2.0, val / total_tok * bar_w)
        if cx + w > RIGHT:
            w = RIGHT - cx
        add(rect(cx, bar_y, w, 10, col))
        cx += w
    y_leg = bar_y + 24
    for row in legend_rows([(l, pct(v, total_tok), c) for l, v, c in segs], bar_w):
        for off, label, value, col in row:
            add(rect(PAD + off, y_leg - 8, 7, 7, col, rx=1.5))
            add(txt(PAD + off + 12, y_leg, label, 10.5, pal["text2"]))
            add(txt(PAD + off + 12 + text_width(label, 10.5) + 6, y_leg, value, 10.5, pal["muted"]))
        y_leg += 18

    # ---- 模型榜
    models = data["by_model"]
    y_m = y_leg + 12
    add(txt(PAD, y_m, "模型榜", 11, pal["muted"], ls="1.4"))
    add(txt(RIGHT, y_m, "共 " + str(len(models)) + " 个模型", 10, pal["faint"], anchor="end"))
    mbar_y = y_m + 10
    segs = [(pretty_model(m["model"]), m["tokens"], PALETTE_MODELS[i]) for i, m in enumerate(models[:7])]
    if len(models) > 7:
        segs.append(("其他", sum(m["tokens"] for m in models[7:]), PALETTE_MODELS[7]))
    add(rect(PAD, mbar_y, bar_w, 10, pal["track"], rx=3))
    cx = PAD
    for label, val, col in segs:
        w = max(2.0, val / total_tok * bar_w)
        if cx + w > RIGHT:
            w = RIGHT - cx
        add(rect(cx, mbar_y, w, 10, col))
        cx += w
    y_leg = mbar_y + 24
    for row in legend_rows([(l, pct(v, total_tok), c) for l, v, c in segs], bar_w):
        for off, label, value, col in row:
            add(rect(PAD + off, y_leg - 8, 7, 7, col, rx=1.5))
            add(txt(PAD + off + 12, y_leg, label, 10.5, pal["text2"]))
            add(txt(PAD + off + 12 + text_width(label, 10.5) + 6, y_leg, value, 10.5, pal["muted"]))
        y_leg += 18

    # ---- 页脚
    foot_y = y_leg + 12
    denom = tot["cached_input_tokens"] + tot["input_tokens"]
    cache_rate = tot["cached_input_tokens"] / denom * 100 if denom else 0
    add(txt(PAD, foot_y, "输入 " + fmt_tokens(tot["input_tokens"]) + " · 输出 "
            + fmt_tokens(tot["output_tokens"]) + " · 缓存 " + "%.0f%%" % cache_rate
            + " · " + fmt_conversations(tot["conversations"]), 10.5, pal["faint"]))
    add(txt(RIGHT, foot_y, "[SYNC] 每日自动刷新 · 更新于 " + today, 10.5, pal["faint"], anchor="end"))

    height = foot_y + 20
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="' + _f(W) + '" height="' + _f(height)
           + '" viewBox="0 0 ' + _f(W) + ' ' + _f(height) + '" role="img" aria-label="AI token usage">',
           "<style>", "".join(css), "</style>",
           rect(CARD_X, 0, CARD_W, height, pal["card"], rx=12),
           '<rect x="' + _f(CARD_X) + '" y="0" width="' + _f(CARD_W) + '" height="' + _f(height)
           + '" rx="12" fill="none" stroke="' + pal["border"] + '" stroke-width="1.5"/>',
           '<g font-family="' + FONT + '">']
    out.extend(body)
    out.append("</g></svg>")
    return "".join(out)


def _zero_data(data: dict) -> bool:
    return not data["totals"]["total_tokens"] and not data["daily"]


# ------------------------------------------------------------ git 同步 / CLI

README_STAMP_RE = r"(token-stats-zh-(?:dark|light)\.svg\?v=)(\d+)"


def _run(args: list, cwd=None) -> tuple:
    """Run a command quietly; return (rc, stdout) with stderr folded into stdout."""
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "").strip()


def _bump_readme_stamp(readme: Path, stamp: str) -> bool:
    """Rewrite the ?v= cache-buster on the token card embeds so camo refetches them."""
    if not readme.exists():
        return False
    text = readme.read_text(encoding="utf-8")
    updated = re.sub(README_STAMP_RE, lambda m: m.group(1) + stamp, text)
    if updated == text:
        return False
    readme.write_text(updated, encoding="utf-8")
    return True


def _unpushed(repo: Path) -> int:
    rc, out = _run(["git", "rev-list", "--count", "@{upstream}..HEAD"], cwd=repo)
    if rc != 0:
        rc2, out2 = _run(["git", "rev-list", "--count", "HEAD", "--not", "--remotes"], cwd=repo)
        return int(out2) if rc2 == 0 and out2.isdigit() else 0
    return int(out) if out.isdigit() else 0


def git_sync(repo: Path, stamp: str, push: bool, paths: list) -> int:
    """Stage only our card files, commit if changed, then push the branch when asked."""
    rc, status = _run(["git", "status", "--porcelain", "--"] + paths, cwd=repo)
    if rc != 0:
        print("[warn] git status failed: " + status, file=sys.stderr)
        return 1
    committed = False
    if status:
        rc, out = _run(["git", "add", "--"] + paths, cwd=repo)
        if rc != 0:
            print("[error] git add failed: " + out, file=sys.stderr)
            return 1
        rc, out = _run(["git", "commit", "-m", "chore: refresh token stats [" + stamp + "]", "--"] + paths, cwd=repo)
        if rc != 0:
            _run(["git", "reset", "-q", "HEAD", "--"] + paths, cwd=repo)
            print("[error] git commit failed: " + out, file=sys.stderr)
            return 1
        committed = True
        print("[info] committed token card refresh")
    else:
        print("[info] token cards unchanged")
    if not push:
        return 0
    if not committed and _unpushed(repo) == 0:
        print("[info] nothing to push")
        return 0
    for attempt in range(1, 4):
        rc, out = _run(["git", "push", "origin", "HEAD"], cwd=repo)
        if rc == 0:
            print("[info] pushed on attempt " + str(attempt))
            return 0
        print("[warn] push attempt " + str(attempt) + " failed: " + out[-300:], file=sys.stderr)
        _run(["git", "pull", "--rebase", "--autostash", "origin", "HEAD"], cwd=repo)
    print("[error] push failed after 3 attempts", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    curated = _load_snapshot()
    litellm = _load_litellm()
    if len(argv) > 2 and argv[1] == "--verify":
        return 1 if verify_row_costs(Path(argv[2]), curated, litellm) else 0
    rows, counts = load_rows()
    data = aggregate(rows, curated, litellm)
    data["today"] = date.today().isoformat()
    if "--json" in argv:
        print(json.dumps({"counts": counts, **data}, ensure_ascii=False, indent=2))
        return 0
    if _zero_data(data):
        print("refuse: no token data", file=sys.stderr)
        return 1
    out_dir = OUT_DIR
    if "--out" in argv:
        out_dir = Path(argv[argv.index("--out") + 1]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for theme in ("dark", "light"):
        svg = svg_card(data, theme, stamp=data["today"])
        path = out_dir / ("token-stats-zh-" + theme + ".svg")
        tmp = path.with_suffix(".svg.tmp")
        tmp.write_text(svg, encoding="utf-8")
        tmp.replace(path)
        written.append(path)
        print("wrote", path, len(svg), "bytes")
    if "--no-stamp" in argv:
        return 0
    repo = Path(argv[argv.index("--repo") + 1]).resolve() if "--repo" in argv else ROOT
    stamp = str(int(time.time()))
    changed = _bump_readme_stamp(repo / "README.md", stamp)
    print("[info] readme stamp", "bumped" if changed else "unchanged")
    rel = [str(x.relative_to(repo)).replace("\\", "/") for x in written] + ["README.md"]
    if "--commit" in argv or "--push" in argv:
        return git_sync(repo, stamp, push="--push" in argv, paths=rel)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
