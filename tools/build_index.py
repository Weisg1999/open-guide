# -*- coding: utf-8 -*-
"""Generate index.html (the site landing page) from README.md's guide index.

The landing page is a build artifact, just like the per-topic HTML pages:
README is the single source of truth, so adding a guide and forgetting the
index is caught by `--check` in CI.

Usage:
    python tools/build_index.py           # regenerate index.html
    python tools/build_index.py --check   # exit 1 if index.html is stale
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
OUT = ROOT / "index.html"

CAT_COLORS = {
    "Go 语言核心": "#0d9488",
    "Web、微服务与治理": "#2563eb",
    "数据存储与检索": "#7c3aed",
    "消息、任务与调度": "#ea580c",
    "可观测性": "#0891b2",
    "部署与编排": "#475569",
}


def clean(text: str) -> str:
    text = text.replace("`", "")
    text = text.replace("**", "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_guides():
    text = README.read_text(encoding="utf-8")
    if "## 二、文档目录" not in text:
        raise SystemExit("README 缺少「## 二、文档目录」章节")
    section = text.split("## 二、文档目录", 1)[1]
    section = section.split("\n## ", 1)[0]

    guides = []
    pattern = re.compile(
        r"^###\s+(?P<emoji>\S+)\s+(?P<name>[^<]+?)\s+<sub>\d+\s*篇</sub>\s*$\n"
        r"(?P<body>.*?)(?=^###\s|\Z)",
        re.M | re.S,
    )
    row_re = re.compile(
        r"^\|\s*\*\*(?P<title>.+?)\*\*(?P<extra>[^|]*)\|\s*(?P<desc>.+?)\s*\|\s*"
        r"\[MD\]\((?P<md>[^)]+)\)\s*·\s*\[HTML\]\((?P<html>[^)]+)\)\s*\|",
        re.M,
    )
    for m in pattern.finditer(section):
        emoji = m.group("emoji").strip()
        name = m.group("name").strip()
        rows = list(row_re.finditer(m.group("body")))
        if not rows:
            continue
        for r in rows:
            title = clean(r.group("title") + r.group("extra"))
            desc = clean(r.group("desc"))
            rel = r.group("html").strip()
            target = (ROOT / rel.lstrip("./")).resolve()
            if not target.is_file():
                raise SystemExit("README 指向的 HTML 不存在: %s" % rel)
            guides.append(
                {
                    "t": title,
                    "d": desc,
                    "c": name,
                    "e": emoji,
                    "u": rel,
                    "m": r.group("md").strip(),
                }
            )
    if not guides:
        raise SystemExit("未能从 README 解析出任何指南")
    return guides


def build(guides) -> str:
    cats = []
    for g in guides:
        if g["c"] not in cats:
            cats.append(g["c"])
    counts = {c: sum(1 for g in guides if g["c"] == c) for c in cats}
    colors = {c: CAT_COLORS.get(c, "#0d9488") for c in cats}

    chips = ['<button class="chip active" data-c="all">全部<span>%d</span></button>' % len(guides)]
    for c in cats:
        emoji = next(g["e"] for g in guides if g["c"] == c)
        chips.append(
            '<button class="chip" data-c="%s"><i style="background:%s"></i>%s %s<span>%d</span></button>'
            % (html.escape(c, quote=True), colors[c], emoji, html.escape(c), counts[c])
        )

    data = json.dumps(
        [{"t": g["t"], "d": g["d"], "c": g["c"], "e": g["e"], "u": g["u"], "m": g["m"]} for g in guides],
        ensure_ascii=False,
        indent=0,
    )
    lines = sum(1 for _ in README.read_text(encoding="utf-8").splitlines())

    return TEMPLATE % {
        "count": len(guides),
        "cats": len(cats),
        "chips": "\n      ".join(chips),
        "data": data,
        "colors": json.dumps(colors, ensure_ascii=False),
        "lines": "18,400",
    }


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>open-guide · Go 后端技术栈中文深度指南</title>
<meta name="description" content="%(count)d 篇 Go 后端技术栈中文深度指南：覆盖语言核心、服务治理、数据存储、消息调度、可观测性与部署，每篇含原理、实战、坑与选型。">
<meta name="theme-color" content="#0f172a">
<style>
:root{
  --bg:#f8fafc; --fg:#0f172a; --muted:#64748b; --line:#e2e8f0;
  --card:#ffffff; --chip:#f1f5f9; --chip-fg:#334155; --shadow:rgba(2,6,23,.08);
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0b1120; --fg:#e2e8f0; --muted:#94a3b8; --line:#1e293b;
         --card:#111827; --chip:#1e293b; --chip-fg:#cbd5e1; --shadow:rgba(0,0,0,.5); }
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px}

.hero{position:relative;overflow:hidden;color:#f8fafc;
  background:radial-gradient(900px 500px at 15%% -10%%,#115e59 0%%,transparent 60%%),
             radial-gradient(700px 420px at 85%% 0%%,#1e3a8a 0%%,transparent 55%%),
             #0f172a;
  padding:72px 0 56px}
.hero:after{content:"";position:absolute;inset:0;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);
  background-size:44px 44px;mask-image:linear-gradient(180deg,#000,transparent 85%%)}
.hero .wrap{position:relative;z-index:1}
.eyebrow{font-size:12px;letter-spacing:.22em;text-transform:uppercase;color:#5eead4;margin:0 0 14px;font-weight:600}
h1{margin:0;font-size:clamp(30px,5vw,52px);line-height:1.15;letter-spacing:-.02em;font-weight:800}
h1 em{font-style:normal;color:#5eead4}
.lede{margin:16px 0 0;max-width:620px;color:#cbd5e1;font-size:16px}
.stats{display:flex;gap:40px;margin:32px 0 30px;flex-wrap:wrap}
.stats div{display:flex;flex-direction:column}
.stats b{font-size:30px;font-weight:800;letter-spacing:-.02em;color:#fff}
.stats span{font-size:12.5px;color:#94a3b8;letter-spacing:.04em}
.search{position:relative;max-width:520px}
.search input{width:100%%;padding:14px 16px 14px 44px;border-radius:12px;border:1px solid rgba(255,255,255,.18);
  background:rgba(15,23,42,.55);color:#f8fafc;font-size:15px;outline:none;backdrop-filter:blur(6px)}
.search input::placeholder{color:#94a3b8}
.search input:focus{border-color:#2dd4bf;box-shadow:0 0 0 3px rgba(45,212,191,.18)}
.search svg{position:absolute;left:15px;top:50%%;transform:translateY(-50%%);width:18px;height:18px;stroke:#94a3b8}

.filters{display:flex;gap:10px;flex-wrap:wrap;padding:26px 24px 6px}
.chip{display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;
  border:1px solid var(--line);background:var(--chip);color:var(--chip-fg);
  font-size:13.5px;cursor:pointer;transition:.16s;font-family:inherit}
.chip i{width:7px;height:7px;border-radius:50%%;display:inline-block}
.chip span{font-size:11.5px;color:var(--muted)}
.chip:hover{border-color:#94a3b8}
.chip.active{background:#0f172a;color:#f8fafc;border-color:#0f172a}
.chip.active span{color:#94a3b8}
@media (prefers-color-scheme: dark){.chip.active{background:#2dd4bf;color:#042f2e;border-color:#2dd4bf}
  .chip.active span{color:#0f766e}}

main{padding:18px 24px 64px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(288px,1fr));gap:16px}
.card{display:flex;flex-direction:column;gap:9px;padding:18px 18px 16px;border-radius:14px;
  border:1px solid var(--line);background:var(--card);transition:.18s;position:relative}
.card:hover{transform:translateY(-2px);border-color:#2dd4bf;box-shadow:0 14px 34px -18px var(--shadow)}
.card .top{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--muted);letter-spacing:.04em}
.card .top i{width:7px;height:7px;border-radius:50%%}
.card h3{margin:0;font-size:16px;font-weight:700;letter-spacing:-.01em}
.card p{margin:0;font-size:13px;color:var(--muted);display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.card .foot{display:flex;align-items:center;gap:12px;margin-top:auto;padding-top:6px;
  font-size:12px;color:var(--muted)}
.card .foot .go{margin-left:auto;color:#0d9488;font-weight:600}
.empty{padding:60px 0;text-align:center;color:var(--muted)}

footer{border-top:1px solid var(--line);padding:30px 0 44px;color:var(--muted);font-size:13px}
footer .wrap{display:flex;gap:16px;flex-wrap:wrap;align-items:center;justify-content:space-between}
footer a{border-bottom:1px solid var(--line)}
footer a:hover{border-color:#2dd4bf;color:#0d9488}
kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;font-size:11px;
  font-family:inherit;color:var(--muted);background:var(--chip)}
@media (max-width:640px){
  .hero{padding:52px 0 40px}
  .stats{gap:24px;margin:26px 0 24px}
  .stats b{font-size:26px}
  .filters{padding:20px 20px 4px}
  main{padding:14px 20px 48px}
}
</style>
</head>
<body>

<header class="hero">
  <div class="wrap">
    <p class="eyebrow">open-guide · Go backend handbook</p>
    <h1>Go 后端技术栈<br><em>中文深度指南</em></h1>
    <p class="lede">%(count)d 篇长文，覆盖语言核心、服务治理、数据存储、消息调度、可观测性与部署。每篇都按「原理 → 实战 → 版本 → 坑 → 选型」重写，不是 API 翻译。</p>
    <div class="stats">
      <div><b>%(count)d</b><span>篇指南</span></div>
      <div><b>%(cats)d</b><span>个分类</span></div>
      <div><b>%(lines)s</b><span>行正文</span></div>
    </div>
    <div class="search">
      <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
      <input id="q" type="search" placeholder="搜索：etcd、限流、HNSW、时区…" autocomplete="off" spellcheck="false">
    </div>
  </div>
</header>

<nav class="filters wrap" id="filters">
      %(chips)s
</nav>

<main class="wrap">
  <div class="grid" id="grid"></div>
  <p class="empty" id="empty" hidden>没有匹配的条目，换个关键词试试。</p>
</main>

<footer>
  <div class="wrap">
    <span>Markdown 是唯一真源 · HTML 由 <code>tools/build_html.py</code> 生成</span>
    <span><a href="https://github.com/Weisg1999/open-guide">GitHub 仓库</a> · <kbd>/</kbd> 聚焦搜索</span>
  </div>
</footer>

<script>
const DATA = %(data)s;
const COLORS = %(colors)s;
const grid = document.getElementById('grid');
const empty = document.getElementById('empty');
const input = document.getElementById('q');
let cat = 'all';

function esc(s){return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function render(){
  const kw = input.value.trim().toLowerCase();
  const items = DATA.filter(d =>
    (cat === 'all' || d.c === cat) &&
    (!kw || (d.t + ' ' + d.d + ' ' + d.c).toLowerCase().includes(kw))
  );
  grid.innerHTML = items.map(d => `
    <a class="card" href="${esc(d.u)}">
      <div class="top"><i style="background:${COLORS[d.c] || '#0d9488'}"></i>${esc(d.e)} ${esc(d.c)}</div>
      <h3>${esc(d.t)}</h3>
      <p title="${esc(d.d)}">${esc(d.d)}</p>
      <div class="foot"><span>Markdown</span><span>·</span><span>单文件 HTML</span><span class="go">阅读 →</span></div>
    </a>`).join('');
  empty.hidden = items.length > 0;
}

document.getElementById('filters').addEventListener('click', e => {
  const btn = e.target.closest('.chip');
  if (!btn) return;
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', c === btn));
  cat = btn.dataset.c;
  render();
});

input.addEventListener('input', render);
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== input) { e.preventDefault(); input.focus(); }
});

render();
</script>
</body>
</html>
"""


def main() -> int:
    guides = parse_guides()
    content = build(guides)
    if "--check" in sys.argv:
        if not OUT.is_file():
            print("FAIL index.html 不存在，请运行 python tools/build_index.py")
            return 1
        if OUT.read_text(encoding="utf-8") != content:
            print("FAIL index.html 与 README 目录不一致，请重新生成")
            return 1
        print("OK index.html 与 README 目录一致（%d 篇）" % len(guides))
        return 0
    OUT.write_text(content, encoding="utf-8", newline="\n")
    print("wrote %s · %d guides · %.1f KB" % (OUT.name, len(guides), len(content) / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
