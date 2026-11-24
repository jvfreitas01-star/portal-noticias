"""Ler link: processa os endereços que a página colocou na fila e grava o texto, cifrado.

Fluxo: a página cifra o endereço com a senha do leitor e o acrescenta a reader/queue.jsonl pela API do
GitHub (o mesmo token dos votos). Esse commit dispara .github/workflows/read-link.yml, que roda este
módulo: decifra cada endereço, baixa a página com segurança, extrai o texto, cifra e grava em
reader/links.json. A página acompanha esse arquivo e abre o texto no leitor. Textos e avisos ficam
7 dias.

Só sites abertos: endereços dos jornais com paywall voltam com o aviso "abra no site". Nada aqui
contorna paywall.

Rodar: PYTHONPATH=src READER_PASSWORD=... python -m portal.readlink
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import socket
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .content import extract
from .core import UA, clean
from .vault import CHECK, ITERATIONS, LINKS_SALT, _b64, dec_with, enc_with, links_cipher

log = logging.getLogger("portal.readlink")

ROOT = Path(__file__).resolve().parents[2]
QUEUE = ROOT / "reader" / "queue.jsonl"
LINKS = ROOT / "reader" / "links.json"
KEEP_DAYS = 7
MAX_BYTES = 5_000_000
MIN_CHARS = 500

PAYWALL = ("valor.globo.com", "estadao.com.br", "ft.com", "wsj.com", "bloomberg.com", "folha.uol.com.br",
           "oglobo.globo.com", "economist.com", "nytimes.com", "washingtonpost.com", "barrons.com")


def link_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _host_is_public(host: str) -> bool:
    """Recusa endereços internos (localhost, rede local, metadados de nuvem)."""
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return False
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


class FetchError(Exception):
    pass


def safe_get(url: str) -> tuple[str, bytes]:
    """GET seguindo redirecionamentos à mão, conferindo cada salto. Só http(s), host público e HTML."""
    for _ in range(6):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise FetchError("endereço inválido")
        if not _host_is_public(p.hostname):
            raise FetchError("endereço não público")
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                                       "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
                         timeout=20, allow_redirects=False, stream=True)
        if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get("Location", ""))
            r.close()
            continue
        if not r.ok:
            raise FetchError(f"o site respondeu HTTP {r.status_code}")
        if "html" not in r.headers.get("content-type", ""):
            raise FetchError("não é uma página HTML")
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                raise FetchError("página grande demais")
        return url, body
    raise FetchError("redirecionamentos demais")


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and clean(tag.get("content")):
            return clean(tag["content"])
    return ""


def read(url: str) -> dict:
    """Devolve o artigo {t, s, p, u, x} ou levanta FetchError com o motivo, em português."""
    host = (urlparse(url).hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in PAYWALL):
        raise FetchError("site com paywall: abra no site, onde você entra com a sua assinatura")
    final, html = safe_get(url)
    host = (urlparse(final).hostname or "").lower()
    soup = BeautifulSoup(html, "html.parser")
    title = _meta(soup, "og:title", "twitter:title") or clean(soup.title.get_text() if soup.title else "")
    site = _meta(soup, "og:site_name") or host.removeprefix("www.")
    published = _meta(soup, "article:published_time", "og:published_time", "date")
    paras = extract(html, host)
    if sum(map(len, paras)) < MIN_CHARS:
        raise FetchError("não achei o texto da matéria (pode ter paywall ou não ser uma notícia)")
    return {"t": title or final, "s": site, "p": published or None, "u": final, "x": paras}


def _load_links(aes) -> dict:
    if LINKS.exists():
        data = json.loads(LINKS.read_text(encoding="utf-8"))
        dec_with(aes, data["check"])  # senha errada: falha aqui, antes de mexer em qualquer coisa
        return data
    return {"v": 1, "salt": _b64(LINKS_SALT), "iter": ITERATIONS, "check": enc_with(aes, CHECK), "a": {}, "st": {}}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    password = os.environ.get("READER_PASSWORD", "")
    if not password:
        raise SystemExit("READER_PASSWORD vazio")
    aes = links_cipher(password)
    data = _load_links(aes)
    today = date.today().isoformat()
    recent = (date.today() - timedelta(days=2)).isoformat()

    lines = QUEUE.read_text(encoding="utf-8").splitlines() if QUEUE.exists() else []
    for line in lines:
        try:
            q = json.loads(line)
            url = dec_with(aes, q["u"]).strip()
        except Exception:
            log.warning("linha da fila ilegível, ignorada")
            continue
        lid = q.get("id")
        if lid != link_id(url):
            log.warning("id não confere com o endereço, ignorado")
            continue
        if lid in data["st"] or str(q.get("ts", ""))[:10] < recent:
            continue  # já processado, ou pedido antigo
        try:
            art = read(url)
            data["a"][lid] = {"t": today, **enc_with(aes, art)}
            data["st"][lid] = {"ok": True, "t": today}
            log.info("lido %s: %d caracteres", urlparse(url).hostname, sum(map(len, art["x"])))
        except FetchError as e:
            data["st"][lid] = {"ok": False, "msg": str(e), "t": today}
            log.info("não lido %s: %s", urlparse(url).hostname, e)
        except Exception as e:  # rede, timeout etc.
            data["st"][lid] = {"ok": False, "msg": "falha ao baixar a página", "t": today}
            log.info("não lido %s: %s", urlparse(url).hostname, e)

    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    data["a"] = {k: v for k, v in data["a"].items() if v.get("t", today) >= cutoff}
    data["st"] = {k: v for k, v in data["st"].items() if v.get("t", today) >= cutoff}
    LINKS.parent.mkdir(exist_ok=True)
    LINKS.write_text(json.dumps(data), encoding="utf-8")
    # A fila nunca é alterada aqui: a página pode estar acrescentando um pedido agora mesmo, e dois
    # commits no mesmo arquivo dariam conflito. Pedidos já processados ou antigos são pulados acima.
    log.info("fila lida: %d linha(s); %d texto(s) guardado(s)", len(lines), len(data["a"]))


if __name__ == "__main__":
    main()
