#!/usr/bin/env python3
"""Render the animated SVG cards for the FuzzDOT profile README.

Every visual block of the README is an SVG in cards/. GitHub shows them through
<img>, which sandboxes them: no scripts, no external requests. What still works
inside the SVG is its own <style> (CSS keyframes) and SMIL (<set>, <animate>),
plus fonts embedded as base64 data URIs. So this script draws everything itself.

Data flow
  1. If GH_TOKEN is set, pull contributions, pinned repos and repo counts from
     the GraphQL API and cache them in data/github.json.
     If the API call fails, fall back to the cached file so a GitHub hiccup
     never blanks the README.
  2. Make sure the subset fonts exist in fonts/ (downloaded and subset once,
     then committed; later runs just read them).
  3. Render every card into cards/ and rewrite the TILES and REPOS blocks in
     README.md so links always match the cards.

Environment
  GH_TOKEN   token for the GraphQL API (GITHUB_TOKEN works for public data;
             a PAT with read:user also counts private contributions)
  GH_LOGIN   GitHub username (default FuzzDOT)
  STATIC=1   render the final frame only, for screenshots before pushing
  MOCK=1     use generated sample data instead of the API (local layout work)
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import random
import re
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

ROOT = Path(__file__).resolve().parent.parent
CARDS = ROOT / "cards"
FONTS = ROOT / "fonts"
DATA = ROOT / "data" / "github.json"
README = ROOT / "README.md"

LOGIN = os.environ.get("GH_LOGIN", "FuzzDOT")
TOKEN = os.environ.get("GH_TOKEN", "")
STATIC = os.environ.get("STATIC") == "1"
MOCK = os.environ.get("MOCK") == "1"

# --------------------------------------------------------------------------- #
# Content. Edit this block, not the drawing code.
# --------------------------------------------------------------------------- #

PROFILE = {
    "first": "Faaz",
    "last": "Mohamed",
    "tagline": ["AI systems researcher. Founder.", "Builder of things that have to work."],
    "affiliation": [
        "Founder of Presage and Adventra Labs.",
        "NSF ExLAIM REU fellow, 2026. CS and Data Science at Pitt.",
    ],
    "site": "https://faazmohamed.com",
}

# number: an int, or "repos" for the live public-repo count.
TILES = [
    {
        "key": "research",
        "label": "Research",
        "color": "violet",
        "number": 2,
        "unit": "preprints on Zenodo",
        "lines": ["NOVA: evidential uncertainty,", "continual learning, causal latents."],
        "link_text": "labs.faazmohamed.com",
        "href": "https://labs.faazmohamed.com",
        "alt": "Research at Adventra Labs: NOVA and two preprints on Zenodo",
    },
    {
        "key": "systems",
        "label": "Systems",
        "color": "emerald",
        "number": "repos",
        "unit": "public repositories",
        "lines": ["VexDB, VERITAS, MerryQuery.", "Correct first, then fast."],
        "link_text": "github.com/FuzzDOT",
        "href": "https://github.com/FuzzDOT?tab=repositories",
        "alt": "Systems: public repositories on GitHub",
    },
    {
        "key": "company",
        "label": "Founder",
        "color": "amber",
        "number": 2,
        "unit": "companies",
        "lines": ["Presage: semantic data for", "shipping logistics. Adventra Labs."],
        "link_text": "faazmohamed.com",
        "href": "https://faazmohamed.com",
        "alt": "Companies: founder of Presage and Adventra Labs",
    },
]

STACK = [
    ("Languages", "Python, C++20, C, TypeScript, Java, Swift"),
    ("ML and research", "PyTorch, NumPy, TensorFlow, mechanistic interpretability, activation probing"),
    ("Backend and data", "FastAPI, Flask, PostgreSQL, pgvector, SQLAlchemy, React"),
    ("Systems and infra", "SIMD (AVX2, NEON), lock-free structures, Docker, AWS, MinIO, Linux, GitHub Actions"),
]

# --------------------------------------------------------------------------- #
# Design tokens
# --------------------------------------------------------------------------- #

C = {
    "paper": "#F4F7F5",
    "plane": "#E9F0EC",
    "rule": "#C9D5CF",
    "ink": "#10231C",
    "muted": "#56675F",
    "faint": "#8A9B93",
    "edge": "#B7C7BF",
    "node": "#8FA39A",
    "emerald": "#0E9F6E",
    "violet": "#6E40C9",
    "amber": "#B7740B",
}
GRAPH_SCALE = ["#E2EAE6", "#A9E2C8", "#5CC99D", "#10B981", "#08724F"]
BAR_IDLE = "#B9D9CB"

# key: (url, axis pins, expect italic, css family, css style, css weight)
_GF = "https://raw.githubusercontent.com/google/fonts/main/ofl"
FONT_SOURCES = {
    "display": (f"{_GF}/newsreader/Newsreader%5Bopsz,wght%5D.ttf", {"opsz": 72, "wght": 360}, False, "fx-display", "normal", 400),
    "text": (f"{_GF}/newsreader/Newsreader%5Bopsz,wght%5D.ttf", {"opsz": 14, "wght": 400}, False, "fx-text", "normal", 400),
    "text-italic": (f"{_GF}/newsreader/Newsreader-Italic%5Bopsz,wght%5D.ttf", {"opsz": 14, "wght": 400}, True, "fx-text", "italic", 400),
    "mono": (f"{_GF}/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf", {"wght": 400}, False, "fx-mono", "normal", 400),
    "mono-bold": (f"{_GF}/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf", {"wght": 600}, False, "fx-mono", "normal", 600),
}
UNICODES = "U+0020-007E,U+00A0,U+00E9,U+2013,U+2014,U+2019,U+201C,U+201D,U+2026,U+2192,U+2605"

SERIF_DISPLAY = "'fx-display',Georgia,'Times New Roman',serif"
SERIF_TEXT = "'fx-text',Georgia,'Times New Roman',serif"
MONO = "'fx-mono',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# Average advance widths (em) used for layout. Mono is exact; serif is close enough.
EM = {"d": 0.45, "t": 0.47, "ti": 0.45, "m": 0.6, "mb": 0.6}

EASE = "cubic-bezier(.2,.8,.2,1)"

BASE_CSS = (
    ".a{transform-box:fill-box;transform-origin:center}"
    ".bar{transform-origin:50% 100%}"
    ".still{display:none}"
    f".d{{font-family:{SERIF_DISPLAY}}}"
    f".t{{font-family:{SERIF_TEXT}}}"
    f".ti{{font-family:{SERIF_TEXT};font-style:italic}}"
    f".m{{font-family:{MONO}}}"
    f".mb{{font-family:{MONO};font-weight:600}}"
    "@keyframes rise{from{opacity:0;transform:translateY(12px)}}"
    "@keyframes fade{from{opacity:0}}"
    "@keyframes pop{0%{opacity:0;transform:scale(.2)}70%{opacity:1;transform:scale(1.3)}100%{transform:scale(1)}}"
    "@keyframes grow{from{transform:scaleY(0)}}"
    "@keyframes draw{from{stroke-dashoffset:1}}"
    "@keyframes swing{0%{opacity:0;transform:translateY(70px) rotate(-7deg)}"
    "55%{opacity:1;transform:translateY(-8px) rotate(1.6deg)}"
    "78%{transform:translateY(3px) rotate(-.6deg)}100%{transform:none}}"
    "@keyframes ring{0%{opacity:.7;transform:scale(.4)}100%{opacity:0;transform:scale(3.2)}}"
)
# The final frame: CSS animations off, SMIL-driven layers swapped for static copies.
# Base styles are always the end state, so turning animations off shows the finished card.
STILL_CSS = "*{animation:none!important}.smil{display:none}.still{display:inline}"


def log(msg: str) -> None:
    print(f"[readme-cards] {msg}", file=sys.stderr)


def esc(s: str) -> str:
    return _xml_escape(str(s), {'"': "&quot;", "'": "&#39;"})


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def width(s: str, size: float, f: str) -> float:
    return len(s) * size * EM[f]


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false, first: 100) {
      totalCount
      nodes { stargazerCount }
    }
    pinnedItems(first: 6, types: REPOSITORY) {
      nodes {
        ... on Repository {
          name description url stargazerCount forkCount pushedAt
          primaryLanguage { name color }
        }
      }
    }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_github() -> dict:
    body = json.dumps({"query": QUERY, "variables": {"login": LOGIN}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": f"{LOGIN}-readme-cards",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
    user = (payload.get("data") or {}).get("user")
    if not user:
        raise RuntimeError(f"user {LOGIN!r} not found")

    cal = user["contributionsCollection"]["contributionCalendar"]
    repos = user["repositories"]
    return {
        "login": LOGIN,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "public_repos": repos["totalCount"],
        # Only the first 100 repos are summed; fine until you pass 100 public repos.
        "stars": sum(r["stargazerCount"] for r in repos["nodes"]),
        "total": cal["totalContributions"],
        "days": [
            {"date": d["date"], "count": d["contributionCount"]}
            for w in cal["weeks"]
            for d in w["contributionDays"]
        ],
        "pinned": [
            {
                "name": r["name"],
                "description": r.get("description") or "",
                "url": r["url"],
                "stars": r["stargazerCount"],
                "forks": r["forkCount"],
                "pushed_at": r["pushedAt"],
                "language": (r.get("primaryLanguage") or {}).get("name"),
                "language_color": (r.get("primaryLanguage") or {}).get("color"),
            }
            for r in user["pinnedItems"]["nodes"]
            if r
        ],
    }


def mock_data() -> dict:
    rng = random.Random(4)
    today = date.today()
    start = today - timedelta(days=370)
    days = []
    for i in range(371):
        d = start + timedelta(days=i)
        busy = 1.0 + 0.8 * math.sin(i / 40)
        c = 0 if rng.random() < 0.22 else int(rng.expovariate(1 / (4 * busy)))
        days.append({"date": d.isoformat(), "count": c})
    return {
        "login": LOGIN,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "public_repos": 31,
        "stars": 64,
        "total": sum(d["count"] for d in days),
        "days": days,
        "pinned": [
            {"name": "VexDB", "description": "C++20 parallel HNSW vector database. 9.5x insert throughput over a naive baseline, 100% recall@10.", "url": "https://github.com/FuzzDOT/VexDB", "stars": 21, "forks": 3, "pushed_at": "2026-10-02T12:00:00Z", "language": "C++", "language_color": "#f34b7d"},
            {"name": "VERITAS", "description": "Deterministic AI evaluation engine: 9 microservices, SHA-256 audit chains, 427 tests.", "url": "https://github.com/FuzzDOT/VERITAS", "stars": 9, "forks": 1, "pushed_at": "2026-08-14T12:00:00Z", "language": "Python", "language_color": "#3572A5"},
            {"name": "LUMEN", "description": "A transformer on a from-scratch NumPy autograd engine, with interpretability tooling.", "url": "https://github.com/FuzzDOT/LUMEN", "stars": 7, "forks": 0, "pushed_at": "2026-06-30T12:00:00Z", "language": "Python", "language_color": "#3572A5"},
            {"name": "AppTS", "description": "Self-hosted job application tracker with ATS scraper integrations.", "url": "https://github.com/FuzzDOT/AppTS", "stars": 4, "forks": 0, "pushed_at": "2026-05-11T12:00:00Z", "language": "TypeScript", "language_color": "#3178c6"},
        ],
    }


def load_data() -> dict:
    if MOCK:
        log("MOCK=1: using generated sample data")
        return mock_data()
    if TOKEN:
        try:
            data = fetch_github()
            DATA.parent.mkdir(parents=True, exist_ok=True)
            DATA.write_text(json.dumps(data, indent=1) + "\n")
            log(f"fetched {len(data['days'])} days, {len(data['pinned'])} pinned repos")
            return data
        except (urllib.error.URLError, RuntimeError, KeyError, TimeoutError) as e:
            log(f"GitHub API failed ({e}); falling back to {DATA.relative_to(ROOT)}")
    if DATA.exists():
        return json.loads(DATA.read_text())
    sys.exit("[readme-cards] no GH_TOKEN, no cached data, and MOCK is not set")


def compute_stats(data: dict) -> dict:
    days = sorted(
        ((date.fromisoformat(d["date"]), int(d["count"])) for d in data["days"]),
        key=lambda x: x[0],
    )
    counts = [c for _, c in days]

    # Today counts toward the streak only once it has a contribution.
    i = len(counts) - 1
    if i >= 0 and counts[i] == 0:
        i -= 1
    current = 0
    while i >= 0 and counts[i] > 0:
        current += 1
        i -= 1

    longest = run = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)

    busiest_day, busiest_count = max(days, key=lambda x: x[1]) if days else (date.today(), 0)

    last = days[-1][0] if days else date.today()
    months = []
    y, m = last.year, last.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    months.reverse()
    per_month = {ym: 0 for ym in months}
    for d, c in days:
        if (d.year, d.month) in per_month:
            per_month[(d.year, d.month)] += c

    return {
        "days": days,
        "total": data.get("total", sum(counts)),
        "current": current,
        "longest": longest,
        "active": sum(1 for c in counts if c > 0),
        "busiest_day": busiest_day,
        "busiest_count": busiest_count,
        "months": [(date(y, m, 1), per_month[(y, m)]) for y, m in months],
        "refreshed": datetime.fromisoformat(data["fetched_at"]).date(),
    }


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #

_download_cache: dict[str, bytes] = {}


def _download(url: str) -> bytes:
    if url not in _download_cache:
        req = urllib.request.Request(url, headers={"User-Agent": "readme-cards"})
        with urllib.request.urlopen(req, timeout=60) as r:
            _download_cache[url] = r.read()
    return _download_cache[url]


def _build_font(url: str, pins: dict, expect_italic: bool, out: Path) -> None:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    font = TTFont(io.BytesIO(_download(url)))
    if "fvar" in font:
        axes = {a.axisTag: a for a in font["fvar"].axes}
        limits = {tag: max(axes[tag].minValue, min(axes[tag].maxValue, v)) for tag, v in pins.items() if tag in axes}
        # Pin every remaining axis to its default so the output is a static font.
        for tag, a in axes.items():
            limits.setdefault(tag, a.defaultValue)
        font = instancer.instantiateVariableFont(font, limits)

    # Filenames lie. Check the font itself before trusting that "Regular" is upright.
    italic = bool(font["OS/2"].fsSelection & 1) or font["post"].italicAngle != 0
    if italic != expect_italic:
        raise RuntimeError(f"{url} reports italic={italic}, expected {expect_italic}")

    opts = subset.Options()
    opts.flavor = "woff"
    opts.layout_features = ["kern", "liga", "lnum", "tnum"]
    opts.name_IDs = [1, 2]
    opts.hinting = False
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=subset.parse_unicodes(UNICODES))
    sub.subset(font)
    font.flavor = "woff"
    out.parent.mkdir(parents=True, exist_ok=True)
    font.save(out)


def load_fonts() -> dict[str, str]:
    """Return base64 WOFF per font key. Missing fonts fall back to system stacks."""
    out = {}
    for key, (url, pins, italic, *_rest) in FONT_SOURCES.items():
        path = FONTS / f"{key}.woff"
        if not path.exists():
            try:
                _build_font(url, pins, italic, path)
                log(f"built fonts/{path.name} ({path.stat().st_size // 1024} KB)")
            except Exception as e:  # network, fontTools missing, bad file: degrade, don't fail
                log(f"font {key!r} unavailable ({e}); cards will use the fallback stack")
                continue
        out[key] = base64.b64encode(path.read_bytes()).decode()
    return out


FONT_DATA: dict[str, str] = {}


def font_css(keys: tuple[str, ...]) -> str:
    rules = []
    for key in keys:
        if key not in FONT_DATA:
            continue
        _, _, _, family, style, weight = FONT_SOURCES[key]
        rules.append(
            f"@font-face{{font-family:'{family}';font-style:{style};font-weight:{weight};"
            f"src:url(data:font/woff;base64,{FONT_DATA[key]}) format('woff')}}"
        )
    return "".join(rules)


# --------------------------------------------------------------------------- #
# SVG primitives
# --------------------------------------------------------------------------- #


def A(name: str, delay: float, dur: float = 0.6, ease: str = EASE, count: str = "1") -> str:
    return f"animation:{name} {dur}s {ease} {delay:.3f}s {count} both"


def text(x, y, content, f="t", size=13, fill=None, anchor=None, a=None, cls="", ls=None, raw=False):
    classes = " ".join(filter(None, [f, "a" if a else "", cls]))
    attrs = [f'x="{x:.1f}"', f'y="{y:.1f}"', f'class="{classes}"', f'font-size="{size}"', f'fill="{fill or C["ink"]}"']
    if anchor:
        attrs.append(f'text-anchor="{anchor}"')
    if ls is not None:
        attrs.append(f'letter-spacing="{ls}"')
    if a:
        attrs.append(f'style="{a}"')
    return f"<text {' '.join(attrs)}>{content if raw else esc(content)}</text>"


def caption(x, y, label, rest, size=12.5, max_width=None, a=None):
    """Paper-style caption: 'Figure 2.' upright, the sentence in italic."""
    lead = f'<tspan class="t" font-style="normal" fill="{C["ink"]}">{esc(label)}</tspan> '
    if not max_width:
        return text(x, y, lead + esc(rest), f="ti", size=size, fill=C["muted"], a=a, raw=True)
    per_line = max(20, int(max_width / (size * EM["ti"])))
    lines = textwrap.wrap(f"{label} {rest}", per_line)
    out = []
    for i, line in enumerate(lines):
        if i == 0 and line.startswith(label):
            body = lead + esc(line[len(label):].lstrip())
        else:
            body = esc(line)
        out.append(text(x, y + i * size * 1.35, body, f="ti", size=size, fill=C["muted"], a=a, raw=True))
    return "".join(out)


def card(w, h, title, body, fonts, defs="", radius=10, background=True):
    css = font_css(fonts) + BASE_CSS + (STILL_CSS if STATIC else f"@media (prefers-reduced-motion:reduce){{{STILL_CSS}}}")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-labelledby="title"><title id="title">{esc(title)}</title>'
        f"<style>{css}</style><defs>{defs}</defs>"
        + (f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="{radius}" fill="{C["paper"]}" stroke="{C["rule"]}"/>' if background else "")
        + f"{''.join(body)}</svg>"
    )


def count_up(x, y, value: int, f: str, size: float, fill: str, start: float, frames: int = 18, step: float = 0.045) -> str:
    """SMIL count-up. CSS can't change text, so each value is its own <text>,
    shown for one step. A static copy covers reduced motion and STATIC mode."""
    vals = [round(value * (1 - (1 - k / (frames - 1)) ** 3)) for k in range(frames)]
    smil = []
    for k, v in enumerate(vals):
        t = start + k * step
        last = k == frames - 1
        anim = (
            f'<set attributeName="opacity" to="1" begin="{t:.3f}s" fill="freeze"/>'
            if last
            else f'<set attributeName="opacity" to="1" begin="{t:.3f}s" dur="{step:.3f}s"/>'
        )
        smil.append(
            f'<text x="{x}" y="{y}" class="{f}" font-size="{size}" fill="{fill}" opacity="0">{v:,}{anim}</text>'
        )
    return f'<g class="smil">{"".join(smil)}</g><g class="still">{text(x, y, f"{value:,}", f=f, size=size, fill=fill)}</g>'


# --------------------------------------------------------------------------- #
# Hero, with an HNSW search as the figure
# --------------------------------------------------------------------------- #


def hnsw_figure(x_left: float, layer_y: list[float]) -> str:
    """A real greedy search over a small three-layer HNSW-style graph."""
    rng = random.Random(1751)  # picked so the search hops in every layer and nodes stay spread out
    n = 16
    pts = [(rng.uniform(0.07, 0.93), rng.uniform(0.12, 0.88)) for _ in range(n)]
    layers = {0: list(range(n)), 1: list(range(7)), 2: list(range(3))}
    half_h, span, skew = 26, 286, 38

    def screen(layer: int, i: int) -> tuple[float, float]:
        u, v = pts[i]
        yc = layer_y[2 - layer]
        return x_left + skew * (1 - v) + u * span, yc - half_h + 2 * half_h * v

    def d2(i, q):
        return (pts[i][0] - q[0]) ** 2 + (pts[i][1] - q[1]) ** 2

    neighbours = {
        L: {i: sorted((j for j in ids if j != i), key=lambda j: d2(j, pts[i]))[:3] for i in ids}
        for L, ids in layers.items()
    }

    entry = 0
    target = max(layers[0], key=lambda i: d2(i, pts[entry]))
    q = (pts[target][0] + 0.03, pts[target][1] - 0.05)
    cur, path = entry, [(2, entry)]
    for L in (2, 1, 0):
        if L != 2:
            path.append((L, cur))
        while True:
            best = min(neighbours[L][cur] + [cur], key=lambda j: d2(j, q))
            if best == cur:
                break
            cur = best
            path.append((L, cur))
    result = cur

    out = []
    for k, L in enumerate((2, 1, 0)):
        yc = layer_y[2 - L]
        poly = [
            (x_left + skew, yc - half_h), (x_left + skew + span, yc - half_h),
            (x_left + span, yc + half_h), (x_left, yc + half_h),
        ]
        pts_s = " ".join(f"{px:.1f},{py:.1f}" for px, py in poly)
        out.append(f'<polygon points="{pts_s}" fill="{C["plane"]}" stroke="{C["rule"]}" class="a" style="{A("fade", 0.25 + k * 0.1, 0.5)}"/>')
        out.append(text(x_left - 10, yc + 4, f"layer {L}", f="ti", size=11.5, fill=C["faint"], anchor="end", a=A("fade", 0.35 + k * 0.1)))

    for k, L in enumerate((2, 1, 0)):
        seen, lines = set(), []
        for i in layers[L]:
            for j in neighbours[L][i]:
                e = tuple(sorted((i, j)))
                if e in seen:
                    continue
                seen.add(e)
                (x1, y1), (x2, y2) = screen(L, i), screen(L, j)
                lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
        out.append(f'<g stroke="{C["edge"]}" stroke-width=".8" class="a" style="{A("fade", 0.55 + k * 0.1, 0.6)}">{"".join(lines)}</g>')

    for k, L in enumerate((2, 1, 0)):
        for idx, i in enumerate(layers[L]):
            x, y = screen(L, i)
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="{C["node"]}" class="a" style="{A("pop", 0.7 + k * 0.12 + idx * 0.015, 0.35)}"/>')

    qx = x_left + skew * (1 - q[1]) + q[0] * span
    qy = layer_y[2] - half_h + 2 * half_h * q[1]
    out.append(
        f'<g class="a" style="{A("fade", 1.0, 0.4)}" stroke="{C["violet"]}" stroke-width="1.4">'
        f'<line x1="{qx - 4:.1f}" y1="{qy - 4:.1f}" x2="{qx + 4:.1f}" y2="{qy + 4:.1f}"/>'
        f'<line x1="{qx - 4:.1f}" y1="{qy + 4:.1f}" x2="{qx + 4:.1f}" y2="{qy - 4:.1f}"/></g>'
    )
    out.append(text(qx + 7, qy - 5, "q", f="ti", size=12.5, fill=C["violet"], a=A("fade", 1.0, 0.4)))

    draw_start, draw_dur = 1.3, 1.6
    coords = [screen(L, i) for L, i in path]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    out.append(
        f'<polyline points="{poly}" fill="none" stroke="{C["emerald"]}" stroke-width="1.8" '
        f'stroke-linejoin="round" stroke-linecap="round" pathLength="1" stroke-dasharray="1" '
        f'class="a" style="{A("draw", draw_start, draw_dur, "linear")}"/>'
    )
    for k, (x, y) in enumerate(coords):
        t = draw_start + draw_dur * k / max(1, len(coords) - 1)
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{C["emerald"]}" class="a" style="{A("pop", t, 0.3)}"/>')

    rx, ry = screen(0, result)
    end = draw_start + draw_dur
    out.append(f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="5" fill="{C["emerald"]}" class="a" style="{A("pop", end, 0.4)}"/>')
    # The only looping motion on the page: the found neighbour keeps a slow pulse.
    out.append(
        f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="5" fill="none" stroke="{C["emerald"]}" stroke-width="1.2" '
        f'opacity="0" class="a" style="{A("ring", end + 0.4, 2.4, "ease-out", "infinite")}"/>'
    )
    return "".join(out)


def card_hero(_stats) -> str:
    W, H = 840, 340
    p = PROFILE
    b = [
        text(32, 118, p["first"], f="d", size=92, a=A("rise", 0.1, 0.8)),
        text(32, 204, p["last"], f="d", size=92, a=A("rise", 0.22, 0.8)),
    ]
    for i, line in enumerate(p["tagline"]):
        b.append(text(36, 244 + i * 24, line, f="t", size=18.5, a=A("rise", 0.4 + i * 0.08)))
    for i, line in enumerate(p["affiliation"]):
        b.append(text(36, 302 + i * 19, line, f="ti", size=13.5, fill=C["muted"], a=A("fade", 0.6 + i * 0.08)))

    b.append(hnsw_figure(500, [72, 150, 228]))
    b.append(
        caption(
            486, 286, "Figure 1.",
            "Greedy search through a three-layer HNSW index, from the entry point down to the nearest neighbour of q.",
            size=12, max_width=330, a=A("fade", 0.9),
        )
    )
    title = f"{p['first']} {p['last']}. {' '.join(p['tagline'])}"
    return card(W, H, title, b, ("display", "text", "text-italic"))


# --------------------------------------------------------------------------- #
# Contribution graph
# --------------------------------------------------------------------------- #


def card_graph(s) -> str:
    W, H = 840, 190
    cell, pitch = 11, 14
    days = s["days"]
    first = days[0][0]
    origin = first - timedelta(days=(first.weekday() + 1) % 7)  # Sunday on or before
    cols = ((days[-1][0] - origin).days // 7) + 1
    skip = max(0, cols - 53)

    nz = sorted(c for _, c in days if c > 0)
    q = [nz[min(len(nz) - 1, int(len(nz) * f))] for f in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]

    def level(c: int) -> int:
        if c == 0:
            return 0
        return 1 if c <= q[0] else 2 if c <= q[1] else 3 if c <= q[2] else 4

    x0 = W - 32 - min(cols, 53) * pitch + (pitch - cell)
    y0 = 42
    b = []

    last_label_col = -10
    for col in range(skip, cols):
        d = origin + timedelta(days=col * 7)
        prev = origin + timedelta(days=(col - 1) * 7)
        c = col - skip
        if (col == skip or d.month != prev.month) and c - last_label_col >= 3 and c <= 50:
            b.append(text(x0 + c * pitch, y0 - 9, d.strftime("%b"), f="t", size=11, fill=C["muted"], a=A("fade", 0.9)))
            last_label_col = c
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        b.append(text(x0 - 8, y0 + row * pitch + 9, label, f="t", size=10.5, fill=C["faint"], anchor="end", a=A("fade", 0.9)))

    for d, cnt in days:
        col = (d - origin).days // 7 - skip
        if col < 0:
            continue
        row = (d.weekday() + 1) % 7
        x, y = x0 + col * pitch, y0 + row * pitch
        delay = 1.0 + col * 0.02 + row * 0.006
        b.append(
            f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2.5" fill="{GRAPH_SCALE[level(cnt)]}" '
            f'class="a" style="{A("pop", delay, 0.35)}"/>'
        )

    ly = y0 + 7 * pitch + 30
    refreshed = f"{s['refreshed'].day} {s['refreshed']:%b %Y}"
    b.append(caption(32, ly, "Figure 2.", f"Contributions over the last year, {s['total']:,} in total. Refreshed {refreshed}.", a=A("fade", 1.6)))
    lx = W - 32 - 5 * 14 - 34
    b.append(text(lx - 6, ly, "Less", f="t", size=11, fill=C["faint"], anchor="end", a=A("fade", 1.6)))
    for i, col in enumerate(GRAPH_SCALE):
        b.append(f'<rect x="{lx + i * 14}" y="{ly - 10}" width="11" height="11" rx="2.5" fill="{col}" class="a" style="{A("fade", 1.6)}"/>')
    b.append(text(lx + 5 * 14 + 4, ly, "more", f="t", size=11, fill=C["faint"], a=A("fade", 1.6)))

    return card(W, H, f"Contribution graph: {s['total']:,} contributions in the last year", b, ("text", "text-italic"))


# --------------------------------------------------------------------------- #
# Monthly bars and Table 1
# --------------------------------------------------------------------------- #


def card_activity(s) -> str:
    W, H = 840, 268
    months = s["months"]
    peak = max((c for _, c in months), default=0) or 1
    best_i = max(range(len(months)), key=lambda i: months[i][1])

    left, right, base, max_h = 32, 520, 212, 132
    slot = (right - left) / len(months)
    bw = 22
    b = [f'<line x1="{left}" y1="{base + .5}" x2="{right}" y2="{base + .5}" stroke="{C["rule"]}" class="a" style="{A("fade", 1.6)}"/>']
    for i, (m, c) in enumerate(months):
        h = max(2, c / peak * max_h)
        x = left + i * slot + (slot - bw) / 2
        fill = C["emerald"] if i == best_i else BAR_IDLE
        b.append(
            f'<rect x="{x:.1f}" y="{base - h:.1f}" width="{bw}" height="{h:.1f}" rx="2" fill="{fill}" '
            f'class="a bar" style="{A("grow", 1.8 + i * 0.06, 0.7)}"/>'
        )
        b.append(text(x + bw / 2, base + 17, m.strftime("%b"), f="t", size=11, fill=C["muted"], anchor="middle", a=A("fade", 1.7)))
        if i == best_i:
            b.append(text(x + bw / 2, base - h - 8, f"{c:,}", f="mb", size=11.5, fill=C["emerald"], anchor="middle", a=A("pop", 1.8 + len(months) * 0.06 + 0.5, 0.45)))
    best_m = months[best_i][0]
    b.append(caption(left, 252, "Figure 3.", f"Contributions per month. {best_m:%B} {best_m.year} was the busiest.", a=A("fade", 2.2)))

    # Table 1, booktabs style: heavy top and bottom rules, a light rule under the header.
    tx, tr = 572, W - 32
    bd = s["busiest_day"]
    rows = [
        ("Current streak", plural(s["current"], "day")),
        ("Longest streak", plural(s["longest"], "day")),
        ("Active days", f"{s['active']}"),
        ("Busiest day", f"{bd:%b} {bd.day} ({s['busiest_count']})"),
    ]
    t0 = 2.0
    b.append(caption(tx, 44, "Table 1.", "Streaks over the last year.", a=A("fade", t0)))
    b.append(f'<line x1="{tx}" y1="60" x2="{tr}" y2="60" stroke="{C["ink"]}" stroke-width="1.2" class="a" style="{A("fade", t0)}"/>')
    b.append(text(tx, 79, "Measure", f="t", size=12, fill=C["muted"], a=A("fade", t0)))
    b.append(text(tr, 79, "Value", f="t", size=12, fill=C["muted"], anchor="end", a=A("fade", t0)))
    b.append(f'<line x1="{tx}" y1="88" x2="{tr}" y2="88" stroke="{C["ink"]}" stroke-width=".5" class="a" style="{A("fade", t0)}"/>')
    for i, (k, v) in enumerate(rows):
        y = 112 + i * 27
        d = t0 + 0.15 + i * 0.1
        b.append(text(tx, y, k, f="t", size=14, a=A("rise", d)))
        b.append(text(tr, y, v, f="m", size=12.5, anchor="end", a=A("rise", d)))
    b.append(f'<line x1="{tx}" y1="207" x2="{tr}" y2="207" stroke="{C["ink"]}" stroke-width="1.2" class="a" style="{A("fade", t0)}"/>')

    title = f"Contributions per month. Current streak {s['current']} days, longest {s['longest']} days, {s['active']} active days."
    return card(W, H, title, b, ("text", "text-italic", "mono", "mono-bold"))


# --------------------------------------------------------------------------- #
# Tiles
# --------------------------------------------------------------------------- #


def card_tile(t: dict, i: int, data: dict) -> str:
    W, H = 272, 236
    color = C[t["color"]]
    value = data["public_repos"] if t["number"] == "repos" else int(t["number"])
    enter = 2.6 + i * 0.15
    inner = [
        f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="10" fill="{C["paper"]}" stroke="{C["rule"]}"/>',
        f'<g clip-path="url(#clip)"><rect x="0" y="0" width="{W}" height="4" fill="{color}"/></g>',
        text(22, 40, t["label"], f="ti", size=18, fill=color),
        count_up(20, 128, value, "d", 84, color, start=enter + 0.45),
        text(22, 154, t["unit"], f="t", size=14.5),
    ]
    for k, line in enumerate(t["lines"]):
        inner.append(text(22, 182 + k * 18, line, f="t", size=13, fill=C["muted"]))
    inner.append(text(22, 220, t["link_text"], f="m", size=10.5, fill=C["faint"]))

    body = [f'<g class="a" style="{A("swing", enter, 1.0, "cubic-bezier(.3,.7,.3,1)")}">{"".join(inner)}</g>']
    defs = f'<clipPath id="clip"><rect x="0" y="0" width="{W}" height="{H}" rx="10"/></clipPath>'
    title = f"{t['label']}: {value:,} {t['unit']}. {' '.join(t['lines'])}"
    # The tile draws its own background inside the swinging group so the whole card moves.
    return card(W, H, title, body, ("display", "text", "text-italic", "mono"), defs=defs, background=False)


# --------------------------------------------------------------------------- #
# Repo cards
# --------------------------------------------------------------------------- #


def card_repo(r: dict, i: int) -> str:
    W, H = 413, 150
    d = 3.4 + i * 0.1
    name = r["name"] if len(r["name"]) <= 36 else r["name"][:35] + "…"
    desc = r["description"] or "No description yet."
    lines = textwrap.wrap(desc, 50)
    if len(lines) > 2:
        lines = [lines[0], textwrap.shorten(" ".join(lines[1:]), 49, placeholder="…")]

    b = [text(22, 42, name, f="mb", size=15, a=A("rise", d))]
    for k, line in enumerate(lines):
        b.append(text(22, 70 + k * 19, line, f="t", size=13.5, fill=C["muted"], a=A("rise", d + 0.05)))

    x, y = 22, 126
    parts = []
    if r.get("language"):
        parts.append(f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{esc(r.get("language_color") or C["node"])}"/>')
        parts.append(text(x + 16, y, r["language"], f="t", size=12.5, fill=C["muted"]))
        x += 16 + width(r["language"], 12.5, "t") + 18
    star_label = plural(r["stars"], "star")
    parts.append(text(x, y, star_label, f="t", size=12.5, fill=C["muted"]))
    x += width(star_label, 12.5, "t") + 18
    if r.get("pushed_at"):
        pushed = datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
        parts.append(text(x, y, f"updated {pushed:%b %Y}", f="t", size=12.5, fill=C["faint"]))
    b.append(f'<g class="a" style="{A("fade", d + 0.15)}">{"".join(parts)}</g>')
    return card(W, H, f"{r['name']}: {desc}", b, ("text", "mono-bold"))


# --------------------------------------------------------------------------- #
# Table 2: stack
# --------------------------------------------------------------------------- #


def card_stack() -> str:
    W = 840
    tx, tr, col2 = 32, W - 32, 212
    per_line = int((tr - col2) / (12 * EM["m"]))
    t0 = 3.8
    b = [caption(tx, 40, "Table 2.", "Tools I reach for.", a=A("fade", t0))]
    y = 56
    b.append(f'<line x1="{tx}" y1="{y}" x2="{tr}" y2="{y}" stroke="{C["ink"]}" stroke-width="1.2" class="a" style="{A("fade", t0)}"/>')
    b.append(text(tx, y + 19, "Area", f="t", size=12, fill=C["muted"], a=A("fade", t0)))
    b.append(text(col2, y + 19, "Tools", f="t", size=12, fill=C["muted"], a=A("fade", t0)))
    y += 28
    b.append(f'<line x1="{tx}" y1="{y}" x2="{tr}" y2="{y}" stroke="{C["ink"]}" stroke-width=".5" class="a" style="{A("fade", t0)}"/>')
    y += 24
    for i, (area, tools) in enumerate(STACK):
        d = t0 + 0.15 + i * 0.1
        lines = textwrap.wrap(tools, per_line)
        b.append(text(tx, y, area, f="t", size=14, a=A("rise", d)))
        for k, line in enumerate(lines):
            b.append(text(col2, y + k * 19, line, f="m", size=12, a=A("rise", d)))
        y += 19 * len(lines) + 12
    y -= 8
    b.append(f'<line x1="{tx}" y1="{y}" x2="{tr}" y2="{y}" stroke="{C["ink"]}" stroke-width="1.2" class="a" style="{A("fade", t0)}"/>')
    H = y + 26
    title = "Tools: " + "; ".join(f"{a}: {t}" for a, t in STACK)
    return card(W, H, title, b, ("text", "text-italic", "mono"))


# --------------------------------------------------------------------------- #
# README blocks
# --------------------------------------------------------------------------- #


def attr(s: str) -> str:
    return esc(s).replace("\n", " ")


def replace_block(md: str, name: str, content: str) -> str:
    pattern = re.compile(rf"(<!-- {name}:START -->)(.*?)(<!-- {name}:END -->)", re.S)
    if not pattern.search(md):
        log(f"README has no {name} markers; leaving it alone")
        return md
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(3)}", md)


def update_readme(data: dict) -> None:
    if not README.exists():
        log("README.md not found; skipping block updates")
        return
    md = README.read_text()
    tiles = "\n".join(
        f'<a href="{attr(t["href"])}"><img src="./cards/tile-{t["key"]}.svg" width="32.5%" alt="{attr(t["alt"])}" /></a>'
        for t in TILES
    )
    repos = "\n".join(
        f'<a href="{attr(r["url"])}"><img src="./cards/repo-{i}.svg" width="49%" '
        f'alt="{attr(r["name"])}: {attr(r["description"] or "repository")}" /></a>'
        for i, r in enumerate(data["pinned"])
    )
    md = replace_block(md, "TILES", tiles)
    md = replace_block(md, "REPOS", repos or "<!-- pin repositories on your profile to show them here -->")
    README.write_text(md)


# --------------------------------------------------------------------------- #


def main() -> None:
    global FONT_DATA
    data = load_data()
    s = compute_stats(data)
    FONT_DATA = load_fonts()

    CARDS.mkdir(exist_ok=True)
    out = {
        "hero.svg": card_hero(s),
        "graph.svg": card_graph(s),
        "activity.svg": card_activity(s),
        "stack.svg": card_stack(),
    }
    for i, t in enumerate(TILES):
        out[f"tile-{t['key']}.svg"] = card_tile(t, i, data)
    for i, r in enumerate(data["pinned"]):
        out[f"repo-{i}.svg"] = card_repo(r, i)

    for stale in CARDS.glob("repo-*.svg"):
        if stale.name not in out:
            stale.unlink()
    for name, svg in out.items():
        (CARDS / name).write_text(svg)
    update_readme(data)
    kb = sum(len(v) for v in out.values()) // 1024
    log(f"wrote {len(out)} cards ({kb} KB total){' in STATIC mode' if STATIC else ''}")


if __name__ == "__main__":
    main()
