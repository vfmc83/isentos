"""
Engage Isentos — build_data.py

Baixa diariamente:
  - Cotações da B3 dos 4 tickers via Yahoo Finance (1 ano de histórico)
  - PDFs Sensibilidade-{TICKER}.pdf do Itaú (Cota Patrimonial, PL, Duration, Yield)
  - Série CDI diário do BCB (série 12)
  - Calendário de feriados B3

Gera:
  - data.json na raiz do repositório, lido pelo dashboard React no client
"""

import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# pdfplumber é mais robusto para extração de PDFs em layout tabular
try:
    import pdfplumber
except ImportError:
    print("ERRO: instale pdfplumber: pip install pdfplumber", file=sys.stderr)
    sys.exit(1)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/605.1.15 EngageDashboard/1.0"
TIMEOUT = 30

OUT_PATH = Path(__file__).resolve().parent.parent / "data.json"

TICKERS = ["ISET11", "ISEN11", "ISTT11", "ISNT11"]

ITAU_PDF_URLS = {
    t: f"https://assetfront.arquivosparceiros.cloud.itau.com.br/FND/Sensibilidade-{t[:-2]}.pdf"
    for t in TICKERS
}

YAHOO_URL_TPL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.SA"
    "?range=1y&interval=1d"
)

BCB_CDI_URL_TPL = (
    "https://api.bcb.gov.br/dados/serie/bcdata.sgs.12/dados"
    "?formato=json&dataInicial={di}&dataFinal={df}"
)

# Metadata fixos por fundo (não muda; só o que vem do PDF muda)
FUNDOS_BASE = {
    "ISET11": {
        "cnpj": "60.365.671/0001-88",
        "nome_completo": "Itaú Isento Setembro 28",
        "cota_inicial": 100.00,
        "data_liq_primaria": "2025-06-11",
        "data_inicio_fundo": "2025-05-19",
        "vencimento": "2028-09-29",
        "taxa_adm": 0.301,
        "tipo": "FIC FII Cotas Set 28",
    },
    "ISEN11": {
        "cnpj": "60.365.845/0001-02",
        "nome_completo": "Itaú Isento Março 29",
        "cota_inicial": 100.00,
        "data_liq_primaria": "2025-06-05",
        "data_inicio_fundo": "2025-05-19",
        "vencimento": "2029-03-29",
        "taxa_adm": 0.301,
        "tipo": "FIC FII Cotas Mar 29",
    },
    "ISTT11": {
        "cnpj": "61.351.720/0001-96",
        "nome_completo": "Itaú Isento Setembro 29",
        "cota_inicial": 1000.00,
        "data_liq_primaria": "2025-08-27",
        "data_inicio_fundo": "2025-06-25",
        "vencimento": "2029-09-28",
        "taxa_adm": 0.361,
        "tipo": "RF Duração Livre Set 29",
    },
    "ISNT11": {
        "cnpj": "60.365.430/0001-39",
        "nome_completo": "Itaú Isento Março 28",
        "cota_inicial": 1035.16,
        "data_liq_primaria": "2025-11-17",
        "data_inicio_fundo": "2025-05-19",
        "vencimento": "2028-03-29",
        "taxa_adm": 0.361,
        "tipo": "RF Duração Livre Mar 28",
    },
}

# Feriados B3 / bancários 2025-2029 (fonte: ANBIMA)
FERIADOS_B3 = [
    "2025-01-01","2025-03-03","2025-03-04","2025-04-18","2025-04-21","2025-05-01",
    "2025-06-19","2025-09-07","2025-10-12","2025-11-02","2025-11-15","2025-11-20",
    "2025-12-24","2025-12-25","2025-12-31",
    "2026-01-01","2026-02-16","2026-02-17","2026-04-03","2026-04-21","2026-05-01",
    "2026-06-04","2026-09-07","2026-10-12","2026-11-02","2026-11-15","2026-11-20",
    "2026-12-24","2026-12-25","2026-12-31",
    "2027-01-01","2027-02-08","2027-02-09","2027-03-26","2027-04-21","2027-05-01",
    "2027-05-27","2027-09-07","2027-10-12","2027-11-02","2027-11-15","2027-11-20",
    "2027-12-24","2027-12-25","2027-12-31",
    "2028-01-01","2028-02-28","2028-02-29","2028-04-14","2028-04-21","2028-05-01",
    "2028-06-15","2028-09-07","2028-10-12","2028-11-02","2028-11-15","2028-11-20",
    "2028-12-25",
    "2029-01-01","2029-02-12","2029-02-13","2029-03-30","2029-04-21","2029-05-01",
    "2029-05-31","2029-09-07","2029-10-12","2029-11-02","2029-11-15","2029-11-20",
    "2029-12-25","2029-12-31",
]


def http_get(url: str, *, accept: str = "*/*") -> bytes:
    """GET com User-Agent e retries simples."""
    ultimo = None
    for tentativa in range(3):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": accept,
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            ultimo = e
            time.sleep(2 ** tentativa)
    raise RuntimeError(f"Falhou após 3 tentativas: {url} | erro: {ultimo}")


