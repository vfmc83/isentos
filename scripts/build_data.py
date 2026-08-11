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
# Snapshot embutido no HTML (script classico, funciona com file:// sem CORS) para
# quando o dashboard e aberto localmente por duplo-clique, sem servidor. Ver uso
# em index.html (AppLoader) -- fallback apenas se o fetch de data.json falhar.
OUT_PATH_JS = Path(__file__).resolve().parent.parent / "data.js"

ITAU_TICKERS = ["ISET11", "ISEN11", "ISTT11", "ISNT11"]
# BISE11 (Bradesco Isento 2031) e de outro emissor e nao tem PDF de sensibilidade
# no mesmo formato/URL do Itau. Sua cota patrimonial e derivada de anchors.json +
# CDI (ver secao 4.1 em main()), nao de PDF baixado automaticamente.
TICKERS = ITAU_TICKERS + ["BISE11"]

ITAU_PDF_URLS = {
    t: f"https://assetfront.arquivosparceiros.cloud.itau.com.br/FND/Sensibilidade-{t[:-2]}.pdf"
    for t in ITAU_TICKERS
}

# BISE11 - feed publico (MZiQ) que alimenta o widget de sensibilidade em
# https://bradescoasset.com.br/fundos/bise11/. O token abaixo NAO e credencial
# de usuario: e um token de leitura publico, embutido no HTML da propria
# pagina (window.sensibilidadeConfig), usado pelo JS do site para popular o
# mesmo widget que qualquer visitante ve sem login. Usado aqui so para extrair
# a linha "Cota Patrimonial (dd/mm/aa): R$ x,xx" que o widget exibe.
BISE11_SENSIBILIDADE_URL = "https://gestora-bradescoasset.mz-sites.com/wp-json/mziq/v1/spreadsheet"
BISE11_SENSIBILIDADE_TOKEN = (
    "nUONu5fqor64lnA8EQMoJ2lzMjJhM2gweFU4TEF2UDJyMkoyVjJnSFVYbVZXT1pjbk1KdGU2bmsxZnM9"
)

