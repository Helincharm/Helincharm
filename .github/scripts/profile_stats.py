#!/usr/bin/env python3
"""Render the profile activity cards: github-stats.svg, stack.svg, streak.svg.

Data sources
  * GitHub GraphQL API: profile totals and the contribution calendar.
  * Repository git trees: the stack card. Only technologies listed in the
    README's "Technologies & Tools" grid are measured. Each one is weighted
    by the bytes of source files that use it: file extension for languages,
    real imports for frameworks, config files for tooling.

Environment
  GITHUB_TOKEN  required; the workflow token is enough for public data.
  STATS_TOKEN   optional read-only token. When set, private repositories are
                included in the stack analysis and private contributions in
                the totals.

Usage: python3 profile_stats.py <output-dir>
"""

import ast
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html import escape
from pathlib import Path

LOGIN = os.environ.get("PROFILE_LOGIN", "Helincharm")
DISPLAY_NAME = os.environ.get("PROFILE_NAME", "Helin")
README = Path(__file__).resolve().parents[2] / "README.md"
API = "https://api.github.com"

TOKEN = os.environ.get("STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
INCLUDE_PRIVATE = bool(os.environ.get("STATS_TOKEN"))

# HELINITY design system
BG = "#090820"
WHITE = "#FFFFFF"
LAVENDER = "#EADDEF"
PURPLE = "#7030EF"
MAGENTA = "#DB1FFF"
SOFT = "#BA9BF8"
VIOLET = "#A087FD"
# Ordered so that neighbouring donut segments stay easy to tell apart.
STACK_COLORS = ["#7030EF", "#FDCBF2", "#0034FE", "#DB1FFF",
                "#BA9BF8", "#776EFE", "#A087FD", "#2044FA"]
FONT = "'Segoe UI', Ubuntu, 'Helvetica Neue', Arial, sans-serif"


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------

def request(url, payload=None, accept="application/vnd.github+json", retries=3):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{LOGIN}-profile-stats",
    }
    body = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if err.code < 500 or attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def graphql(query, **variables):
    data = json.loads(request(f"{API}/graphql", {"query": query, "variables": variables}))
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


PROFILE_QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(first: 1, includeUserRepositories: true,
      contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]) { totalCount }
    repositories(first: 100, after: $after, ownerAffiliations: OWNER, isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes { name isPrivate stargazerCount defaultBranchRef { name } }
    }
  }
}
"""

CALENDAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
  }
}
"""


def fetch_profile():
    repos, after = [], None
    while True:
        user = graphql(PROFILE_QUERY, login=LOGIN, after=after)["user"]
        page = user["repositories"]
        repos += [r for r in page["nodes"] if INCLUDE_PRIVATE or not r["isPrivate"]]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    user["repositories"] = repos
    return user


def fetch_calendar(created_at):
    """Daily contribution counts and commit total since the account was created."""
    now = dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    days, commits = {}, 0
    while start < now:
        end = min(start + dt.timedelta(days=365) - dt.timedelta(seconds=1), now)
        coll = graphql(CALENDAR_QUERY, login=LOGIN, **{
            "from": start.isoformat(), "to": end.isoformat()})["user"]["contributionsCollection"]
        commits += coll["totalCommitContributions"]
        for week in coll["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[dt.date.fromisoformat(day["date"])] = day["contributionCount"]
        start = end + dt.timedelta(seconds=1)
    return dict(sorted(days.items())), commits


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def streaks(days):
    """Current and longest streak as (length, first_day, last_day)."""
    dates = list(days)
    today = dates[-1]
    # Today still counts as "in progress", so an empty today does not break the streak.
    end = today if days[today] else today - dt.timedelta(days=1)
    cur_len, day = 0, end
    while days.get(day, 0) > 0:
        cur_len += 1
        day -= dt.timedelta(days=1)
    current = (cur_len, day + dt.timedelta(days=1), end) if cur_len else (0, today, today)

    longest, run_start, run_len = (0, today, today), None, 0
    for date in dates:
        if days[date]:
            run_start = run_start or date
            run_len += 1
            if run_len > longest[0]:
                longest = (run_len, run_start, date)
        else:
            run_start, run_len = None, 0
    return current, longest


def activity_score(commits, prs, issues, stars, followers):
    """0..1 score: each metric saturates around a typical value, then a weighted mean."""
    parts = [(commits, 250, 2), (prs, 50, 3), (issues, 25, 1), (stars, 50, 4), (followers, 10, 1)]
    return sum(w * (1 - 2 ** (-v / m)) for v, m, w in parts) / sum(w for _, _, w in parts)


# --------------------------------------------------------------------------
# Stack analysis
# --------------------------------------------------------------------------

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "dist", "build", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".tox", "site-packages", "vendor", "third_party",
             ".next", "coverage"}
