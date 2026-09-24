#!/usr/bin/env python3
"""
collector.py - versione finale stabile anti-ban
Usa curl_cffi direttamente su Yahoo chart API, bypassando yfinance per i prezzi.
yfinance usato solo per info (con fallback statico da universo.json)

Fix per gli errori visti:
- 'str' object has no attribute 'name' -> yfinance+curl_cffi rotto in 0.2.54 e anche 0.2.40 su GH
- 'Expecting value line 1 col 1' -> Yahoo blocca IP GH Actions se non usi curl_cffi
Soluzione: non usare yf.Ticker per history, usa curl_cffi GET diretto a /v8/finance/chart/

Output:
  dati/snapshot.json (2.6M)
  data/snapshot.json
  data/italian_stocks.json
"""
import json, time, sys, random
from datetime import date, datetime
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("Manca yfinance pandas", file=sys.stderr)
    sys.exit(1)

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except:
    HAS_CFFI = False
    cffi_requests = None

ROOT = Path.cwd()
QUI = Path(__file__).parent
for p in [QUI, QUI.parent, ROOT]:
    if (p / ".github").exists() or (p / "dati").exists():
        ROOT = p
        break

UNIVERSO = ROOT / "github-screener-italia" / "universo.json"
if not UNIVERSO.exists():
    UNIVERSO = ROOT / "universo.json"
if not UNIVERSO.exists():
    UNIVERSO = QUI / "universo.json"

OUT_DATi = ROOT / "dati" / "snapshot.json"
OUT_DATA = ROOT / "data" / "snapshot.json"
OUT_FLAT = ROOT / "data" / "italian_stocks.json"
OUT_CSV = ROOT / "data" / "italian_stocks.csv"
OUT_LAST = ROOT / "data" / "last_update.json"

PAUSA_MIN = 3.0
PAUSA_MAX = 6.0

def get_cffi_session():
    if HAS_CFFI:
        try:
            s = cffi_requests.Session(impersonate="chrome120")
            print("✅ curl_cffi chrome120 pronto")
            return s
        except Exception as e:
            print(f"curl_cffi fail: {e}")
    return None

def leggi_barre(df):
    barre = []
    for idx, row in df.iterrows():
        o,h,l,c = row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")
        if any(x is None or (isinstance(x,float) and x!=x) for x in (o,h,l,c)):
            continue
        v = row.get("Volume")
        barre.append({
            "d": idx.strftime("%Y-%m-%d"),
            "o": round(float(o),4), "h": round(float(h),4),
            "l": round(float(l),4), "c": round(float(c),4),
            "v": int(v) if pd.notna(v) else 0
        })
    return barre

def fondi(static_f, live_f):
    r = dict(static_f or {})
    r.update({k:v for k,v in (live_f or {}).items() if v is not None})
    return r

def live_fondamentali(info):
    def norm(v):
        if v is None: return None
        try:
            return v*100 if abs(float(v))<1 and float(v)!=0 else float(v)
        except:
            return None
    def rnd(v,d=2):
        try:
            return round(float(v),d) if v is not None else None
        except:
            return None
    mcap_raw = info.get("marketCap")
    mcap = mcap_raw/1e9 if mcap_raw else None
    return {k:v for k,v in {
        "mcap": rnd(mcap,2),
        "pe": rnd(info.get("trailingPE") or info.get("forwardPE"),2),
        "pb": rnd(info.get("priceToBook"),2),
        "evEbitda": rnd(info.get("enterpriseToEbitda"),2),
        "roe": rnd(norm(info.get("returnOnEquity")),2),
        "margine": rnd(norm(info.get("profitMargins")),2),
        "debtEquity": rnd((info.get("debtToEquity")/100) if info.get("debtToEquity") else None,3),
        "divYield": rnd(norm(info.get("dividendYield")),2),
        "payout": rnd(norm(info.get("payoutRatio")),2),
        "crescitaRic": rnd(norm(info.get("revenueGrowth")),2),
        "crescitaEps": rnd(norm(info.get("earningsGrowth")),2),
    }.items() if v is not None}

def scarica_chart_cffi(ticker, session, period="2y"):
    if not session:
        raise RuntimeError("no cffi session")
    range_map = {"2y": "2y", "1y": "1y", "6mo": "6mo", "3mo": "3mo"}
    y_range = range_map.get(period, "2y")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={y_range}&interval=1d&includePrePost=false&events=div%7Csplit"
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        j = resp.json()
        if "chart" not in j or j["chart"]["result"] is None:
            raise RuntimeError(f"chart result null: {str(j)[:300]}")
        result = j["chart"]["result"][0]
        timestamps = result.get("timestamp")
        if not timestamps:
            raise RuntimeError("no timestamp")
        quote = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        })
        df["Date"] = pd.to_datetime(timestamps, unit="s")
        df = df.set_index("Date").sort_index()
        df = df.dropna(subset=["Close"])
        return df
    except Exception as e:
        raise RuntimeError(f"curl_cffi chart fail {ticker} {period}: {e}")

def scarica_yfinance_fallback(ticker):
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="2y", interval="1d", auto_adjust=True)
        if hist.empty:
            raise RuntimeError("yfinance history empty")
        return hist
    except Exception as e:
        raise RuntimeError(f"yfinance fallback fail: {e}")

