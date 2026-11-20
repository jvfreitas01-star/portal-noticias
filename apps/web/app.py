"""Portal de notícias — etapa 1: RSS → página de lista.

Abrir a página nunca baixa nada: uma thread em segundo plano coleta os feeds
a cada REFRESH_MINUTES e a página só lê o cache.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from portal.core import FEEDS, collect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("portal.web")

REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))
HOST = os.environ.get("WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_PORT", "8765"))

app = Flask(__name__)
cache = {"items": None, "updated": None, "refreshing": True}
lock = threading.Lock()


def refresh_loop() -> None:
    while True:
        with lock:
            cache["refreshing"] = True
        try:
            items = collect()
            with lock:
                cache.update(items=items, updated=datetime.now(timezone.utc).isoformat())
        except Exception:
            log.exception("falha na coleta; mantendo o cache anterior")
        finally:
            with lock:
                cache["refreshing"] = False
        time.sleep(REFRESH_MINUTES * 60)


@app.get("/api/items")
def api_items():
    with lock:
        if cache["items"] is None:  # primeira coleta ainda não terminou
            return jsonify({"error": "carregando"}), 503
        return jsonify({"items": cache["items"], "updated": cache["updated"],
                        "refreshing": cache["refreshing"]})


@app.get("/")
def index():
    return PAGE.replace("__SOURCES__", "|".join(f[0] for f in FEEDS))


PAGE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portal</title>
<style>
  :root { --bg:#fafaf8; --fg:#1d1d1b; --muted:#6b6b66; --line:#e4e3de; --accent:#0b5cad; --chip:#efeee9; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#161615; --fg:#ecebe6; --muted:#9a9993; --line:#2c2c2a; --accent:#6aa9ec; --chip:#242422; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width:780px; margin:0 auto; padding:20px 16px 60px; }
  header { display:flex; align-items:baseline; justify-content:space-between; gap:12px; flex-wrap:wrap; }
  h1 { font-size:22px; margin:0; }
  #status { color:var(--muted); font-size:13px; }
  #q { width:100%; margin:14px 0 10px; padding:9px 12px; font:inherit; color:inherit;
       background:transparent; border:1px solid var(--line); border-radius:8px; }
  #chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:8px; }
  .chip { border:0; background:var(--chip); color:inherit; font:inherit; font-size:13px;
          padding:4px 10px; border-radius:999px; cursor:pointer; }
  .chip.on { background:var(--accent); color:#fff; }
  ul { list-style:none; padding:0; margin:0; }
  li { padding:12px 0; border-bottom:1px solid var(--line); }
  li a { color:inherit; text-decoration:none; font-weight:600; }
  li a:hover { color:var(--accent); }
  .meta { color:var(--muted); font-size:13px; margin-top:2px; }
  .sum { color:var(--muted); font-size:14px; margin-top:4px;
         display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .empty { color:var(--muted); padding:30px 0; }
</style>
</head>
<body>
<main>
  <header><h1>Portal</h1><span id="status">carregando…</span></header>
  <input id="q" type="search" placeholder="Filtrar manchetes (ex.: concessão, debênture, STF)">
  <div id="chips"></div>
  <ul id="list"></ul>
</main>
<script>
const SOURCES = "__SOURCES__".split("|");
let items = [], active = new Set();

const fmt = iso => iso ? new Date(iso).toLocaleString("pt-BR",
  {day:"2-digit", month:"2-digit", hour:"2-digit", minute:"2-digit"}) : "";
const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const norm = s => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

function chips() {
  const counts = {};
  items.forEach(i => counts[i.source] = (counts[i.source] || 0) + 1);
  document.getElementById("chips").innerHTML = SOURCES.map(s =>
    `<button class="chip${active.has(s) ? " on" : ""}" data-s="${esc(s)}">${esc(s)} ${counts[s] || 0}</button>`).join("");
}

function render() {
  const q = norm(document.getElementById("q").value.trim());
  const shown = items.filter(i => (!active.size || active.has(i.source)) &&
    (!q || norm(i.title + " " + i.summary).includes(q)));
  document.getElementById("list").innerHTML = shown.length ? shown.map(i => `
    <li><a href="${esc(i.link)}" target="_blank" rel="noopener">${esc(i.title)}</a>
      <div class="meta">${esc(i.source)} · ${fmt(i.published)}</div>
      ${i.summary ? `<div class="sum">${esc(i.summary)}</div>` : ""}</li>`).join("")
    : `<div class="empty">Nenhuma manchete.</div>`;
}

async function load() {
  const r = await fetch("/api/items");
  if (r.status === 503) { setTimeout(load, 3000); return; }  // primeira coleta em andamento
  const d = await r.json();
  items = d.items;
  document.getElementById("status").textContent =
    `${items.length} manchetes · atualizado ${fmt(d.updated)}`;
  chips(); render();
}

document.getElementById("chips").addEventListener("click", e => {
  const s = e.target.dataset.s; if (!s) return;
  active.has(s) ? active.delete(s) : active.add(s);
  chips(); render();
});
document.getElementById("q").addEventListener("input", render);
load();
setInterval(load, 5 * 60 * 1000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    log.info("abrindo em http://%s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False)
