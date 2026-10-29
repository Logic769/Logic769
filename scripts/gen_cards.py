#!/usr/bin/env python3
"""Generate self-hosted SVG cards for the GitHub profile README.

All data comes from public GitHub endpoints (REST API + the public
contribution calendar page). No third-party image services are involved,
so refresh latency is bounded only by the workflow schedule.

Local run:  GH_TOKEN=$(gh auth token) python scripts/gen_cards.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOGIN = os.environ.get("GITHUB_LOGIN", "Logic769")
TOKEN = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "assets"
UA = "profile-cards/1.0 (+https://github.com/" + LOGIN + ")"
API = "https://api.github.com"
FONT = '-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif'

THEMES = {
    "dark": dict(bg="#1a1b27", border="#2f334d", text="#c0caf5", muted="#565f89",
                 accent="#7aa2f7", good="#9ece6a", warn="#e0af68", track="#24283b"),
    "light": dict(bg="#ffffff", border="#d8dee4", text="#1f2328", muted="#6a737d",
                  accent="#4d6bfe", good="#059669", warn="#b45309", track="#eaeef2"),
}


# ---------------------------------------------------------------- transport
def _auth_variants(accept):
    """Try the authenticated request first, then fall back to anonymous."""
    variants = []
    if TOKEN:
        variants.append({"User-Agent": UA, "Accept": accept, "Authorization": "Bearer " + TOKEN})
    variants.append({"User-Agent": UA, "Accept": accept})
    return variants


def _fetch(url, accept, decode_json):
    last = None
    for headers in _auth_variants(accept):
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=45) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                return json.loads(raw) if decode_json else raw
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (401, 403) and "Authorization" in headers:
                    break  # credentials rejected: retry the whole URL anonymously
                time.sleep(1 + attempt)
            except Exception as exc:  # noqa: BLE001 - network flakiness is expected
                last = exc
                time.sleep(2 + attempt * 2)
    print("[warn] GET %s failed: %s" % (url, last), file=sys.stderr)
    return None


def get_json(url, accept="application/vnd.github+json"):
    return _fetch(url, accept, True)


def get_text(url):
    return _fetch(url, "text/html", False)


# ------------------------------------------------------------------- gather
def collect_user():
    return get_json(API + "/users/" + LOGIN) or {}


def collect_repos():
    repos = []
    for page in range(1, 4):
        chunk = get_json(API + "/users/" + LOGIN + "/repos?per_page=100&page=%d&sort=pushed" % page)
        if not chunk:
            break
        repos.extend(chunk)
        if len(chunk) < 100:
            break
    return repos


def search_total(query, kind="issues"):
    path = "/search/issues?q=" if kind == "issues" else "/search/commits?q="
    data = get_json(API + path + urllib.parse.quote(query) + "&per_page=1")
    if not data:
        return None
    return data.get("total_count")


def collect_contributions():
    """Return {date(str) -> count} and total from the public calendar page."""
    html = get_text("https://github.com/users/" + LOGIN + "/contributions")
    if not html:
        return {}, 0
    id_to_date = {}
    for td in re.findall(r"<td[^>]*>", html):
        if "ContributionCalendar-day" not in td:
            continue
        d = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', td)
        i = re.search(r'id="(contribution-day-[^"]+)"', td)
        if d and i:
            id_to_date[i.group(1)] = d.group(1)
    daily = {}
    for m in re.finditer(
        r'<tool-tip[^>]*\bfor="(contribution-day-[^"]+)"[^>]*>(.*?)</tool-tip>', html, re.S
    ):
        date = id_to_date.get(m.group(1))
        if not date:
            continue
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        hit = re.match(r"(\d[\d,]*)\s+contribution", text)
        daily[date] = int(hit.group(1).replace(",", "")) if hit else 0
    if not daily:
        print("[warn] contribution calendar parsed empty", file=sys.stderr)
    return daily, sum(daily.values())


def collect_events():
    return get_json(API + "/users/" + LOGIN + "/events/public?per_page=30") or []


# ------------------------------------------------------------------- derive
def compute_streaks(daily):
    if not daily:
        return 0, 0, 0
    today = datetime.now(timezone.utc).date()
    cursor = today
    if daily.get(cursor.isoformat(), 0) == 0:
        cursor = today - timedelta(days=1)
    current = 0
    while daily.get(cursor.isoformat(), 0) > 0:
        current += 1
        cursor -= timedelta(days=1)
    longest = run = 0
    for date in sorted(daily):
        if daily[date] > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return current, longest, sum(daily.values())


def relative_time(iso):
    try:
        stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    seconds = int((datetime.now(timezone.utc) - stamp).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    if seconds < 2592000:
        return "%dd ago" % (seconds // 86400)
    if seconds < 31536000:
        return "%dmo ago" % (seconds // 2592000)
    return "%dy ago" % (seconds // 31536000)


def describe_event(ev):
    kind = ev.get("type") or ""
    repo = (ev.get("repo") or {}).get("name") or ""
    payload = ev.get("payload") or {}
    if kind == "PushEvent":
        n = len(payload.get("commits") or [])
        if n == 0:
            # public event payloads are often truncated; do not claim "0 commits"
            return "Pushed to %s" % repo
        return "Pushed %d commit%s to %s" % (n, "" if n == 1 else "s", repo)
    if kind == "CreateEvent":
        ref = payload.get("ref_type") or "ref"
        name = payload.get("ref") or ""
        return ("Created %s %s in %s" % (ref, name, repo)).replace("  ", " ")
    if kind == "DeleteEvent":
        return "Deleted %s in %s" % (payload.get("ref_type") or "ref", repo)
    if kind == "PullRequestEvent":
        return "%s pull request in %s" % ((payload.get("action") or "updated").capitalize(), repo)
    if kind == "PullRequestReviewEvent":
        return "Reviewed a pull request in %s" % repo
    if kind == "IssuesEvent":
        return "%s an issue in %s" % ((payload.get("action") or "updated").capitalize(), repo)
    if kind == "IssueCommentEvent":
        return "Commented on an issue in %s" % repo
    if kind == "WatchEvent":
        return "Starred %s" % repo
    if kind == "ForkEvent":
        return "Forked %s" % repo
    if kind == "ReleaseEvent":
        return "Published a release in %s" % repo
    if kind == "PublicEvent":
        return "Made %s public" % repo
    if kind == "MemberEvent":
        return "Updated collaborators in %s" % repo
    return "%s in %s" % (kind.replace("Event", "") or "Activity", repo)


# -------------------------------------------------------------------- render
def esc(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def svg_open(width, height, theme, label):
    t = theme
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-label="%s">\n'
        '  <rect x="0.5" y="0.5" width="%d" height="%d" rx="10" fill="%s" stroke="%s"/>\n'
        % (width, height, width, height, esc(label), width - 1, height - 1, t["bg"], t["border"])
    )


def title_block(t, text, x=26, y=36):
    return (
        '  <text x="%d" y="%d" font-family=\'%s\' font-size="14" font-weight="700" '
        'fill="%s">%s</text>\n' % (x, y, FONT, t["accent"], esc(text))
    )


def render_stats(t, data, width=420, height=200):
    if data.get("followers") is None and data.get("repos") is None:
        return None  # API unreachable: keep whatever is already committed
    body = svg_open(width, height, t, "GitHub stats")
    body += title_block(t, "GitHub Stats")
    cells = [
        ("Total Stars", _fmt(data["stars"])),
        ("Commits", _fmt(data["commits"])),
        ("Pull Requests", _fmt(data["prs"])),
        ("Issues", _fmt(data["issues"])),
        ("Followers", _fmt(data["followers"])),
        ("Repositories", _fmt(data["repos"])),
    ]
    for index, (label, value) in enumerate(cells):
        col = index % 2
        row = index // 2
        lx = 26 if col == 0 else 226
        vx = 192 if col == 0 else 394
        y = 84 + row * 42
        body += (
            '  <text x="%d" y="%d" font-family=\'%s\' font-size="11" fill="%s">%s</text>\n'
            % (lx, y, FONT, t["muted"], esc(label))
        )
        body += (
            '  <text x="%d" y="%d" font-family=\'%s\' font-size="20" font-weight="700" '
            'text-anchor="end" fill="%s">%s</text>\n'
            % (vx, y + 4, FONT, t["text"], esc(value))
        )
    body += "</svg>\n"
    return body


def _fmt(value):
    return "n/a" if value is None else ("{:,}".format(int(value)))


def render_languages(t, langs, width=420, height=200):
    if not langs:
        return None
    body = svg_open(width, height, t, "Most used languages")
    body += title_block(t, "Most Used Languages")
    body += (
        '  <text x="26" y="54" font-family=\'%s\' font-size="10" fill="%s">'
        'share of non-fork repos by primary language</text>\n' % (FONT, t["muted"])
    )
    if not langs:
        body += '  <text x="26" y="110" font-family=\'%s\' font-size="12" fill="%s">no data</text>\n' % (FONT, t["muted"])
    palette = [t["accent"], t["good"], t["warn"], "#bb9af7", "#7dcfff"]
    for index, (name, pct) in enumerate(langs[:5]):
        y = 76 + index * 26
        body += (
            '  <text x="26" y="%d" font-family=\'%s\' font-size="11.5" fill="%s">%s</text>\n'
            % (y, FONT, t["text"], esc(name))
        )
        body += (
            '  <text x="394" y="%d" font-family=\'%s\' font-size="11.5" text-anchor="end" '
            'fill="%s">%.1f%%</text>\n' % (y, FONT, t["muted"], pct)
        )
        body += (
            '  <rect x="26" y="%d" width="368" height="5" rx="2.5" fill="%s"/>\n'
            % (y + 5, t["track"])
        )
        body += (
            '  <rect x="26" y="%d" width="%.1f" height="5" rx="2.5" fill="%s"/>\n'
            % (y + 5, max(3.0, 368 * pct / 100.0), palette[index % len(palette)])
        )
    body += "</svg>\n"
    return body


def render_streak(t, streak, width=420, height=200):
    if not streak:
        return None
    current, longest, total = streak
    body = svg_open(width, height, t, "Contribution streak")
    body += title_block(t, "Contribution Streak")
    cells = [
        (current, "current streak"),
        (longest, "longest streak"),
        (total, "contributions / yr"),
    ]
    for index, (value, label) in enumerate(cells):
        cx = 84 + index * 126
        body += (
            '  <text x="%d" y="118" font-family=\'%s\' font-size="26" font-weight="700" '
            'text-anchor="middle" fill="%s">%s</text>\n'
            % (cx, FONT, t["accent"] if index == 0 else t["text"], esc(value))
        )
        body += (
            '  <text x="%d" y="142" font-family=\'%s\' font-size="10.5" text-anchor="middle" '
            'fill="%s">%s</text>\n' % (cx, FONT, t["muted"], esc(label))
        )
    body += (
        '  <text x="210" y="174" font-family=\'%s\' font-size="10" text-anchor="middle" '
        'fill="%s">updated %s UTC</text>\n'
        % (FONT, t["muted"], datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    )
    body += "</svg>\n"
    return body


def render_repos(t, repos, width=420, height=200):
    if not repos:
        return None
    body = svg_open(width, height, t, "Recently updated repositories")
    body += title_block(t, "Recently Updated")
    if not repos:
        body += '  <text x="26" y="110" font-family=\'%s\' font-size="12" fill="%s">no data</text>\n' % (FONT, t["muted"])
    for index, repo in enumerate(repos[:5]):
        y = 74 + index * 25
        name = (repo.get("name") or "").strip() or "unknown"
        if len(name) > 26:
            name = name[:23] + "..."
        bits = []
        if repo.get("language"):
            bits.append(repo["language"])
        if relative_time(repo.get("pushed_at") or ""):
            bits.append(relative_time(repo.get("pushed_at") or ""))
        meta = "  \u00b7  ".join(bits) or "\u2014"
        body += (
            '  <text x="26" y="%d" font-family=\'%s\' font-size="11.5" font-weight="600" '
            'fill="%s">%s</text>\n' % (y, FONT, t["text"], esc(name))
        )
        body += (
            '  <text x="394" y="%d" font-family=\'%s\' font-size="10.5" text-anchor="end" '
            'fill="%s">%s</text>\n' % (y, FONT, t["muted"], esc(meta))
        )
    body += "</svg>\n"
    return body


def render_activity(t, events, width=840, height=176):
    if not events:
        return None
    body = svg_open(width, height, t, "Recent activity")
    body += title_block(t, "Recent Activity")
    if not events:
        body += '  <text x="26" y="110" font-family=\'%s\' font-size="12" fill="%s">no recent public activity</text>\n' % (FONT, t["muted"])
    for index, ev in enumerate(events[:5]):
        y = 68 + index * 22
        text = describe_event(ev)
        if len(text) > 74:
            text = text[:71] + "..."
        body += '  <circle cx="30" cy="%d" r="3" fill="%s"/>\n' % (y - 4, t["accent"])
        body += (
            '  <text x="44" y="%d" font-family=\'%s\' font-size="11.5" fill="%s">%s</text>\n'
            % (y, FONT, t["text"], esc(text))
        )
        body += (
            '  <text x="812" y="%d" font-family=\'%s\' font-size="10.5" text-anchor="end" '
            'fill="%s">%s</text>\n' % (y, FONT, t["muted"], esc(relative_time(ev.get("created_at") or "")))
        )
    body += "</svg>\n"
    return body


# ---------------------------------------------------------------------- main
def main():
    user = collect_user()
    repos = collect_repos()
    daily, calendar_total = collect_contributions()
    events = collect_events()

    own = [r for r in repos if not r.get("fork")]
    recent = sorted(
        [r for r in own if not r.get("archived")],
        key=lambda r: r.get("pushed_at") or "",
        reverse=True,
    )
    langs = {}
    for repo in own:
        name = repo.get("language")
        if name:
            langs[name] = langs.get(name, 0) + 1
    total_lang = sum(langs.values()) or 1
    ranked = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:5]
    langs_pct = [(name, count * 100.0 / total_lang) for name, count in ranked]

    data = dict(
        stars=sum(r.get("stargazers_count", 0) for r in repos),
        commits=search_total("author:" + LOGIN, kind="commits"),
        prs=search_total("author:%s type:pr" % LOGIN),
        issues=search_total("author:%s type:issue" % LOGIN),
        followers=user.get("followers"),
        repos=user.get("public_repos"),
    )
    streak = compute_streaks(daily) if daily else None
    print(
        "[info] stars=%s commits=%s prs=%s issues=%s followers=%s repos=%s | streak=%s calendar_total=%s events=%d"
        % (data["stars"], data["commits"], data["prs"], data["issues"], data["followers"], data["repos"],
           streak, calendar_total, len(events)),
        file=sys.stderr,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for theme_name, theme in THEMES.items():
        outputs = {
            "stats": render_stats(theme, data),
            "top-langs": render_languages(theme, langs_pct),
            "streak": render_streak(theme, streak),
            "recent-repos": render_repos(theme, recent),
            "activity": render_activity(theme, events),
        }
        for name, svg in outputs.items():
            if svg is None:
                print("[warn] skip %s-%s: no data, keeping existing file" % (name, theme_name), file=sys.stderr)
                continue
            path = OUT_DIR / ("%s-%s.svg" % (name, theme_name))
            path.write_text(svg, encoding="utf-8")
            print("wrote %s (%d bytes)" % (path.relative_to(ROOT), len(svg)), file=sys.stderr)


if __name__ == "__main__":
    main()