MAX_FILE = 1_000_000  # larger files are usually generated

EXTENSIONS = {
    "Python": (".py", ".pyi", ".ipynb"),
    "TypeScript": (".ts", ".tsx", ".mts", ".cts"),
    "C#": (".cs",),
}
PY_IMPORTS = {
    "PyTorch": {"torch"},
    "TensorFlow": {"tensorflow", "keras"},
    "FastAPI": {"fastapi"},
    "PostgreSQL": {"psycopg", "psycopg2", "asyncpg", "pg8000"},
    "Raspberry Pi": {"RPi", "gpiozero", "picamera", "picamera2"},
    "ESP32": {"esp32"},
}
JS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*|import\s*\(\s*|import\s+)['"]([^'"]+)['"]""")
DOCKER_FILE = re.compile(r"^(Dockerfile(\..+)?|.+\.Dockerfile|(docker-)?compose(\.[\w-]+)?\.ya?ml)$")
# Connection strings are not counted: analysis tools and tests mention them without using a database.
POSTGRES_IMAGE = re.compile(r"image:\s*['\"]?postgres", re.I)
POSTGRES_JS = {"pg", "postgres", "pg-promise"}


def readme_stack():
    """Technology names from the README's "Technologies & Tools" grid (img alt texts)."""
    text = README.read_text(encoding="utf-8")
    section = text.split("Technologies & Tools", 1)[-1].split("</table>", 1)[0]
    return re.findall(r'alt="([^"]+)"', section)


