#!/usr/bin/env python3
"""生成 GitHub 主页用的 Token 统计卡（终端窗 + 琥珀热力图 + 蓝色贪吃蛇）。

基线：Acfufu/Acfufu 的 token-stats.py（同款视觉语言、热力图与蛇的 CSS 动画机制），
按本机环境在其基础上改造：
  - Windows 可用：去掉 fcntl 守护与 flock 单例锁，改由 Windows 计划任务每日驱动 --once；
    刷新互斥改用 msvcrt 文件锁（非 Windows 平台回退 fcntl）。
  - 定价：本机 TokenTracker 官方 curated 价表（scripts/token_card/pricing-snapshot.json，MIT）
    + 本机 LiteLLM 缓存，复刻 TokenTracker matcher 解析顺序与 DeepSeek 分时半价口径。
  - 输出：assets/token-stats-zh-{dark,light}.svg（中文双主题），提交并推送本仓库。

数据源：~/.tokentracker/tracker/queue.jsonl（按 (source, model, hour_start) last-wins 去重）。

用法：
    python scripts/token-stats.py                  # 生成 + 提交 + 推送
    python scripts/token-stats.py --once           # 同上（计划任务入口）
    python scripts/token-stats.py --no-sync        # 只生成 SVG
    python scripts/token-stats.py --json           # 打印聚合结果
    python scripts/token-stats.py --verify <row-costs.jsonl>   # 逐行成本断言
"""

import argparse
import datetime
import html
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import deque

# ============ 配置 ============
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
ASSETS_DIR = os.path.join(REPO_DIR, "assets")
OUT_FILES = (("token-stats-zh-dark.svg", True), ("token-stats-zh-light.svg", False))
PRICING_SNAPSHOT = os.path.join(SCRIPT_DIR, "token_card", "pricing-snapshot.json")
PRICING_CACHE = os.path.expanduser("~/.tokentracker/cache/pricing.json")   # LiteLLM 定价缓存
TRACKER_QUEUE = os.path.expanduser("~/.tokentracker/tracker/queue.jsonl")  # TokenTracker 记录
README_PATH = os.path.join(REPO_DIR, "README.md")
LOCK_PATH = os.path.join(REPO_DIR, ".token-stats-refresh.lock")

# ============ 定价（TokenTracker 官方价表口径） ============
# curated 快照里 exact 条目存高峰价，off-peak 由本模块按 hour_start 各乘 0.5。
ZERO_RATES = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
DEEPSEEK_TIME_PRICED = ("deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-v4.1-flash",
                        "deepseek-flash", "deepseek-v4-pro")
DEEPSEEK_WEEKEND_FROM = datetime.datetime(2026, 8, 22, 16, 0, 0, tzinfo=datetime.timezone.utc)
LOCAL_INFERENCE_SOURCES = {"lmstudio"}
PI_SUBSCRIPTION_SOURCES = {"pi-github-copilot", "pi-copilot",
                           "prime-agent-github-copilot", "prime-agent-copilot"}
AUTHORITATIVE_COST_SOURCES = {"grok", "cline"}
REASONING_FOLD_SOURCES = {"codex", "acode", "every-code", "omo", "cline", "muse"}

_SKU_MARKER = re.compile(r"(?:^|-)(?:preview|next|exp|experimental|beta|alpha)(?:-|$)")
_SUFFIX_STRIP = [re.compile(p) for p in (
    r"-xhigh-fast$", r"-high-fast$", r"-medium-fast$", r"-low-fast$",
    r"-xhigh$", r"-high$", r"-medium$", r"-low$", r"-fast$",
)]
_TABLES = None


def _round10(x):
    return math.floor(x * 1e10 + 0.5) / 1e10 if x >= 0 else -math.floor(-x * 1e10 + 0.5) / 1e10


