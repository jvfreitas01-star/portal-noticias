"""Coleta de manchetes via RSS + etiquetas por palavra-chave (plano B quando a LLM falha).

FEEDS: (nome, url, janela_em_horas). Cada URL foi testada à mão em 2026-11-20.
"""
from __future__ import annotations

import calendar
import logging
import re
import socket
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import feedparser
from bs4 import BeautifulSoup

log = logging.getLogger("portal.core")

socket.setdefaulttimeout(25)  # um feed lento não pode travar a coleta inteira

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (nome, url, horas). Fontes com pouco volume ganham janela maior.
FEEDS: list[tuple[str, str, int]] = [
    # Empresas e crédito
    ("Brazil Journal", "https://braziljournal.com/feed/", 48),
    ("NeoFeed", "https://neofeed.com.br/feed/", 48),
    ("Valor Empresas", "https://valor.globo.com/rss/valor/empresas", 24),
    ("Valor Finanças", "https://valor.globo.com/rss/valor/financas", 24),
    ("InfoMoney", "https://www.infomoney.com.br/feed/", 24),
    ("Exame", "https://exame.com/feed/", 24),
    # Infraestrutura
    ("Agência iNFRA", "https://agenciainfra.com/blog/feed/", 48),  # /feed/ dá 404
    ("MegaWhat", "https://www.megawhat.energy/feed", 48),
    ("Estradas", "https://www.estradas.com.br/feed/", 48),
    ("AeroFlap", "https://www.aeroflap.com.br/feed/", 48),
    # Política nacional
    ("Valor Política", "https://valor.globo.com/rss/valor/politica", 24),
    ("Folha Poder", "https://feeds.folha.uol.com.br/poder/rss091.xml", 24),
    ("Estadão Política", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/politica/", 24),
    ("Poder360", "https://www.poder360.com.br/feed/", 24),
    ("JOTA", "https://www.jota.info/feed", 48),
    # Internacional e macro
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss", 24),
    ("Bloomberg Politics", "https://feeds.bloomberg.com/politics/news.rss", 24),
    ("FT World", "https://www.ft.com/world?format=rss", 24),
    ("WSJ World", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews", 24),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", 24),
]
# Sem RSS: InfraJournal (infrajournal.com).
# Testados e fora do ar em 2026-11-20: Reuters (401), g1 Política (0), Canal Energia (0),
# epbr (522), Portos e Navios (404), Brasil Energia (404), BNamericas (404).

# Plano B: palavras por seção (mesmos ids do brief). Só decide quando a LLM falha.
WORDS: dict[str, list[str]] = {
    "1": ["debenture", "debentures", "emissao", "rating", "fitch", "moody's", "s&p", "recuperacao judicial",
          "balanco", "lucro", "prejuizo", "ebitda", "resultado", "alavancagem", "ipo", "follow-on",
          "aquisicao", "fusao", "m&a", "dividendos", "credores", "divida", "bonds", "earnings"],
    "2": ["concessao", "concessoes", "concessionaria", "leilao", "rodovia", "rodovias", "aeroporto",
          "aeroportos", "porto", "portos", "ferrovia", "saneamento", "energia", "transmissao",
          "distribuidora", "antt", "anac", "aneel", "antaq", "tcu", "pedagio", "reequilibrio",
          "outorga", "infraestrutura", "ppp"],
    "3": ["congresso", "senado", "camara", "stf", "lula", "planalto", "ministro", "governo federal",
          "reforma tributaria", "arcabouco", "orcamento", "eleicao", "eleicoes", "tarcisio"],
    "4": ["fed", "fomc", "treasury", "treasuries", "tariff", "tariffs", "sanctions", "opec", "oil",
          "china", "trump", "ukraine", "russia", "israel", "iran", "ecb", "yields", "dollar"],
}


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def fold(text: str) -> str:
    """minúsculas, sem acento: 'Concessão' -> 'concessao'."""
    t = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


_WORD_RE = {sec: re.compile(r"\b(" + "|".join(re.escape(w) for w in ws) + r")\b")
            for sec, ws in WORDS.items()}


def match(text: str) -> list[str]:
    """Seções cujas palavras aparecem no texto (título + resumo)."""
    t = fold(text)
    return [sec for sec, rx in _WORD_RE.items() if rx.search(t)]


def _published(e) -> datetime | None:
    st = e.get("published_parsed") or e.get("updated_parsed")
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def _fetch(url: str) -> list:
    entries = feedparser.parse(url, agent=UA).entries
    if not entries:  # feeds falham de forma intermitente: tenta mais uma vez
        time.sleep(2)
        entries = feedparser.parse(url, agent=UA).entries
    return entries


def collect(max_per_feed: int = 30) -> list[dict]:
    """Baixa todos os feeds em paralelo e devolve itens novos, do mais recente ao mais antigo."""
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda f: _fetch(f[1]), FEEDS))

    seen: set[str] = set()
    items: list[dict] = []
    status = []
    for (source, _url, hours), entries in zip(FEEDS, results):
        kept = 0
        for e in entries[:max_per_feed]:
            title, link = clean(e.get("title")), e.get("link")
            if not title or not link or link in seen:
                continue
            pub = _published(e)
            if pub and (now - pub).total_seconds() > hours * 3600:
                continue
            seen.add(link)
            summary = clean(BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" "))
            summary = re.sub(r"\s*The post .* appeared first on .*$", "", summary)[:500]  # rodapé do WordPress
            items.append({
                "source": source,
                "title": title,
                "link": link,
                "published": pub.isoformat() if pub else None,
                "summary": summary,
                "groups": match(f"{title} {summary}"),
            })
            kept += 1
        status.append(f"{source.replace(' ', '_')}={len(entries)}/{kept}")
    log.info("FEED-STATUS %s", " ".join(status))  # baixados/mantidos; 0/0 = feed morto

    items.sort(key=lambda i: i["published"] or "", reverse=True)
    return items
