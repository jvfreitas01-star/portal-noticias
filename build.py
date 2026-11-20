"""Execução diária: coleta → LLM → gera site/index.html (página estática).

Rodar local:  .venv/Scripts/python build.py
No GitHub Actions roda sozinho 1x por dia (ver .github/workflows/daily.yml).
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from portal.content import fetch_for_stories  # noqa: E402
from portal.core import collect  # noqa: E402
from portal.judge import judge_stories, load_brief, sections  # noqa: E402
from portal.vault import article_id, seal  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("portal.build")

SITE = ROOT / "site"
BRT = timezone(timedelta(hours=-3))
MAX_SHOWN = 30      # teto de histórias acima do corte, no total
MAX_MORE = 25       # teto de histórias em "mostrar mais", por seção
REPO = os.environ.get("GITHUB_REPOSITORY", "jvfreitas01-star/portal-noticias")
LABELS_PATH = "labels/labels.jsonl"
READER_PASSWORD = os.environ.get("READER_PASSWORD", "")  # vazio = sem leitor (títulos abrem o site)


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def hhmm(iso: str | None) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(BRT).strftime("%d/%m %H:%M")


def story_html(s: dict, k: int, texts: dict[str, str]) -> str:
    lead, rest = s["items"][0], s["items"][1:]
    aid = next((texts[i["link"]] for i in s["items"] if i["link"] in texts), None)
    read = f' data-r="{aid}"' if aid else ""
    badge = ' · <span class="rd">📖 texto completo</span>' if aid else ""
    votes = (f'<span class="vote" data-k="{k}"><button data-v="YES" title="Quero mais assim">👍</button>'
             f'<button data-v="NO" title="Não quero isto">👎</button></span>')
    score = "" if s["score"] is None else f'<span class="score">{s["score"]}</span>'
    why = f' · <span class="why">{esc(s["why"])}</span>' if s.get("why") else ""
    more = ""
    if rest:
        rows = "".join(f'<li><a href="{esc(i["link"])}" target="_blank" rel="noopener">{esc(i["title"])}</a>'
                       f' <span class="meta">{esc(i["source"])}</span></li>' for i in rest)
        more = f'<details class="also"><summary>+{len(rest)} fonte(s)</summary><ul>{rows}</ul></details>'
    summary = f'<div class="sum">{esc(lead["summary"])}</div>' if lead["summary"] else ""
    return (f'<article>{score}<a class="t" href="{esc(lead["link"])}" target="_blank" rel="noopener"{read}>'
            f'{esc(lead["title"])}</a><div class="meta">{esc(lead["source"])} · {hhmm(lead["published"])}{why}{badge}</div>'
            f'{summary}{more}{votes}</article>')


def top_stories(stories: list[dict], mode: str) -> list[dict]:
    """As histórias que aparecem acima do corte."""
    secs = sections(load_brief()) + [("0", "Outros")]
    passing = [s for s in stories if s["pass"]]
    if mode == "ai":  # teto global pelas MAIORES notas (por seção, a última ficaria vazia)
        return sorted(passing, key=lambda s: -(s["score"] or 0))[:MAX_SHOWN]
    # sem LLM: até 8 por seção, para nenhuma engolir as outras
    return [s for sid, _ in secs for s in [x for x in passing if x["sec"] == sid][:8]]


def render(stories: list[dict], mode: str, n_items: int, texts: dict[str, str] | None = None) -> str:
    secs = sections(load_brief()) + [("0", "Outros")]
    now = datetime.now(BRT)
    texts = texts or {}
    keep = {id(s) for s in top_stories(stories, mode)}
    body, shown, rendered = [], 0, []

    def one(s: dict) -> str:
        rendered.append([{"link": i["link"], "title": i["title"], "source": i["source"],
                          "summary": i["summary"][:200]} for i in s["items"]])
        return story_html(s, len(rendered) - 1, texts)

    for sid, name in secs:
        mine = [s for s in stories if s["sec"] == sid]
        top = [s for s in mine if id(s) in keep]
        shown += len(top)
        rest = [s for s in mine if s not in top][:MAX_MORE]
        if not top and not rest:
            continue
        part = [f'<section><h2>{esc(name)} <span class="n">{len(top)}</span></h2>']
        part += [one(s) for s in top] or ([] if sid == "0" else ['<p class="empty">Nada acima do corte hoje.</p>'])
        if rest:
            part.append(f'<details class="more"><summary>Mostrar mais ({len(rest)})</summary>'
                        + "".join(one(s) for s in rest) + "</details>")
        part.append("</section>")
        body.append("".join(part))

    badge = ("" if mode == "ai" else
             '<p class="warn">A LLM não respondeu nesta execução: seleção feita só por palavras-chave.</p>')
    data = json.dumps({"repo": REPO, "path": LABELS_PATH, "stories": rendered},
                      ensure_ascii=False).replace("</", "<\\/")
    return PAGE.format(date=now.strftime("%d/%m/%Y"), time=now.strftime("%H:%M"),
                       n=n_items, s=shown, badge=badge, body="".join(body)
                       ).replace("__DATA__", data).replace("__VOTE_JS__", VOTE_JS).replace("__READER_JS__", READER_JS)


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
.vote {{ display:flex; gap:6px; margin-top:8px; }}
.vote button {{ font-size:14px; line-height:1; padding:5px 10px; border-radius:999px; cursor:pointer;
               background:transparent; border:1px solid var(--line); color:inherit; filter:grayscale(1); opacity:.6; }}
.vote button.on {{ filter:none; opacity:1; border-color:var(--accent); background:var(--chip); }}
article.no {{ opacity:.45; }}
#sync {{ position:sticky; bottom:0; background:var(--bg); border-top:1px solid var(--line);
         padding:8px 0; font-size:13px; color:var(--muted); display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
#sync:empty {{ display:none; }}
#sync button, footer button {{ font:inherit; font-size:13px; color:var(--accent); background:none; border:0;
                               padding:0; cursor:pointer; text-decoration:underline; }}
footer {{ margin-top:40px; font-size:13px; color:var(--muted); }}
.rd {{ color:var(--accent); }}
#reader {{ position:fixed; inset:0; z-index:10; overflow-y:auto; background:var(--bg); }}
#reader[hidden] {{ display:none; }}
.rbar {{ position:sticky; top:0; display:flex; justify-content:space-between; align-items:center; gap:12px;
         max-width:700px; margin:0 auto; padding:10px 16px; background:var(--bg); border-bottom:1px solid var(--line); }}
.rbar button, .rbar a {{ font:inherit; font-size:14px; color:var(--accent); background:none; border:0; padding:0;
                         cursor:pointer; text-decoration:none; }}
#rbody {{ max-width:700px; margin:0 auto; padding:20px 16px 80px; border:0;
          font:18px/1.65 Georgia,"Times New Roman",serif; }}
#rbody h1 {{ font:700 26px/1.25 system-ui,-apple-system,"Segoe UI",sans-serif; margin:0 0 8px; }}
#rbody .meta {{ font-family:system-ui,-apple-system,"Segoe UI",sans-serif; margin-bottom:20px; }}
#rbody p {{ margin:0 0 1em; }}
#rbody form {{ display:grid; gap:8px; font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif; }}
#rbody form .row {{ display:flex; gap:6px; }}
#rbody input {{ flex:1; min-width:0; font:inherit; padding:8px 10px; color:inherit; background:transparent;
                border:1px solid var(--line); border-radius:8px; }}
#rbody form button {{ font:inherit; color:#fff; background:var(--accent); border:0; padding:8px 14px; border-radius:8px; }}
#tokform {{ margin-top:10px; }}
#tokform:not([hidden]) {{ display:grid; gap:6px; }}
#tokform .row {{ display:flex; gap:6px; }}
#tok {{ flex:1; min-width:0; font:inherit; padding:8px 10px; color:inherit; background:transparent;
        border:1px solid var(--line); border-radius:8px; }}
footer button.save {{ text-decoration:none; color:#fff; background:var(--accent); padding:8px 14px; border-radius:8px; }}
</style></head>
<body><main>
<header><h1>Portal · {date}</h1><span class="st">{s} histórias de {n} manchetes · gerado às {time}</span></header>
{badge}
{body}
<div id="sync"></div>
<footer>👍/👎 ensinam o ranking. <button id="cfg">Configurar envio de votos</button>
<form id="tokform" hidden>
  <label for="tok">Token do GitHub (fine-grained, só o repositório do portal, Contents: Read and write).
  Fica só neste navegador.</label>
  <div class="row"><input id="tok" type="password" autocomplete="off" spellcheck="false" placeholder="github_pat_…">
  <button type="submit" class="save">Salvar</button></div>
  <span id="tokmsg"></span>
</form></footer>
</main>
<div id="reader" hidden><div class="rbar"><button id="rclose">← Voltar</button>
<a id="rorig" href="#" target="_blank" rel="noopener">Abrir no site ↗</a></div><article id="rbody"></article></div>
<script id="portal-data" type="application/json">__DATA__</script>
<script>__VOTE_JS__</script>
<script>__READER_JS__</script>
</body></html>
"""

