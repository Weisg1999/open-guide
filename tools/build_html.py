#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
open-guide HTML builder.

Markdown is the single source of truth; every *.md guide is rendered into a
dependency-free, single-file HTML page beside it. No third-party packages.

Usage:
    python tools/build_html.py                # rebuild every guide
    python tools/build_html.py --topic gin    # rebuild one topic
    python tools/build_html.py --check        # CI mode: fail if HTML is stale
    python tools/build_html.py --stats        # print per-guide structure stats

Theme colours / hero metadata live in tools/themes.json. A topic that is not
listed there is picked up automatically and gets a stable palette derived from
its directory name, so adding a new guide needs no build configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEMES_FILE = Path(__file__).resolve().parent / "themes.json"
FRAGMENTS_FILE = Path(__file__).resolve().parent / "fragments.json"
ANCHOR_MAP_FILE = Path(__file__).resolve().parent / "anchor_map.json"

# --------------------------------------------------------------------------
# Theme configuration
# --------------------------------------------------------------------------

DEFAULT_COLORS = {
    "accent": "#2563eb", "accent-light": "#dbeafe", "accent-dark": "#1e40af",
    "bg": "#f6f8fb", "card": "#ffffff", "border": "#e3e9f2",
    "text": "#1e293b", "muted": "#64748b", "ok": "#059669",
    "warn": "#d97706", "danger": "#dc2626", "code-bg": "#0f172a",
    "kw": "#93c5fd", "str": "#86efac", "cmt": "#64748b",
    "num": "#fbbf24", "fn": "#f472b6", "typ": "#67e8f9",
}

# Used when a topic has no entry in themes.json (e.g. a newly added guide).
AUTO_PALETTE = [
    ("#2563eb", "#dbeafe", "#1e40af", "#f6f8fb", "#0f172a"),
    ("#0d9488", "#ccfbf1", "#115e59", "#f4faf9", "#03211d"),
    ("#7c3aed", "#ede9fe", "#5b21b6", "#f8f7fc", "#1a1033"),
    ("#ea580c", "#ffedd5", "#9a3412", "#fdf9f5", "#241408"),
    ("#e11d48", "#ffe4e6", "#9f1239", "#fdf6f7", "#2a0a14"),
    ("#16a34a", "#dcfce7", "#166534", "#f4faf5", "#052e16"),
    ("#0891b2", "#cffafe", "#155e75", "#f4fafc", "#04222b"),
    ("#d97706", "#fef3c7", "#92400e", "#fdf9f3", "#1c1206"),
]

LANG_LABELS = {
    "go": "Go", "bash": "Shell", "sh": "Shell", "shell": "Shell",
    "yaml": "YAML", "yml": "YAML", "json": "JSON", "js": "JavaScript",
    "javascript": "JavaScript", "protobuf": "Protobuf", "proto": "Protobuf",
    "python": "Python", "text": "Text", "txt": "Text", "conf": "Config",
    "toml": "TOML", "sql": "SQL", "dockerfile": "Dockerfile",
    "promql": "PromQL", "gitignore": "Gitignore", "ini": "INI",
}

LEVEL_BADGES = {
    "入门": ("b", "入门"), "初级": ("b", "初级"), "速览": ("b", "速览"),
    "中级": ("m", "中级"), "实战": ("m", "实战"), "核心": ("m", "核心"),
    "高级": ("a", "高级"), "进阶": ("a", "进阶"), "避坑": ("a", "避坑"),
    "决策": ("a", "决策"), "版本": ("b", "版本"),
}
SECTION_BADGES = [
    ("b", "入门"), ("b", "速览"), ("m", "核心"), ("m", "实战"),
    ("b", "版本"), ("a", "避坑"), ("a", "决策"),
]


def load_themes() -> dict:
    if THEMES_FILE.exists():
        return json.loads(THEMES_FILE.read_text(encoding="utf-8"))
    return {}


def load_fragments() -> dict:
    """Hand-crafted visual blocks (SVG diagrams, feature cards, callouts).

    These have no Markdown equivalent, so they are versioned as data and
    injected by the builder. Markdown stays the single source of *text*.
    """
    if FRAGMENTS_FILE.exists():
        return json.loads(FRAGMENTS_FILE.read_text(encoding="utf-8"))
    return {}


def load_anchor_map() -> dict:
    """Legacy semantic anchors in the hand-written HTML -> slug anchors."""
    if ANCHOR_MAP_FILE.exists():
        return json.loads(ANCHOR_MAP_FILE.read_text(encoding="utf-8"))
    return {}


