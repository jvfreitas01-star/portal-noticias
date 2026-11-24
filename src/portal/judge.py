"""LLM agrupa manchetes em histórias, dá nota 0-100 e escolhe a seção.

Usa qualquer API compatível com OpenAI chat-completions (padrão: OpenRouter, modelos :free).
Se a LLM falhar, a história cai no plano B por palavra-chave (mostra mais, nunca menos).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .core import fold, match

log = logging.getLogger("portal.judge")

ROOT = Path(__file__).resolve().parents[2]
PREFS = ROOT / "config" / "preferences.md"

LLM_URL = os.environ.get("LLM_URL", "https://openrouter.ai/api/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# Lista de modelos tentados em ordem. Modelos grátis mudam: se vazio, descobre os :free do OpenRouter.
LLM_MODELS = [m.strip() for m in os.environ.get("LLM_MODELS", "").split(",") if m.strip()]
THRESHOLD = int(os.environ.get("JUDGE_THRESHOLD", "70"))
BATCH = 40
MAX_TRIES_PER_BATCH = 3  # no plano grátis do OpenRouter (50/dia) falhas também contam

GENERIC = set(fold(w) for w in """
mercado mercados bolsa bolsas ações acoes dolar dólar stocks market markets rally shares
brasil governo diz após apos sobre contra para entre mais novo nova anos hoje
bilhao bilhoes milhao milhoes bilhão bilhões milhão milhões queda alta sobe cai caem sobem segundo
diante durante ainda pode podem deve devem quer pede afirma anuncia empresa empresas""".split() + """
says after over with from that this what into amid could will would""".split())


# ---------- brief e seções ----------

def load_brief() -> str:
    text = PREFS.read_text(encoding="utf-8")
    m = re.search(r"^## Goals brief\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return (m.group(1) if m else text).strip()


def sections(brief: str) -> list[tuple[str, str]]:
    return [(i, n.strip()) for i, n in re.findall(r"^\s*(\d+b?)\.\s+\*\*(.+?)\.?\*\*", brief, re.M)]


# ---------- similaridade (só para ordenar os lotes) ----------

def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]{4,}", fold(text)) if w not in GENERIC]


def _cos(a: Counter, b: Counter) -> float:
    num = sum(a[k] * b[k] for k in a.keys() & b.keys())
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def order_by_similarity(items: list[dict]) -> list[int]:
    """Cadeia do vizinho mais próximo: manchetes parecidas caem no mesmo lote."""
    n = len(items)
    if n == 0:
        return []
    vecs = [Counter(_words(f"{i['title']} {i['summary'][:200]}")) for i in items]
    seq, left = [0], set(range(1, n))
    while left:
        last = vecs[seq[-1]]
        nxt = max(left, key=lambda j: (_cos(last, vecs[j]), -j))
        seq.append(nxt)
        left.remove(nxt)
    return seq


# ---------- chamada à LLM ----------

# Preferidos quando LLM_MODELS está vazio (grátis e com JSON em 2026-11-20). Os que sumirem do
# catálogo são pulados; se nenhum sobrar, usa os :free maiores que o catálogo oferecer.
# Nemotron primeiro: de 20 a 23/11 respondeu todos os lotes, enquanto o Gemma 31B devolveu 429 em quase toda
# chamada (e no plano grátis cada 429 também gasta a cota diária).
PREFERRED = ["nvidia/nemotron-3.5-super-vl:free", "google/gemma-4-31b-it:free", "google/gemma-4-26b-a4b-it:free"]

_free_cache: list[str] | None = None
_lock = threading.Lock()
_auth_failed = False  # chave recusada: não adianta tentar outros modelos (e cada tentativa gasta cota)
_cooling: dict[str, float] = {}  # modelo -> hora do último 429: fica de fora por COOLDOWN segundos
COOLDOWN = 90


def _models() -> list[str]:
    global _free_cache
    if LLM_MODELS:
        return LLM_MODELS
    with _lock:
        if _free_cache is None:
            try:
                data = requests.get("https://openrouter.ai/api/v1/models", timeout=30).json()["data"]
                free = {m["id"]: m.get("context_length") or 0 for m in data if m["id"].endswith(":free")
                        and "response_format" in (m.get("supported_parameters") or [])}
                rest = sorted((m for m in free if m not in PREFERRED), key=lambda m: -_size(m))
                _free_cache = [m for m in PREFERRED if m in free] + rest
            except Exception:
                log.exception("não consegui listar modelos grátis")
                _free_cache = list(PREFERRED)
            log.info("modelos, em ordem: %s", _free_cache[:MAX_TRIES_PER_BATCH])
    return _free_cache


def _size(model_id: str) -> float:
    """Tamanho em bilhões de parâmetros lido do nome (ex.: '31b' -> 31); 0 se não houver."""
    nums = re.findall(r"(\d+(?:\.\d+)?)b\b", model_id.lower())
    return max(map(float, nums)) if nums else 0.0


def call_llm(prompt: str) -> str | None:
    global _auth_failed
    if not LLM_API_KEY or _auth_failed:
        return None
    now = time.time()
    for model in [m for m in _models() if now - _cooling.get(m, 0) > COOLDOWN][:MAX_TRIES_PER_BATCH]:
        if _auth_failed:
            return None
        try:
            r = requests.post(LLM_URL, timeout=180, headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                              json={"model": model, "temperature": 0, "max_tokens": 4000,
                                    "response_format": {"type": "json_object"},
                                    "reasoning": {"enabled": False},
                                    "messages": [
                                        {"role": "system", "content": "Editor de notícias de empresas, crédito e "
                                         "infraestrutura. Responda com um único objeto JSON. Sem prosa. Comece com {"},
                                        {"role": "user", "content": prompt}]})
            if r.ok:
                text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
                if text:
                    log.info("lote respondido por %s", model)
                    return text
            log.warning("modelo %s: HTTP %s %s", model, r.status_code, r.text[:200])
            if r.status_code == 429:
                _cooling[model] = time.time()
                log.info("modelo %s em limite (429): fica de fora por %d s", model, COOLDOWN)
            if r.status_code == 402:  # modelo pago sem saldo: não tenta mais nesta execução
                _cooling[model] = float("inf")
                log.warning("modelo %s sem saldo de crédito (402): fora nesta execução", model)
            if r.status_code in (401, 403):
                _auth_failed = True
                log.error("chave LLM_API_KEY recusada: confira o segredo no GitHub")
                return None
        except Exception as e:
            log.warning("modelo %s: %s", model, e)
    return None


def parse_json(text: str | None) -> dict | None:
    if not text or "{" not in text:
        return None
    try:
        obj, _ = json.JSONDecoder(strict=False).raw_decode(text[text.find("{"):])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


# ---------- votos como exemplos ----------

LABELS = ROOT / "labels" / "labels.jsonl"


def is_holdout(link: str) -> bool:
    """1 em cada 4 links fica fora dos exemplos, reservado para medir a qualidade depois."""
    return int(hashlib.sha1(link.encode("utf-8")).hexdigest(), 16) % 4 == 0


def load_votes() -> list[dict]:
    """Último voto de cada link (CLEAR = voto desfeito), sem os reservados para teste."""
    if not LABELS.exists():
        return []
    last: dict[str, dict] = {}
    for line in LABELS.read_text(encoding="utf-8").splitlines():
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if v.get("link"):
            last[v["link"]] = v
    return [v for v in last.values() if v.get("label") in ("YES", "NO") and not is_holdout(v["link"])]


def pick_examples(batch: list[dict], votes: list[dict], k: int = 20) -> list[dict]:
    """Os k votos SIM e k votos NÃO mais parecidos com este lote."""
    if not votes:
        return []
    target = Counter(w for it in batch for w in _words(f"{it['title']} {it['summary'][:200]}"))
    out = []
    for label in ("YES", "NO"):
        pool = [v for v in votes if v["label"] == label]
        pool.sort(key=lambda v: -_cos(target, Counter(_words(f"{v['title']} {v.get('summary', '')}"))))
        out += pool[:k]
    return out


# ---------- prompt ----------

def build_prompt(brief: str, secs: list[tuple[str, str]], batch: list[dict],
                 examples: list[dict] | None = None) -> str:
    sec_list = "\n".join(f'"{i}" = {n}' for i, n in secs) + '\n"0" = outro / nenhuma'
    lines = "\n".join(f"[{k}] ({it['source']}) {it['title']} — {it['summary'][:200]}"
                      for k, it in enumerate(batch))
    ex = ""
    if examples:
        rows = "\n".join(f"{'SIM' if v['label'] == 'YES' else 'NÃO'} ({v.get('source', '')}) {v['title']}"
                         f" — {v.get('summary', '')[:120]}" for v in examples)
        ex = (f"\n\nMEUS VOTOS ANTERIORES (SIM = quis ler, NÃO = não quis). Quando os votos e o texto acima "
              f"discordarem, os votos valem mais:\n{rows}")
    return f"""O QUE EU QUERO LER:
{brief}{ex}