# Leitor: reader.json tem os textos cifrados (ver src/portal/vault.py). A senha é digitada uma vez
# por aparelho e fica no navegador. Todo texto entra via textContent (nunca como HTML).
READER_JS = r"""
(() => {
const box = document.getElementById("reader"), body = document.getElementById("rbody"),
      orig = document.getElementById("rorig");
const bytes = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const store = {
  get() { try { return localStorage.getItem("reader_pw") || ""; } catch { return ""; } },
  set(v) { try { v ? localStorage.setItem("reader_pw", v) : localStorage.removeItem("reader_pw"); } catch {} },
};
let vault = null, key = null;

async function loadVault() {
  if (!vault) vault = await fetch("reader.json", {cache: "no-store"}).then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  return vault;
}
async function dec(k, x) {
  const pt = await crypto.subtle.decrypt({name: "AES-GCM", iv: bytes(x.n)}, k, bytes(x.c));
  return JSON.parse(new TextDecoder().decode(pt));
}
async function unlock(pw) {
  const v = await loadVault();
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveKey"]);
  const k = await crypto.subtle.deriveKey({name: "PBKDF2", salt: bytes(v.salt), iterations: v.iter, hash: "SHA-256"},
                                          base, {name: "AES-GCM", length: 256}, false, ["decrypt"]);
  try { if (await dec(k, v.check) !== "portal-ok") return false; } catch { return false; }
  key = k; return true;
}
function el(tag, text, cls) { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; }

function askPassword(aid, msg) {
  body.replaceChildren();
  const f = el("form");
  f.append(el("div", msg || "Digite a senha do leitor (a mesma que você salvou no GitHub como READER_PASSWORD). Ela fica guardada neste navegador."));
  const row = el("div", "", "row"), inp = el("input"), ok = el("button", "Abrir");
  inp.type = "password"; inp.autocomplete = "current-password"; row.append(inp, ok); f.append(row);
  f.addEventListener("submit", async e => {
    e.preventDefault(); ok.disabled = true; ok.textContent = "…";
    const good = await unlock(inp.value).catch(() => false);
    if (good) { store.set(inp.value); show(aid); } else askPassword(aid, "Senha errada. Tente de novo.");
  });
  body.append(f); inp.focus();
}

async function show(aid) {
  body.replaceChildren(el("p", "Abrindo…", "meta"));
  try {
    const v = await loadVault();
    if (!key) {
      const saved = store.get();
      if (!saved || !(await unlock(saved))) { store.set(""); return askPassword(aid); }
    }
    if (!v.a[aid]) throw new Error("texto não encontrado");
    const a = await dec(key, v.a[aid]);
    orig.href = a.u;
    const when = a.p ? new Date(a.p).toLocaleString("pt-BR", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}) : "";
    body.replaceChildren(el("h1", a.t), el("div", [a.s, when].filter(Boolean).join(" · "), "meta"));
    for (const p of a.x) body.append(el("p", p));
    const end = el("p", "", "meta"), link = el("a", "Ler no site original ↗");
    link.href = a.u; link.target = "_blank"; link.rel = "noopener"; end.append(link); body.append(end);
  } catch (e) {
    body.replaceChildren(el("p", "Não consegui abrir este texto (" + (e.message || "erro") + "). Use \"Abrir no site\".", "meta"));
  }
}

function open(aid, href) {
  orig.href = href; box.hidden = false; box.scrollTop = 0; document.body.style.overflow = "hidden";
  history.pushState({reader: aid}, "", "#ler");
  show(aid);
}
function close() { box.hidden = true; document.body.style.overflow = ""; }

document.addEventListener("click", e => {
  const a = e.target.closest("a[data-r]");
  if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;  // ctrl+clique: site original
  e.preventDefault(); open(a.dataset.r, a.href);
});
document.getElementById("rclose").addEventListener("click", () => history.state && history.state.reader ? history.back() : close());
addEventListener("popstate", () => { if (!box.hidden) close(); });
addEventListener("keydown", e => { if (e.key === "Escape" && !box.hidden) history.back(); });
if (location.hash === "#ler") history.replaceState(null, "", location.pathname);
})();
"""

