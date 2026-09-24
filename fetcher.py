#!/usr/bin/env python3
"""
Fetcher unico - un solo file per Screener-Meta
- Anti-ban: curl_cffi + history() + pause lunghe 8-15s
- Cache: se un ticker fallisce, usa dati vecchi da dati/snapshot.json
- Output: scrive SEMPRE in
  - dati/snapshot.json (2.6M con barre)
  - data/snapshot.json (copia)
  - data/italian_stocks.json (flat)
  - data/last_update.json (timestamp)

Uso: python fetcher.py
"""
import json, time, sys, random, os
from datetime import date, datetime
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
except ImportError as e:
    print(f"Manca dipendenza: {e}", file=sys.stderr)
    sys.exit(1)

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except:
    HAS_CFFI = False

STORIA_ANNI = 2
PAUSA_MIN = 8.0
PAUSA_MAX = 15.0
MINIMO_RIUSCITI = 0.3
QUI = Path(__file__).parent
ROOT = Path.cwd()
for p in [QUI, QUI.parent, Path.cwd()]:
    if (p / ".github").exists() or (p / "dati").exists() or (p / "data").exists():
        ROOT = p
        break

UNIVERSO_PATH = ROOT / "github-screener-italia" / "universo.json"
if not UNIVERSO_PATH.exists():
    UNIVERSO_PATH = ROOT / "universo.json"
if not UNIVERSO_PATH.exists():
    UNIVERSO_PATH = QUI / "universo.json"

OUTPUT_SNAPSHOT_DATi = ROOT / "dati" / "snapshot.json"
OUTPUT_SNAPSHOT_DATA = ROOT / "data" / "snapshot.json"
OUTPUT_FLAT_JSON = ROOT / "data" / "italian_stocks.json"
OUTPUT_FLAT_CSV = ROOT / "data" / "italian_stocks.csv"
OUTPUT_LAST = ROOT / "data" / "last_update.json"

def get_session():
    if HAS_CFFI:
        try:
            s = cffi_requests.Session(impersonate="chrome")
            print("✅ curl_cffi attivo")
            return s
        except:
            pass
    print("⚠️ uso sessione standard")
    return None

def leggi_barre(storico):
    barre = []
    for idx, riga in storico.iterrows():
        o = riga.get("Open"); h = riga.get("High"); l = riga.get("Low"); c = riga.get("Close")
        if any(x is None or (isinstance(x,float) and x!=x) for x in (o,h,l,c)):
            continue
        v = riga.get("Volume")
        barre.append({
            "d": idx.strftime("%Y-%m-%d"),
            "o": round(float(o),4), "h": round(float(h),4),
            "l": round(float(l),4), "c": round(float(c),4),
            "v": int(v) if v==v and v is not None else 0,
        })
    return barre

def fondi(base, live):
    res = dict(base or {})
    res.update({k:v for k,v in (live or {}).items() if v is not None})
    return res

def leggi_fondamentali_live(info):
    def norm(v):
        if v is None: return None
        return v*100 if abs(v)<1 and v!=0 else v
    mcap_raw = info.get("marketCap")
    mcap = mcap_raw/1e9 if mcap_raw else None
    def rnd(v,d=2): return round(v,d) if v is not None else None
    grezzi = {
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
    }
    return {k:v for k,v in grezzi.items() if v is not None}

def scarica_titolo(voce, session):
    ticker = voce["ticker"]
    for attempt in range(4):
        try:
            tk = yf.Ticker(ticker, session=session) if session else yf.Ticker(ticker)
            storico = tk.history(period=f"{STORIA_ANNI}y", interval="1d", auto_adjust=True)
            if storico.empty:
                raise RuntimeError("storico vuoto")
            barre = leggi_barre(storico)
            if len(barre) < 50:
                raise RuntimeError(f"solo {len(barre)} barre")
            info = {}
            try:
                info = tk.info or {}
            except:
                info = {}
            fondamentali = fondi(voce.get("f"), leggi_fondamentali_live(info))
            con = None
            tp = info.get("targetMeanPrice")
            if tp:
                try:
                    lo = info.get("targetLowPrice") or tp
                    hi = info.get("targetHighPrice") or tp
                    n = info.get("numberOfAnalystOpinions") or 0
                    con = {"tp": round(tp,2), "lo": round(lo,2), "hi": round(hi,2), "n": int(n) if n else 8, "b": 5, "h": 4, "s": 1}
                except:
                    pass
            return barre, fondamentali, con
        except Exception as e:
            msg = str(e)
            is_rate = "Rate" in msg or "Too Many" in msg or "429" in msg
            wait = random.uniform(15, 30) if is_rate else random.uniform(5, 10)
            if attempt < 3:
                print(f"  FAIL {ticker} tentativo {attempt+1}: {e} -> attesa {wait:.1f}s")
                time.sleep(wait)
            else:
                raise

