# Portal de notícias

Portal pessoal: todo dia às 6h (Brasília) o GitHub Actions lê ~20 feeds RSS, uma LLM gratuita
(OpenRouter) agrupa as manchetes em histórias e dá nota com base no brief, e o resultado é
publicado no GitHub Pages.

- `config/preferences.md` — o brief: o que quero ler. Os itens numerados em negrito viram as seções.
- `src/portal/core.py` — lista de fontes (`FEEDS`) e palavras-chave do plano B.
- `src/portal/judge.py` — agrupamento e notas pela LLM, com checagens contra índices errados.
- `build.py` — gera `site/index.html`.
- `.github/workflows/daily.yml` — agendamento diário.

Configuração no GitHub: segredo `LLM_API_KEY` (chave do OpenRouter). Opcionais, em variáveis:
`LLM_MODELS` (lista de modelos separada por vírgula; vazio = descobre os `:free`) e `JUDGE_THRESHOLD` (padrão 70).

Rodar local: `python build.py` (sem chave, sai só com palavras-chave).
