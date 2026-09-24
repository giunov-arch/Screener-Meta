#!/usr/bin/env python3
"""
collector.py - versione ORIGINALE funzionante, ripristinata
Logica:
- universo.json contiene già tutti i fondamentali statici (pe, pb, roe, divYield, cet1...)
- NON chiama mai tk.info / quoteSummary -> evita 429 Client Error
- Prende solo prezzi da Yahoo chart API via curl_cffi (bypassa blocco IP GitHub Actions)
- Se Yahoo blocca, usa cache da dati/snapshot.json
- Output: dati/snapshot.json + data/snapshot.json + data/italian_stocks.json
"""
import json, time, sys, random
from datetime import date, datetime
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("Manca pandas", file=sys.stderr)
    sys.exit(1)

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except:
    HAS_CFFI = False

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
OUT_LAST = ROOT / "data" / "last_update.json"

PAUSA_MIN = 2.5
PAUSA_MAX = 5.5

def get_session():
    if HAS_CFFI:
        try:
            s = cffi_requests.Session(impersonate="chrome120")
            print("✅ curl_cffi attivo")
            return s
        except Exception as e:
            print(f"curl_cffi fail: {e}")
    print("⚠️ curl_cffi non trovato - Yahoo potrebbe dare 429")
    return None

def fetch_chart(ticker, session, period="2y"):
    """Fetch diretto chart API - niente yfinance, niente quoteSummary"""
    if not session:
        raise RuntimeError("Serve curl_cffi session per bypassare blocco")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={period}&interval=1d&includePrePost=false"
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = session.get(url, headers=headers, timeout=25)
    if resp.status_code == 429:
        raise RuntimeError(f"429 Too Many Requests - {ticker}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} {resp.text[:200]}")
    j = resp.json()
    if "chart" not in j or j["chart"]["result"] is None:
        raise RuntimeError(f"chart null: {str(j)[:300]}")
    res = j["chart"]["result"][0]
    ts = res.get("timestamp")
    if not ts:
        raise RuntimeError("no timestamp")
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "Open": q.get("open"),
        "High": q.get("high"),
        "Low": q.get("low"),
        "Close": q.get("close"),
        "Volume": q.get("volume"),
    })
    df["Date"] = pd.to_datetime(ts, unit="s")
    df = df.set_index("Date").sort_index().dropna(subset=["Close"])
    if df.empty:
        raise RuntimeError("df empty dopo dropna")
    return df

def df_to_barre(df):
    barre = []
    for idx, row in df.iterrows():
        o,h,l,c = row["Open"], row["High"], row["Low"], row["Close"]
        if any(pd.isna(x) for x in (o,h,l,c)):
            continue
        v = row.get("Volume")
        barre.append({
            "d": idx.strftime("%Y-%m-%d"),
            "o": round(float(o),4), "h": round(float(h),4),
            "l": round(float(l),4), "c": round(float(c),4),
            "v": int(v) if pd.notna(v) else 0
        })
    return barre