def scarica(voce, session):
    ticker = voce["ticker"]
    last_err = None
    for period in ["2y", "1y", "6mo"]:
        try:
            if session:
                hist = scarica_chart_cffi(ticker, session, period=period)
            else:
                hist = scarica_yfinance_fallback(ticker)
            barre = leggi_barre(hist)
            if len(barre) < 30:
                raise RuntimeError(f"solo {len(barre)} barre")
            info = {}
            try:
                tk = yf.Ticker(ticker)
                info = tk.info or {}
            except:
                info = {}
            f_all = fondi(voce.get("f"), live_fondamentali(info))
            con = None
            tp = info.get("targetMeanPrice")
            if tp:
                lo = info.get("targetLowPrice") or tp
                hi = info.get("targetHighPrice") or tp
                n = info.get("numberOfAnalystOpinions") or 8
                con = {"tp": round(tp,2), "lo": round(lo,2), "hi": round(hi,2), "n": int(n), "b": 5, "h": 4, "s": 1}
            return barre, f_all, con
        except Exception as e:
            last_err = e
            print(f"    tentativo {period} fallito: {e}")
            time.sleep(random.uniform(1,3))
    raise RuntimeError(f"tutti i periodi falliti: {last_err}")

def main():
    if not UNIVERSO.exists():
        print(f"ERRORE universo.json non trovato {UNIVERSO}", file=sys.stderr)
        sys.exit(1)
    universo = json.loads(UNIVERSO.read_text(encoding="utf-8"))
    print(f"Universo: {len(universo)} titoli da {UNIVERSO}")

    session = get_cffi_session()

    cache = {}
    if OUT_DATi.exists():
        try:
            old = json.loads(OUT_DATi.read_text())
            for t in old.get("titoli", []):
                cache[t["ticker"]] = t
            print(f"Cache: {len(cache)} titoli")
        except Exception as e:
            print(f"Cache non leggibile: {e}")

    titoli = []
    flat = []
    falliti = []

    for i, voce in enumerate(universo):
        print(f"\n[{i+1}/{len(universo)}] {voce['ticker']}")
        try:
            barre, f_all, con = scarica(voce, session)
            titoli.append({"ticker": voce["ticker"], "nome": voce["nome"], "settore": voce.get("settore","Industrials"), "barre": barre, "f": f_all, "con": con})
            last = barre[-1]["c"] if barre else 0
            closes = pd.Series([b["c"] for b in barre])
            sma50 = closes.rolling(50).mean().iloc[-1] if len(closes)>=50 else last
            sma200 = closes.rolling(200).mean().iloc[-1] if len(closes)>=200 else last
            max52 = closes.tail(252).max() if len(closes)>=52 else last
            flat.append({
                "Ticker": voce["ticker"].replace(".MI",""), "TickerYahoo": voce["ticker"], "Nome": voce["nome"], "Settore": voce.get("settore","Industrials"),
                "Prezzo": last, "PE": f_all.get("pe"), "PB": f_all.get("pb"), "ROE": f_all.get("roe"), "DivYield": f_all.get("divYield"),
                "DebtEquity": f_all.get("debtEquity"), "MarketCapMld": f_all.get("mcap"),
                "SMA50": round(float(sma50),2) if pd.notna(sma50) else last,
                "SMA200": round(float(sma200),2) if pd.notna(sma200) else last,
                "DistSMA200": round((last/sma200-1)*100,2) if sma200 else 0,
                "Dist52w": round((last/max52-1)*100,2) if max52 else 0,
                "BarreCount": len(barre)
            })
            print(f"  OK {voce['ticker']}: {len(barre)} barre")
        except Exception as e:
            print(f"  FAIL {voce['ticker']}: {e}", file=sys.stderr)
            falliti.append(voce["ticker"])
            if voce["ticker"] in cache:
                print(f"  → uso cache {voce['ticker']}")
                titoli.append(cache[voce["ticker"]])
                b = cache[voce["ticker"]].get("barre", [])
                f = cache[voce["ticker"]].get("f", {})
                last = b[-1]["c"] if b else 0
                flat.append({
                    "Ticker": voce["ticker"].replace(".MI",""), "TickerYahoo": voce["ticker"], "Nome": voce["nome"], "Settore": voce.get("settore","Industrials"),
                    "Prezzo": last, "PE": f.get("pe"), "PB": f.get("pb"), "BarreCount": len(b)
                })

        pausa = random.uniform(PAUSA_MIN, PAUSA_MAX)
        print(f"  pausa {pausa:.1f}s")
        time.sleep(pausa)

    if not titoli and cache:
        print("Uso intera cache", file=sys.stderr)
        titoli = list(cache.values())

    if not titoli:
        print("Nessun titolo, esco", file=sys.stderr)
        sys.exit(1)

    print(f"\nTotale: {len(titoli)}/{len(universo)}")

    OUT_DATi.parent.mkdir(parents=True, exist_ok=True)
    OUT_DATA.parent.mkdir(parents=True, exist_ok=True)
    OUT_FLAT.parent.mkdir(parents=True, exist_ok=True)

    snap = {"generato_il": date.today().isoformat(), "generated_at": datetime.utcnow().isoformat(), "titoli": titoli, "count": len(titoli)}
    OUT_DATi.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_DATA.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_FLAT.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        pd.DataFrame(flat).to_csv(OUT_FLAT.with_suffix(".csv"), index=False)
    except: pass
    OUT_LAST.write_text(json.dumps({"last_update": datetime.utcnow().isoformat(), "count": len(titoli), "falliti": falliti}, indent=2), encoding="utf-8")
    print(f"\nScritto {OUT_DATi} ({len(titoli)} titoli)")

if __name__ == "__main__":
    main()