def parse_sensibilidade_pdf(pdf_bytes: bytes) -> dict:
    """Extrai dados da Cota Patrimonial do PDF Sensibilidade-{TICKER}.pdf."""
    import io
    out = {
        "data_ref": None, "cota_patrimonial_ref": None, "pl": None,
        "duration": None, "yld_pct_cdi": None, "qtd_cotas": None, "caixa_pct": None,
    }
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texto = "\n".join((page.extract_text() or "") for page in pdf.pages)
    
    m = re.search(r"Data de refer[êe]ncia:\s*(\d{2})/(\d{2})/(\d{4})", texto, re.I)
    if m:
        out["data_ref"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    
    m = re.search(r"Valor Cota Patrimonial\s*([\d.,]+)", texto, re.I)
    if m:
        out["cota_patrimonial_ref"] = float(m.group(1).replace(".", "").replace(",", "."))
    
    m = re.search(r"Patrim[ôo]nio L[íi]quido\s*R\$\s*([\d.,]+)", texto, re.I)
    if m:
        out["pl"] = float(m.group(1).replace(".", "").replace(",", "."))
    
    m = re.search(r"Duration da carteira\s*([\d.,]+)", texto, re.I)
    if m:
        out["duration"] = float(m.group(1).replace(",", "."))
    
    m = re.search(r"Yield Cota patrimonial[^\d]*([\d.,]+)\s*%", texto, re.I)
    if m:
        out["yld_pct_cdi"] = float(m.group(1).replace(",", ".")) / 100.0
    
    m = re.search(r"Quantidade total de cotas\s*([\d.,]+)", texto, re.I)
    if m:
        out["qtd_cotas"] = float(m.group(1).replace(".", "").replace(",", "."))
    
    m = re.search(r"Posi[çc][ãa]o em Caixa\s*([\d.,]+)\s*%", texto, re.I)
    if m:
        out["caixa_pct"] = float(m.group(1).replace(",", "."))
    
    return out


def fetch_yahoo_historico(ticker: str) -> list:
    """Histórico OHLCV de 1 ano via Yahoo Finance."""
    url = YAHOO_URL_TPL.format(symbol=ticker)
    raw = http_get(url, accept="application/json")
    data = json.loads(raw)
    result = (data.get("chart") or {}).get("result")
    if not result or not isinstance(result, list):
        raise RuntimeError(f"Yahoo: sem 'chart.result' para {ticker}")
    r0 = result[0]
    timestamps = r0.get("timestamp") or []
    quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    
    serie = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes) or closes[i] is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        serie.append({
            "d": d,
            "c": round(float(closes[i]), 4),
            "v": int(volumes[i] or 0) if i < len(volumes) else 0,
            "q": 0,
            "n": 0,
        })
    serie.sort(key=lambda x: x["d"])
    
    # Adiciona "hoje" se ainda não estiver
    meta = r0.get("meta") or {}
    rmp = meta.get("regularMarketPrice")
    rmt = meta.get("regularMarketTime")
    if rmp and rmt:
        hoje_iso = datetime.fromtimestamp(rmt, tz=timezone.utc).strftime("%Y-%m-%d")
        if not serie or serie[-1]["d"] != hoje_iso:
            serie.append({
                "d": hoje_iso,
                "c": round(float(rmp), 4),
                "v": int(meta.get("regularMarketVolume") or 0),
                "q": 0, "n": 0,
            })
    return serie


def fetch_cdi() -> dict:
    """Série 12 do BCB: CDI diário em % ao dia desde 2025-05-01 até hoje."""
    hoje = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    url = BCB_CDI_URL_TPL.format(di="01/05/2025", df=hoje)
    raw = http_get(url, accept="application/json")
    arr = json.loads(raw)
    out = {}
    for item in arr:
        d, m, y = item["data"].split("/")
        out[f"{y}-{m}-{d}"] = float(item["valor"]) / 100.0
    return out


def main():
    print(f"[{datetime.now()}] Iniciando build_data.py", flush=True)
    
    fundos = {}
    for t, base in FUNDOS_BASE.items():
        fundos[t] = dict(base)
    
    # 1) PDFs Itaú
    print("\n=== Baixando PDFs Itaú ===", flush=True)
    for t, url in ITAU_PDF_URLS.items():
        print(f"  {t}: {url}", flush=True)
        try:
            pdf_bytes = http_get(url, accept="application/pdf")
            extraido = parse_sensibilidade_pdf(pdf_bytes)
            print(f"    OK: {extraido}", flush=True)
            for k, v in extraido.items():
                if v is not None:
                    fundos[t][k] = v
        except Exception as e:
            print(f"    FALHA: {e}", flush=True)
    
    # 2) Yahoo
    print("\n=== Baixando histórico Yahoo Finance ===", flush=True)
    historico = {}
    for t in TICKERS:
        try:
            print(f"  {t}.SA...", flush=True)
            historico[t] = fetch_yahoo_historico(t)
            print(f"    OK: {len(historico[t])} pregões", flush=True)
        except Exception as e:
            print(f"    FALHA: {e}", flush=True)
            historico[t] = []
    
    # 3) CDI BCB
    print("\n=== Baixando CDI diário BCB (série 12) ===", flush=True)
    try:
        cdi = fetch_cdi()
        print(f"  OK: {len(cdi)} dias", flush=True)
    except Exception as e:
        print(f"  FALHA: {e}", flush=True)
        cdi = {}
    
    # 4) Monta JSON final
    out = {
        "fundos": fundos,
        "historico": historico,
        "cdi_serie": cdi,
        "feriados": FERIADOS_B3,
        "data_geracao": datetime.now(timezone.utc).isoformat(),
    }
    
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    
    tamanho_kb = OUT_PATH.stat().st_size / 1024
    print(f"\n=== OK gerado: {OUT_PATH} ({tamanho_kb:.1f} KB) ===", flush=True)


if __name__ == "__main__":
    main()