# Votos: guardados no navegador e enviados ao arquivo labels/labels.jsonl do repositório pela API do
# GitHub, com um token pessoal que fica só no navegador de quem vota (nunca no repositório).
VOTE_JS = r"""
(() => {
const D = JSON.parse(document.getElementById("portal-data").textContent);
const API = `https://api.github.com/repos/${D.repo}/contents/${D.path}`;
const ls = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
let votes = ls.get("votes", {});      // link -> "YES" | "NO"
let queue = ls.get("queue", []);      // linhas ainda não enviadas
let token = ls.get("gh_token", "");
let busy = false, lastError = "";

function paint() {
  document.querySelectorAll(".vote").forEach(el => {
    const lead = D.stories[+el.dataset.k][0].link, v = votes[lead];
    el.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
    el.closest("article").classList.toggle("no", v === "NO");
  });
  const s = document.getElementById("sync");
  if (!queue.length) { s.innerHTML = ""; return; }
  const n = new Set(queue.map(q => q.ts)).size;   // um clique = uma história, mesmo com várias fontes
  s.innerHTML = !token
    ? `${n} voto(s) guardado(s) só neste aparelho. <button data-a="cfg">Configurar envio</button>`
    : busy ? `Enviando ${n} voto(s)…`
    : `${n} voto(s) não enviado(s)${lastError ? " (" + lastError + ")" : ""}. <button data-a="send">Tentar de novo</button>`;
}

function vote(k, v) {
  const items = D.stories[k], lead = items[0].link;
  const label = votes[lead] === v ? "CLEAR" : v;   // clicar de novo desfaz
  const ts = new Date().toISOString();
  for (const it of items) {
    if (label === "CLEAR") delete votes[it.link]; else votes[it.link] = label;
    queue.push({ts, label, link: it.link, title: it.title, source: it.source, summary: it.summary});
  }
  ls.set("votes", votes); ls.set("queue", queue);
  paint(); clearTimeout(vote.t); vote.t = setTimeout(send, 1500);  // junta cliques seguidos num envio
}

const b64 = s => { const b = new TextEncoder().encode(s); let r = ""; for (const x of b) r += String.fromCharCode(x); return btoa(r); };
const unb64 = s => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\n/g, "")), c => c.charCodeAt(0)));

async function send() {
  if (!token || busy || !queue.length) return paint();
  busy = true; lastError = ""; paint();
  const batch = queue.slice();
  const H = {Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json"};
  try {
    for (let attempt = 0; attempt < 3; attempt++) {
      const g = await fetch(API + "?t=" + Date.now(), {headers: H, cache: "no-store"});
      if (g.status === 401 || g.status === 403) throw new Error("token recusado");
      let sha, old = "";
      if (g.ok) { const j = await g.json(); sha = j.sha; old = unb64(j.content || ""); }
      else if (g.status !== 404) throw new Error("GitHub " + g.status);
      if (old && !old.endsWith("\n")) old += "\n";
      const body = {message: `votos (${batch.length})`, content: b64(old + batch.map(x => JSON.stringify(x)).join("\n") + "\n")};
      if (sha) body.sha = sha;
      const p = await fetch(API, {method: "PUT", headers: H, body: JSON.stringify(body)});
      if (p.ok) { queue = queue.slice(batch.length); ls.set("queue", queue); break; }
      if (p.status === 401 || p.status === 403) throw new Error("token recusado");
      if (p.status !== 409 && p.status !== 422) throw new Error("GitHub " + p.status);
      // 409/422: outro aparelho gravou ao mesmo tempo; relê e tenta de novo
      if (attempt === 2) throw new Error("conflito, tente de novo");
    }
  } catch (e) { lastError = e.message || "sem conexão"; }
  busy = false; paint();
  if (!form.hidden) msg.textContent = lastError ? "Erro: " + lastError : queue.length ? msg.textContent : "Votos enviados ✓";
  if (queue.length && !lastError) send();
}

// Campo na própria página (alguns navegadores de celular bloqueiam a caixa prompt()).
const form = document.getElementById("tokform"), msg = document.getElementById("tokmsg");
function configure() {
  form.hidden = !form.hidden;
  msg.textContent = token ? "Já existe um token salvo neste navegador. Salve vazio para apagar." : "";
  if (!form.hidden) { document.getElementById("tok").focus(); form.scrollIntoView({block: "center"}); }
}
form.addEventListener("submit", e => {
  e.preventDefault();
  const inp = document.getElementById("tok");
  token = inp.value.trim().replace(/^Bearer\s+/i, ""); inp.value = "";
  ls.set("gh_token", token);
  let saved = false; try { saved = localStorage.getItem("gh_token") !== null; } catch {}
  msg.textContent = !token ? "Token apagado deste navegador."
    : saved ? "Token salvo. Enviando votos…"
    : "Este navegador não deixa guardar dados (janela anônima?). Vale só até fechar a página.";
  lastError = ""; send(); paint();
});

document.addEventListener("click", e => {
  const b = e.target.closest(".vote button");
  if (b) return vote(+b.closest(".vote").dataset.k, b.dataset.v);
  const a = e.target.closest("[data-a]");
  if (a) return a.dataset.a === "cfg" ? configure() : send();
});
document.getElementById("cfg").addEventListener("click", configure);
paint(); send();
})();
"""