# Dois hosts espelhados do Yahoo. Se query1 falhar/zerar, tenta query2.
YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
YAHOO_URL_TPL = (
    "https://{host}/v8/finance/chart/{symbol}.SA"
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
    "BISE11": {
        "cnpj": "64.964.326/0001-11",
        "nome_completo": "Bradesco Isento 2031",
        "cota_inicial": 100.00,
        "data_liq_primaria": "2026-03-31",
        "data_inicio_fundo": "2026-02-13",
        "vencimento": "2031-03-31",
        "taxa_adm": 0.20,
        "tipo": "FIC FI-Infra Isento 2031",
        # Sem PDF automatico (ver ITAU_PDF_URLS), entao yld_pct_cdi nao vem de PDF
        # como nos fundos Itau -- precisa estar aqui, senao fica None e quebra
        # (NaN) o Simulador de Yield e o badge do card. Retorno-alvo divulgado
        # pela Bradesco Asset: 95% CDI para quem entrou na oferta primaria.
        "yld_pct_cdi": 95.0,
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


def contar_du_b3(data_ini_iso: str, data_fim_iso: str) -> int:
    """Conta dias úteis estritamente após data_ini até data_fim (inclusive).

    Mesma convenção do `contarDU()` do frontend (index.html) -- usada ali para
    o stat "DU até vencimento". Mantida em espelho aqui (não importada, já que
    um roda em JS no browser e o outro em Python no build) para que a Duration
    calculada do BISE11 bata com o DU até vencimento mostrado no card.
    """
    feriados_set = set(FERIADOS_B3)
    ini = datetime.strptime(data_ini_iso, "%Y-%m-%d").date()
    fim = datetime.strptime(data_fim_iso, "%Y-%m-%d").date()

    def is_du(d):
        if d.weekday() >= 5:
            return False
        return d.isoformat() not in feriados_set

    n = 0
    d = ini + timedelta(days=1)
    while d <= fim:
        if is_du(d):
            n += 1
        d += timedelta(days=1)
    return n


def duration_anos_du252(data_ref_iso: str, vencimento_iso: str) -> float:
    """Duration (anos) via convenção ANBIMA DU/252, assumindo pagamento único
    no vencimento (bullet) -- caso do BISE11, sem cupons intermediários. Para
    um zero-coupon/bullet, a duration de Macaulay é exatamente o prazo até o
    vencimento, então isso equivale ao "DU até vencimento" do card / 252.
    """
    du = contar_du_b3(data_ref_iso, vencimento_iso)
    return round(du / 252.0, 2)


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

    # Yield Cota patrimonial: armazenado como percentual (95, 96...), nao decimal
    m = re.search(r"Yield Cota patrimonial[^\d]*([\d.,]+)\s*%", texto, re.I)
    if m:
        out["yld_pct_cdi"] = float(m.group(1).replace(",", "."))

    m = re.search(r"Quantidade total de cotas\s*([\d.,]+)", texto, re.I)
    if m:
        out["qtd_cotas"] = float(m.group(1).replace(".", "").replace(",", "."))

    m = re.search(r"Posi[çc][ãa]o em Caixa\s*([\d.,]+)\s*%", texto, re.I)
    if m:
        out["caixa_pct"] = float(m.group(1).replace(",", "."))

    return out


def fetch_bise11_cota() -> dict:
    """Busca a Cota Patrimonial (dd/mm/aa) do BISE11 no feed publico da Bradesco
    Asset (mesmo usado pelo widget em bradescoasset.com.br/fundos/bise11/).

    Faz o parse do JSON e procura a frase "Cota Patrimonial (dd/mm/aa): R$ x,xx"
    em qualquer célula da planilha (em vez de depender de uma chave fixa), pra
    não quebrar se o layout da planilha mudar.

    Retorna {"data_ref": "YYYY-MM-DD", "cota_patrimonial_ref": float} ou {} se
    falhar (nesse caso o chamador cai no fallback por ancoras+CDI).
    """
    url = f"{BISE11_SENSIBILIDADE_URL}?token={BISE11_SENSIBILIDADE_TOKEN}"
    try:
        raw = http_get(url, accept="application/json")
        payload = json.loads(raw)
    except Exception as e:
        print(f"    FALHA (fetch feed Bradesco): {e}", flush=True)
        return {}

    # Achata todas as células de texto da planilha num único blob e procura a
    # frase ali -- json.loads já desfaz o escaping ("\/" -> "/") do payload.
    celulas = []
    worksheet = payload.get("Worksheet") if isinstance(payload, dict) else None
    if isinstance(worksheet, dict):
        for linha in worksheet.values():
            if isinstance(linha, dict):
                celulas.extend(str(v) for v in linha.values() if v)
    texto = " | ".join(celulas) if celulas else json.dumps(payload, ensure_ascii=False)

    m = re.search(
        r"Cota Patrimonial\s*\((\d{2})/(\d{2})/(\d{2})\)\s*:\s*R\$\s*([\d.,]+)",
        texto, re.I,
    )
    if not m:
        print("    FALHA: 'Cota Patrimonial' nao encontrada no feed Bradesco", flush=True)
        return {}

    dd, mm, yy, val = m.groups()
    try:
        cota = float(val.replace(".", "").replace(",", "."))
    except ValueError:
        return {}
    return {"data_ref": f"20{yy}-{mm}-{dd}", "cota_patrimonial_ref": cota}


def fetch_yahoo_historico(ticker: str) -> list:
    """Histórico OHLCV de 1 ano via Yahoo Finance.

    Tenta query1 e, se falhar ou retornar série vazia, faz fallback para query2
    antes de desistir. Reduz o status 'partial' quando um host está com rate-limit.
    """
    ultimo_erro = None
    for host in YAHOO_HOSTS:
        url = YAHOO_URL_TPL.format(host=host, symbol=ticker)
        try:
            raw = http_get(url, accept="application/json")
            data = json.loads(raw)
            result = (data.get("chart") or {}).get("result")
            if not result or not isinstance(result, list):
                raise RuntimeError(f"Yahoo[{host}]: sem 'chart.result' para {ticker}")
            serie = _parse_yahoo_result(result[0])
            if serie:
                return serie
            ultimo_erro = RuntimeError(f"Yahoo[{host}]: série vazia para {ticker}")
        except Exception as e:
            ultimo_erro = e
            print(f"    aviso {host}: {e}", flush=True)
    raise RuntimeError(f"Yahoo falhou em todos os hosts para {ticker}: {ultimo_erro}")


def _parse_yahoo_result(r0: dict) -> list:
    """Converte o bloco chart.result[0] do Yahoo numa série [{d,c,v,q,n}]."""
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


def gerar_historico_patrim(cdi_serie):
    """Gera serie diaria de cota patrimonial por fundo usando ancoras + CDI BCB.

    Logica (metodo B - carrego CDI com cravamento):
    - Entre duas ancoras: aplica CDI BCB realizado * yld dia a dia (formato suave
      seguindo o carrego), depois aplica um fator de ajuste distribuido
      geometricamente para CRAVAR exatamente na cota oficial da ancora seguinte.
      Isso elimina degraus artificiais e mantem fidelidade as cotas oficiais.
    - Presente (apos ultima ancora ate hoje): aplica CDI BCB realizado * yld.
      Se o CDI do dia ainda nao foi publicado, usa o ultimo CDI disponivel.
    - Futuro NAO e gerado aqui (o frontend projeta).
    """
    anchors_path = Path(__file__).resolve().parent / "anchors.json"
    if not anchors_path.exists():
        print(f"  ANCHORS nao encontrado: {anchors_path}", flush=True)
        return {}
    with open(anchors_path, "r", encoding="utf-8") as f:
        anchors_data = json.load(f)

    feriados_set = set(FERIADOS_B3)

    def parse_d(iso):
        return datetime.strptime(iso, "%Y-%m-%d").date()

    def is_du(d):
        if d.weekday() >= 5:
            return False
        return d.isoformat() not in feriados_set

    def listar_du(d_ini, d_fim):
        out = []
        d = d_ini
        while d <= d_fim:
            if is_du(d):
                out.append(d.isoformat())
            d = d + timedelta(days=1)
        return out

    # Ultimo CDI diario disponivel (para projetar dias sem CDI publicado)
    cdi_ordenado = sorted(cdi_serie.items())
    ultimo_cdi = cdi_ordenado[-1][1] if cdi_ordenado else 0.0

    def cdi_do_dia(iso):
        v = cdi_serie.get(iso)
        return v if v is not None else ultimo_cdi

    hoje = datetime.now(timezone.utc).date()
    result = {}

    for ticker, info in anchors_data.items():
        ancoras = info.get("ancoras", [])
        yld = float(info.get("yld_pct_cdi", 0)) / 100.0
        if not ancoras:
            result[ticker] = []
            continue

        serie = []
        # Interpolacao por janela entre ancoras consecutivas (carrego CDI + cravamento)
        for i in range(len(ancoras) - 1):
            d_ini = parse_d(ancoras[i]["data"])
            d_fim = parse_d(ancoras[i + 1]["data"])
            c_ini = float(ancoras[i]["cota"])
            c_fim = float(ancoras[i + 1]["cota"])
            dus = listar_du(d_ini, d_fim)
            n = len(dus) - 1
            if n <= 0:
                continue

            # Passo 1: cota provisoria por CDI realizado * yld
            provis = [c_ini]
            cur = c_ini
            for j in range(1, len(dus)):
                cur = cur * (1.0 + cdi_do_dia(dus[j]) * yld)
                provis.append(cur)

            # Passo 2: fator de cravamento distribuido geometricamente
            #   cota_final = provis[k] * (c_fim/provis[-1]) ** (k/n)
            alvo = c_fim / provis[-1] if provis[-1] != 0 else 1.0
            for k, iso in enumerate(dus):
                if k == 0:
                    if not serie or serie[-1]["d"] != iso:
                        serie.append({"d": iso, "cota": round(c_ini, 2)})
                else:
                    ajustada = provis[k] * (alvo ** (k / n))
                    serie.append({"d": iso, "cota": round(ajustada, 2)})
            serie[-1]["cota"] = round(c_fim, 2)

        # Estende da ultima ancora ate hoje usando CDI BCB realizado * yld
        ultima_data = parse_d(ancoras[-1]["data"])
        ultima_cota = float(ancoras[-1]["cota"])
        if ultima_data < hoje:
            dus_futuro = listar_du(ultima_data, hoje)
            cur = ultima_cota
            for j, iso in enumerate(dus_futuro):
                if j == 0:
                    continue
                cur = cur * (1.0 + cdi_do_dia(iso) * yld)
                serie.append({"d": iso, "cota": round(cur, 2)})

        result[ticker] = serie

    return result


def main():
    print(f"[{datetime.now()}] Iniciando build_data.py", flush=True)

    fundos = {}
    for t, base in FUNDOS_BASE.items():
        fundos[t] = dict(base)

    # data.json anterior — carregado cedo para servir de fallback de PDF e de mercado.
    prev = {}
    if OUT_PATH.exists():
        try:
            prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  aviso: não foi possível ler data.json anterior: {e}", flush=True)
    prev_fundos = prev.get("fundos") or {}

    # 1) PDFs Itaú — Cota Patrimonial, PL, Duration, Yield. O frontend (FundoCard)
    #    depende desses campos; se faltarem, a página fica em branco.
    PDF_FIELDS = ("data_ref", "cota_patrimonial_ref", "pl", "duration",
                  "yld_pct_cdi", "qtd_cotas", "caixa_pct")
    PDF_ESSENCIAIS = ("cota_patrimonial_ref", "pl", "duration", "yld_pct_cdi")
    pdf_status = {}
    print("\n=== Baixando PDFs Itaú ===", flush=True)
    for t, url in ITAU_PDF_URLS.items():
        print(f"  {t}: {url}", flush=True)
        try:
            pdf_bytes = http_get(url, accept="application/pdf")
            extraido = parse_sensibilidade_pdf(pdf_bytes)
            for k, v in extraido.items():
                if v is not None:
                    fundos[t][k] = v
            faltando = [k for k in PDF_ESSENCIAIS if fundos[t].get(k) is None]
            pdf_status[t] = "ok" if not faltando else "parcial"
            print(f"    {pdf_status[t].upper()}: {extraido}", flush=True)
        except Exception as e:
            pdf_status[t] = "falha"
            print(f"    FALHA: {e}", flush=True)

    # 1.1) Carry-forward: campos essenciais ausentes herdam do data.json anterior,
    #      para um tropeço do PDF do Itaú não derrubar o dashboard (mesma lógica do
    #      fallback de mercado do Yahoo). pdf_status sinaliza degradação p/ o monitor.
    for t in ITAU_PDF_URLS:
        pf = prev_fundos.get(t) or {}
        herdados = []
        for k in PDF_FIELDS:
            if fundos[t].get(k) is None and pf.get(k) is not None:
                fundos[t][k] = pf[k]
                herdados.append(k)
        if herdados:
            print(f"    {t}: herdado do anterior -> {herdados}", flush=True)
            if pdf_status.get(t) != "ok":
                pdf_status[t] = f"{pdf_status.get(t, '?')}+herdado"
        faltando = [k for k in PDF_ESSENCIAIS if fundos[t].get(k) is None]
        if faltando:
            pdf_status[t] = f"{pdf_status.get(t, '?')}(faltou:{','.join(faltando)})"

    # 1.2) BISE11 - Cota Patrimonial via feed público da Bradesco Asset (mesmo
    #      widget de bradescoasset.com.br/fundos/bise11/). Se falhar, o card
    #      cai no fallback por âncoras+CDI (seção 4.1 abaixo).
    print("\n=== Buscando Cota Patrimonial BISE11 (Bradesco) ===", flush=True)
    bise11_live = fetch_bise11_cota()
    if bise11_live:
        fundos["BISE11"]["cota_patrimonial_ref"] = bise11_live["cota_patrimonial_ref"]
        fundos["BISE11"]["data_ref"] = bise11_live["data_ref"]
        print(f"    OK: {bise11_live}", flush=True)
    else:
        print("    sem dado novo -- cai no fallback por âncoras+CDI (seção 4.1)", flush=True)

    # 2) Yahoo
    print("\n=== Baixando histórico Yahoo Finance ===", flush=True)
    historico = {}
    ok_yahoo = 0
    for t in TICKERS:
        try:
            print(f"  {t}.SA...", flush=True)
            historico[t] = fetch_yahoo_historico(t)
            print(f"    OK: {len(historico[t])} pregões", flush=True)
            if historico[t]:
                ok_yahoo += 1
        except Exception as e:
            print(f"    FALHA: {e}", flush=True)
            historico[t] = []

    # 2.3) Se TODOS os tickers zerarem, aborta sem sobrescrever um data.json bom.
    if ok_yahoo == 0:
        prev_hist = prev.get("historico") or {}
        tinha_antes = any(prev_hist.get(t) for t in TICKERS)
        if tinha_antes:
            print(
                "  ERRO: Yahoo retornou vazio para os 4 tickers. "
                "Mantendo data.json anterior (nao sobrescreve dado bom).",
                file=sys.stderr, flush=True,
            )
            sys.exit(1)
        print("  aviso: Yahoo vazio e nao ha data.json anterior; segue mesmo assim.", flush=True)

    # 2.2) Ultimo preco de mercado por fundo (fallback p/ o card quando a serie falha).
    #   Usa o ultimo ponto da serie de hoje; se hoje falhou, herda do data.json anterior.
    prev_fundos = prev.get("fundos") or {}
    for t in TICKERS:
        serie = historico.get(t) or []
        if serie:
            fundos[t]["ultimo_mercado"] = serie[-1]["c"]
            fundos[t]["data_mercado"] = serie[-1]["d"]
        else:
            pf = prev_fundos.get(t) or {}
            if pf.get("ultimo_mercado") is not None:
                fundos[t]["ultimo_mercado"] = pf["ultimo_mercado"]
                fundos[t]["data_mercado"] = pf.get("data_mercado")
                print(f"    {t}: mercado herdado do anterior ({pf.get('data_mercado')})", flush=True)

    # 3) CDI BCB
    print("\n=== Baixando CDI diario BCB (serie 12) ===", flush=True)
    try:
        cdi = fetch_cdi()
        print(f"  OK: {len(cdi)} dias", flush=True)
    except Exception as e:
        print(f"  FALHA: {e}", flush=True)
        cdi = {}

    # 4) Cota patrimonial calibrada (interpolacao por ancoras + projecao CDI)
    print("\n=== Gerando historico_patrim ===", flush=True)
    try:
        historico_patrim = gerar_historico_patrim(cdi)
        total = sum(len(v) for v in historico_patrim.values())
        print(f"  OK: {total} pontos em {len(historico_patrim)} fundos", flush=True)
    except Exception as e:
        print(f"  FALHA: {e}", flush=True)
        historico_patrim = {}

    # 4.1) Fundos sem PDF de sensibilidade (ex.: BISE11) usam a cota patrimonial
    #      calculada (ancoras + CDI) como cota_patrimonial_ref/data_ref do card,
    #      caso ainda nao tenham vindo de uma fonte ao vivo (ex.: BISE11 via
    #      feed publico da Bradesco na secao 1.2 -- tem prioridade sobre isso).
    for t in TICKERS:
        if t in ITAU_PDF_URLS:
            continue
        if fundos[t].get("cota_patrimonial_ref") is not None:
            continue
        serie_pat = historico_patrim.get(t) or []
        if serie_pat:
            fundos[t]["cota_patrimonial_ref"] = serie_pat[-1]["cota"]
            fundos[t]["data_ref"] = serie_pat[-1]["d"]

    # 4.2) Duration do BISE11 -- sem PDF de sensibilidade (que traz a Duration
    #      pronta pros fundos Itau), entao calculamos: pagamento unico no
    #      vencimento (bullet, sem cupom), logo a duration de Macaulay e
    #      exatamente o prazo ate o vencimento. Convencao ANBIMA DU/252, igual
    #      ao "DU ate vencimento" ja mostrado no card (mesma contagem de dias
    #      uteis do frontend -- ver contar_du_b3()).
    if fundos["BISE11"].get("data_ref"):
        try:
            fundos["BISE11"]["duration"] = duration_anos_du252(
                fundos["BISE11"]["data_ref"], fundos["BISE11"]["vencimento"]
            )
        except Exception as e:
            print(f"    aviso: falha ao calcular duration BISE11: {e}", flush=True)

    # 5) Monta JSON final
    out = {
        "fundos": fundos,
        "historico": historico,
        "cdi_serie": cdi,
        "feriados": FERIADOS_B3,
        "data_geracao": datetime.now(timezone.utc).isoformat(),
        "historico_patrim": historico_patrim,
        "pdf_status": pdf_status,
    }

    out_json = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    OUT_PATH.write_text(out_json, encoding="utf-8")
    OUT_PATH_JS.write_text(f"window.__ENGAGE_DATA__ = {out_json};", encoding="utf-8")

    tamanho_kb = OUT_PATH.stat().st_size / 1024
    print(f"\n=== OK gerado: {OUT_PATH} ({tamanho_kb:.1f} KB) | fallback: {OUT_PATH_JS.name} ===", flush=True)

    # Sinaliza degradação dos PDFs (build segue verde, mas avisa no log p/ o monitor).
    degradado = {t: s for t, s in pdf_status.items() if not s.startswith("ok")}
    if degradado:
        print(f"AVISO: PDFs Itaú degradados (usando carry-forward): {degradado}",
              file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
