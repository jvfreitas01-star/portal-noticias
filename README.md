# Portal de notícias

Portal pessoal: todo dia às 6h, 12h e 18h (Brasília) o GitHub Actions lê ~20 feeds RSS, uma LLM gratuita
(OpenRouter) agrupa as manchetes em histórias e dá nota com base no brief, e o resultado é
publicado no GitHub Pages.

- `config/preferences.md` — o brief: o que quero ler. Os itens numerados em negrito viram as seções.
- `src/portal/core.py` — lista de fontes (`FEEDS`) e palavras-chave do plano B.
- `src/portal/judge.py` — agrupamento e notas pela LLM, com checagens contra índices errados.
- `src/portal/content.py` — texto completo, só de sites abertos (lista `HOSTS`).
- `src/portal/vault.py` — cifra os textos (AES-GCM, chave pela senha).
- `build.py` — gera `site/index.html` e `site/reader.json`.
- `labels/labels.jsonl` — votos 👍/👎 gravados pela página.
- `.github/workflows/daily.yml` — agendamento diário.

Configuração no GitHub: segredos `LLM_API_KEY` (chave do OpenRouter) e `READER_PASSWORD` (senha do leitor:
os textos completos vão cifrados em `reader.json`, porque o site é público; sem ela, não há leitor). Opcionais, em variáveis:
`LLM_MODELS` (modelos tentados em ordem, separados por vírgula; vazio = descobre os `:free`. Hoje:
Nemotron e Gemma grátis e `anthropic/claude-haiku-5.5` pago, só quando os grátis falham) e `JUDGE_THRESHOLD` (padrão 70).

Rodar local: `python build.py` (sem chave, sai só com palavras-chave).
