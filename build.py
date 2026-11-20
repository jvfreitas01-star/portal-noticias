"""Execução diária: coleta → LLM → gera site/index.html (página estática).

Rodar local:  .venv/Scripts/python build.py
No GitHub Actions roda sozinho 1x por dia (ver .github/workflows/daily.yml).
"""
from __future__ import annotations

import html
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from portal.core import collect  # noqa: E402
from portal.judge import judge_stories, load_brief, sections  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("portal.build")

SITE = ROOT / "site"
BRT = timezone(timedelta(hours=-3))
MAX_SHOWN = 30      # teto de histórias acima do corte, no total
MAX_MORE = 25       # teto de histórias em "mostrar mais", por seção


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def hhmm(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(BRT).strftime("%d/%m %H:%M")


def story_html(s: dict) -> str:
    lead, rest = s["items"][0], s["items"][1:]
    score = "" if s["score"] is None else f'<span class="score">{s["score"]}</span>'
    why = f' · <span class="why">{esc(s["why"])}</span>' if s.get("why") else ""
    more = ""
    if rest:
        rows = "".join(f'<li><a href="{esc(i["link"])}" target="_blank" rel="noopener">{esc(i["title"])}</a>'
                       f' <span class="meta">{esc(i["source"])}</span></li>' for i in rest)
        more = f'<details class="also"><summary>+{len(rest)} fonte(s)</summary><ul>{rows}</ul></details>'
    summary = f'<div class="sum">{esc(lead["summary"])}</div>' if lead["summary"] else ""
    return (f'<article>{score}<a class="t" href="{esc(lead["link"])}" target="_blank" rel="noopener">'
            f'{esc(lead["title"])}</a><div class="meta">{esc(lead["source"])} · {hhmm(lead["published"])}{why}</div>'
            f'{summary}{more}</article>')


def render(stories: list[dict], mode: str, n_items: int) -> str:
    secs = sections(load_brief()) + [("0", "Outros")]
    now = datetime.now(BRT)
    # Teto global pelas MAIORES notas (não por ordem de seção, senão a última seção fica vazia).
    passing = [s for s in stories if s["pass"]]
    if mode == "ai":
        keep = {id(s) for s in sorted(passing, key=lambda s: -(s["score"] or 0))[:MAX_SHOWN]}
    else:  # sem LLM: até 8 por seção, para nenhuma engolir as outras
        keep = {id(s) for sid, _ in secs for s in [x for x in passing if x["sec"] == sid][:8]}
    body, shown = [], 0
    for sid, name in secs:
        mine = [s for s in stories if s["sec"] == sid]
        top = [s for s in mine if id(s) in keep]
        shown += len(top)
        rest = [s for s in mine if s not in top][:MAX_MORE]
        if not top and not rest:
            continue
        part = [f'<section><h2>{esc(name)} <span class="n">{len(top)}</span></h2>']
        part += [story_html(s) for s in top] or ['<p class="empty">Nada acima do corte hoje.</p>']
        if rest:
            part.append(f'<details class="more"><summary>Mostrar mais ({len(rest)})</summary>'
                        + "".join(story_html(s) for s in rest) + "</details>")
        part.append("</section>")
        body.append("".join(part))

    badge = ("" if mode == "ai" else
             '<p class="warn">A LLM não respondeu nesta execução: seleção feita só por palavras-chave.</p>')
    return PAGE.format(date=now.strftime("%d/%m/%Y"), time=now.strftime("%H:%M"),
                       n=n_items, s=shown, badge=badge, body="".join(body))


PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Portal · {date}</title>
<style>
:root {{ --bg:#fafaf8; --fg:#1d1d1b; --muted:#6b6b66; --line:#e4e3de; --accent:#0b5cad; --chip:#efeee9; --warn:#8a5a00; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161615; --fg:#ecebe6; --muted:#9a9993; --line:#2c2c2a; --accent:#6aa9ec; --chip:#242422; --warn:#e0b25a; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:780px; margin:0 auto; padding:20px 16px 60px; }}
header {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; }}
h1 {{ font-size:22px; margin:0; }}
.st {{ color:var(--muted); font-size:13px; }}
h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.04em; margin:28px 0 4px;
      padding-bottom:6px; border-bottom:2px solid var(--fg); }}
h2 .n {{ color:var(--muted); font-weight:400; }}
article {{ padding:12px 0; border-bottom:1px solid var(--line); }}
a.t {{ color:inherit; text-decoration:none; font-weight:600; }}
a.t:hover, .also a:hover {{ color:var(--accent); }}
.score {{ float:right; margin-left:10px; font-size:12px; color:var(--muted); background:var(--chip);
          padding:1px 7px; border-radius:999px; }}
.meta {{ color:var(--muted); font-size:13px; margin-top:2px; }}
.why {{ font-style:italic; }}
.sum {{ color:var(--muted); font-size:14px; margin-top:4px; display:-webkit-box; -webkit-line-clamp:2;
        -webkit-box-orient:vertical; overflow:hidden; }}
details summary {{ cursor:pointer; color:var(--accent); font-size:13px; margin-top:6px; }}
.also ul {{ margin:6px 0 0; padding-left:18px; font-size:14px; }}
.also a {{ color:inherit; }}
.more > summary {{ margin:12px 0; font-size:14px; }}
.more article {{ opacity:.85; }}
.empty {{ color:var(--muted); font-size:14px; }}
.warn {{ color:var(--warn); font-size:14px; }}
</style></head>
<body><main>
<header><h1>Portal · {date}</h1><span class="st">{s} histórias de {n} manchetes · gerado às {time}</span></header>
{badge}
{body}
</main></body></html>
"""


def main() -> None:
    items = collect()
    stories, mode = judge_stories(items)
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(render(stories, mode, len(items)), encoding="utf-8")
    (SITE / "data.json").write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                                                "mode": mode, "stories": stories}, ensure_ascii=False),
                                    encoding="utf-8")
    log.info("site/index.html gerado")


if __name__ == "__main__":
    main()
