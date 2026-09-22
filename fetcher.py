#!/usr/bin/env python3
"""
fetcher v4 - basato su collector.py dell'utente

Perché collector.py funziona meglio di fetch.py precedente:

1. tk.history() vs yf.download(): history() usa endpoint chart v8 meno rate-limitato,
   download() usa batch che Yahoo ora blocca su .MI dopo pochi ticker

2. Fallback statico universo.json -> f: collector non si affida solo a Yahoo per fondamentali.
   Se Yahoo non ritorna un campo, usa valore statico da universo.json via fondi().
   CET1 ad esempio non esiste proprio su Yahoo, deve venire da statico.

3. normalizza_percentuale(): Yahoo ritorna ROE a volte 0.152 a volte 15.2. Collector gestisce.

4. leggi_barre() con check NaN robusto (x != x) e salvataggio di tutte le barre OHLCV,
   non solo ultimo prezzo. Così l'app può disegnare grafici.

5. Sicurezza MINIMO_RIUSCITI: se <50% titoli fallisce, non pubblica snapshot a metà.

Questo v4 unisce:
- logica anti-ban di collector (history, pausa 1.5s, cache static fallback)
- output compatibile sia con Screener Italia (italian_stocks.json piatto) sia con Il Listino (snapshot.json con barre)
"""

import json, time, sys, argparse
from datetime import date
from pathlib import Path
import yfinance as yf

STORIA_ANNI = 2
PAUSA_SECONDI = 2.0  # aumentato da 1.5 per GitHub Actions shared IP
MINIMO_RIUSCITI = 0.5
QUI = Path(__file__).parent
UNIVERSO_PATH = QUI / "universo.json"  # se esiste, usa static fallback come collector
OUTPUT_SNAPSHOT = QUI / "dati" / "snapshot.json"
OUTPUT_FLAT_JSON = QUI / "data" / "italian_stocks.json"
OUTPUT_FLAT_CSV = QUI / "data" / "italian_stocks.csv"

CAMPI_F = ["mcap", "pe", "pb", "evEbitda", "roe", "margine", "debtEquity", "divYield", "payout", "crescitaRic", "crescitaEps", "fcfYield", "cet1"]

def normalizza_percentuale(v):
    if v is None: return None
    return v * 100 if abs(v) < 1 else v

def arrotonda(v, cifre=2):
    return round(v, cifre) if v is not None else None

def leggi_barre(storico):
    barre = []
    for indice, riga in storico.iterrows():
        o,h,l,c = riga.get("Open"), riga.get("High"), riga.get("Low"), riga.get("Close")
        if any(x is None or x != x for x in (o,h,l,c)): continue
        v = riga.get("Volume")
        barre.append({
            "d": indice.strftime("%Y-%m-%d"),
            "o": round(float(o),4), "h": round(float(h),4),
            "l": round(float(l),4), "c": round(float(c),4),
            "v": int(v) if v==v and v is not None else 0,
        })
    return barre

def leggi_fondamentali_live(info):
    mcap_raw = info.get("marketCap")
    mcap = (mcap_raw/1e9) if mcap_raw else None
    pe = info.get("trailingPE") or info.get("forwardPE")
    roe = normalizza_percentuale(info.get("returnOnEquity"))
    margine = normalizza_percentuale(info.get("profitMargins"))
    debt = info.get("debtToEquity")
    debtEq = (debt/100) if debt is not None else None
    div = normalizza_percentuale(info.get("dividendYield"))
    payout = normalizza_percentuale(info.get("payoutRatio"))
    crescRic = normalizza_percentuale(info.get("revenueGrowth"))
    crescEps = normalizza_percentuale(info.get("earningsGrowth"))
    fcf = info.get("freeCashflow")
    fcfYield = (fcf/mcap_raw*100) if (fcf and mcap_raw) else None

    grezzi = {
        "mcap": arrotonda(mcap,2), "pe": arrotonda(pe,2),
        "pb": arrotonda(info.get("priceToBook"),2),
        "evEbitda": arrotonda(info.get("enterpriseToEbitda"),2),
        "roe": arrotonda(roe,2), "margine": arrotonda(margine,2),
        "debtEquity": arrotonda(debtEq,3),
        "divYield": arrotonda(div,2), "payout": arrotonda(payout,2),
        "crescitaRic": arrotonda(crescRic,2), "crescitaEps": arrotonda(crescEps,2),
        "fcfYield": arrotonda(fcfYield,2),
    }
    return {k:v for k,v in grezzi.items() if v is not None}

def fondi(base, live):
    res = dict(base or {})
    res.update(live or {})
    return res