def theme_for(topic: str, themes: dict, h1: str, quote: str) -> dict:
    if topic in themes:
        t = dict(themes[topic])
        t["colors"] = {**DEFAULT_COLORS, **t.get("colors", {})}
        return t
    idx = int(hashlib.sha1(topic.encode("utf-8")).hexdigest(), 16) % len(AUTO_PALETTE)
    accent, light, dark, bg, code_bg = AUTO_PALETTE[idx]
    colors = dict(DEFAULT_COLORS)
    colors.update(accent=accent, **{
        "accent-light": light, "accent-dark": dark, "bg": bg, "code-bg": code_bg,
    })
    return {
        "title": h1 or topic,
        "ver": "",
        "subtitle": (quote or "")[:70],
        "tags": [],
        "colors": colors,
    }


# --------------------------------------------------------------------------
# Inline rendering
# --------------------------------------------------------------------------

_PLACEHOLDER = "\x00{}\x00"


def href_for(url: str) -> str:
    """In HTML, point relative .md links at the generated .html page."""
    if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#|//)", url, re.I):
        return url
    m = re.match(r"^([^#]*)(#.*)?$", url)
    path, frag = m.group(1), m.group(2) or ""
    if path.lower().endswith(".md"):
        path = path[:-3] + ".html"
    return path + frag