def load_pricing_tables():
    """(curated 快照, LiteLLM 每百万 token 价表)；缺文件时降级并告警，不中断出卡。"""
    global _TABLES
    if _TABLES is not None:
        return _TABLES
    try:
        with open(PRICING_SNAPSHOT, encoding="utf-8") as f:
            curated = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[warn] 定价快照不可用（{e}），全部按未定价计", file=sys.stderr)
        curated = {"exact": {}, "alias": {}, "source_exact": {}, "fuzzy": []}
    litellm = {}
    try:
        with open(PRICING_CACHE, encoding="utf-8") as f:
            raw = json.load(f)
        for name, entry in raw.items():
            if name.startswith("_") or not isinstance(entry, dict):
                continue
            conv = {}
            for src_key, dst in (("input_cost_per_token", "input"),
                                 ("output_cost_per_token", "output"),
                                 ("cache_read_input_token_cost", "cache_read"),
                                 ("cache_creation_input_token_cost", "cache_write")):
                v = entry.get(src_key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    conv[dst] = _round10(v * 1000000)
            if conv:
                litellm[name] = conv
    except (OSError, ValueError) as e:
        print(f"[warn] LiteLLM 缓存不可用（{e}），仅用 curated 价表", file=sys.stderr)
    _TABLES = (curated, litellm)
    return _TABLES


def _exact_ci(table, model):
    if not table or not model:
        return None
    if model in table:
        return table[model]
    low = model.lower()
    for key, val in table.items():
        if key.lower() == low:
            return val
    return None


def _contained_ci(table, model):
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


def _dot_restored(model):
    low = model.lower()
    restored = re.sub(r"(\d+)-(\d+)", r"\1.\2", low)
    return "" if restored == low else restored


def lookup_pricing(model, source, curated, litellm):
    """复刻 TokenTracker matcher 的解析顺序，返回 (rates, source_tag)。"""
    if not model:
        return None, "empty"
    low = model.lower()
    dot = _dot_restored(model)
    src = (source or "").lower()

    if src == "workbuddy-ai" and low.strip() == "auto":
        return None, "miss"
    if src == "cline" and low.endswith(":free"):
        return dict(ZERO_RATES), "curated:cline-free-suffix"
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
        return dict(ZERO_RATES), "curated:opencode-free-suffix" if src == "opencode" else "curated:free-suffix"

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


def _is_time_priced(model):
    low = (model or "").lower()
    return any(name in low for name in DEEPSEEK_TIME_PRICED)


def _parse_hour_start(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _off_peak(row):
    """显式 pricing_tier 优先 → 北京周末（2026-08-22 16:00Z 起）→ UTC 高峰 01-04、06-10 时。"""
    tier = row.get("pricing_tier")
    if tier == "off_peak":
        return True
    if tier == "peak":
        return False
    dt = _parse_hour_start(row.get("hour_start", ""))
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    ts = dt.astimezone(datetime.timezone.utc)
    if ts >= DEEPSEEK_WEEKEND_FROM:
        beijing_day = (ts + datetime.timedelta(hours=8)).weekday()  # 周一=0
        if (beijing_day + 1) % 7 in (0, 6):                          # 转成 JS getUTCDay 口径：周日=0
            return True
    return not (1 <= ts.hour < 4 or 6 <= ts.hour < 10)


def _norm_rates(rates):
    if not rates:
        return dict(ZERO_RATES)
    return {k: float(rates.get(k) or 0) for k in ZERO_RATES}


def row_pricing(row, curated, litellm):
    rates, _tag = lookup_pricing(row.get("model") or "", row.get("source") or "", curated, litellm)
    rates = _norm_rates(rates)
    if (row.get("source") or "").lower() == "acode":
        return rates
    if not _is_time_priced(row.get("model") or "") or not _off_peak(row):
        return rates
    return {k: (rates.get(k) or 0) * 0.5 for k in ZERO_RATES}


def compute_row_cost(row, curated, litellm):
    """单行成本（USD）：五列计费，绝不用 total_tokens 当分母。"""
    src = (row.get("source") or "").lower()
    if src in LOCAL_INFERENCE_SOURCES or src in PI_SUBSCRIPTION_SOURCES:
        return 0.0
    reported = row.get("total_cost_usd")
    if src in AUTHORITATIVE_COST_SOURCES and isinstance(reported, (int, float)) and reported > 0:
        return float(reported)
    rates = row_pricing(row, curated, litellm)
    folded = (row.get("source") or "") in REASONING_FOLD_SOURCES
    reasoning_cost = 0.0 if folded else _num(row.get("reasoning_output_tokens")) * rates["output"]
    return (
        _num(row.get("input_tokens")) * rates["input"]
        + _num(row.get("output_tokens")) * rates["output"]
        + _num(row.get("cached_input_tokens")) * rates["cache_read"]
        + _num(row.get("cache_creation_input_tokens")) * rates["cache_write"]
        + reasoning_cost
    ) / 1000000

# ============ 数据获取 ============
def _num(v):
    """数据入口数值防御：JSONL 行字段类型不可信（str/bool/null/NaN/Inf 一律按 0 计）。"""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0


def model_canon(model):
    """原始模型名 → 规范名：剥 provider 路径前缀与 openclaw 前缀（不改大小写）。"""
    m = str(model or "").strip()
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    if m.startswith("zaicoding_"):
        m = m[len("zaicoding_"):]
    elif m.startswith("zai_"):
        m = m[len("zai_"):]
    return m or "unknown"


def load_tracker():
    """主层：读 TokenTracker queue.jsonl（append-only，同 (source|model|hour_start) 取最后一条）。"""
    if not os.path.exists(TRACKER_QUEUE):
        print(f"[warn] 未找到 {TRACKER_QUEUE}", file=sys.stderr)
        return None
    curated, litellm = load_pricing_tables()
    r = {"total_tokens": 0, "input": 0, "output": 0, "cached": 0, "cc": 0,
         "conversations": 0, "cost": 0.0, "by_agent": {}, "by_model": {},
         "by_model_cost": {}, "period_start": None, "peak_day": None,
         "peak_day_tokens": 0, "by_day": {}, "agent_models": {},
         "unpriced": {}}
    dedup = {}
    with open(TRACKER_QUEUE, encoding="utf-8") as f:
        for line in f:
            try:
                b = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(b, dict):
                continue                  # null/[1,2]/"str" 等结构坏行：先拦下
            key = (str(b.get("source") or ""), str(b.get("model") or ""),
                   str(b.get("hour_start") or ""))
            dedup[key] = b                # last-wins（sync 重发累计值，只留最新）
    peak = {}
    pdt = None
    for b in dedup.values():
        src = str(b.get("source") or "other")
        mdl = model_canon(b.get("model"))
        inp = _num(b.get("input_tokens"))
        cached = _num(b.get("cached_input_tokens"))
        cc = _num(b.get("cache_creation_input_tokens"))
        out = _num(b.get("output_tokens"))
        bill = b.get("billable_total_tokens")
        tot = bill if isinstance(bill, (int, float)) and not isinstance(bill, bool) \
            and math.isfinite(bill) else _num(b.get("total_tokens"))
        conv = _num(b.get("conversation_count"))
        inp, cached, cc, out, conv, tot = (max(x, 0) for x in (inp, cached, cc, out, conv, tot))

        r["total_tokens"] += tot
        r["input"] += inp
        r["output"] += out
        r["cached"] += cached
        r["cc"] += cc
        r["conversations"] += conv
        r["by_agent"][src] = r["by_agent"].get(src, 0) + tot
        r["by_model"][mdl] = r["by_model"].get(mdl, 0) + tot
        if tot > 0:
            r["agent_models"].setdefault(src, set()).add(mdl)

        rates, tag = lookup_pricing(mdl, src, curated, litellm)
        if rates is None or not any((rates.get(k) or 0) > 0 for k in ZERO_RATES):
            u = r["unpriced"].setdefault(mdl, {"tokens": 0, "status": "unpriced" if rates is None else "free"})
            u["tokens"] += tot
        cost = compute_row_cost(b, curated, litellm)
        r["cost"] += cost
        r["by_model_cost"][mdl] = r["by_model_cost"].get(mdl, 0.0) + cost

        hs = b.get("hour_start")
        if isinstance(hs, str) and hs:
            ldt = None
            try:
                ldt = datetime.datetime.fromisoformat(hs.replace("Z", "+00:00")).astimezone()
                day = ldt.date().isoformat()
                liso = ldt.isoformat(timespec="minutes")
            except (ValueError, OverflowError, OSError):
                day, liso = hs[:10], hs
            peak[day] = peak.get(day, 0) + tot
            if ldt is not None and (pdt is None or ldt < pdt):
                pdt = ldt
                r["period_start"] = liso
    if peak:
        day, toks = max(peak.items(), key=lambda x: x[1])
        r["peak_day"], r["peak_day_tokens"] = day, toks
    r["by_day"] = peak
    r["agent_models"] = {k.lower(): len(v) for k, v in r["agent_models"].items()}
    return r

# ============ 聚合 ============
def _agent_disp(name):
    """agent 源名 → 展示名（本机常见源给固定写法，其余按 capitalize 兜底）。"""
    fixed = {"dsh": "DSH", "codex": "Codex", "zcode": "ZCode", "omp": "OMP",
             "opencode": "OpenCode", "claude": "Claude", "gemini": "Gemini"}
    return fixed.get(name.lower(), name.capitalize())


def aggregate():
    result = {
        "total_tokens": 0, "total_cost": 0.0, "conversations": 0,
        "by_agent": {}, "by_model": {}, "models": [], "period": "",
        "period_start": "", "top_model": "", "peak_day": "", "peak_day_tokens": 0,
        "daily": [], "agent_models": {}, "unpriced": {},
        "stats": {"input": 0, "output": 0, "cached": 0, "cc": 0},
    }

    tr = load_tracker()
    if tr:
        result["total_tokens"] = tr["total_tokens"]
        result["total_cost"] = tr["cost"]
        result["conversations"] = tr["conversations"]
        result["by_agent"] = tr["by_agent"]
        result["by_model"] = tr["by_model"]
        result["agent_models"] = tr.get("agent_models") or {}
        result["period_start"] = (tr["period_start"] or "")[:7]
        result["peak_day"] = tr["peak_day"]
        result["peak_day_tokens"] = tr["peak_day_tokens"]
        result["stats"] = {k: tr[k] for k in ("input", "output", "cached", "cc")}
        result["daily"] = sorted((tr.get("by_day") or {}).items())
        result["unpriced"] = tr.get("unpriced") or {}
        if tr["period_start"]:
            result["period"] = f"{tr['period_start'][:10]} ~ {datetime.date.today().isoformat()}"

    # 模型榜（官方命名 + 排序；0-token 模型不占槽位）
    merged = {}
    for mn, tok in result["by_model"].items():
        name = MODEL_NAMES.get(mn, mn)
        merged[name] = merged.get(name, 0) + tok
    result["models"] = sorted(((n, t) for n, t in merged.items() if t > 0),
                              key=lambda x: -x[1])[:10]
    if result["models"]:
        result["top_model"] = result["models"][0][0]

    # by agent 排序 + 展示名归一
    agents = {}
    for name, tok in result["by_agent"].items():
        disp = _agent_disp(name)
        agents[disp] = agents.get(disp, 0) + tok
    result["by_agent"] = dict(sorted(agents.items(), key=lambda x: -x[1]))
    return result


MODEL_NAMES = {
    "gpt-5.6-sol": "GPT-5.6 Sol", "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.6-terra": "GPT-5.6 Terra", "gpt-5.6": "GPT-5.6",
    "gpt-5.5": "GPT-5.5", "gpt-5.4": "GPT-5.4", "gpt-5.4-mini": "GPT-5.4 Mini",
    "gpt-5.3-codex-spark": "GPT-5.3 Codex Spark", "gpt-5": "GPT-5",
    "codex-auto-review": "Codex Auto Review",
    "deepseek-v4-flash": "DeepSeek V4 Flash", "deepseek-v4-pro": "DeepSeek V4 Pro",
    "MiniMax-M2.7": "MiniMax M2.7", "minimax-m2.7": "MiniMax M2.7",
    "minimax-m3": "MiniMax M3", "MiniMax-M3": "MiniMax M3",
    "claude-opus-4-7": "Claude Opus 4.7", "claude-opus-4-6": "Claude Opus 4.6",
    "claude-opus-4-5": "Claude Opus 4.5", "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-sonnet-4-5": "Claude Sonnet 4.5",
    "glm-5.2": "GLM-5.2", "GLM-5.2": "GLM-5.2", "glm-5.1": "GLM-5.1",
    "glm-5.2-x": "GLM-5.2 X", "GLM-5-Turbo": "GLM-5 Turbo",
    "glm-5.3": "GLM-5.3", "GLM-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3 Flash", "GLM-5.3-Flash": "GLM-5.3 Flash",
    "glm-5.3-flashx": "GLM-5.3 FlashX", "GLM-5.3-FlashX": "GLM-5.3 FlashX",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "kimi-k3": "Kimi K3",
    "gpt-6-astra": "GPT-6 Astra", "gpt-6-luna": "GPT-6 Luna",
    "gpt-6.1-sol": "GPT-6.1 Sol", "gpt-6.1-terra": "GPT-6.1 Terra",
    "claude-opus-5.5": "Claude Opus 5.5",
    "hy4-preview-f": "Hunyuan Hy4 Preview",
    "kimi-k2.6": "Kimi K2.6", "mimo-v2.5": "MiMo V2.5",
    "gemini-3-flash-preview": "Gemini 3 Flash", "hy3": "Hunyuan Hy3",
    "qwen3.7-plus": "Qwen3.7 Plus",
    "deepseek-v4.1-flash": "DeepSeek V4.1 Flash", "deepseek-v4.1-flash-fast": "DeepSeek V4.1 Flash Fast",
    "deepseek-v4.1-flash-free": "DeepSeek V4.1 Flash Free", "deepseek-v4-flash-fast": "DeepSeek V4 Flash Fast",
    "never-stuck": "NeverStuck", "qwen3.8-flash": "Qwen3.8 Flash",
    "gemini-3.8-flash": "Gemini 3.8 Flash", "dpcc/gpt-6.1-sol": "GPT-6.1 Sol",
    "deepseek-flash": "DeepSeek Flash", "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
    "deepseek-v4-flash": "DeepSeek V4 Flash", "gpt-6-sol": "GPT-6 Sol",
    "deepseek-v4.1-flash-fast": "DeepSeek V4.1 Flash Fast",
    "unknown": "Unknown",
}

AGENT_COLORS = {
    "codex": "#6c63ff", "claude": "#51cf66", "opencode": "#22b8cf",
    "gemini": "#4285F4", "zcode": "#e64980", "other": "#8b949e",
    "workbuddy": "#e3b341",   # 原灰色兜底与 Other 相撞，堆叠条新增金色
    "dsh": "#7aa2f7", "omp": "#22b8cf",
}

# 压缩单条（BY TOOL / TOP MODELS，2026-11-02 grill 定稿）
MODEL_BAR_COLORS = ["#58a6ff", "#51cf66", "#e64980", "#22b8cf", "#a371f7", "#e3b341"]
CW_MAX = 888   # 卡内绘图宽 X1-X0


def _pct1(p):
    """图例口径：一位小数，整数省小数（49%、37.2%）。"""
    return f"{p:.1f}%".replace(".0%", "%")


def _pct2(p):
    """悬停口径：TokenTracker 同款两位小数，<0.01% 兜底。"""
    return "<0.01%" if 0 < p < 0.01 else f"{p:.2f}%"


def _bar_layout(r, zh, ys, acc):
    """压缩双条预计算：分段+悬停文案+图例行数+纵向编排。
    工具 Top5+Other（悬停带模型数，Other 展开前 5 明细）；
    模型 Top7+Other（Other 分母/明细取 merged 全榜，不受 models[:10] 影响）；
    分母统一=总 token；图例按 Plex 7.2px/字符贪心换行。
    纵向编排（2026-11-02 二次调整）：活动区 → 趋势区 → 双条区 → 页脚。"""
    total = r["total_tokens"]
    agents = sorted(((n, t) for n, t in (r["by_agent"] or {}).items() if t > 0),
                    key=lambda x: -x[1])
    merged = {}
    for mn, tok in (r["by_model"] or {}).items():
        nm = MODEL_NAMES.get(mn, mn)
        if tok > 0:
            merged[nm] = merged.get(nm, 0) + tok
    models = sorted(merged.items(), key=lambda x: -x[1])
    n_models = len(models)
    agent_models = r.get("agent_models") or {}
    more = "更多" if zh else "more"

    def _tip_main(name, tok, k=None):
        p = tok / total * 100 if total else 0
        s = f"{name} · {fmt_tokens(tok)} ({_pct2(p)})"
        if k is not None:
            s += f" · {k} 个模型" if zh else f" · {k} models"
        return s

    def _tip_other(rest, disp_fn):
        tt = sum(t for _, t in rest)
        p = tt / total * 100 if total else 0
        parts = [f"{disp_fn(n)} {_pct2(t / total * 100 if total else 0)}"
                 for n, t in rest[:5]]
        if len(rest) > 5:
            parts.append(f"+{len(rest) - 5} {more}")
        return f"Other · {fmt_tokens(tt)} ({_pct2(p)}) — " + " · ".join(parts)

    t_shown, t_rest = agents[:5], agents[5:]
    tool_segs = [(_agent_disp(n), t, AGENT_COLORS.get(n.lower(), "#8b949e"),
                  _tip_main(_agent_disp(n), t, agent_models.get(n.lower(), 0)))
                 for n, t in t_shown]
    tool_segs.append(("Other", total - sum(t for _, t in t_shown), "#8b949e",
                      _tip_other(t_rest, _agent_disp)))
    m_shown, m_rest = models[:7], models[7:]
    model_segs = []
    for i, (nm, t) in enumerate(m_shown):
        model_segs.append((nm, t, acc if i == 0 else MODEL_BAR_COLORS[i - 1],
                           _tip_main(nm, t)))
    model_segs.append(("Other", total - sum(t for _, t in m_shown), "#8b949e",
                       _tip_other(m_rest, lambda n: n)))

    def _rows(segs):
        line, n_rows, end_x = [], 1, 0
        for name, tok, _, _ in segs:
            s = _pct1(tok / total * 100 if total else 0)
            iw = 12 + len(name) * 7.2 + 7.2 + len(s) * 7.2
            px = end_x + 34 if line else 0
            if line and px + iw > CW_MAX:
                n_rows += 1
                line, px = [], 0
            line.append(px)
            end_x = px + iw
        return n_rows

    rows_tool, rows_model = _rows(tool_segs), _rows(model_segs)
    yo = (ys + 279) + 24 - 496      # lg_y = ys+279；趋势分隔线 = lg_y+24，内部 -496+yo
    bt = 694 + yo + 13              # 双条标题 = 趋势末(yv+4 = 694+yo) + 13
    leg2_end = bt + 112 + 20 * (rows_tool + rows_model - 2)
    foot_div = leg2_end + 12
    tt = fmt_tokens(total)
    cap = f"共 {tt} · {n_models} 个模型" if zh else f"{tt} total · {n_models} models"
    return dict(tool_segs=tool_segs, model_segs=model_segs, rows_tool=rows_tool,
                rows_model=rows_model, cap=cap, yo=yo, bt=bt,
                foot_div=foot_div, foot_text=foot_div + 28, h=foot_div + 48)

MONO = ("'Plex',ui-monospace,'SF Mono',Menlo,Consolas,'PingFang SC',"
        "'Hiragino Sans GB','Microsoft YaHei',monospace")


def _font_face_css():
    parts = []
    for weight, name in (("normal", "reg"), ("bold", "bold")):
        p = os.path.join(REPO_DIR, "assets", "gen-cache", f"plex-{name}.b64")
        try:
            with open(p) as f:
                b64 = f.read().strip()
        except OSError:
            continue
        parts.append(f"@font-face{{font-family:'Plex';font-style:normal;font-weight:{weight};"
                     f"src:url(data:font/truetype;base64,{b64}) format('truetype');}}")
    return "".join(parts)


def fmt_tokens(n):
    if n >= 999.5e6: return f"{n/1e9:.1f}B"   # 999999999 进位为 1.0B，不显示 "1000.0M"
    if n >= 999_500: return f"{n/1e6:.1f}M"   # 999999 进位为 1.0M，不显示 "1000K"
    if n >= 999.5: return f"{n/1e3:.0f}K"
    return str(n)


def esc(s):
    # stdlib 等价：html.escape(quote=False) 只转义 & < >，引号原样
    return html.escape(s, quote=False)


def _nice_max(v):
    """向上取整到 0.5B 粒度（4.14B → 4.5B），保底 1B"""
    step = 0.5e9
    return max(1e9, math.ceil(v / step) * step)


def _parse_daily(r):
    """r['daily'] [(ISO日期, 数值)] → {date: 数值}；坏日期跳过（外部数据边界）"""
    daily = {}
    for k, v in (r.get("daily") or []):
        try:
            daily[datetime.date.fromisoformat(k)] = v
        except ValueError:
            pass
    return daily


def trend_section(r, PAL, L, x0, x1, yo=0):
    """USAGE TREND 区块：最近 30 天日柱 + 7日滚动均线 + 峰值日标注 + 图例
    + 输入/缓存/输出/写缓存拆分明细行。明细为全时段，图表仅窗口。
    x0/x1 为绘图横轴；yo 为终端卡 titlebar 引入的纵向偏移。"""
    out = []
    X0, X1 = x0, x1
    CW = X1 - X0
    today = datetime.date.today()

    daily = _parse_daily(r)

    # 窗口收缩：仅最近 30 天（chart 范围；拆分明细行仍为全时段）
    lo = today - datetime.timedelta(days=29)
    daily = {d: v for d, v in daily.items() if lo <= d <= today}
    start = lo
    span_days = max((today - start).days, 1)

    days_sorted = sorted(daily)
    # 7日滚动均值：活跃日窗口（当前活跃日 + 前6个活跃日）
    roll = {}
    for i, d in enumerate(days_sorted):
        win = days_sorted[max(0, i - 6): i + 1]
        roll[d] = sum(daily[x] for x in win) / len(win)

    vmax = _nice_max(max(daily.values()) if daily else 0)
    plot_top, plot_bot = 528 + yo, 628 + yo
    PH = plot_bot - plot_top

    def px(d):
        return X0 + (d - start).days / span_days * CW

    def py(v):
        return plot_bot - (v / vmax) * PH

    out.append(f'<line x1="{X0}" y1="{496 + yo}" x2="{X1}" y2="{496 + yo}" stroke="{PAL["track"]}" stroke-width="1"/>')
    out.append(f'<text x="{X0}" y="{514 + yo}" font-size="11" font-weight="600" class="muted" letter-spacing="2">{L["trend"]}</text>')

    # 图例（右上，与标题同行）：色块=每日用量，线段=7日均值，圆点=峰值日
    leg_items = [(L["lg_bar"], "bar"), (L["lg_avg"], "line"), (L["lg_peak"], "dot")]
    lx = X1
    for lab, kind in reversed(leg_items):
        lw = len(lab) * 6.3 + 24
        lx -= lw
        if kind == "bar":
            out.append(f'<rect x="{lx}" y="{508 + yo}" width="10" height="10" rx="1" fill="{PAL["acc"]}"/>')
            tx = lx + 14
        elif kind == "line":
            out.append(f'<line x1="{lx}" y1="{513 + yo}" x2="{lx + 18}" y2="{513 + yo}" stroke="{PAL["acc_hi"]}" stroke-width="2" stroke-linecap="round"/>')
            tx = lx + 22
        else:
            out.append(f'<circle cx="{lx + 5}" cy="{513 + yo}" r="5" fill="{PAL["acc_hi"]}" stroke="{PAL["bg"]}" stroke-width="1.2"/>')
            tx = lx + 14
        out.append(f'<text x="{tx}" y="{514 + yo}" font-size="10" font-weight="500" class="muted">{lab}</text>')
        lx -= 12

    # Y 网格线（3 级 faint + 基线）
    for frac in (0.25, 0.5, 0.75):
        y = plot_bot - PH * frac
        out.append(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="{PAL["faint"]}" stroke-width="1" opacity="0.35"/>')
    out.append(f'<line x1="{X0}" y1="{plot_bot}" x2="{X1}" y2="{plot_bot}" stroke="{PAL["track"]}" stroke-width="1"/>')

    # 日柱（纵向渐变，零日跳过留空隙；峰值日 acc_hi 高亮）
    acc = PAL["acc"]
    grad_id = "tg" + ("d" if PAL.get("dark") else "l")
    out.append(f'<defs><linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">'
               f'<stop offset="0" stop-color="{acc}" stop-opacity="0.45"/>'
               f'<stop offset="1" stop-color="{acc}" stop-opacity="0.06"/></linearGradient></defs>')
    bw = CW / span_days * 0.72
    pk_day = max(daily, key=daily.get) if daily else None
    for d in days_sorted:
        v = daily[d]
        x = max(px(d) - bw / 2, X0)
        w = min(bw, X1 - x)
        y = py(v)
        fill = PAL["acc_hi"] if d == pk_day else f"url(#{grad_id})"
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{plot_bot - y:.1f}" rx="1" fill="{fill}"/>')

    # 叠加折线：7日滚动均线（平滑稀疏日尖峰），acc_hi 2px 圆角；无活跃日不输出空 points
    if days_sorted:
        rpts = " ".join(f"{px(d):.1f},{py(roll[d]):.1f}" for d in days_sorted)
        out.append(f'<polyline points="{rpts}" fill="none" stroke="{PAL["acc_hi"]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>')

    # 峰值日标注（窗口内重算；圆点 + 日期 + 数值）
    if pk_day:
        xp, yp = px(pk_day), py(daily[pk_day])
        out.append(f'<circle cx="{xp:.1f}" cy="{yp:.1f}" r="4" fill="{PAL["acc_hi"]}" stroke="{PAL["bg"]}" stroke-width="2"/>')
        lbl = f'{pk_day.month}/{pk_day.day} · {fmt_tokens(daily[pk_day])}'
        anchor = "end" if xp > X1 - 90 else "start"
        lx = xp - 8 if anchor == "end" else xp + 8
        out.append(f'<text x="{lx:.1f}" y="{yp - 9:.1f}" font-size="10" font-weight="600" class="text2" text-anchor="{anchor}">{lbl}</text>')

    # X 周刻度（周一起始，M/D 标签；30 天窗口按周稀疏标（逐日标注会碰撞））
    wk0 = start - datetime.timedelta(days=start.weekday())
    last_lx = None
    dd = wk0
    while dd <= today:
        x = px(dd)
        if x >= X0:
            out.append(f'<line x1="{x:.1f}" y1="{plot_bot}" x2="{x:.1f}" y2="{plot_bot + 5}" stroke="{PAL["faint"]}" stroke-width="1" opacity="0.5"/>')
            if x < X1 - 22 and (last_lx is None or x - last_lx >= 34):
                out.append(f'<text x="{x:.1f}" y="{646 + yo}" font-size="9.5" font-weight="500" class="faint" text-anchor="middle">{dd.month}/{dd.day}</text>')
                last_lx = x
        dd += datetime.timedelta(days=7)

    # ccswitch 风格拆分明细行：输入/缓存/输出/写缓存（彩色圆点 + 数值 + 占比）
    st = r.get("stats", {})
    parts = [st.get("input", 0), st.get("cached", 0), st.get("output", 0), st.get("cc", 0)]
    stot = sum(parts)
    names = [L["c_in"], L["c_cached"], L["c_out"], L["c_cc"]]
    cell_w = CW / 4
    yl, yv = 668 + yo, 690 + yo
    for i, (v, nm) in enumerate(zip(parts, names)):
        share = v / stot * 100 if stot else 0
        x = X0 + cell_w * i + 16
        out.append(f'<circle cx="{x + 6}" cy="{yl - 5}" r="3.5" fill="{PAL["split"][i]}"/>')
        out.append(f'<text x="{x + 16}" y="{yl}" font-size="9.5" font-weight="600" class="faint" letter-spacing="1.5">{nm}</text>')
        out.append(f'<text x="{x + 16}" y="{yv}" font-size="15" font-weight="700" class="text num">{fmt_tokens(v)}'
                   f'<tspan font-size="11" font-weight="500" class="muted"> · {share:.1f}%</tspan></text>')
        if i < 3:
            out.append(f'<line x1="{X0 + cell_w * (i + 1):.1f}" y1="{yl - 6}" x2="{X0 + cell_w * (i + 1):.1f}" y2="{yv + 4}" stroke="{PAL["track"]}" stroke-width="1"/>')
    return out


# ============ 贪吃蛇路径规划 ============
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
                # 踩尾原步：先弹尾再进格，保持 occupied 与 body 同步
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
            # 长身后尾巴必须仍可达，否则这步会把路走死
            if len(body) < 8:
                return True
            new_occ = set(occupied) | {cr}
            new_tail = body[0]
            if cr not in remaining:  # 只有吃格子才净长一节
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
            # 被围：追尾巴一步步退，尾巴挪开就腾出空间。
            # 踩尾格合法（尾巴同拍挪走，尾格必非食物、不会长身），死角全靠它脱困。
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
                if pool:  # 哪边空间大往哪边挪
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


def svg_card(r, dark=True, zh=False, live=None):
    """生成 token 卡片 SVG：终端卡外壳（同画像卡视觉语言）+ 英雄头部 + 活跃热力图
    + 双栏条形图（工具/模型）+ 使用趋势 + 页脚（含 daemon LIVE 叙事）。

    live=None 时按 PID 文件锁探测守护是否真在跑；页脚只如实标注，
    不再硬编码 [LIVE]（守护死后卡片谎报在跑正是 12 天停更不被察觉的原因）。"""
    if live is None:
        live = daily_task_running()
    W, H = 960, 792
    CARD_X, CARD_W, R, TB_H = 4, 952, 12, 38
    PAD = 32
    X0, X1 = CARD_X + PAD, CARD_X + CARD_W - PAD
    CW = X1 - X0
    YS = TB_H  # 内容整体下移一个 titlebar 高度
    now = datetime.date.today().isoformat()

    # 数据驱动的迷你统计（真实缓存拆分 / 峰值 / 对话数，缺数据时回退旧值）
    st = r.get("stats", {})
    inp, out = st.get("input", 0), st.get("output", 0)
    cached, cc = st.get("cached", 0), st.get("cc", 0)
    denom = inp + out + cached + cc
    cpct = cached / denom * 100 if denom else 0.0   # 无分量数据显 0%，不伪造 "cached 95%"
    conv_k = r.get("conversations", 0) / 1000
    _up = sum((v or {}).get("tokens", 0) for v in (r.get("unpriced") or {}).values())
    up_pct = _up / r["total_tokens"] * 100 if r.get("total_tokens") else 0.0
    start = r.get("period_start") or "2026-03"
    try:
        d = datetime.date.fromisoformat(start + "-01")
        start_disp = f"{d.strftime('%b %Y')}"
        start_disp_cn = f"{d.year} 年 {d.month} 月"
    except ValueError:
        start_disp, start_disp_cn = start, start

    # 画像卡同族调色：暗色 #0a0e14 卡体 + #22406a 蓝灰描边，琥珀 accent 保留
    if dark:
        PAL = dict(bg="#0a0e14", card="#0a0e14", titlebar="#111826", border="#22406a",
                   track="#161b22", text="#e6edf3", text2="#c9d1d9", muted="#6e7f95",
                   faint="#57606a", user="#58a6ff", bios="#79c0ff", liv="#f0d861",
                   acc="#f59e0b", acc_hi="#fbbf24", dark=True,
                   snake=("#c9e4ff", "#79c0ff", "#58a6ff"),
                   snake_tail=("#4485d4", "#3a6cb0"),
                   split=["#fbbf24", "#f59e0b", "#fde68a", "#8b949e"])
        HM = ["#161b22", "#4a3a12", "#7d5a17", "#b9821d", "#fbbf24"]   # 琥珀色阶（0-4）
    else:
        PAL = dict(bg="#ffffff", card="#ffffff", titlebar="#f6f8fa", border="#a9c4e4",
                   track="#ebedf0", text="#1f2328", text2="#424a53", muted="#8c9bb0",
                   faint="#8c959f", user="#0969da", bios="#0a5cc2", liv="#9a6700",
                   acc="#b45309", acc_hi="#d97706", dark=False,
                   snake=("#54aeff", "#0a5cc2", "#0969da"),
                   snake_tail=("#3d8ae0", "#79b3ec"),
                   split=["#d97706", "#b45309", "#f59e0b", "#8c959f"])
        HM = ["#ebedf0", "#f5e0b0", "#e8c26a", "#d69b2f", "#b45309"]
    LAY = _bar_layout(r, zh, TB_H, PAL["acc"])
    H = LAY["h"]   # 卡高由区块编排推导：活动区 → 趋势区 → 双条区 → 页脚

    if zh:
        L = dict(aria="AI Token 用量",
                 tb_cap="token 用量 · 实时",
                 hero_lbl="AI TOKEN 使用量", hero_sub=f"{conv_k:.0f}K+ 对话 · 自 {start_disp_cn} 起",
                 cost_lbl="估算成本", cost_sub="TokenTracker 官方价表",
                 heatmap="TOKEN 活跃热力图", less="少", more="多", today="今日",
                 eaten_lbl="$ 吃掉的 token：",
                 by_tool="按工具", top_models="模型榜",
                 trend="使用趋势", c_in="输入", c_cached="缓存读", c_out="输出", c_cc="缓存写",
                 lg_bar="每日用量", lg_avg="7日均值", lg_peak="峰值日",
                 foot_stats=f"输入 {fmt_tokens(inp)} · 输出 {fmt_tokens(out)} · 缓存 {cpct:.0f}% · {conv_k:.0f}K 对话"
                            + (f" · 未定价 {up_pct:.1f}%" if up_pct >= 0.1 else ""),
                 live_note="[LIVE] 计划任务在跑 · 每日刷新" if live else "[SYNC] 手动同步 · 计划任务未注册",
                 foot_updated="更新于", more_tools="其余工具",
                 wd={1: "一", 3: "三", 5: "五"})
        def mon_label(m):
            return f"{m} 月"
    else:
        L = dict(aria="AI token usage",
                 tb_cap="ai token usage · live",
                 hero_lbl="AI TOKEN USAGE", hero_sub=f"{conv_k:.0f}K+ conversations since {start_disp}",
                 cost_lbl="ESTIMATED COST", cost_sub="official pricing · TokenTracker rates",
                 heatmap="TOKEN ACTIVITY", less="Less", more="More", today="TODAY",
                 eaten_lbl="$ tokens eaten:",
                 by_tool="BY TOOL", top_models="TOP MODELS",
                 trend="USAGE TREND", c_in="INPUT", c_cached="CACHED", c_out="OUTPUT", c_cc="CACHE WRITE",
                 lg_bar="Daily", lg_avg="7d avg", lg_peak="Peak",
                 foot_stats=f"input {fmt_tokens(inp)} · output {fmt_tokens(out)} · cached {cpct:.0f}% · {conv_k:.0f}K conversations"
                            + (f" · unpriced {up_pct:.1f}%" if up_pct >= 0.1 else ""),
                 live_note="[LIVE] task on · regen daily" if live else "[SYNC] manual sync · task off",
                 foot_updated="updated", more_tools="more tools",
                 wd={1: "Mon", 3: "Wed", 5: "Fri"})
        def mon_label(m):
            return datetime.date(2024, m, 1).strftime("%b")

    hm_cls = "\n".join(f"    .h{k} {{ fill: {c}; }}" for k, c in enumerate(HM))
    lines = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="{L['aria']}">
  <style>
    {_font_face_css()}
    .text {{ fill: {PAL['text']}; font-family: {MONO}; }}
    .text2 {{ fill: {PAL['text2']}; font-family: {MONO}; }}
    .muted {{ fill: {PAL['muted']}; font-family: {MONO}; }}
    .faint {{ fill: {PAL['faint']}; font-family: {MONO}; }}
    .bios {{ fill: {PAL['bios']}; font-family: {MONO}; }}
    .track {{ fill: {PAL['track']}; }}
    .num {{ font-variant-numeric: tabular-nums; }}
{hm_cls}
  </style>
  <rect x="{CARD_X}" y="0" width="{CARD_W}" height="{H}" rx="{R}" fill="{PAL['card']}" stroke="{PAL['border']}"/>
  <rect x="{CARD_X}" y="0" width="{CARD_W}" height="{TB_H}" rx="{R}" fill="{PAL['titlebar']}" stroke="{PAL['border']}"/>
  <circle cx="{CARD_X + 20}" cy="19" r="5.5" fill="#ff5f57"/>
  <circle cx="{CARD_X + 38}" cy="19" r="5.5" fill="#febc2e"/>
  <circle cx="{CARD_X + 56}" cy="19" r="5.5" fill="#28c840"/>
  <text x="{CARD_X + 82}" y="25.2" font-size="15" font-family="{MONO}"><tspan fill="{PAL['user']}" font-weight="700">rzqhd778@github</tspan><tspan fill="{PAL['muted']}"> ~ % </tspan><tspan fill="{PAL['text']}">tail -f ~/.tokens</tspan></text>
  <text x="{CARD_X + CARD_W - 20}" y="25.2" font-size="15" font-family="{MONO}" fill="{PAL['muted']}" text-anchor="end">{esc(L['tb_cap'])}</text>''']

    def text(x, y, s, size, weight, cls, anchor="start", spacing=None, num=False):
        extra = f' letter-spacing="{spacing}"' if spacing else ""
        if num:
            cls = f"{cls} num"
        lines.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" class="{cls}" text-anchor="{anchor}"{extra}>{esc(s)}</text>')

    # ===== 英雄头部：左大数字 + 右成本（编辑不对称） =====
    text(X0, 46 + YS, L["hero_lbl"], 11, 600, "bios", spacing="2")
    hv = fmt_tokens(r["total_tokens"])
    text(X0, 92 + YS, hv, 42, 700, "text", num=True)
    text(X0 + len(hv) * 25 + 14, 88 + YS, "tokens", 13, 600, "muted")
    text(X0, 114 + YS, L["hero_sub"], 11.5, 400, "muted")
    text(X1, 46 + YS, L["cost_lbl"], 11, 600, "bios", anchor="end", spacing="2")
    _cost = r["total_cost"]
    _cost_txt = f"${_cost:,.0f}" if _cost < 1000 else f"${_cost/1000:.1f}K"
    text(X1, 88 + YS, _cost_txt, 30, 700, "text", anchor="end", num=True)
    text(X1, 114 + YS, L["cost_sub"], 11.5, 400, "muted", anchor="end")

    # ===== TOKEN ACTIVITY 热力图（GitHub contribution 风格，琥珀色阶） =====
    lines.append(f'<line x1="{X0}" y1="{128 + YS}" x2="{X1}" y2="{128 + YS}" stroke="{PAL["track"]}" stroke-width="1"/>')
    text(X0, 146 + YS, L["heatmap"], 11, 600, "muted", spacing="2")

    daily = _parse_daily(r)
    today_d = datetime.date.today()
    start_d = None
    try:
        start_d = datetime.date.fromisoformat((r.get("period_start") or "")[:10])
    except ValueError:
        start_d = None
    if start_d is None:
        start_d = min(daily) if daily else today_d - datetime.timedelta(days=167)

    pos = sorted([v for v in daily.values() if v > 0])
    if pos:
        n = len(pos)
        th = [pos[min(n - 1, int(n * 0.4))], pos[min(n - 1, int(n * 0.7))], pos[min(n - 1, int(n * 0.9))]]

        def lvl(v):
            if v <= 0:
                return 0
            if v > th[2]:
                return 4
            if v > th[1]:
                return 3
            if v > th[0]:
                return 2
            return 1
    else:
        lvl = lambda v: 0

    cell, pitch = 10, 13
    week0 = start_d - datetime.timedelta(days=(start_d.weekday() + 1) % 7)  # 周日开头
    cols = []
    ws = week0
    while ws <= today_d:
        cols.append(ws)
        ws += datetime.timedelta(days=7)
    MAX_COLS = (CW - 72) // pitch  # 列上限：预留右侧 "TODAY 今日计数" 的 72px，防计数器越出卡体
    if len(cols) > MAX_COLS:       # 全历史越限时滚动保留最近窗口（13px/列 ≈ 2027 年触顶）
        cols = cols[-MAX_COLS:]
    ncol = len(cols)
    hm_w = ncol * pitch
    grid_x = X0 + (CW - hm_w) // 2
    hm_y = 170 + YS

    # 月份标签（列对应周一→该月起始列）
    prev_m = None
    for i, ws in enumerate(cols):
        m = ws.month
        label_it = (i == 0 and m != prev_m) or (i > 0 and m != prev_m)
        if label_it and i > 0 and ws < start_d:
            label_it = False
        if label_it:
            x = grid_x + i * pitch
            if x < X1 - 26:
                lines.append(f'<text x="{x}" y="{162 + YS}" font-size="9.5" font-weight="500" class="faint" text-anchor="start">{mon_label(m)}</text>')
        prev_m = m
    for row, lb in L["wd"].items():
        lines.append(f'<text x="{grid_x - 8}" y="{hm_y + row * pitch + 8}" font-size="9" font-weight="500" class="faint" text-anchor="end">{lb}</text>')
    # ===== 贪吃蛇层：觅食式 BFS 寻路 + 吃格子变长，真吃到周期末 =====
    # 蛇像玩家操纵一样逐格狩猎（BFS 绕开蛇身、防自困），每吃一格长一节；
    # 每格按占用模拟生成 keyframes：进格闪头色 → 按入队年龄转暗（头亮尾暗），
    # 蛇尾离开后被吃格子保持空色（track）直到周期末，周期重启热力图复原。
    # prefers-reduced-motion 下整层静止、热力图完整可读。
    NC = 92
    grid_cells = {}  # (col,row) -> (x, y, lev, tokens)，只含真实渲染的格子
    for i, ws in enumerate(cols):
        for row in range(7):
            dd = ws + datetime.timedelta(days=row)
            if start_d <= dd <= today_d:
                grid_cells[(i, row)] = (grid_x + i * pitch, hm_y + row * pitch,
                                        lvl(daily.get(dd, 0)), daily.get(dd, 0))
    plan = plan_snake_route({cr: g[2] for cr, g in grid_cells.items()})
    route, eats, growth, pops = plan[:4] if plan else ([], [], [], [])
    n = len(route)
    SPS, PAUSE = 10, 26  # 10 格/秒；片尾停 2.6s 让吃空的棋盘和满格计数停留
    total = n + PAUSE
    dur = total / SPS

    def pct(s):
        return round(s / total * 100, 3)

    # 占用模拟 → 每格被蛇身占据的时间区间（按 pops 重放真实蛇身，
    # 踩尾原步会让蛇身不再是 route 的连续切片，不能按切片偷懒）
    intervals, open_iv = {}, {}
    body, prev = deque(), set()
    swaps = []  # 踩尾瞬间：头进格 == 同拍弹出的尾格（该格连续占用，区间不断开）
    for s in range(n):
        if body and route[s] == body[0]:
            swaps.append(s)
        body.append(route[s])
        if pops[s]:
            body.popleft()
        cur = set(body)
        for cr in cur - prev:
            open_iv[cr] = s
        for cr in prev - cur:
            intervals.setdefault(cr, []).append((open_iv.pop(cr), s))
        prev = cur
    for cr, s0 in open_iv.items():
        intervals.setdefault(cr, []).append((s0, total))  # 在场蛇身持续到片尾

    eaten_at = {cr: s for s, cr in eats}  # 格子 -> 吃掉时的路由下标
    snk_head, snk_flash, snk_body = PAL["snake"]
    snk_old1, snk_old2 = PAL["snake_tail"]
    RAMP = ((1, snk_flash), (5, snk_body), (13, snk_old1), (26, snk_old2))
    # 脱困可读性三件套：踩尾段（连续踩尾 ±4 步窗口）内 A 身段幽灵化（向背景混 45%）、
    # B 踩尾瞬间琥珀闪框（stroke 通道，与 fill 互不干扰）；C 踩尾瞬间头显形——
    # 踩尾格连续占用不换区间，原渲染在此漏发头色事件，蛇头会"被身子吞掉"。
    bg_hex = PAL["bg"]

    def _ghost(c, k=0.45):
        return "#" + "".join(f"{round(int(c[i:i + 2], 16) * (1 - k) + int(bg_hex[i:i + 2], 16) * k):02x}"
                             for i in (1, 3, 5))
    ramp_ghost = tuple((age, _ghost(col)) for age, col in RAMP)
    _runs = []
    for s in swaps:
        if _runs and s - _runs[-1][1] <= 3:
            _runs[-1][1] = s
        else:
            _runs.append([s, s])
    _windows = [(max(0, a - 4), min(n, b + 4)) for a, b in _runs]

    def _in_escape(a, b):
        return any(a < wb and b > wa for wa, wb in _windows)
    snake_css = ["@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }"]
    for cr, ivs in intervals.items():
        base = HM[grid_cells[cr][2]]
        cls = f"m{cr[0]}_{cr[1]}"
        stops = [(0.0, base)]
        swap_hits = [s for s in swaps if route[s] == cr]
        for a, b in ivs:
            post = PAL["track"] if eaten_at.get(cr, n) <= b else base
            pa = pct(a)
            stops.append((max(pa - 0.05, 0.0), None))  # 进格前保持原色
            stops.append((pa, snk_head))
            ramp = ramp_ghost if _in_escape(a, b) else RAMP
            for age, col in ramp:  # 身体按入队年龄转暗：头亮尾暗（脱困窗口内转幽灵色）
                if a + age < b:
                    stops.append((pct(a + age), col))
            for s in [x for x in swap_hits if a <= x < b]:  # C：踩尾瞬间头显形，再按幽灵 ramp 衰老
                ps = pct(s)
                stops.append((max(ps - 0.05, 0.0), None))
                stops.append((ps, snk_head))
                ramp2 = ramp_ghost if _in_escape(s, s + 1) else RAMP
                for age, col in ramp2:
                    if s + age < b:
                        stops.append((pct(s + age), col))
            if b < total:
                pb = pct(b)
                stops.append((max(pb - 0.05, 0.0), None))
                stops.append((pb, post))
        frames, last = [], base
        for p, col in stops:
            if col is None:
                frames.append(f"{p:.3f}% {{ fill:{last}; }}")
            else:
                frames.append(f"{p:.3f}% {{ fill:{col}; }}")
                last = col
        frames.append(f"100% {{ fill:{last}; }}")
        anims = f"k{cls} {dur:.1f}s linear infinite"
        if swap_hits:  # B：一格一组合并闪框（同格多段 stroke 动画会互相覆盖）
            pts = [(0.0, "transparent")]
            for s in swap_hits:
                pts.append((max(pct(s) - 0.10, 0.0), "transparent"))
                pts.append((pct(s), f"{PAL['acc']}; stroke-width: 2"))
                pts.append((min(pct(s) + 0.12, 99.99), "transparent"))
            kf = " ".join(f"{p:.3f}% {{ stroke: {c}; }}" for p, c in pts)
            snake_css.append(f"@keyframes s{cls} {{ {kf} }}")
            anims += f", s{cls} {dur:.1f}s linear infinite"
        snake_css.append(f"@keyframes k{cls} {{ {' '.join(frames)} }}\n"
                         f".{cls} {{ animation: {anims}; }}")

    # "$ tokens eaten" 前缀和沿新路由累计（吃掉的仍是全盘所有 token 格）
    pre, run = [0], 0
    for s, cr in enumerate(route):
        if eaten_at.get(cr) == s:
            run += grid_cells[cr][3]
        pre.append(run)
    tt = fmt_tokens(run)
    for k in range(NC):
        i0, i1 = round(k * n / NC), round((k + 1) * n / NC)
        if i1 <= i0:
            continue
        a, b = pct(i0), pct(i1)
        if i1 >= n:  # 最后一段连着片尾暂停一起停留
            frames = f"0% {{ opacity:0; }} {a:.3f}% {{ opacity:1; }} 100% {{ opacity:1; }}"
        else:
            frames = f"0% {{ opacity:0; }} {a:.3f}% {{ opacity:1; }} {b:.3f}%,100% {{ opacity:0; }}"
        snake_css.append(f"@keyframes r{k} {{ {frames} }}\n"
                         f".r{k} {{ opacity:0; animation: r{k} {dur:.1f}s steps(1,end) infinite; }}")

    for (i, row), (x_, y_, lev, _tok) in sorted(grid_cells.items()):
        cls = f"m{i}_{row}" if (i, row) in intervals else f"h{lev}"
        lines.append(f'<rect x="{x_}" y="{y_}" width="{cell}" height="{cell}" rx="2" '
                     f'class="{cls}" fill="{HM[lev]}"/>')
    # 图例（网格右下，GitHub 风格）
    sw, lg_y = 8, hm_y + 7 * pitch + 18
    sw_x_end = grid_x + hm_w - 30
    sw_x0 = sw_x_end - (5 * sw + 4 * 2)
    text(sw_x0 - 8, lg_y + 8, L["less"], 10, 500, "faint", anchor="end")
    for k in range(5):
        lines.append(f'<rect x="{sw_x0 + k * (sw + 2)}" y="{lg_y}" width="{sw}" height="{sw}" rx="1.5" class="h{k}"/>')
    text(grid_x + hm_w - 4, lg_y + 8, L["more"], 10, 500, "faint", anchor="end")
    # 今日实时计数（网格右侧垂直居中；与热力图同源，随同步刷新）
    sx = grid_x + hm_w + 26
    t_label = L["today"]
    text(sx, hm_y + 36, t_label.upper() if not zh else t_label, 9.5, 600, "faint", spacing="2" if not zh else None)
    text(sx, hm_y + 54, fmt_tokens(daily.get(today_d, 0)), 13, 600, "text2", num=True)

    # 蛇层样式 + "$ tokens eaten" 继电器计数（与蛇进度同周期同步；与图例同线靠左）
    lines.append(f"<style>{''.join(snake_css)}</style>")
    if n:   # 无食物（route 空）时不渲染悬空的 eaten 标签
        lines.append(f'<text x="{X0}" y="{lg_y + 8}" font-size="9.5" font-weight="500" '
                     f'font-family="{MONO}" fill="{PAL["muted"]}">{esc(L["eaten_lbl"])}</text>')
        for k in range(NC):
            i0, i1 = round(k * n / NC), round((k + 1) * n / NC)
            if i1 <= i0:
                continue
            lines.append(f'<text class="r{k}" x="{X0 + 108}" y="{lg_y + 8}" font-size="9.5" font-weight="600" '
                         f'font-family="{MONO}" fill="{PAL["user"]}">{fmt_tokens(pre[i1])}/{tt}</text>')

    lines.extend(trend_section(r, PAL, L, X0, X1, LAY["yo"]))

    # ===== 压缩单条：BY TOOL ｜ TOP MODELS（grill 定稿；置于趋势区后、页脚前） =====
    # 一条堆叠条 + 圆点图例；图例一位小数、悬停两位小数（<0.01% 兜底）；
    # 工具 Top5+Other（悬停带模型数）/ 模型 Top7+Other；分母=总 token；
    # 条 10px rx3 同卡内小条语言，段间无缝无底轨。
    _bt = LAY["bt"]
    def _seg_rects(x, y, w, h, segs):
        out = [f'<clipPath id="cbar{x}y{y}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="3"/></clipPath>',
               f'<g clip-path="url(#cbar{x}y{y})">']
        cx = x
        for i, (name, tok, color, tip) in enumerate(segs):
            bw = w * tok / r["total_tokens"] if r["total_tokens"] else 0
            bw = max(bw, 2) if i < len(segs) - 1 else (x + w - cx)
            out.append(f'<rect x="{cx:.1f}" y="{y}" width="{max(bw, 0.5):.1f}" height="{h}" '
                       f'fill="{color}"><title>{esc(tip)}</title></rect>')
            cx += bw
        out.append("</g>")
        return "".join(out)

    def _legend_svg(x, y, w, segs):
        rows, ly = [], y
        line, all_rows, end_x = [], [], 0
        for name, tok, color, _ in segs:
            s = _pct1(tok / r["total_tokens"] * 100 if r["total_tokens"] else 0)
            iw = 12 + len(name) * 7.2 + 7.2 + len(s) * 7.2
            px = end_x + 34 if line else 0
            if line and px + iw > w:
                all_rows.append(line)
                line, px = [], 0
            line.append((px, name, s, color))
            end_x = px + iw
        if line:
            all_rows.append(line)
        for ln in all_rows:
            for px, name, s, color in ln:
                lx = x + px
                rows.append(f'<circle cx="{lx + 4.5:.1f}" cy="{ly - 3:.1f}" r="4.5" fill="{color}"/>')
                rows.append(f'<text x="{lx + 14:.1f}" y="{ly:.1f}" font-size="12" font-family="{MONO}">'
                            f'<tspan font-weight="500" class="text2">{esc(name)}</tspan>'
                            f'<tspan class="muted"> {s}</tspan></text>')
            ly += 20
        return "".join(rows), ly - 20

    text(X0, _bt, L["by_tool"], 11, 600, "muted", spacing="2")
    text(X1, _bt, LAY["cap"], 10.5, 400, "faint", anchor="end", num=True)
    lines.append(_seg_rects(X0, _bt + 10, CW, 10, LAY["tool_segs"]))
    _lg, _leg_end = _legend_svg(X0, _bt + 40, CW, LAY["tool_segs"])
    lines.append(_lg)

    _div = _leg_end + 16
    lines.append(f'<line x1="{X0}" y1="{_div}" x2="{X1}" y2="{_div}" stroke="{PAL["track"]}" stroke-width="1"/>')
    text(X0, _div + 16, L["top_models"], 11, 600, "muted", spacing="2")
    text(X1, _div + 16, LAY["cap"], 10.5, 400, "faint", anchor="end", num=True)
    lines.append(_seg_rects(X0, _div + 26, CW, 10, LAY["model_segs"]))
    _lg2, _leg_end2 = _legend_svg(X0, _div + 56, CW, LAY["model_segs"])
    lines.append(_lg2)

    # ===== 页脚（右侧接管 whoami 卡退场后的 daemon LIVE 叙事） =====
    lines.append(f'<line x1="{X0}" y1="{LAY["foot_div"]}" x2="{X1}" y2="{LAY["foot_div"]}" stroke="{PAL["track"]}" stroke-width="1"/>')
    text(X0, LAY["foot_text"], L["foot_stats"], 10.5, 400, "muted", num=True)
    lines.append(f'<text x="{X1}" y="{LAY["foot_text"]}" font-size="10.5" font-family="{MONO}" text-anchor="end">'
                 f'<tspan fill="{PAL["liv"]}">{esc(L["live_note"])}</tspan>'
                 f'<tspan fill="{PAL["muted"]}"> · {esc(L["foot_updated"])} {now}</tspan></text>')

    return "\n".join(lines) + "\n</svg>\n"

# ============ main ============
def _zero_data(r):
    """零值护栏判据：总量为零且各日也为零 → True（防止把公开卡覆写成 0 tokens 并推送）。"""
    return not r.get("total_tokens") and not sum(v for _, v in (r.get("daily") or []))


def _refresh_lock():
    """单轮刷新互斥：并发启动时后到者跳过，避免 SVG/README 并发写与 git index.lock 相撞。

    Acfufu 原实现用 fcntl.flock（Unix 专有，Windows 直接 ImportError）；这里在 Windows 上
    改用 msvcrt.locking 对同一锁文件加独占锁，非 Windows 平台保留 fcntl 路径。
    返回持锁 fd；抢不到返回 None。
    """
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # 锁文件首字节，非阻塞
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd):
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


TASK_NAME = "Logic769-TokenCard-Refresh"


def daily_task_running():
    """探测 Windows 计划任务是否已注册（替代 Acfufu 的守护 flock 探测，决定页脚 [LIVE]/[SYNC]）。

    schtasks /query 输出随系统语言本地化，因此只判定退出码与是否含 "Ready"/"Running"/
    "就绪"/"正在运行" 之一；探测失败一律按未注册处理，页脚如实标 [SYNC]，不谎报。
    """
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                             capture_output=True, text=True, timeout=15)
    except Exception:
        return False
    if out.returncode != 0:
        return False
    text = (out.stdout or "").lower()
    return any(s in text for s in ("ready", "running", "就绪", "正在运行"))


def _unpushed_commits():
    """本地产出、远程还没有的提交数（离线也准，无需 fetch）。异常时按 1 处理（宁多推不少推）。"""
    try:
        out = subprocess.run(["git", "rev-list", "--count", "HEAD", "--not", "--remotes"],
                             capture_output=True, text=True, timeout=15, cwd=REPO_DIR)
        return max(int(out.stdout.strip() or 0), 0)
    except Exception:
        return 1


README_STAMP_RE = r"(assets/token-stats-zh-(?:dark|light)\.svg)(\?v=[^\"]*)?"
GIT_PATHS = ["assets/token-stats-zh-dark.svg", "assets/token-stats-zh-light.svg", "README.md"]


def _bump_readme_cache_bust(version):
    """给 README 的卡片引用追加 ?v=<version>（幂等：旧 v 会被替换）。

    GitHub 渲染 README 时把相对路径重写为恒定 URL，各层 HTTP 缓存按 URL 命中 →
    内容变了 URL 没变就吃到旧缓存。加 query 后每次刷新 URL 都变 → 必 MISS。
    """
    try:
        with open(README_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        print("   ⚠️ README 读取失败，跳过 cache-bust")
        return
    text = re.sub(README_STAMP_RE, lambda m: m.group(1) + "?v=" + str(version), text)
    tmp = f"{README_PATH}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, README_PATH)
    except OSError as e:
        print(f"   ⚠️ README 原子写失败（{e}）")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return
    print(f"   README 缓存参数已更新 v={version}")


def _heal_divergence():
    """push 被拒（远端有新提交）后自愈：fetch 后 rebase -X ours，冲突行取已发布的远端版本。

    本仓库的 profile-cards 工作流每 6 小时也会提交，与本地刷新抢推送是常态；
    -X ours（rebase 语义 = 上游侧）保证已发布的 v= 单调，本地下轮重新 bump 即可。
    """
    git_dir = os.path.join(REPO_DIR, ".git")
    if any(os.path.exists(os.path.join(git_dir, s)) for s in ("rebase-merge", "rebase-apply")):
        return   # 已有 rebase 在进行（可能是人工操作）：不插手
    try:
        subprocess.run(["git", "fetch", "origin"], capture_output=True, timeout=30, cwd=REPO_DIR)
        subprocess.run(["git", "rebase", "--autostash", "-X", "ours", "@{upstream}"],
                       capture_output=True, timeout=60, cwd=REPO_DIR)
    except Exception:
        return
    for state in ("rebase-merge", "rebase-apply"):
        if os.path.exists(os.path.join(git_dir, state)):
            subprocess.run(["git", "rebase", "--abort"], capture_output=True, timeout=60, cwd=REPO_DIR)
            return


def _git_dirty(paths, cached=False):
    args = ["git", "diff"] + (["--cached"] if cached else []) + ["--quiet", "--"] + paths
    return subprocess.run(args, cwd=REPO_DIR).returncode != 0


def git_sync():
    """提交 SVG/README 变更并推送。

    - 工作区或暂存区有变更 → 重写 README 的 ?v= → commit（pathspec 限定，不扫进手工暂存内容）；
      commit 失败只回滚本管线文件的暂存，异常上抛由 main 兜底。
    - 存在未推送提交（含上轮推送失败遗留）→ push，退避重试 3 次，每次失败先 rebase 自愈。
    """
    svg_paths = [p for p in GIT_PATHS if p != "README.md"]
    svg_dirty = _git_dirty(svg_paths) or _git_dirty(svg_paths, cached=True)
    readme_dirty = _git_dirty(["README.md"]) or _git_dirty(["README.md"], cached=True)
    if not svg_dirty:
        print("   卡片无变化，跳过提交")
    else:
        # README 若已有别人的未提交改动（如手工编辑），绝不代提交：本轮只提交 SVG，
        # 放弃 ?v= 缓存击穿（宁可可能读到旧缓存，也不把用户改动夹带进自动提交）。
        paths = GIT_PATHS
        if readme_dirty:
            print("   ⚠️ README 有未提交改动，本轮不代提交 README（跳过缓存击穿）")
            paths = svg_paths
        else:
            _bump_readme_cache_bust(int(time.time()))
        subprocess.run(["git", "add"] + paths, check=True, cwd=REPO_DIR)
        try:
            subprocess.run(["git", "commit", "-m", "chore: refresh token stats", "--"] + paths,
                           check=True, cwd=REPO_DIR)
        except subprocess.CalledProcessError:
            subprocess.run(["git", "reset", "-q", "HEAD", "--"] + paths, check=False, cwd=REPO_DIR)
            raise
        print("   已提交，推送中…")
    if _unpushed_commits() <= 0:
        return
    for attempt in range(1, 4):
        try:
            r = subprocess.run(["git", "push", "origin", "HEAD"], timeout=90, cwd=REPO_DIR)
        except subprocess.TimeoutExpired:
            r = None
            print("   推送超时（90s）", flush=True)
        if r is not None and r.returncode == 0:
            print("   推送成功")
            return
        _heal_divergence()
        if attempt < 3:
            print(f"   推送失败（第 {attempt} 次），30s 后重试…", flush=True)
            time.sleep(30)
    print("   推送失败（已重试 3 次，下轮周期自动重试）")


def verify_row_costs(path):
    """与 TokenTracker 原生实现导出的逐行成本做逐行断言（口径回归测试）。"""
    curated, litellm = load_pricing_tables()
    rows = {}
    with open(TRACKER_QUEUE, encoding="utf-8") as f:
        for line in f:
            try:
                b = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(b, dict):
                continue
            rows[(str(b.get("source") or ""), str(b.get("model") or ""),
                  str(b.get("hour_start") or ""))] = b
    bad = checked = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ref = json.loads(line)
            key = (ref["source"], ref["model"], ref["hour_start"])
            row = rows.get(key)
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
    print(f"verify: checked={checked} mismatches={bad}")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出聚合 JSON")
    ap.add_argument("--once", action="store_true", help="只同步一次（Windows 计划任务入口，默认行为）")
    ap.add_argument("--no-sync", action="store_true", help="只生成 SVG，不提交/推送")
    ap.add_argument("--verify", metavar="ROWFILE", help="逐行比对成本基准后退出")
    args = ap.parse_args()

    # SVG 写盘、git 子进程、锁文件全部锚定仓库根：任意 cwd 启动都安全
    os.chdir(REPO_DIR)

    if args.verify:
        return 1 if verify_row_costs(args.verify) else 0

    r = aggregate()
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0

    if _zero_data(r):
        print("⚠️  聚合结果为零，疑似数据源异常，拒绝写盘/推送")
        return 1

    lock_fd = _refresh_lock()
    if lock_fd is None:
        print("⚠️  另一刷新在跑，跳过本轮刷新")
        return 0
    try:
        live = daily_task_running()
        os.makedirs(ASSETS_DIR, exist_ok=True)
        for name, dark in OUT_FILES:
            svg = svg_card(r, dark=dark, zh=True, live=live)
            path = os.path.join(ASSETS_DIR, name)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(svg)
            os.replace(tmp, path)
            print(f"✅ 已生成 assets/{name}")
        print(f"   tokens: {fmt_tokens(r['total_tokens'])} | cost: ${r['total_cost']:,.0f} | conversations: {r['conversations']:,}")
        print(f"   cached: {r['stats']['cached']/max(sum(r['stats'].values()),1)*100:.0f}% | peak: {r['peak_day']} {fmt_tokens(r['peak_day_tokens'])}")
        print(f"   by agent: {', '.join(f'{k} {fmt_tokens(v)}' for k, v in r['by_agent'].items())}")
        print(f"   top: {', '.join(f'{n} {fmt_tokens(t)}' for n, t in r['models'][:5])}")

        if not args.no_sync:
            try:
                git_sync()
            except Exception as e:
                print(f"⚠️  同步失败（{e}）", flush=True)
                return 1
    finally:
        _release_lock(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