SEÇÕES:
{sec_list}

TAREFA:
Agrupe as manchetes abaixo em histórias. Uma história = um acontecimento: manchetes em que ler uma
torna as outras redundantes (mesma empresa e mesmo negócio, mesma decisão, mesmo dado divulgado).
Tema parecido não basta. Na dúvida, separe. Manchetes em português e inglês sobre o mesmo fato vão juntas.
Só junte manchetes que tenham o MESMO agente principal (empresa, pessoa, órgão) fazendo o MESMO ato, e todas
precisam citar esse agente. Manchetes de resumo do dia ("Ibovespa hoje...", "...e mais destaques", "Markets
Wrap", agendas, listas de vários assuntos) ficam SEMPRE sozinhas.
Para cada história dê:
- "items": índices das manchetes
- "lead": índice da manchete mais informativa
- "head": as 4 primeiras palavras da manchete lead, copiadas
- "who": o agente principal, escrito como aparece nas manchetes (ex.: "Petrobras", "Fachin", "SEC")
- "s": 0 a 100, a chance de eu querer ler isto
- "sec": id da seção
- "why": até 8 palavras, em português
Toda manchete deve aparecer em exatamente uma história.
Responda só com JSON: {{"stories": [{{"items": [int], "lead": int, "head": str, "who": str, "s": int, "sec": str, "why": str}}]}}

MANCHETES:
{lines}"""


# ---------- guardas: não confiar nos índices da LLM ----------

def _title_words(it: dict) -> set[str]:
    return set(_words(it["title"]))


# Manchetes de resumo do dia: falam de vários assuntos e por isso "ligam" fatos diferentes. Nunca entram em
# grupo, e a nota fica limitada (raramente valem a leitura). Visto em 23/11: "Ibovespa hoje acompanha
# ajuste fiscal, PMIs e plano da Petrobras" juntou o plano da Petrobras com a votação do ajuste fiscal.
ROUNDUP = re.compile(
    r"\b(ibovespa|bolsas?|mercados?|dolar|day trade)\s+hoje\b|\be mais destaques\b|\bdestaques do dia\b"
    r"|\bo que (acompanhar|esperar|saber|move)\b|\bminuto a minuto\b|\bao vivo\b|\bresumo do dia\b"
    r"|^agenda\b|\bagenda do dia\b|\bsaiba quais\b.*\bpauta\b|\bmarkets wrap\b|^firstft\b|\bwhat to watch\b"
    r"|\blive updates\b|\bmorning (briefing|bid)\b|\bevening briefing\b")
ROUNDUP_MAX = 60
STOP = {"de", "da", "do", "das", "dos", "the", "and", "for", "von"}


def is_roundup(it: dict) -> bool:
    return bool(ROUNDUP.search(fold(it["title"])))


def _has_actor(it: dict, who: str) -> bool:
    """A manchete cita o agente? Por palavra (3+ letras) ou radical de 5 letras ('Petrobras' ~ 'Petrobrás')."""
    ww = [w for w in re.findall(r"[a-z0-9]{3,}", fold(who or "")) if w not in STOP and w not in GENERIC]
    if not ww:
        return False
    tw = set(re.findall(r"[a-z0-9]{3,}", fold(it["title"])))
    return any(w in tw or (len(w) >= 5 and any(t[:5] == w[:5] for t in tw if len(t) >= 5)) for w in ww)


def _head_ok(head: str, lead_item: dict) -> bool:
    hw = set(re.findall(r"[a-z0-9]+", fold(head or "")))
    tw = set(re.findall(r"[a-z0-9]+", fold(lead_item["title"])))
    return bool(hw & tw)


def _connected(idx: list[int], lead: int, batch: list[dict]) -> tuple[list[int], list[int]]:
    """Itens precisam dividir uma palavra (4+ letras, não genérica) com o lead, direto ou em cadeia."""
    ok, todo = {lead}, set(idx) - {lead}
    changed = True
    while changed and todo:
        changed = False
        for j in list(todo):
            if any(_title_words(batch[j]) & _title_words(batch[k]) for k in ok):
                ok.add(j)
                todo.remove(j)
                changed = True
    return sorted(ok), sorted(todo)


def judge_batch(brief, secs, batch: list[dict], votes: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Devolve (histórias, extraviados). Itens que a LLM esqueceu viram histórias sem nota."""
    valid_secs = {i for i, _ in secs}
    data = parse_json(call_llm(build_prompt(brief, secs, batch, pick_examples(batch, votes or []))))
    if not data or not isinstance(data.get("stories"), list):
        return [_single(it, None) for it in batch], []

    used: set[int] = set()
    stories, strays = [], []
    for s in data["stories"]:
        try:
            idx = sorted({int(i) for i in s.get("items", []) if 0 <= int(i) < len(batch)} - used)
            lead = int(s.get("lead", idx[0] if idx else -1))
        except (TypeError, ValueError):
            continue
        if not idx:
            continue
        if lead not in idx:
            lead = idx[0]
        if not _head_ok(str(s.get("head", "")), batch[lead]):
            strays += [batch[i] for i in idx]   # índices escorregaram
            used.update(idx)
            continue
        used.update(idx)
        try:
            score = max(0, min(100, int(s.get("s", 0))))
        except (TypeError, ValueError):
            score = None
        sec = str(s.get("sec", "0"))
        sec = sec if sec in valid_secs else "0"
        why = str(s.get("why", ""))[:80]
        # Resumos do dia saem do grupo e viram história própria, com nota limitada.
        rounds = [i for i in idx if is_roundup(batch[i])]
        for i in rounds:
            stories.append({"items": [batch[i]], "score": None if score is None else min(score, ROUNDUP_MAX),
                            "sec": sec, "why": "resumo do dia"})
        idx = [i for i in idx if i not in rounds]
        if not idx:
            continue
        if lead not in idx:
            lead = idx[0]
        good, bad = _connected(idx, lead, batch)
        if len(good) > 1:  # mesmo agente: toda manchete do grupo precisa citá-lo
            who = str(s.get("who", ""))
            if not _has_actor(batch[lead], who):
                bad, good = bad + [i for i in good if i != lead], [lead]
            else:
                bad += [i for i in good if not _has_actor(batch[i], who)]
                good = [i for i in good if _has_actor(batch[i], who)]
        strays += [batch[i] for i in bad]
        stories.append({
            "items": [batch[lead]] + [batch[i] for i in good if i != lead],
            "score": score,
            "sec": sec,
            "why": why,
        })
    stories += [_single(batch[i], None) for i in range(len(batch)) if i not in used]
    return stories, strays


def _single(item: dict, score) -> dict:
    groups = item.get("groups") or []
    return {"items": [item], "score": score, "sec": groups[0] if groups else "0", "why": ""}


# ---------- passada de junção: a mesma notícia partida entre lotes ----------

MERGE_MIN = 50    # só histórias com nota a partir daqui entram na junção
MERGE_MAX = 150   # teto de histórias numa chamada (tamanho do prompt)


def _story_words(s: dict) -> set[str]:
    return set().union(*(_title_words(i) for i in s["items"]))


def _connected_group(idx: list[int], cands: list[dict]) -> list[int]:
    """Maior grupo ligado por palavras de título (direto ou em cadeia), a partir do de maior nota."""
    if not idx:
        return []
    start = max(idx, key=lambda i: cands[i]["score"] or 0)
    ok, todo, changed = {start}, set(idx) - {start}, True
    while changed and todo:
        changed = False
        for j in list(todo):
            if any(_story_words(cands[j]) & _story_words(cands[k]) for k in ok):
                ok.add(j)
                todo.remove(j)
                changed = True
    return sorted(ok)


def merge_pass(stories: list[dict]) -> list[dict]:
    """Uma chamada com o lead de cada história forte: junta as que são o mesmo fato ('same') e
    aninha as de tema muito próximo sob a melhor ('related'). Se a LLM falhar, nada muda."""
    cands = sorted((s for s in stories if (s["score"] or 0) >= MERGE_MIN and not is_roundup(s["items"][0])),
                   key=lambda s: -(s["score"] or 0))[:MERGE_MAX]
    if len(cands) < 2:
        return stories
    lines = "\n".join(f"[{k}] ({s['items'][0]['source']}) {s['items'][0]['title']}" for k, s in enumerate(cands))
    prompt = f"""Cada linha abaixo é uma história diferente de um noticiário, em português ou inglês.
Encontre:
- "same": grupos de linhas que relatam O MESMO acontecimento: o mesmo ato, da mesma pessoa ou empresa,
  no mesmo dia (a mesma emissão, a mesma decisão judicial, o mesmo dado divulgado, o mesmo fechamento de
  bolsa). Ler uma torna as outras redundantes. Português e inglês sobre o mesmo fato contam como o mesmo.
  NÃO são o mesmo: depoimentos de pessoas diferentes no mesmo caso; decisões diferentes do mesmo tribunal;
  uma empresa e a reação do mercado a ela; duas empresas fazendo coisas parecidas.
- "related": desdobramentos DIFERENTES de um mesmo caso ou da mesma empresa (ex.: dois depoimentos no
  mesmo inquérito; o plano de investimento e o projeto de usinas da mesma companhia).
  NÃO são relacionadas: empresas diferentes no mesmo setor; o mesmo país em assuntos diferentes.
Na dúvida, não agrupe: deixar separado é sempre aceitável, juntar errado esconde notícia.
Todo grupo tem um agente comum ("who": a empresa, pessoa ou órgão, escrito como aparece nas linhas), e TODAS
as linhas do grupo precisam citá-lo. Se não houver um agente citado em todas, não é grupo.
Cada linha entra em no máximo um grupo. Grupos têm 2 ou mais linhas.
Responda só com JSON: {{"same": [{{"items": [int], "who": str}}], "related": [{{"items": [int], "who": str}}]}}

HISTÓRIAS:
{lines}"""
    data = parse_json(call_llm(prompt))
    if not data:
        log.info("junção: sem resposta da LLM, histórias ficam como estão")
        return stories

    used: set[int] = set()

    def groups(key: str) -> list[list[int]]:
        out = []
        for g in data.get(key) or []:
            if not isinstance(g, dict) or not str(g.get("who", "")).strip():
                continue  # sem agente comum declarado: não junta
            who = str(g["who"])
            try:
                idx = sorted({int(i) for i in g.get("items", []) if 0 <= int(i) < len(cands)} - used)
            except (TypeError, ValueError):
                continue
            # toda história do grupo precisa citar o agente no título principal
            idx = [i for i in idx if _has_actor(cands[i]["items"][0], who)]
            idx = _connected_group(idx, cands)  # mesmo guarda dos lotes: precisa dividir palavras
            if key == "same" and len(idx) >= 2:
                # "mesmo fato" exige mais: 2+ palavras distintivas em comum entre os TÍTULOS PRINCIPAIS e o da
                # história de maior nota. Perde algumas junções português/inglês, mas juntar errado esconde
                # notícia (pior que ver uma repetida).
                best = max(idx, key=lambda i: cands[i]["score"] or 0)
                stems = lambda s: {w[:5] for w in _title_words(s["items"][0])}  # 'contábeis' ~ 'contabilidade'
                bw = stems(cands[best])
                idx = [i for i in idx if i == best or len(stems(cands[i]) & bw) >= 2]
            if len(idx) >= 2:
                log.info("junção %s [%s]: %s", key, who, " || ".join(cands[i]["items"][0]["title"][:50] for i in idx))
                used.update(idx)
                out.append(idx)
        return out

    gone: set[int] = set()  # id() das histórias absorvidas
    n_same = n_rel = 0
    for idx in groups("same"):
        best = max((cands[i] for i in idx), key=lambda s: s["score"] or 0)
        for i in idx:
            s = cands[i]
            if s is best:
                continue
            seen = {it["link"] for it in best["items"]}
            best["items"] += [it for it in s["items"] if it["link"] not in seen]
            best.setdefault("related", []).extend(s.get("related", []))
            gone.add(id(s))
            n_same += 1
    for idx in groups("related"):
        best = max((cands[i] for i in idx), key=lambda s: s["score"] or 0)
        for i in idx:
            s = cands[i]
            if s is best or id(s) in gone:
                continue
            best.setdefault("related", []).append(
                {"items": s["items"], "score": s["score"], "why": s.get("why", "")})
            best["related"].extend(s.get("related", []))
            gone.add(id(s))
            n_rel += 1
    log.info("junção: %d histórias unidas como mesmo fato, %d aninhadas como relacionadas", n_same, n_rel)
    return [s for s in stories if id(s) not in gone]


# ---------- orquestração ----------

def judge_stories(pool: list[dict]) -> tuple[list[dict], str]:
    """Devolve (histórias, modo). modo = 'ai' se a LLM respondeu, 'rules' se só palavra-chave."""
    brief = load_brief()
    secs = sections(brief)
    seq = order_by_similarity(pool)
    batches = [[pool[i] for i in seq[k:k + BATCH]] for k in range(0, len(seq), BATCH)]
    votes = load_votes()
    log.info("julgando %d manchetes em %d lotes; %d votos como exemplos", len(pool), len(batches), len(votes))

    stories, strays = [], []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for st, sy in ex.map(lambda b: judge_batch(brief, secs, b, votes), batches):
            stories += st
            strays += sy
    if strays:  # uma segunda chance, num lote só deles
        log.info("%d extraviados, nova rodada", len(strays))
        for k in range(0, len(strays), BATCH):
            st, sy = judge_batch(brief, secs, strays[k:k + BATCH], votes)
            stories += st + [_single(it, None) for it in sy]

    scored = sum(1 for s in stories if s["score"] is not None)
    mode = "ai" if scored else "rules"
    if scored:
        stories = merge_pass(stories)
    for s in stories:
        if s["score"] is None:  # lote falhou: plano B por palavra-chave (fail-open)
            s["pass"] = any(match(f"{it['title']} {it['summary']}") for it in s["items"])
        else:
            s["pass"] = s["score"] >= THRESHOLD
    order = {i: n for n, (i, _) in enumerate(secs)}
    stories.sort(key=lambda s: (order.get(s["sec"], 99), -(s["score"] if s["score"] is not None else -1),
                                -len(s["items"])))
    log.info("modo=%s histórias=%d com_nota=%d passam=%d", mode, len(stories), scored,
             sum(s["pass"] for s in stories))
    return stories, mode
