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
from urllib.parse import urlparse

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
        "clique aqui", "siga o", "receba as notícias", "todos os direitos reservados", "whatsapp",
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
    """Parágrafos do bloco que tem mais texto em <p> filhos diretos (o corpo da matéria)."""
    best, best_len = None, 0
    for el in soup.find_all(True):
        ps = el.find_all("p", recursive=False)
        n = sum(len(p.get_text(" ", strip=True)) for p in ps)
        if n > best_len:
            best, best_len = el, n
    return best.find_all("p", recursive=False) if best else []


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


def extract(html: bytes, host: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")  # bytes, não texto: senão os acentos quebram
    ld = clean(_ld_body(soup))
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "figure", "noscript"]):
        t.decompose()
    for t in soup.select("[class*=related],[class*=newsletter],[class*=recommend],[class*=share],"
                         "[class*=comment],[class*=banner],[id*=related]"):
        t.decompose()
    paras: list[str] = []
    for sel in HOSTS.get(host, [None]):
        nodes = soup.select(sel) if sel else _densest(soup)
        seen: set[str] = set()
        paras = [p for p in (clean(n.get_text(" ")) for n in nodes) if _keep(p, seen)]
        if len(paras) >= 3:
            break
    if len(ld) > sum(map(len, paras)) * 1.2:  # JSON-LD com corpo maior: usa ele
        paras = [p for p in re.split(r"\n{1,}|(?<=[.!?])\s{2,}", ld) if len(p) >= 40] or [ld]
    return paras


_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                         "Accept": "text/html,application/xhtml+xml"})


def fetch_text(link: str) -> list[str] | None:
    host = urlparse(link).netloc.lower()
    try:
        r = _session.get(link, timeout=25)
        if not r.ok or "html" not in r.headers.get("content-type", ""):
            log.info("texto %s: HTTP %s", host, r.status_code)
            return None
        paras = extract(r.content, host)
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