def scarica_titolo(voce):
    ticker = voce["ticker"]
    tk = yf.Ticker(ticker)
    storico = tk.history(period=f"{STORIA_ANNI}y", interval="1d")
    if storico.empty:
        raise RuntimeError("nessun dato storico")
    barre = leggi_barre(storico)
    if len(barre)<60:
        raise RuntimeError(f"solo {len(barre)} barre")
    info = tk.info or {}
    fondamentali = fondi(voce.get("f"), leggi_fondamentali_live(info))
    # consenso opzionale
    con = None
    tp = info.get("targetMeanPrice")
    if tp is not None:
        lo, hi = info.get("targetLowPrice"), info.get("targetHighPrice")
        try:
            trend = tk.recommendations
            b=h=s=0
            if trend is not None and not trend.empty:
                riga = trend.iloc[0]
                b = int(riga.get("strongBuy",0) or 0)+int(riga.get("buy",0) or 0)
                h = int(riga.get("hold",0) or 0)
                s = int(riga.get("sell",0) or 0)+int(riga.get("strongSell",0) or 0)
            n = info.get("numberOfAnalystOpinions") or (b+h+s) or None
            if n:
                con = {"tp": round(tp,3), "lo": round(lo or tp,3), "hi": round(hi or tp,3), "n": int(n), "b": b, "h": h, "s": s}
        except: pass
    return barre, fondamentali, con

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universo", default=str(UNIVERSO_PATH))
    args = parser.parse_args()

    universo_path = Path(args.universo)
    if universo_path.exists():
        universo = json.loads(universo_path.read_text(encoding="utf-8"))
        print(f"Universo caricato da {universo_path}: {len(universo)} titoli con fallback statico")
    else:
        # fallback se universo.json non esiste: costruisci da TICKERS base
        print("universo.json non trovato, uso lista base con f vuoti")
        base_tickers = ["ENI.MI","ENEL.MI","ISP.MI","UCG.MI","G.MI","STM.MI","RACE.MI","LDO.MI","PRY.MI","SRG.MI","TRN.MI","PST.MI","GASI.MI","MB.MI","NEXI.MI","CPR.MI","BC.MI","MONC.MI","AMP.MI","BREM.MI","BZU.MI","IP.MI","AZM.MI","MED.MI","SPM.MI","TEN.MI","TIT.MI","BPE.MI","BMPS.MI","BAMI.MI","CNHI.MI","STLA.MI","REC.MI","DNLM.MI","DIA.MI","IG.MI","ITL.MI","HER.MI","ERG.MI","A2A.MI","HOV.MI","CS.MI"]
        universo = [{"ticker": t, "nome": t.replace(".MI",""), "settore": "Industrials", "f": {}} for t in base_tickers]

    titoli = []
    flat = []
    falliti = []

    for voce in universo:
        ticker = voce["ticker"]
        try:
            barre, fondamentali, consenso = scarica_titolo(voce)
            titoli.append({"ticker": ticker, "nome": voce["nome"], "settore": voce["settore"], "barre": barre, "f": fondamentali, "con": consenso})
            # flat per Screener Italia
            last_c = barre[-1]["c"] if barre else 0
            # calcola tecnica da barre
            import pandas as pd
            closes = pd.Series([b["c"] for b in barre])
            sma50 = closes.rolling(50).mean().iloc[-1] if len(closes)>=50 else last_c
            sma200 = closes.rolling(200).mean().iloc[-1] if len(closes)>=200 else last_c
            max52 = closes.tail(252).max()
            flat.append({
                "Ticker": ticker.replace(".MI",""), "TickerYahoo": ticker, "Nome": voce["nome"], "Settore": voce["settore"],
                "Prezzo": last_c, "PE": fondamentali.get("pe"), "PB": fondamentali.get("pb"),
                "ROE": fondamentali.get("roe"), "DivYield": fondamentali.get("divYield"),
                "DebtEquity": fondamentali.get("debtEquity"), "MarketCapMld": fondamentali.get("mcap"),
                "SMA50": round(float(sma50),2), "SMA200": round(float(sma200),2),
                "DistSMA200": round((last_c/sma200-1)*100,2) if sma200 else 0,
                "Dist52w": round((last_c/max52-1)*100,2) if max52 else 0,
                "Pattern": "Golden Cross" if last_c > sma50 > sma200 else "Neutrale",
                "BarreCount": len(barre)
            })
            print(f"  ok {ticker}: {len(barre)} barre, {len(fondamentali)}/13 campi")
        except Exception as e:
            falliti.append(ticker)
            print(f"  FAIL {ticker}: {e}", file=sys.stderr)
        time.sleep(PAUSA_SECONDI)

    if len(titoli) < len(universo)*MINIMO_RIUSCITI:
        print(f"Solo {len(titoli)}/{len(universo)} riusciti - sotto soglia, non scrivo", file=sys.stderr)
        sys.exit(1)

    # output 1: snapshot ricco (per Il Listino)
    OUTPUT_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {"generato_il": date.today().isoformat(), "titoli": titoli}
    OUTPUT_SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    # output 2: flat per Screener Italia
    OUTPUT_FLAT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FLAT_JSON.write_text(json.dumps(flat, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import pandas as pd
        pd.DataFrame(flat).to_csv(OUTPUT_FLAT_CSV, index=False)
    except: pass

    print(f"\nScritto {OUTPUT_SNAPSHOT} e {OUTPUT_FLAT_JSON} - {len(titoli)}/{len(universo)} titoli")
    if falliti: print(f"Falliti: {', '.join(falliti)}", file=sys.stderr)

if __name__ == "__main__":
    main()