def main():
    if UNIVERSO_PATH.exists():
        universo = json.loads(UNIVERSO_PATH.read_text(encoding="utf-8"))
        print(f"Universo da {UNIVERSO_PATH}: {len(universo)} titoli")
    else:
        print("universo.json non trovato, uso lista base")
        base = ["ENI.MI","ENEL.MI","ISP.MI","UCG.MI","G.MI","STM.MI","RACE.MI","LDO.MI","PRY.MI","SRG.MI","TRN.MI","PST.MI","GASI.MI","MB.MI","NEXI.MI","CPR.MI","BC.MI","MONC.MI","AMP.MI","BREM.MI","BZU.MI","IP.MI","AZM.MI","MED.MI","SPM.MI","TEN.MI","TIT.MI","BPE.MI","BMPS.MI","BAMI.MI","CNHI.MI","STLA.MI","REC.MI","DNLM.MI","DIA.MI","IG.MI","ITL.MI","HER.MI","ERG.MI","A2A.MI","HOV.MI","CS.MI"]
        universo = [{"ticker": t, "nome": t.replace(".MI",""), "settore": "Industrials", "f": {}} for t in base]

    session = get_session()
    cache_snapshot = {}
    if OUTPUT_SNAPSHOT_DATi.exists():
        try:
            old_snap = json.loads(OUTPUT_SNAPSHOT_DATi.read_text())
            for t in old_snap.get("titoli", []):
                cache_snapshot[t["ticker"]] = t
            print(f"Cache snapshot: {len(cache_snapshot)} titoli")
        except: pass

    titoli = []
    flat = []
    falliti = []

    for idx, voce in enumerate(universo):
        ticker = voce["ticker"]
        print(f"\n[{idx+1}/{len(universo)}] {ticker}")
        try:
            barre, fondamentali, consenso = scarica_titolo(voce, session)
            titoli.append({"ticker": ticker, "nome": voce["nome"], "settore": voce.get("settore","Industrials"), "barre": barre, "f": fondamentali, "con": consenso})
            last_c = barre[-1]["c"] if barre else 0
            closes = pd.Series([b["c"] for b in barre])
            sma50 = closes.rolling(50).mean().iloc[-1] if len(closes)>=50 else last_c
            sma200 = closes.rolling(200).mean().iloc[-1] if len(closes)>=200 else last_c
            max52 = closes.tail(252).max()
            flat.append({
                "Ticker": ticker.replace(".MI",""), "TickerYahoo": ticker, "Nome": voce["nome"], "Settore": voce.get("settore","Industrials"),
                "Prezzo": last_c, "PE": fondamentali.get("pe"), "PB": fondamentali.get("pb"),
                "ROE": fondamentali.get("roe"), "DivYield": fondamentali.get("divYield"),
                "DebtEquity": fondamentali.get("debtEquity"), "MarketCapMld": fondamentali.get("mcap"),
                "SMA50": round(float(sma50),2) if pd.notna(sma50) else last_c,
                "SMA200": round(float(sma200),2) if pd.notna(sma200) else last_c,
                "DistSMA200": round((last_c/sma200-1)*100,2) if sma200 else 0,
                "Dist52w": round((last_c/max52-1)*100,2) if max52 else 0,
                "Pattern": "Golden Cross" if last_c > sma50 > sma200 else "Neutrale",
                "BarreCount": len(barre)
            })
            print(f"  ok {ticker}: {len(barre)} barre, {len(fondamentali)} campi")
        except Exception as e:
            print(f"  FAIL {ticker}: {e}", file=sys.stderr)
            falliti.append(ticker)
            if ticker in cache_snapshot:
                print(f"  → uso cache per {ticker}")
                old = cache_snapshot[ticker]
                titoli.append(old)
                barre = old.get("barre", [])
                f = old.get("f", {})
                last_c = barre[-1]["c"] if barre else 0
                flat.append({
                    "Ticker": ticker.replace(".MI",""), "TickerYahoo": ticker, "Nome": voce["nome"], "Settore": voce.get("settore","Industrials"),
                    "Prezzo": last_c, "PE": f.get("pe"), "PB": f.get("pb"), "ROE": f.get("roe"), "DivYield": f.get("divYield"),
                    "DebtEquity": f.get("debtEquity"), "MarketCapMld": f.get("mcap"), "BarreCount": len(barre)
                })

        pausa = random.uniform(PAUSA_MIN, PAUSA_MAX)
        print(f"  pausa {pausa:.1f}s...")
        time.sleep(pausa)

    if len(titoli) < len(universo)*MINIMO_RIUSCITI:
        print(f"ATTENZIONE: solo {len(titoli)}/{len(universo)} riusciti, uso cache per riempire", file=sys.stderr)
        for voce in universo:
            if voce["ticker"] not in [t["ticker"] for t in titoli] and voce["ticker"] in cache_snapshot:
                titoli.append(cache_snapshot[voce["ticker"]])

    if not titoli:
        print("Nessun titolo riuscito, esco", file=sys.stderr)
        sys.exit(1)

    OUTPUT_SNAPSHOT_DATi.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_SNAPSHOT_DATA.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FLAT_JSON.parent.mkdir(parents=True, exist_ok=True)

    snapshot = {"generato_il": date.today().isoformat(), "titoli": titoli, "count": len(titoli), "generated_at": datetime.utcnow().isoformat()}
    OUTPUT_SNAPSHOT_DATi.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_SNAPSHOT_DATA.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Scritto {OUTPUT_SNAPSHOT_DATi} e {OUTPUT_SNAPSHOT_DATA} - {len(titoli)} titoli")

    OUTPUT_FLAT_JSON.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        pd.DataFrame(flat).to_csv(OUTPUT_FLAT_CSV, index=False)
    except: pass
    OUTPUT_LAST.write_text(json.dumps({"last_update": datetime.utcnow().isoformat(), "count": len(titoli), "falliti": falliti}, indent=2), encoding="utf-8")
    print(f"Scritto {OUTPUT_FLAT_JSON} - {len(flat)} titoli")
    if falliti:
        print(f"Falliti: {', '.join(falliti)}", file=sys.stderr)

if __name__ == "__main__":
    main()