def inline(text: str) -> str:
    stash: list[str] = []

    def keep(markup: str) -> str:
        stash.append(markup)
        return _PLACEHOLDER.format(len(stash) - 1)

    # Inline code first, so its content is never reinterpreted as markup.
    text = re.sub(
        r"`([^`]+)`",
        lambda m: keep("<code>" + html.escape(m.group(1), quote=False) + "</code>"),
        text,
    )
    # Images, then links.
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)",
        lambda m: keep(
            '<img src="' + html.escape(m.group(2), quote=True) + '" alt="'
            + html.escape(m.group(1), quote=True) + '">'
        ),
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)",
        lambda m: keep(
            '<a href="' + html.escape(href_for(m.group(2)), quote=True) + '"'
            + (' title="' + html.escape(m.group(3), quote=True) + '"' if m.group(3) else "")
            + ">" + inline_plain(m.group(1)) + "</a>"
        ),
        text,
    )
    # Bare <https://...> autolinks. Other angle brackets are literal text.
    text = re.sub(
        r"<((?:https?|mailto):[^>\s]+)>",
        lambda m: keep(
            '<a href="' + html.escape(m.group(1), quote=True) + '">'
            + html.escape(m.group(1), quote=False) + "</a>"
        ),
        text,
    )
    text = html.escape(text, quote=False)
    text = re.sub(r"\\\|", "|", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    text = re.sub(_PLACEHOLDER.replace("{}", r"(\d+)"), lambda m: stash[int(m.group(1))], text)
    text = re.sub(r" {2,}$", "<br>", text)
    return text


def inline_plain(text: str) -> str:
    """Inline rendering for link labels (no nested links)."""
    return re.sub(r"[*`]", "", html.escape(text, quote=False))


def slug(text: str) -> str:
    """GitHub-compatible anchor: keep CJK/word chars, drop punctuation."""
    text = re.sub(r"`[^`]*`", lambda m: m.group(0)[1:-1], text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).lower()
    text = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", text)
    text = re.sub(r"\s+", "-", text.strip())
    return re.sub(r"-+", "-", text).strip("-")


# --------------------------------------------------------------------------
# Block rendering
# --------------------------------------------------------------------------

FENCE_RE = re.compile(r"^\s*```\s*([\w+-]*)\s*$")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")


def split_cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    row = row.replace("\\|", "\x01")
    return [c.strip().replace("\x01", "|") for c in row.split("|")]


def render_table(lines: list[str]) -> str:
    header = split_cells(lines[0])
    rows = [split_cells(r) for r in lines[2:]]
    out = ["<div class=\"table-wrap\"><table><thead><tr>"]
    for cell in header:
        out.append("<th>" + inline(cell) + "</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for i in range(len(header)):
            out.append("<td>" + inline(row[i] if i < len(row) else "") + "</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render_list(lines: list[str]) -> str:
    items: list[dict] = []
    for line in lines:
        m = LIST_RE.match(line)
        if m:
            body = m.group(3)
            task = ""
            tm = re.match(r"^\[([ xX])\]\s*(.*)$", body)
            if tm:
                checked = tm.group(1).lower() == "x"
                task = '<input type="checkbox" disabled' + (" checked" if checked else "") + "> "
                body = tm.group(2)
            items.append({"indent": len(m.group(1).replace("\t", "    ")),
                          "ol": bool(re.match(r"\d", m.group(2))),
                          "body": task + inline(body)})
        elif items:
            items[-1]["body"] += " " + inline(line.strip())

    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for it in items:
        tag = "ol" if it["ol"] else "ul"
        while stack and stack[-1][0] > it["indent"]:
            out.append("</%s>" % stack.pop()[1])
        if not stack or it["indent"] > stack[-1][0]:
            out.append("<%s>" % tag)
            stack.append((it["indent"], tag))
        elif stack[-1][1] != tag:
            out.append("</%s>" % stack.pop()[1])
            out.append("<%s>" % tag)
            stack.append((it["indent"], tag))
        out.append("<li>" + it["body"] + "</li>")
    while stack:
        out.append("</%s>" % stack.pop()[1])
    return "".join(out)


def render_quote(lines: list[str]) -> str:
    inner = [re.sub(r"^\s*>\s?", "", ln) for ln in lines]
    joined = "\n".join(inner)
    cls = ""
    if re.search(r"⚠️|警告|Warning|注意", joined):
        cls = ' class="warn"'
    elif re.search(r"💡|📌|提示|Tip|Note", joined):
        cls = ' class="tip"'
    body, _ = render_blocks(inner, collect_headings=False)
    return "<blockquote%s>%s</blockquote>" % (cls, body)


def is_block_start(line: str) -> bool:
    return bool(
        FENCE_RE.match(line) or HEAD_RE.match(line) or HR_RE.match(line)
        or LIST_RE.match(line) or HR_RE.match(line) or line.lstrip().startswith(">")
        or (line.lstrip().startswith("|") and TABLE_SEP_RE.match(line))
    )


def render_blocks(lines: list[str], collect_headings: bool = True):
    """Return (html, headings) where headings is a list of (level, text, id)."""
    out: list[str] = []
    headings: list[tuple[int, str, str]] = []
    used_ids: set[str] = set()
    i, n = 0, len(lines)
    para: list[str] = []

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(x.strip() for x in para)) + "</p>")
            para.clear()

    while i < n:
        line = lines[i]

        if not line.strip():
            flush_para()
            i += 1
            continue

        m = FENCE_RE.match(line)
        if m:
            flush_para()
            lang = (m.group(1) or "text").lower()
            i += 1
            code: list[str] = []
            while i < n and not FENCE_RE.match(lines[i]):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            out.append(
                '<pre><code class="lang-' + html.escape(lang, quote=True) + '">'
                + html.escape("\n".join(code), quote=False) + "</code></pre>"
            )
            continue

        m = HEAD_RE.match(line)
        if m:
            flush_para()
            level = len(m.group(1))
            text = m.group(2).strip()
            base = slug(text) or "section"
            anchor, k = base, 1
            while anchor in used_ids:
                k += 1
                anchor = "%s-%d" % (base, k)
            used_ids.add(anchor)
            if collect_headings:
                headings.append((level, text, anchor))
            out.append('<h%d id="%s">%s</h%d>' % (level, anchor, inline(text), level))
            i += 1
            continue

        if HR_RE.match(line):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        if line.lstrip().startswith(">"):
            flush_para()
            q: list[str] = []
            while i < n and (lines[i].lstrip().startswith(">") or
                             (lines[i].strip() and not is_block_start(lines[i]) and q)):
                q.append(lines[i])
                i += 1
            out.append(render_quote(q))
            continue

        if line.lstrip().startswith("|") and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            tbl = [lines[i], lines[i + 1]]
            i += 2
            while i < n and lines[i].lstrip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            out.append(render_table(tbl))
            continue

        if LIST_RE.match(line):
            flush_para()
            lst: list[str] = []
            while i < n and (LIST_RE.match(lines[i]) or
                             (lines[i].strip() and lines[i].startswith((" ", "\t"))
                              and not FENCE_RE.match(lines[i]))):
                lst.append(lines[i])
                i += 1
            out.append(render_list(lst))
            continue

        if is_block_start(line):
            flush_para()
            # A lone table separator or stray marker: skip defensively.
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    return "\n".join(out), headings


# --------------------------------------------------------------------------
# Page assembly
# --------------------------------------------------------------------------

CSS_TEMPLATE = """
  :root{
    --accent:%(accent)s; --accent-light:%(accent-light)s; --accent-dark:%(accent-dark)s;
    --bg:%(bg)s; --card:%(card)s; --border:%(border)s; --text:%(text)s; --muted:%(muted)s;
    --ok:%(ok)s; --warn:%(warn)s; --danger:%(danger)s;
    --code-bg:%(code-bg)s; --kw:%(kw)s; --str:%(str)s; --cmt:%(cmt)s; --num:%(num)s; --fn:%(fn)s; --typ:%(typ)s;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html{scroll-behavior:smooth;scroll-padding-top:24px}
  body{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);line-height:1.75;font-size:15.5px}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  .layout{display:flex;max-width:1280px;margin:0 auto}
  nav.sidebar{width:255px;flex-shrink:0;position:sticky;top:0;height:100vh;overflow-y:auto;padding:28px 14px 40px 22px;border-right:1px solid var(--border)}
  nav.sidebar .logo{font-weight:800;font-size:19px;margin-bottom:4px;color:var(--accent-dark)}
  nav.sidebar .logo .ver{font-size:11px;background:var(--accent-light);color:var(--accent);border-radius:10px;padding:2px 8px;vertical-align:2px;margin-left:6px}
  nav.sidebar .sub{font-size:12px;color:var(--muted);margin-bottom:22px}
  nav.sidebar a{display:block;padding:6px 10px;border-radius:8px;font-size:13.5px;color:var(--muted)}
  nav.sidebar a:hover{background:var(--accent-light);color:var(--accent-dark);text-decoration:none}
  nav.sidebar a.sub-link{padding-left:24px;font-size:12.5px}
  nav.sidebar a.active{background:var(--accent-light);color:var(--accent-dark);font-weight:600}
  main{flex:1;min-width:0;padding:36px 44px 90px;max-width:960px}
  .hero{background:linear-gradient(135deg,%(accent-dark)s 0%%,%(accent)s 58%%,%(accent-dark)s 100%%);color:#fff;border-radius:18px;padding:42px 40px;margin-bottom:34px;box-shadow:0 10px 30px rgba(0,0,0,.12)}
  .hero h1{font-size:30px;margin-bottom:10px}
  .hero p{opacity:.94;font-size:15px;max-width:680px}
  .hero a{color:#fff;text-decoration:underline}
  .hero .meta{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
  .hero .meta span{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.25);padding:3px 12px;border-radius:20px;font-size:12.5px}
  section{margin-bottom:46px}
  h2{font-size:22px;margin-bottom:18px;padding-left:12px;border-left:4px solid var(--accent);scroll-margin-top:20px}
  h3{font-size:17px;margin:24px 0 12px;color:var(--accent-dark);scroll-margin-top:20px}
  h4{font-size:15px;margin:16px 0 8px;color:var(--text)}
  p{margin-bottom:12px}
  ul,ol{padding-left:24px;margin-bottom:12px}
  li{margin-bottom:6px}
  blockquote{border-left:4px solid var(--accent);background:var(--card);border-radius:0 10px 10px 0;padding:10px 18px;margin:14px 0;color:var(--muted);font-size:14.2px}
  blockquote.warn{border-left-color:var(--warn);background:#fffbeb;color:#92400e}
  blockquote.tip{border-left-color:var(--ok);background:#ecfdf5;color:#065f46}
  blockquote p{margin:4px 0}
  hr{border:0;border-top:1px solid var(--border);margin:26px 0}
  pre{background:var(--code-bg);color:#e2e8f0;border-radius:12px;padding:18px 20px;overflow-x:auto;font-size:13.2px;line-height:1.65;margin:14px 0;position:relative}
  pre .codelabel{position:absolute;top:0;right:0;font-size:11px;color:#8fa8cf;background:rgba(255,255,255,.09);padding:2px 12px;border-radius:0 12px 0 10px;user-select:none}
  code{font-family:"JetBrains Mono","Fira Code",Consolas,"Courier New",monospace}
  p code,li code,td code,blockquote code{background:var(--accent-light);color:var(--accent-dark);border:1px solid var(--border);padding:1px 6px;border-radius:6px;font-size:.88em}
  .kw{color:var(--kw)} .str{color:var(--str)} .cmt{color:var(--cmt);font-style:italic} .num{color:var(--num)} .fn{color:var(--fn)} .typ{color:var(--typ)}
  .table-wrap{overflow-x:auto}
  table{width:100%%;border-collapse:collapse;margin:14px 0;background:var(--card);border-radius:12px;overflow:hidden;font-size:13.8px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
  th{background:var(--accent-light);color:var(--accent-dark);text-align:left;padding:10px 14px;font-size:13px}
  td{padding:9px 14px;border-top:1px solid var(--border);vertical-align:top}
  tr:nth-child(even) td{background:rgba(0,0,0,.015)}
  img{max-width:100%%}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px 26px;margin:18px 0}
  .figure{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;margin:18px 0}
  .figure svg{width:100%%;height:auto;display:block}
  .figure figcaption{text-align:center;font-size:12.5px;color:var(--muted);margin-top:12px}
  .features{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:18px 0}
  .feature{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
  .feature .ft,.feature .fh{font-size:14.5px;font-weight:700;color:var(--accent-dark);margin-bottom:8px}
  .feature .fic{font-size:21px}
  .feature p{font-size:12.6px;color:var(--muted);margin:0;line-height:1.62}
  .timeline{position:relative;margin:18px 0;padding-left:32px}
  .timeline::before{content:"";position:absolute;left:9px;top:8px;bottom:8px;width:2px;background:var(--accent-light)}
  .tl{position:relative;margin-bottom:13px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:11px 16px}
  .tl::before{content:"";position:absolute;left:-30px;top:16px;width:11px;height:11px;border-radius:50%%;background:var(--accent);border:2.5px solid #fff;box-shadow:0 0 0 2px var(--accent-light)}
  .tl b{color:var(--accent-dark)}
  .tip,.warn,.danger,.callout{border-radius:12px;padding:14px 18px;margin:14px 0;font-size:14px}
  .tip,.callout.info{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46}
  .warn,.callout.warning{background:#fffbeb;border:1px solid #fde68a;color:#92400e}
  .danger,.callout.danger{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}
  .callout.ok{background:#e6f4ec;border-left:5px solid var(--ok);color:#065f46}
  .callout p{margin:4px 0}
  @media (max-width:760px){ .features{grid-template-columns:1fr} }
  .lvl{display:inline-block;font-size:11px;border-radius:6px;padding:1px 9px;margin-left:8px;vertical-align:2px}
  .lvl.b{background:var(--accent-light);color:var(--accent-dark);border:1px solid var(--border)}
  .lvl.m{background:#fef3c7;color:#b45309;border:1px solid #fde68a}
  .lvl.a{background:#fce7f3;color:#be185d;border:1px solid #fbcfe8}
  footer{margin-top:60px;padding-top:18px;border-top:1px solid var(--border);font-size:12.5px;color:var(--muted);text-align:center}
  @media (max-width:900px){
    nav.sidebar{display:none}
    main{padding:24px 18px 60px}
    .hero{padding:28px 22px}
  }
"""

JS_TEMPLATE = r"""
(function(){
  var goRe = /(\/\/[^\n]*)|(\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\\n])*"|`[^`]*`)|\b(package|import|func|return|if|else|for|range|defer|go|select|case|break|continue|type|struct|interface|map|chan|var|const|switch|default|nil|true|false)\b|\b(make|new|append|len|cap|panic|recover|error|string|int|int64|int32|uint|uint64|bool|byte|rune|float64|any)\b|\b(\d[\d_]*(?:\.\d+)?)\b|\b([a-zA-Z_]\w*)(?=\()/g;
  document.querySelectorAll('pre code.lang-go').forEach(function(b){
    var src = b.textContent;
    b.innerHTML = src.replace(goRe, function(m,c1,c2,s,kw,typ,num,fn){
      if(c1||c2) return '<span class="cmt">'+m+'</span>';
      if(s) return '<span class="str">'+m+'</span>';
      if(kw) return '<span class="kw">'+m+'</span>';
      if(typ) return '<span class="typ">'+m+'</span>';
      if(num) return '<span class="num">'+m+'</span>';
      if(fn) return '<span class="fn">'+m+'</span>';
      return m;
    });
  });
  var shellRe = /(^|\n)(\s*#[^\n]*)|("(?:\\.|[^"\\])*"|'[^']*')|\b(echo|cd|export|go|docker|kubectl|python|pip|curl|sudo|make|git|cat|grep|chmod|systemctl)\b|\$[\w{][\w}]*/g;
  document.querySelectorAll('pre code.lang-bash, pre code.lang-sh, pre code.lang-shell').forEach(function(b){
    b.innerHTML = b.textContent.replace(shellRe, function(m,lead,c,s,kw,v){
      if(c) return lead+'<span class="cmt">'+c+'</span>';
      if(s) return '<span class="str">'+m+'</span>';
      if(kw) return '<span class="kw">'+m+'</span>';
      if(v) return '<span class="num">'+m+'</span>';
      return m;
    });
  });
  var labels = %(labels)s;
  document.querySelectorAll('pre code[class^="lang-"]').forEach(function(b){
    var lang = (b.className.match(/lang-([\w+-]+)/) || [])[1];
    var label = labels[lang] || lang.toUpperCase();
    var s = document.createElement('span');
    s.className = 'codelabel'; s.textContent = label;
    b.closest('pre').appendChild(s);
  });
  var links = Array.prototype.slice.call(document.querySelectorAll('nav.sidebar a[href^="#"]'));
  if(!('IntersectionObserver' in window) || !links.length) return;
  var obs = new IntersectionObserver(function(entries){
    entries.forEach(function(e){
      if(!e.isIntersecting) return;
      links.forEach(function(a){ a.classList.toggle('active', a.getAttribute('href') === '#'+e.target.id); });
    });
  }, {rootMargin:'-10%% 0px -80%% 0px'});
  document.querySelectorAll('h2[id], h3[id]').forEach(function(el){ obs.observe(el); });
})();
"""


def norm_title(s: str) -> str:
    return re.sub(r"[^0-9\w\u4e00-\u9fff]", "", s).lower()


def inject_fragments(sections: list[str], section_meta: list[tuple[str, str]],
                     anchors: set[str], topic: str, fragments: dict,
                     anchor_map: dict) -> None:
    """Place versioned visual blocks back into their chapters.

    Prefer the exact heading anchor; when the original HTML used a hand-edited
    heading (which is itself drift), fall back to matching the h2 chapter text,
    then to the chapter index. Every fragment is guaranteed a home.
    """
    items = fragments.get(topic, [])
    if not items or not sections:
        return
    amap = anchor_map.get(topic, {})

    def rewrite(block: str) -> str:
        return re.sub(r'href="#([^"]+)"',
                      lambda m: 'href="#%s"' % amap.get(m.group(1), m.group(1)),
                      block)

    plans: dict[tuple[int, str, str], list[str]] = {}
    for item in items:
        target = None
        mode = "index"
        anchor = slug(item.get("after", "")) if item.get("after") else ""
        if anchor and anchor in anchors:
            for i, sec in enumerate(sections):
                if re.search(r'<h[23] id="%s"' % re.escape(anchor), sec):
                    target, mode = i, "anchor"
                    break
        if target is None:
            key = norm_title(item.get("h2", ""))
            for i, (plain, _) in enumerate(section_meta):
                p = norm_title(plain)
                if key and p and (key in p or p in key):
                    target, mode = i, "h2"
                    break
        if target is None:
            idx = int(item.get("section", 1) or 1) - 1
            target = min(max(idx, 0), len(sections) - 1)
        plans.setdefault((target, mode, anchor), []).append(rewrite(item["html"]))

    for (target, mode, anchor), blocks in plans.items():
        joined = "\n".join(blocks)
        if mode == "anchor":
            pattern = re.compile(r'(<h[23] id="%s">.*?</h[23]>)' % re.escape(anchor), re.S)
            sections[target] = pattern.sub(lambda m: m.group(1) + joined,
                                           sections[target], count=1)
        else:
            sections[target] = sections[target].replace(
                "</section>", joined + "\n</section>")


def badge_for(text: str, index: int):
    for word, (cls, label) in LEVEL_BADGES.items():
        if "（%s）" % word in text or "(%s)" % word in text:
            return cls, label
    if 1 <= index <= len(SECTION_BADGES):
        return SECTION_BADGES[index - 1]
    return None


def build_page(md_path: Path, topic: str, themes: dict) -> tuple[str, dict]:
    raw = md_path.read_text(encoding="utf-8")
    lines = raw.replace("\r\n", "\n").split("\n")

    # h1 and the opening blockquote drive the hero.
    h1 = ""
    for ln in lines[:10]:
        m = re.match(r"^#\s+(.+)$", ln)
        if m:
            h1 = m.group(1).strip()
            break
    quote_lines = []
    for ln in lines:
        if re.match(r"^#{1,6}\s", ln):
            continue
        if ln.startswith(">"):
            quote_lines.append(re.sub(r"^>\s?", "", ln).strip())
        elif quote_lines and not ln.strip():
            break
        elif quote_lines:
            break
    quote = " ".join(x for x in quote_lines if x)
    quote_plain = re.sub(r"[*`\[\]]|<[^>]+>", "", quote)

    theme = theme_for(topic, themes, h1, quote_plain)
    colors = theme["colors"]

    body_html, headings = render_blocks(lines)

    # Wrap each h2 block in a <section> and attach difficulty badges.
    chunks = re.split(r'(?=<h2 id=")', body_html)
    sections: list[str] = []
    section_meta: list[tuple[str, str]] = []
    prelude: list[str] = []
    sec_index = 0
    for chunk in chunks:
        m = re.match(r'<h2 id="([^"]+)">(.*?)</h2>', chunk, re.S)
        if not m:
            prelude.append(chunk)
            continue
        sec_index += 1
        anchor, inner = m.group(1), m.group(2)
        plain = re.sub(r"<[^>]+>", "", inner)
        badge = badge_for(plain, sec_index)
        if badge:
            chunk = chunk.replace(
                "</h2>", '<span class="lvl %s">%s</span></h2>' % badge, 1)
        sections.append('<section data-anchor="%s">\n%s\n</section>' % (anchor, chunk))
        section_meta.append((plain, anchor))

    inject_fragments(sections, section_meta,
                     {a for _, _, a in headings}, topic,
                     load_fragments(), load_anchor_map())

    # The h1 is shown in the hero; the opening blockquote stays in the body so
    # version/source links are never lost. Markdown remains the full source.
    prelude_html = "\n".join(prelude)
    prelude_html = re.sub(r"<h1[^>]*>.*?</h1>", "", prelude_html, flags=re.S)
    prelude_html = re.sub(r"<hr>", "", prelude_html, count=1)

    # Sidebar TOC: h2 = top level, h3 = indented sub-links.
    toc: list[str] = []
    for level, text, anchor in headings:
        if level == 2:
            toc.append('<a href="#%s">%s</a>' % (anchor, html.escape(re.sub(r"[*`]", "", text))))
        elif level == 3:
            toc.append('<a class="sub-link" href="#%s">%s</a>'
                       % (anchor, html.escape(re.sub(r"[*`]", "", text))))

    title = theme.get("title") or h1 or topic
    ver = theme.get("ver", "")
    subtitle = theme.get("subtitle", "")
    tags = theme.get("tags", [])
    hero_parts = []
    intro = theme.get("intro", "")
    if intro:
        hero_parts.append("<p>" + html.escape(intro, quote=False) + "</p>")
    elif quote:
        hero_parts.append("<p>" + inline(quote) + "</p>")
    if subtitle and subtitle not in quote_plain:
        hero_parts.append('<p style="opacity:.8;font-size:13.5px">'
                          + html.escape(subtitle, quote=False) + "</p>")
    if tags:
        hero_parts.append('<div class="meta">'
                          + "".join("<span>" + html.escape(t, quote=False) + "</span>" for t in tags)
                          + "</div>")

    css = CSS_TEMPLATE % colors
    js = JS_TEMPLATE % {"labels": json.dumps(LANG_LABELS, ensure_ascii=False)}

    doc = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>%(title)s · 技术指南 | open-guide</title>
<meta name="generator" content="open-guide/tools/build_html.py">
<style>%(css)s</style>
</head>
<body>
<div class="layout">
<nav class="sidebar">
  <div class="logo">%(title)s%(ver)s</div>
  <div class="sub">%(subtitle)s</div>
%(toc)s
</nav>
<main>
<div class="hero">
  <h1>%(h1)s</h1>
%(hero)s
</div>
%(prelude)s
%(sections)s
<footer>由 <code>tools/build_html.py</code> 从同名 Markdown 生成 · Markdown 为唯一真源 · open-guide</footer>
</main>
</div>
<script>%(js)s</script>
</body>
</html>
""" % {
        "title": html.escape(title, quote=False),
        "ver": ('<span class="ver">%s</span>' % html.escape(ver, quote=False)) if ver else "",
        "subtitle": html.escape(subtitle, quote=False),
        "h1": html.escape(h1 or title, quote=False),
        "hero": "\n".join(hero_parts),
        "toc": "\n".join("  " + a for a in toc),
        "prelude": prelude_html.strip(),
        "sections": "\n".join(sections),
        "css": css,
        "js": js,
    }

    stats = {
        "topic": topic,
        "h2": sum(1 for lv, _, _ in headings if lv == 2),
        "h3": sum(1 for lv, _, _ in headings if lv == 3),
        "code": len(re.findall(r"^```", raw, re.M)) // 2,
        "tables": len(re.findall(r"^\|[\s:|-]+\|$", raw, re.M)),
        "anchors": [a for _, _, a in headings],
        "internal_links": re.findall(r"\]\(#([^)]+)\)", raw),
        "titles": [text for _, text, _ in headings],
        "lead": "\n".join(lines[:10]),
        "lines": len(lines),
    }
    return doc, stats


# --------------------------------------------------------------------------
# Required-element audit (see README section 5)
# --------------------------------------------------------------------------

ELEMENT_PATTERNS = [
    ("E1 定位",   r"简介|是什么|为什么需要|什么是|概述|定义"),
    ("E2 能力",   r"特性总览|能力总览|功能特性|功能分类|数据模型|核心概念|技术栈|类型约束|三定律"),
    ("E3 原理",   r"原理|架构|实现|设计|核心概念|进阶|reflect\.|存储与查询|概念与任务"),
    ("E4 实战",   r"实战|使用方法|完整可运行|功能详解|部署指南|综合实战|动态调用|结构体"),
    ("E5 版本",   r"版本|演进|变化|产品线|v2 → v3|4\.0"),
    ("E6 坑",     r"常见坑|最佳实践|注意事项|陷阱|限制与常见坑|性能与"),
    ("E7 选型",   r"选型|对比|如何取舍|如何选择|学习路线|关系"),
]


def audit_elements(st: dict) -> list[str]:
    titles = " ".join(st["titles"])
    missing = []
    for name, pattern in ELEMENT_PATTERNS:
        ok = bool(re.search(pattern, titles, re.I))
        # Element 5 may also be satisfied by the opening version blockquote
        # (language-feature tutorials have no upstream release).
        if name.startswith("E5") and not ok:
            ok = bool(re.search(r"v?\d+\.\d+|Go 1\.\d+|主线版本|对应版本", st["lead"]))
        if not ok:
            missing.append(name)
    return missing


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def discover() -> list[Path]:
    return sorted(p for p in ROOT.glob("*/*.md")
                  if p.name.lower() not in ("readme.md", "readme_en.md"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Render open-guide Markdown into single-file HTML.")
    ap.add_argument("--topic", help="only build this topic directory name")
    ap.add_argument("--check", action="store_true", help="fail if any HTML is out of date (CI)")
    ap.add_argument("--stats", action="store_true", help="print structure statistics")
    ap.add_argument("--audit", action="store_true",
                    help="check the 7 required elements; fail on any missing element")
    args = ap.parse_args()

    themes = load_themes()
    paths = discover()
    if args.topic:
        paths = [p for p in paths if p.parent.name == args.topic]
        if not paths:
            print("topic not found: %s" % args.topic, file=sys.stderr)
            return 2

    stale: list[str] = []
    stats_rows: list[dict] = []
    for md in paths:
        topic = md.parent.name
        doc, st = build_page(md, topic, themes)
        target = md.with_suffix(".html")
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if args.check:
            if current != doc:
                stale.append(topic)
        else:
            if current != doc:
                target.write_text(doc, encoding="utf-8", newline="\n")
                print("  built  %s" % target.relative_to(ROOT).as_posix())
            else:
                print("  same   %s" % target.relative_to(ROOT).as_posix())

        broken = [a for a in st["internal_links"] if a not in st["anchors"]]
        st["broken"] = broken
        st["missing"] = audit_elements(st)
        stats_rows.append(st)

    audit_missing = [(st["topic"], st["missing"]) for st in stats_rows if st["missing"]]
    if args.audit:
        print("\n=== Required-element audit (README section 5) ===")
        names = [n.split()[0] for n, _ in ELEMENT_PATTERNS]
        print("%-16s %s" % ("topic", " ".join("%-5s" % n for n in names)))
        for st in stats_rows:
            cells = ["ok" if not any(m.startswith(n + " ") for m in st["missing"])
                     else "MISS" for n in names]
            print("%-16s %s" % (st["topic"], " ".join("%-5s" % c for c in cells)))
        if audit_missing:
            print("\nMissing elements:")
            for topic, miss in audit_missing:
                print("  %-16s %s" % (topic, ", ".join(miss)))
        else:
            print("\nOK: all guides cover the 7 required elements.")

    if args.stats or args.check:
        print("\n%-16s %4s %4s %5s %6s %7s" % ("topic", "h2", "h3", "code", "tables", "broken"))
        for st in stats_rows:
            print("%-16s %4d %4d %5d %6d %7s"
                  % (st["topic"], st["h2"], st["h3"], st["code"], st["tables"],
                     ",".join(st["broken"]) if st["broken"] else "-"))

    if args.check:
        if stale:
            print("\nStale HTML (run `python tools/build_html.py`): %s" % ", ".join(stale),
                  file=sys.stderr)
        else:
            print("\nOK: all HTML files match their Markdown source.")

    if not args.check:
        total_lines = sum(st["lines"] for st in stats_rows)
        print("\n%d guides · %d Markdown lines · Markdown is the single source of truth"
              % (len(stats_rows), total_lines))

    bad = [(st["topic"], st["broken"]) for st in stats_rows if st["broken"]]
    if bad:
        print("Warning: unresolved internal anchors: %s" % bad, file=sys.stderr)
    return 1 if (stale or audit_missing) else 0


if __name__ == "__main__":
    sys.exit(main())