def main():
    if not UNIVERSO.exists():
        print(f"ERRORE universo.json non trovato", file=sys.stderr)
        sys.exit(1)
    universo = json.loads(UNIVERSO.read_text(encoding="utf-8"))
    print(f"Universo: {len(universo)} titoli da {UNIVERSO}")

    session = get_session()
    if not session:
        print("ERRORE: curl_cffi necessario per GitHub Actions", file=sys.stderr)
        sys.exit(1)

    cache = {}
    if OUT_DATi.exists():
        try:
            old = json.loads(OUT_DATi.read_text())
            for t in old.get("titoli", []):
                cache[t["ticker"]] = t
            print(f"Cache: {len(cache)} titoli")
        except:
            pass

    titoli = []
    flat = []
    falliti = []

    for i, voce in enumerate(universo):
        ticker = voce["ticker"]
        print(f"\n[{i+1}/{len(universo)}] {ticker}")
        try:
            df = None
            last_err = None
            for period in ["2y", "1y", "6mo"]:
                try:
                    df = fetch_chart(ticker, session, period=period)
                    break
                except Exception as e:
                    last_err = e
                    print(f"  {period} fail: {e}")
                    time.sleep(random.uniform(1,2))
            if df is None:
                raise last_err

            barre = df_to_barre(df)
            if len(barre) < 30:
                raise RuntimeError(f"solo {len(barre)} barre")

            # usa SOLO fondamentali statici da universo.json - niente tk.info!
            f_static = voce.get("f", {})
            last = barre[-1]["c"]
            closes = pd.Series([b["c"] for b in barre])
            sma50 = closes.rolling(50).mean().iloc[-1] if len(closes)>=50 else last
            sma200 = closes.rolling(200).mean().iloc[-1] if len(closes)>=200 else last
            max52 = closes.tail(252).max() if len(closes)>=52 else last

            titoli.append({
                "ticker": ticker,
                "nome": voce["nome"],
                "settore": voce.get("settore","Industrials"),
                "barre": barre,
                "f": f_static,
                "con": None  # consenso vuoto, verrà calcolato da index.html
            })
            flat.append({
                "Ticker": ticker.replace(".MI",""),
                "TickerYahoo": ticker,
                "Nome": voce["nome"],
                "Settore": voce.get("settore","Industrials"),
                "Prezzo": last,
                "PE": f_static.get("pe"),
                "PB": f_static.get("pb"),
                "ROE": f_static.get("roe"),
                "DivYield": f_static.get("divYield"),
                "DebtEquity": f_static.get("debtEquity"),
                "MarketCapMld": f_static.get("mcap"),
                "SMA50": round(float(sma50),2) if pd.notna(sma50) else last,
                "SMA200": round(float(sma200),2) if pd.notna(sma200) else last,
                "DistSMA200": round((last/sma200-1)*100,2) if sma200 else 0,
                "Dist52w": round((last/max52-1)*100,2) if max52 else 0,
                "BarreCount": len(barre)
            })
            print(f"  OK {ticker}: {len(barre)} barre, prezzo {last}")

        except Exception as e:
            print(f"  FAIL {ticker}: {e}", file=sys.stderr)
            falliti.append(ticker)
            if ticker in cache:
                print(f"  → uso cache {ticker}")
                titoli.append(cache[ticker])
                b = cache[ticker].get("barre", [])
                f = cache[ticker].get("f", {})
                last = b[-1]["c"] if b else 0
                flat.append({
                    "Ticker": ticker.replace(".MI",""),
                    "TickerYahoo": ticker,
                    "Nome": voce["nome"],
                    "Settore": voce.get("settore","Industrials"),
                    "Prezzo": last,
                    "PE": f.get("pe"),
                    "PB": f.get("pb"),
                    "BarreCount": len(b)
                })

        time.sleep(random.uniform(PAUSA_MIN, PAUSA_MAX))

    if not titoli and cache:
        print("Nessun titolo nuovo, uso cache completa", file=sys.stderr)
        titoli = list(cache.values())

    if not titoli:
        print("Nessun dato, esco", file=sys.stderr)
        sys.exit(1)

    OUT_DATi.parent.mkdir(parents=True, exist_ok=True)
    OUT_DATA.parent.mkdir(parents=True, exist_ok=True)
    OUT_FLAT.parent.mkdir(parents=True, exist_ok=True)

    snap = {
        "generato_il": date.today().isoformat(),
        "generated_at": datetime.utcnow().isoformat(),
        "titoli": titoli,
        "count": len(titoli)
    }
    OUT_DATi.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_DATA.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_FLAT.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        pd.DataFrame(flat).to_csv(str(OUT_FLAT).replace(".json",".csv"), index=False)
    except:
        pass
    OUT_LAST.write_text(json.dumps({"last_update": datetime.utcnow().isoformat(), "count": len(titoli), "falliti": falliti}, indent=2), encoding="utf-8")

    print(f"\n✅ Scritto {OUT_DATi} ({len(titoli)} titoli, {sum(len(t.get('barre',[])) for t in titoli)} barre)")
    print(f"✅ Scritto {OUT_FLAT} ({len(flat)} titoli)")

if __name__ == "__main__":
    main()
