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
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("portal.core")

socket.setdefaulttimeout(25)  # um feed lento não pode travar a coleta inteira

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (nome, url, horas, teto de itens). Fontes com pouco volume ganham janela maior; feeds de seção
# grandes ganham teto menor para caber na cota grátis da LLM. Um link que aparece em dois feeds fica
# com o nome do primeiro, por isso as seções mais específicas vêm antes das gerais.
FEEDS: list[tuple[str, str, int, int]] = [
    # Empresas e crédito
    ("Brazil Journal", "https://braziljournal.com/feed/", 48, 30),
    ("NeoFeed", "https://neofeed.com.br/feed/", 48, 30),
    ("Valor Empresas", "https://valor.globo.com/rss/valor/empresas", 24, 30),
    ("Valor Finanças", "https://valor.globo.com/rss/valor/financas", 24, 30),
    ("Estadão Economia", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/economia/", 24, 20),
    ("InfoMoney", "https://www.infomoney.com.br/feed/", 24, 30),
    ("Exame", "https://exame.com/feed/", 24, 20),
    ("FT Companies", "https://www.ft.com/companies?format=rss", 24, 15),
    ("FT Markets", "https://www.ft.com/markets?format=rss", 24, 15),
    ("WSJ Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", 24, 20),
    ("WSJ Business", "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness", 24, 20),
    # Infraestrutura
    ("Agência iNFRA", "https://agenciainfra.com/blog/feed/", 48, 30),  # /feed/ dá 404
    ("MegaWhat", "https://www.megawhat.energy/feed", 48, 30),
    ("Estradas", "https://www.estradas.com.br/feed/", 48, 30),
    ("AeroFlap", "https://www.aeroflap.com.br/feed/", 48, 30),
    ("FT Energy", "https://www.ft.com/energy?format=rss", 24, 15),
    # Política nacional
    ("Valor Política", "https://valor.globo.com/rss/valor/politica", 24, 30),
    ("Valor Legislação", "https://valor.globo.com/rss/valor/legislacao", 24, 15),
    ("Valor Brasil", "https://valor.globo.com/rss/valor/brasil", 24, 20),
    ("Folha Poder", "https://feeds.folha.uol.com.br/poder/rss091.xml", 24, 30),
    ("Estadão Política", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/politica/", 24, 20),
    ("Poder360", "https://www.poder360.com.br/feed/", 24, 30),
    ("JOTA", "https://www.jota.info/feed", 48, 30),
    # Internacional e macro
    ("FT Brazil", "https://www.ft.com/brazil?format=rss", 72, 15),
    ("FT Emerging Markets", "https://www.ft.com/emerging-markets?format=rss", 24, 15),
    ("FT World", "https://www.ft.com/world?format=rss", 24, 15),
    ("Valor Mundo", "https://valor.globo.com/rss/valor/mundo", 24, 20),
    ("Estadão Internacional", "https://www.estadao.com.br/arc/outboundfeeds/feeds/rss/sections/internacional/", 24, 15),
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss", 24, 30),
    ("Bloomberg Politics", "https://feeds.bloomberg.com/politics/news.rss", 24, 20),
    ("WSJ World", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews", 24, 15),
]
# Sem RSS: InfraJournal (infrajournal.com).
# Testados e fora do ar em 2026-11-20: Reuters (401), g1 Política (0), Canal Energia (0),
# epbr (522), Portos e Navios (404), Brasil Energia (404), BNamericas (404).
# Retirado em 2026-11-24: BBC World (73 manchetes em 4 dias, nenhuma selecionada).
# Também fora (2026-11-24): feeds de seção do Valor (Infraestrutura, Energia, Mercados) estão parados
# desde outubro; Estadão Negócios e Infraestrutura devolvem o mesmo conteúdo do feed de Economia.

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


def _dedup_keys(link: str, title: str) -> tuple[str, str]:
    """O mesmo texto aparece em vários feeds de seção, às vezes com parâmetros diferentes no link."""
    return re.sub(r"^https?://(www\.)?", "", link.split("?")[0].split("#")[0]).rstrip("/"), fold(title)


_META = ("og:description", "description", "twitter:description")


def _describe(link: str) -> str:
    """A descrição pública da matéria (a frase que o site publica para buscadores e redes sociais).
    Só o <head>; o corpo da matéria não é lido aqui."""
    try:
        r = requests.get(link, headers={"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"},
                         timeout=12, stream=True)
        if not r.ok:
            return ""
        head = b""
        for chunk in r.iter_content(16384):
            head += chunk
            if b"</head>" in head.lower() or len(head) > 400_000:
                break
        r.close()
        soup = BeautifulSoup(head.split(b"</head>")[0] + b"</head>", "html.parser")
        for name in _META:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and clean(tag.get("content")):
                return clean(tag["content"])[:500]
    except Exception:
        pass
    return ""


def enrich(items: list[dict], min_len: int = 60, cap: int = 150) -> None:
    """Preenche o resumo das manchetes que chegaram sem resumo (ex.: Estadão manda só o título)."""
    todo = [i for i in items if len(i["summary"]) < min_len][:cap]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        descs = list(pool.map(lambda i: _describe(i["link"]), todo))
    n = 0
    for it, d in zip(todo, descs):
        if len(d) > len(it["summary"]) and fold(d) != fold(it["title"]):
            it["summary"] = d
            it["groups"] = match(f"{it['title']} {d}")
            n += 1
    log.info("resumos preenchidos pela descrição da página: %d de %d sem resumo", n, len(todo))


def collect() -> list[dict]:
    """Baixa todos os feeds em paralelo e devolve itens novos, do mais recente ao mais antigo."""
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda f: _fetch(f[1]), FEEDS))

    seen: set[str] = set()
    items: list[dict] = []
    status = []
    for (source, _url, hours, cap), entries in zip(FEEDS, results):
        kept = 0
        for e in entries:
            if kept >= cap:
                break
            title, link = clean(e.get("title")), e.get("link")
            if not title or not link:
                continue
            keys = _dedup_keys(link, title)
            if keys[0] in seen or keys[1] in seen:
                continue
            pub = _published(e)
            if pub and (now - pub).total_seconds() > hours * 3600:
                continue
            seen.update(keys)
            summary = clean(BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" "))
            summary = re.sub(r"\s*The post .* appeared first on .*$", "", summary)  # rodapé do WordPress
            # aviso de paywall no lugar do resumo (Valor): descarta; enrich() busca a descrição pública
            summary = re.sub(r"\s*Matéria exclusiva para assinantes\..*$", "", summary)[:500]
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

    enrich(items)
    items.sort(key=lambda i: i["published"] or "", reverse=True)
    return items