def python_modules(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def notebook_source(raw):
    try:
        cells = json.loads(raw).get("cells", [])
    except ValueError:
        return ""
    return "\n".join("".join(c.get("source", [])) for c in cells if c.get("cell_type") == "code")


CONTENT_EXT = (".py", ".pyi", ".ipynb", ".ts", ".js", ".mjs", ".cjs", ".cs",
               ".ino", ".ini", ".cpp", ".c", ".h")


def techs_by_name(path):
    """Technologies that follow from the file name alone."""
    name = path.rsplit("/", 1)[-1]
    used = {tech for tech, exts in EXTENSIONS.items() if name.endswith(exts)}
    if name.endswith((".tsx", ".jsx")):
        used.add("React")
    if DOCKER_FILE.match(name):
        used.add("Docker")
    if re.match(r"^\.github/workflows/[^/]+\.ya?ml$", path):
        used.add("GitHub Actions")
    return used


def techs_by_content(path, text):
    """Technologies that need the file content: imports, connection strings."""
    name = path.rsplit("/", 1)[-1]
    used = set()
    if name.endswith((".py", ".pyi", ".ipynb")):
        source = notebook_source(text) if name.endswith(".ipynb") else text
        mods = python_modules(source)
        used.update(tech for tech, names in PY_IMPORTS.items() if mods & names)
    if name.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        mods = set(JS_IMPORT.findall(text))
        if any(m == "react" or m.startswith(("react/", "react-dom")) for m in mods):
            used.add("React")
        if mods & POSTGRES_JS:
            used.add("PostgreSQL")
    if name.endswith((".ino", ".ini", ".cpp", ".c", ".h")) and re.search(r"\besp32\b", text, re.I):
        used.add("ESP32")
    if name.endswith(".cs") and re.search(r"^\s*using\s+Npgsql\b", text, re.M):
        used.add("PostgreSQL")
    if name.endswith((".yml", ".yaml")) and POSTGRES_IMAGE.search(text):
        used.add("PostgreSQL")
    return used


def measure_repo(name, branch, weights):
    """Add each file's byte size to every technology it uses.

    File sizes come from the git tree, so binaries are never downloaded; only
    source files whose imports matter are fetched.
    """
    tree = json.loads(request(f"{API}/repos/{LOGIN}/{name}/git/trees/{branch}?recursive=1"))
    for item in tree["tree"]:
        path, size = item["path"], item.get("size", 0)
        if item["type"] != "blob" or size > MAX_FILE or any(p in SKIP_DIRS for p in path.split("/")[:-1]):
            continue
        used = techs_by_name(path)
        base = path.rsplit("/", 1)[-1]
        if path.endswith(CONTENT_EXT) or (DOCKER_FILE.match(base) and base.endswith((".yml", ".yaml"))):
            raw = request(item["url"], accept="application/vnd.github.raw+json")
            used |= techs_by_content(path, raw.decode("utf-8", "ignore"))
        for tech in used:
            weights[tech] = weights.get(tech, 0) + size


def stack_usage(repos, catalog, top=8):
    weights = {}
    for repo in repos:
        if repo["name"].lower() == LOGIN.lower() or not repo["defaultBranchRef"]:
            continue  # the profile repo itself and empty repositories
        measure_repo(repo["name"], repo["defaultBranchRef"]["name"], weights)
    shown = sorted(((t, weights[t]) for t in catalog if weights.get(t)), key=lambda x: -x[1])[:top]
    total = sum(w for _, w in shown) or 1
    return [(tech, 100 * w / total) for tech, w in shown]


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------

def card(width, height, label, body, extra_defs=""):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">
  <defs>
    <linearGradient id="title" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="{WHITE}"/>
      <stop offset="0.45" stop-color="{LAVENDER}"/>
      <stop offset="1" stop-color="{SOFT}"/>
    </linearGradient>
    <radialGradient id="haze" cx="0.92" cy="0.05" r="0.8">
      <stop offset="0" stop-color="{PURPLE}" stop-opacity="0.16"/>
      <stop offset="1" stop-color="{PURPLE}" stop-opacity="0"/>
    </radialGradient>{extra_defs}
  </defs>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="{BG}" stroke="{PURPLE}" stroke-opacity="0.28"/>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="url(#haze)"/>
  <g font-family="{FONT}">
{body}
  </g>
</svg>
"""


def short_number(n):
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


# Primer Octicons (MIT), 16px
ICONS = {
    "star": "M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.751.751 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Zm0 2.445L6.615 5.5a.75.75 0 0 1-.564.41l-3.097.45 2.24 2.184a.75.75 0 0 1 .216.664l-.528 3.084 2.769-1.456a.75.75 0 0 1 .698 0l2.77 1.456-.53-3.084a.75.75 0 0 1 .216-.664l2.24-2.183-3.096-.45a.75.75 0 0 1-.564-.41L8 2.694Z",
    "commit": "M11.93 8.5a4.002 4.002 0 0 1-7.86 0H.75a.75.75 0 0 1 0-1.5h3.32a4.002 4.002 0 0 1 7.86 0h3.32a.75.75 0 0 1 0 1.5Zm-1.43-.75a2.5 2.5 0 1 0-5 0 2.5 2.5 0 0 0 5 0Z",
    "pr": "M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z",
    "issue": "M8 9.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Z",
    "repo": "M2 2.5A2.5 2.5 0 0 1 4.5 0h8.75a.75.75 0 0 1 .75.75v12.5a.75.75 0 0 1-.75.75h-2.5a.75.75 0 0 1 0-1.5h1.75v-2h-8a1 1 0 0 0-.714 1.7.75.75 0 1 1-1.072 1.05A2.495 2.495 0 0 1 2 11.5Zm10.5-1h-8a1 1 0 0 0-1 1v6.708A2.486 2.486 0 0 1 4.5 9h8ZM5 12.25a.25.25 0 0 1 .25-.25h3.5a.25.25 0 0 1 .25.25v3.25a.25.25 0 0 1-.4.2l-1.45-1.087a.249.249 0 0 0-.3 0L5.4 15.7a.25.25 0 0 1-.4-.2Z",
    "github": "M6.766 11.328c-2.063-.25-3.516-1.734-3.516-3.656 0-.781.281-1.625.75-2.188-.203-.515-.172-1.609.063-2.062.625-.078 1.468.25 1.968.703.594-.187 1.219-.281 1.985-.281.765 0 1.39.094 1.953.265.484-.437 1.344-.765 1.969-.687.218.422.25 1.515.046 2.047.5.593.766 1.39.766 2.203 0 1.922-1.453 3.375-3.547 3.64.531.344.89 1.094.89 1.954v1.625c0 .468.391.734.86.547C13.781 14.359 16 11.53 16 8.03 16 3.61 12.406 0 7.984 0 3.563 0 0 3.61 0 8.031a7.88 7.88 0 0 0 5.172 7.422c.422.156.828-.125.828-.547v-1.25c-.219.094-.5.156-.75.156-1.031 0-1.64-.562-2.078-1.609-.172-.422-.36-.672-.719-.719-.187-.015-.25-.093-.25-.187 0-.188.313-.328.625-.328.453 0 .844.281 1.25.86.313.452.64.655 1.031.655s.641-.14 1-.5c.266-.265.47-.5.657-.656",
}


def stats_svg(rows, score):
    width, height = 459, 230
    body = [f'    <text x="26" y="42" font-size="18" font-weight="700" fill="url(#title)">{escape(DISPLAY_NAME)}\'s GitHub Stats</text>']
    for i, (icon, label, value) in enumerate(rows):
        y = 82 + i * 29
        body.append(f'    <path d="{ICONS[icon]}" transform="translate(26 {y - 12})" fill="{VIOLET}"/>')
        body.append(f'    <text x="52" y="{y}" font-size="14" font-weight="600" fill="{LAVENDER}">{escape(label)}:</text>')
        body.append(f'    <text x="236" y="{y}" font-size="14" font-weight="700" fill="{WHITE}">{escape(value)}</text>')
    cx, cy, r = 372, 128, 46
    circumference = 2 * math.pi * r
    filled = max(0.04, min(score, 1)) * circumference
    body += [
        f'    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{PURPLE}" stroke-opacity="0.2" stroke-width="8"/>',
        f'    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="url(#ring)" stroke-width="8" stroke-linecap="round"'
        f' stroke-dasharray="{filled:.1f} {circumference:.1f}" transform="rotate(-90 {cx} {cy})"/>',
        f'    <path d="{ICONS["github"]}" transform="translate({cx - 20} {cy - 20}) scale(2.5)" fill="{LAVENDER}"/>',
    ]
    ring = f"""
    <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{PURPLE}"/>
      <stop offset="1" stop-color="{MAGENTA}"/>
    </linearGradient>"""
    return card(width, height, f"{DISPLAY_NAME}'s GitHub Stats", "\n".join(body), ring)


def stack_svg(items):
    width, height = 374, 230
    body = ['    <text x="26" y="42" font-size="18" font-weight="700" fill="url(#title)">Most Used Stack</text>']
    if not items:
        body.append(f'    <text x="26" y="80" font-size="13" fill="{LAVENDER}">No data yet</text>')
        return card(width, height, "Most Used Stack", "\n".join(body))

    row_gap = 20
    top = 62 + (8 - len(items)) * row_gap / 2 + 14
    for i, (tech, pct) in enumerate(items):
        y = top + i * row_gap
        body.append(f'    <circle cx="31" cy="{y - 4.5:.1f}" r="5" fill="{STACK_COLORS[i]}"/>')
        body.append(f'    <text x="44" y="{y:.1f}" font-size="13" font-weight="600" fill="{LAVENDER}">{escape(tech)}'
                    f' <tspan fill="{SOFT}" font-weight="400">{pct:.1f}%</tspan></text>')

    cx, cy, r, stroke = 282, 132, 54, 18
    if len(items) == 1:
        body.append(f'    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{STACK_COLORS[0]}" stroke-width="{stroke}"/>')
    else:
        gap = math.radians(1.6)
        angle = -math.pi / 2
        for i, (_, pct) in enumerate(items):
            sweep = 2 * math.pi * pct / 100
            a0, a1 = angle + gap / 2, angle + max(sweep - gap / 2, gap / 2 + 0.001)
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            large = 1 if a1 - a0 > math.pi else 0
            body.append(f'    <path d="M{x0:.2f} {y0:.2f}A{r} {r} 0 {large} 1 {x1:.2f} {y1:.2f}" fill="none"'
                        f' stroke="{STACK_COLORS[i]}" stroke-width="{stroke}"/>')
            angle += sweep
    return card(width, height, "Most Used Stack", "\n".join(body))


def fmt_day(day, with_year):
    return f"{day:%b} {day.day}" + (f", {day.year}" if with_year else "")


def fmt_range(first, last, today):
    with_year = first.year != today.year or last.year != today.year
    if first == last:
        return fmt_day(first, with_year)
    return f"{fmt_day(first, with_year)} - {fmt_day(last, with_year)}"


def streak_svg(total, since, current, longest, today):
    width, height = 560, 196
    col = width / 3
    defs = f"""
    <linearGradient id="figure" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{LAVENDER}"/>
      <stop offset="1" stop-color="{VIOLET}"/>
    </linearGradient>
    <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{PURPLE}"/>
      <stop offset="1" stop-color="{MAGENTA}"/>
    </linearGradient>"""

    def side(x, number, label, sub):
        return [
            f'    <text x="{x:.1f}" y="92" text-anchor="middle" font-size="32" font-weight="700" fill="url(#figure)">{escape(number)}</text>',
            f'    <text x="{x:.1f}" y="124" text-anchor="middle" font-size="14" font-weight="600" fill="{LAVENDER}">{escape(label)}</text>',
            f'    <text x="{x:.1f}" y="150" text-anchor="middle" font-size="12" fill="{LAVENDER}" fill-opacity="0.65">{escape(sub)}</text>',
        ]

    cx, cy, r = width / 2, 78, 40
    since_text = f"{fmt_day(since, True)} - Present"
    body = side(col / 2, f"{total:,}", "Total Contributions", since_text)
    body += [
        f'    <line x1="{col:.1f}" y1="38" x2="{col:.1f}" y2="158" stroke="{SOFT}" stroke-opacity="0.22"/>',
        f'    <line x1="{2 * col:.1f}" y1="38" x2="{2 * col:.1f}" y2="158" stroke="{SOFT}" stroke-opacity="0.22"/>',
        f'    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="url(#ring)" stroke-width="5"/>',
        f'    <circle cx="{cx}" cy="{cy - r}" r="11" fill="{BG}"/>',
        # flame
        f'    <path d="M{cx} {cy - r - 10}c3.2 3.6 7 6.6 7 11.2a7 7 0 0 1-14 0c0-2.6 1.2-4.4 2.8-5.9.2 1.8 1 3 2.3 3.6-.4-3.4.3-6.2 1.9-8.9Z"'
        f' fill="url(#ring)"/>',
        f'    <text x="{cx}" y="{cy + 10}" text-anchor="middle" font-size="28" font-weight="700" fill="{WHITE}">{current[0]}</text>',
        f'    <text x="{cx}" y="146" text-anchor="middle" font-size="14" font-weight="700" fill="{SOFT}">Current Streak</text>',
        f'    <text x="{cx}" y="170" text-anchor="middle" font-size="12" fill="{LAVENDER}" fill-opacity="0.65">{escape(fmt_range(current[1], current[2], today))}</text>',
    ]
    body += side(col * 2.5, str(longest[0]), "Longest Streak", fmt_range(longest[1], longest[2], today))
    return card(width, height, "GitHub contribution streak", "\n".join(body), defs)


# --------------------------------------------------------------------------

def main():
    if not TOKEN:
        sys.exit("GITHUB_TOKEN or STATS_TOKEN is required")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    out.mkdir(parents=True, exist_ok=True)

    profile = fetch_profile()
    days, commits = fetch_calendar(profile["createdAt"])
    stars = sum(r["stargazerCount"] for r in profile["repositories"])
    prs, issues = profile["pullRequests"]["totalCount"], profile["issues"]["totalCount"]
    contributed = profile["repositoriesContributedTo"]["totalCount"]
    score = activity_score(commits, prs, issues, stars, profile["followers"]["totalCount"])
    rows = [
        ("star", "Total Stars Earned", short_number(stars)),
        ("commit", "Total Commits", short_number(commits)),
        ("pr", "Total PRs", short_number(prs)),
        ("issue", "Total Issues", short_number(issues)),
        ("repo", "Contributed To", short_number(contributed)),
    ]

    catalog = readme_stack()
    items = stack_usage(profile["repositories"], catalog)
    current, longest = streaks(days)
    today = list(days)[-1]

    (out / "github-stats.svg").write_text(stats_svg(rows, score), encoding="utf-8")
    (out / "stack.svg").write_text(stack_svg(items), encoding="utf-8")
    (out / "streak.svg").write_text(
        streak_svg(sum(days.values()), list(days)[0], current, longest, today), encoding="utf-8")

    print(f"private repos included: {INCLUDE_PRIVATE}")
    print("stats:", ", ".join(f"{label}={value}" for _, label, value in rows), f"score={score:.2f}")
    print("stack:", ", ".join(f"{t} {p:.1f}%" for t, p in items) or "-")
    print(f"streak: total={sum(days.values())} current={current[0]} longest={longest[0]}")


if __name__ == "__main__":
    main()
