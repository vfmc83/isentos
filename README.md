# Engage Isentos · Dashboard

Dashboard interativo dos 4 fundos isentos do Itaú (ISET11, ISEN11, ISTT11, ISNT11).

**Acesse**: https://engage-isentos.vercel.app

## Como funciona

1. **Atualização automática diária**: GitHub Actions roda `scripts/build_data.py` toda noite às 21h UTC (18h BRT). O script baixa:
   - Cotações da B3 dos 4 tickers via Yahoo Finance
   - PDFs `Sensibilidade-{TICKER}.pdf` do Itaú (cota patrimonial, PL, duration)
   - Série diária do CDI (BCB série 12)
   
2. **Geração de `data.json`**: O script consolida tudo em `data.json` na raiz e faz commit no repositório.

3. **Deploy automático**: A Vercel detecta o commit e republica o site em ~30 segundos.

4. **Cliente**: O `index.html` é estático. Ao carregar, faz `fetch('./data.json')` e renderiza o dashboard React com os dados frescos.

## Estrutura

```
.
├── index.html                  # Dashboard React (CDN, sem build)
├── data.json                   # Atualizado pelo Action (não editar à mão)
├── scripts/
│   └── build_data.py           # Baixa Yahoo + PDFs Itaú + CDI BCB
└── .github/workflows/
    └── daily.yml               # Schedule diário 21h UTC
```

## Rodar o script localmente (debug)

```bash
pip install pdfplumber
python scripts/build_data.py
```

Gera `data.json` na raiz. Abra `index.html` num servidor local:

```bash
python -m http.server 8000
# acesse http://localhost:8000
```

## Forçar atualização imediata

GitHub → aba **Actions** → workflow "Daily data update" → botão **Run workflow**.

## Troubleshooting

- **Site mostra "Erro ao carregar dados"**: o Vercel republicou antes do `data.json` ser commitado, ou a última execução do Action falhou. Veja a aba Actions.
- **Yahoo retornou 429**: rate limit. O script tem 3 retries com backoff; se ainda assim falhar, é só tentar de novo em 5 min (rodar manualmente).
- **PDF Itaú indisponível**: o script grava o que conseguiu e mantém os valores da última execução bem-sucedida. Não deixa o site quebrado.
