"""Texto completo das matérias selecionadas, só de sites abertos (sem paywall).

Cada site muda o HTML de tempos em tempos: anote abaixo quando o extrator foi conferido.
Sites com paywall (Valor, Estadão, Folha, FT, WSJ, Bloomberg) ficam de fora de propósito:
o leitor não contorna paywall; nesses casos o título abre o site original.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from .core import UA, clean

log = logging.getLogger("portal.content")

# host -> seletores CSS dos parágrafos, tentados em ordem (None = escolhe o bloco com mais texto).
# Conferido em 2026-11-20.
HOSTS: dict[str, list[str | None]] = {
    "neofeed.com.br": [None],
    "braziljournal.com": [".post-body p", None],  # corpo dividido em vários <div> sem classe
    "www.infomoney.com.br": [None],
    "exame.com": [None],
    "megawhat.uol.com.br": [None],
    "agenciainfra.com": [None],
    "estradas.com.br": [None],
    "www.poder360.com.br": [None],
    "www.bbc.co.uk": ["[data-component=text-block] p", None],
    "www.bbc.com": ["[data-component=text-block] p", None],
}

# Abaixo disso costuma ser só a chamada: melhor abrir o site original. Baixo porque a lista acima
# só tem sites abertos, e notas curtas (iNFRA, ~1.100 caracteres) vêm completas.
MIN_CHARS = 700

JUNK = ["leia mais", "leia também", "veja também", "assine", "newsletter", "cookies", "inscreva-se",
        "clique aqui", "siga o canal", "siga-nos", "receba as notícias", "todos os direitos reservados",
        "canal do whatsapp", "grupo de whatsapp", "no whatsapp",  # não "whatsapp" sozinho: derrubava matéria sobre o app
        "matéria exclusiva para assinantes", "the post ", "appeared first on", "sign up", "read more"]


def allowed(link: str) -> bool:
    return urlparse(link).netloc.lower() in HOSTS


def _upper_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum(c.isupper() for c in letters) / len(letters) if letters else 0


def _keep(p: str, seen: set[str]) -> bool:
    low = p.lower()
    ok = (len(p) >= 40 and p not in seen and _upper_ratio(p) <= 0.3
          and not any(j in low for j in JUNK if len(p) < 300))  # frase-lixo só derruba parágrafo curto
    seen.add(p)
    return ok


def _densest(soup: BeautifulSoup) -> list[Tag]:
    """Parágrafos do corpo da matéria: o bloco com mais texto em <p>.

    Conta os <p> filhos diretos de cada elemento e também os "netos" (p dentro de um invólucro por
    parágrafo, como no g1). O avô só ganha se tiver bem mais texto, porque costuma incluir lixo ao lado."""
    by_parent: dict[int, list] = {}
    by_grand: dict[int, list] = {}
    for p in soup.find_all("p"):
        n = len(p.get_text(" ", strip=True))
        if n < 40:
            continue
        par = p.parent
        if par is not None:
            by_parent.setdefault(id(par), [par, 0])[1] += n
            if par.parent is not None:
                by_grand.setdefault(id(par.parent), [par.parent, 0])[1] += n
    best_p = max(by_parent.values(), key=lambda x: x[1], default=None)
    best_g = max(by_grand.values(), key=lambda x: x[1], default=None)
    if best_g and (not best_p or best_g[1] > best_p[1] * 1.5):
        return best_g[0].find_all("p")
    return best_p[0].find_all("p", recursive=False) if best_p else []


def _ld_body(soup: BeautifulSoup) -> str:
    best = ""
    for sc in soup.select('script[type="application/ld+json"]'):
        try:
            stack = [json.loads(sc.string or "")]
        except ValueError:
            continue
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                b = x.get("articleBody")
                if isinstance(b, str) and len(b) > len(best):
                    best = b
                stack += list(x.values())
            elif isinstance(x, list):
                stack += x
    return best


_BODY_KEYS = {"body", "content", "articlebody", "text", "html", "bodyhtml", "contenthtml", "rawhtml", "value"}


def _embedded_body(soup: BeautifulSoup) -> list[str]:
    """Texto completo escondido no JSON da página (Next.js __NEXT_DATA__ e similares), quando o HTML
    visível só traz os primeiros parágrafos. Pega o maior bloco de texto sob chaves de corpo."""
    best: list[str] = []
    for sc in soup.select("script#__NEXT_DATA__, script[type='application/json']"):
        try:
            stack = [json.loads(sc.string or "")]
        except ValueError:
            continue
        chunks: list[str] = []
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for k, v in x.items():
                    if isinstance(v, str) and k.lower() in _BODY_KEYS and len(v) > 120:
                        chunks.append(v)
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(x, list):
                stack += x
        for c in chunks:
            if "<p" in c:  # HTML dentro do JSON
                paras = [clean(t.get_text(" ")) for t in BeautifulSoup(c, "html.parser").find_all("p")]
            else:
                paras = [clean(t) for t in re.split(r"\n+", c)]
            paras = [t for t in paras if len(t) >= 40]
            if sum(map(len, paras)) > sum(map(len, best)):
                best = paras
    return best


def amp_url(html: bytes, base: str) -> str | None:
    """Endereço da versão AMP, se a página declarar uma."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("link", rel=lambda v: v and "amphtml" in (v if isinstance(v, list) else [v]))
    return urljoin(base, tag["href"]) if tag and tag.get("href") else None