def build_reader(shown: list[dict]) -> dict[str, str]:
    """Baixa o texto das histórias visíveis e grava site/reader.json cifrado. Devolve {link: id}."""
    if not READER_PASSWORD:
        log.info("READER_PASSWORD vazio: sem leitor de texto completo")
        (SITE / "reader.json").unlink(missing_ok=True)
        return {}
    got = fetch_for_stories(shown)
    meta = {i["link"]: i for s in shown for i in s["items"]}
    articles, ids = {}, {}
    for link, paras in got.items():
        it, aid = meta[link], article_id(link)
        articles[aid] = {"t": it["title"], "s": it["source"], "p": it["published"], "u": link, "x": paras}
        ids[link] = aid
    (SITE / "reader.json").write_text(json.dumps(seal(READER_PASSWORD, articles)), encoding="utf-8")
    return ids


def main() -> None:
    SITE.mkdir(exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] == "--from":
        # Só remonta a página a partir de um data.json já julgado (não coleta nem chama a LLM).
        d = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        stories, mode = d["stories"], d["mode"]
        n_items = d.get("n_items") or sum(len(s["items"]) for s in stories)
    else:
        items = collect()
        stories, mode = judge_stories(items)
        n_items = len(items)
    texts = build_reader(top_stories(stories, mode))
    (SITE / "index.html").write_text(render(stories, mode, n_items, texts), encoding="utf-8")
    (SITE / "data.json").write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                                                "mode": mode, "n_items": n_items, "stories": stories},
                                               ensure_ascii=False), encoding="utf-8")
    log.info("site/index.html gerado")


if __name__ == "__main__":
    main()
