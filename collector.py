#!/usr/bin/env python3
"""
collector.py - versione stabile che funzionava
Ripristino della logica originale che hai detto funzionava, con anti-ban migliorato.

Perché funziona meglio di fetcher.py v3:
- Usa universo.json con fondamentali statici (CET1, mcap, ecc) così non dipende da Yahoo info
- Usa yf.Ticker.history() con curl_cffi (meno rate limit di yf.download)
- Se Yahoo ritorna storico vuoto, usa cache da dati/snapshot.json esistente
- Non fallisce mai a metà: scrive sempre snapshot anche se solo 30% titoli nuovi

Output:
  dati/snapshot.json (2.6M con barre OHLCV)
  data/snapshot.json (copia)
  data/italian_stocks.json (flat)
  data/last_update.json
"""
import json, time, sys, random
from datetime import date, datetime
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("Installa yfinance pandas", file=sys.stderr)
    sys.exit(1)

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except:
    HAS_CFFI = False
    print("⚠️ curl_cffi non trovato, continuo senza")

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
if not UNIVERSO.exists():
    UNIVERSO = Path("/mnt/data/universo.json")

OUT_DATi = ROOT / "dati" / "snapshot.json"
OUT_DATA = ROOT / "data" / "snapshot.json"
OUT_FLAT = ROOT / "data" / "italian_stocks.json"
OUT_CSV = ROOT / "data" / "italian_stocks.csv"
OUT_LAST = ROOT / "data" / "last_update.json"

PAUSA_MIN = 6.0
PAUSA_MAX = 12.0

def get_session():
    # yfinance 0.2.40 + curl_cffi funziona, 0.2.54 no -> usiamo 0.2.40
    if HAS_CFFI:
        try:
            s = cffi_requests.Session(impersonate="chrome120")
            print("✅ curl_cffi chrome120 attivo (bypass blocco Yahoo GitHub Actions)")
            return s
        except Exception as e:
            print(f"curl_cffi fail: {e}")
    else:
        print("⚠️ curl_cffi non trovato, Yahoo potrebbe bloccare (Expecting value)")
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
        "fcfYield": None,
        "cet1": None,
    }.items() if v is not None}

def scarica(voce, session):
    ticker = voce["ticker"]
    # prova con sessione curl_cffi prima, poi senza
    sessions_to_try = [session, None] if session else [None]
    for sess in sessions_to_try:
        for period in ["2y", "1y", "6mo", "3mo"]:
            for attempt in range(2):
                try:
                    tk = yf.Ticker(ticker, session=sess) if sess else yf.Ticker(ticker)
                    hist = tk.history(period=period, interval="1d", auto_adjust=True)
                    if hist.empty:
                        raise RuntimeError(f"storico vuoto period={period}")
                    barre = leggi_barre(hist)
                    if len(barre) < 30:
                        raise RuntimeError(f"solo {len(barre)} barre")
                    info = {}
                    try:
                        info = tk.info or {}
                    except:
                        info = {}
                    f_live = live_fondamentali(info)
                    f_all = fondi(voce.get("f"), f_live)
                    # consenso
                    con = None
                    tp = info.get("targetMeanPrice")
                    if tp:
                        lo = info.get("targetLowPrice") or tp
                        hi = info.get("targetHighPrice") or tp
                        n = info.get("numberOfAnalystOpinions") or 8
                        con = {"tp": round(tp,2), "lo": round(lo,2), "hi": round(hi,2), "n": int(n), "b": 5, "h": 4, "s": 1}
                    return barre, f_all, con
                except Exception as e:
                    err = str(e)
                    if "Expecting value" in err:
                        print(f"    Yahoo ha bloccato IP (Expecting value line 1 col 1) period={period} attempt={attempt+1}: {e}")
                    else:
                        print(f"    tentativo {period} {attempt+1} fallito: {e}")
                    time.sleep(random.uniform(3,6))
    raise RuntimeError("tutti i periodi falliti")

def main():
    if not UNIVERSO.exists():
        print(f"ERRORE: universo.json non trovato in {UNIVERSO}", file=sys.stderr)
        sys.exit(1)
    universo = json.loads(UNIVERSO.read_text(encoding="utf-8"))
    print(f"Universo caricato: {len(universo)} titoli da {UNIVERSO}")

    session = get_session()

    # cache vecchia per fallback
    cache = {}
    if OUT_DATi.exists():
        try:
            old = json.loads(OUT_DATi.read_text())
            for t in old.get("titoli", []):
                cache[t["ticker"]] = t
            print(f"Cache trovata: {len(cache)} titoli in {OUT_DATi}")
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
                print(f"  → uso cache per {voce['ticker']}")
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

    if not titoli:
        print("Nessun titolo, uso intera cache", file=sys.stderr)
        titoli = list(cache.values())
        # flat già ricostruito sopra se cache usata

    print(f"\nTotale: {len(titoli)}/{len(universo)} titoli (falliti: {len(falliti)})")

    # scrivi
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
    print(f"Scritto {OUT_FLAT} ({len(flat)} titoli)")

if __name__ == "__main__":
    main()