def extract(html: bytes, host: str) -> list[str]:
    """Corpo da matéria. Tenta, e fica com o mais longo: seletores do site ou bloco mais denso de <p>;
    articleBody do JSON-LD; texto completo embutido no JSON da página."""
    soup = BeautifulSoup(html, "html.parser")  # bytes, não texto: senão os acentos quebram
    ld_raw = _ld_body(soup)
    embedded = _embedded_body(soup)
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "figure", "noscript"]):
        t.decompose()
    for t in soup.select("[class*=related],[class*=newsletter],[class*=recommend],[class*=share],"
                         "[class*=comment],[class*=banner],[id*=related]"):
        if t.name not in ("html", "body", "main", "article"):  # no g1 a própria <html> casa com o filtro
            t.decompose()
    paras: list[str] = []
    for sel in HOSTS.get(host, [None]):
        nodes = soup.select(sel) if sel else _densest(soup)
        seen: set[str] = set()
        paras = [p for p in (clean(n.get_text(" ")) for n in nodes) if _keep(p, seen)]
        if len(paras) >= 3:
            break
    ld = [t for t in (clean(x) for x in re.split(r"\n+", ld_raw)) if len(t) >= 40] or ([clean(ld_raw)] if ld_raw else [])
    for alt in (ld, embedded):  # alternativa com corpo bem maior ganha
        if sum(map(len, alt)) > sum(map(len, paras)) * 1.2:
            seen = set()
            paras = [p for p in alt if _keep(p, seen)] or alt
    return paras


# Requisição com "cara" de Chrome (assinatura TLS/HTTP2 de navegador real): muitos sites devolvem 403
# para a assinatura do Python. Antes do primeiro artigo de um site, visita a página inicial (como uma
# pessoa faria) para receber os cookies, e manda a página inicial como Referer.
try:
    from curl_cffi import requests as _cr
    _browser = _cr.Session(impersonate="chrome")
except Exception:  # sem curl_cffi: cai no requests comum
    _cr, _browser = None, requests.Session()
    _browser.headers.update({"User-Agent": UA})
_browser.headers.update({"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
_warmed: set[str] = set()


_plain = requests.Session()
_plain.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                       "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})


def browser_get(url: str, allow_redirects: bool = True, timeout: int = 25):
    """GET com cara de Chrome. Se o curl_cffi não conseguir conectar (visto num Windows em 24/11: timeouts
    em sites que o requests abria na hora), cai para o requests comum: no pior caso, igual ao antes."""
    p = urlparse(url)
    home = f"{p.scheme}://{p.netloc}/"
    if _cr is not None:
        try:
            if p.netloc not in _warmed:
                _warmed.add(p.netloc)
                try:  # sem seguir redirecionamento: o Ler link confere cada salto
                    _browser.get(home, timeout=10, allow_redirects=False)
                except Exception:
                    pass
            return _browser.get(url, timeout=min(timeout, 15), allow_redirects=allow_redirects,
                                headers={"Referer": home})
        except Exception as e:
            log.info("curl_cffi falhou em %s (%s); tentando requests", p.netloc, str(e)[:60])
    return _plain.get(url, timeout=timeout, allow_redirects=allow_redirects, headers={"Referer": home})


def fetch_text(link: str) -> list[str] | None:
    host = urlparse(link).netloc.lower()
    try:
        r = browser_get(link)
        if not r.ok or "html" not in r.headers.get("content-type", ""):
            log.info("texto %s: HTTP %s", host, r.status_code)
            return None
        paras = extract(r.content, host)
        if sum(map(len, paras)) < MIN_CHARS and (amp := amp_url(r.content, str(r.url))):
            ra = browser_get(amp)  # alguns sites entregam o texto inteiro só na versão AMP
            if ra.ok:
                alt = extract(ra.content, host)
                if sum(map(len, alt)) > sum(map(len, paras)):
                    paras = alt
    except Exception as e:
        log.info("texto %s: %s", host, e)
        return None
    if sum(map(len, paras)) < MIN_CHARS:
        log.info("texto %s: curto demais (%d)", host, sum(map(len, paras)))
        return None
    return paras


def fetch_for_stories(stories: list[dict]) -> dict[str, list[str]]:
    """Para cada história, tenta o texto da primeira fonte aberta. Devolve {link: parágrafos}.
    Um site por vez (com pausa), vários sites em paralelo."""
    wanted: list[list[str]] = [[i["link"] for i in s["items"] if allowed(i["link"])] for s in stories]
    by_host: dict[str, list[list[str]]] = defaultdict(list)
    for links in wanted:
        if links:
            by_host[urlparse(links[0]).netloc.lower()].append(links)

    def run(jobs: list[list[str]]) -> dict[str, list[str]]:
        got = {}
        for links in jobs:
            for link in links[:2]:  # até 2 fontes abertas por história
                paras = fetch_text(link)
                time.sleep(1.5)
                if paras:
                    got[link] = paras
                    break
        return got

    out: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for got in ex.map(run, by_host.values()):
            out.update(got)
    log.info("texto completo: %d de %d histórias com fonte aberta", len(out), sum(1 for w in wanted if w))
    return out
