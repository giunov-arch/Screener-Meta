#!/usr/bin/env python3
"""
collector.py - v2 con fondamentali REALI da Yahoo via curl_cffi
Prima versione usava solo universo.json statico -> sembrava casuale.
Ora:
- Prezzi: chart API via curl_cffi (già funzionava)
- Fondamentali: quoteSummary API via curl_cffi con moduli leggeri, bypassa 429
- Fallback a universo.json se Yahoo blocca
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

PAUSA_MIN = 3.0
PAUSA_MAX = 6.0

def get_session():
    if HAS_CFFI:
        try:
            s = cffi_requests.Session(impersonate="chrome120")
            print("✅ curl_cffi attivo")
            return s
        except Exception as e:
            print(f"curl_cffi fail: {e}")
    print("⚠️ curl_cffi non trovato")
    return None

def fetch_chart(ticker, session, period="2y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range={period}&interval=1d&includePrePost=false"
    resp = session.get(url, timeout=25)
    if resp.status_code == 429:
        raise RuntimeError(f"429 chart {ticker}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} chart {ticker}: {resp.text[:200]}")
    j = resp.json()
    if "chart" not in j or j["chart"]["result"] is None:
        raise RuntimeError(f"chart null {ticker}: {str(j)[:300]}")
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
        raise RuntimeError("df empty")
    return df

def fetch_fondamentali_yahoo(ticker, session):
    """
    Scarica fondamentali REALI da Yahoo quoteSummary via curl_cffi
    Usa moduli leggeri per evitare 429: price, defaultKeyStatistics, financialData, summaryDetail
    Ritorna dict con chiavi compatibili con f: mcap, pe, pb, roe, divYield, etc.
    """
    # Proviamo prima con moduli leggeri separati per evitare 429 su richiesta grossa
    modules_list = [
        "price,defaultKeyStatistics",  # pe, pb, mcap base
        "financialData,summaryDetail"  # roe, divYield, debt etc
    ]
    
    merged = {}
    
    for modules in modules_list:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules}&corsDomain=finance.yahoo.com&formatted=false"
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 429:
                print(f"    429 su {modules} per {ticker}, pausa 5s e retry altro host")
                time.sleep(5)
                # prova query2 come fallback
                url2 = url.replace("query1", "query2")
                resp = session.get(url2, timeout=20)
                if resp.status_code == 429:
                    raise RuntimeError(f"429 anche su query2 per {ticker} {modules}")
            
            if resp.status_code != 200:
                print(f"    HTTP {resp.status_code} su {modules} per {ticker}")
                continue
                
            j = resp.json()
            result = j.get("quoteSummary", {}).get("result")
            if not result:
                print(f"    quoteSummary result null per {ticker} {modules}")
                continue
            result = result[0]
            
            # Estrai campi
            price = result.get("price", {})
            stats = result.get("defaultKeyStatistics", {})
            fin = result.get("financialData", {})
            summary = result.get("summaryDetail", {})
            
            # mcap in miliardi
            mcap_raw = price.get("marketCap", {}).get("raw") or stats.get("enterpriseValue", {}).get("raw")
            if mcap_raw:
                merged["mcap"] = round(mcap_raw / 1e9, 2)
            
            # PE
            pe = (stats.get("trailingPE", {}).get("raw") or 
                  stats.get("forwardPE", {}).get("raw") or
                  summary.get("trailingPE", {}).get("raw"))
            if pe:
                merged["pe"] = round(pe, 2)
            
            # PB
            pb = stats.get("priceToBook", {}).get("raw")
            if pb:
                merged["pb"] = round(pb, 2)
            
            # ROE
            roe = fin.get("returnOnEquity", {}).get("raw")
            if roe:
                merged["roe"] = round(roe*100, 2) if roe < 1 else round(roe, 2)
            
            # Margine
            marg = fin.get("profitMargins", {}).get("raw")
            if marg:
                merged["margine"] = round(marg*100, 2) if marg < 1 else round(marg, 2)
            
            # Debt/Equity
            de = fin.get("debtToEquity", {}).get("raw")
            if de is not None:
                merged["debtEquity"] = round(de/100, 3) if de > 10 else round(de, 3)
            
            # DivYield
            dy = summary.get("dividendYield", {}).get("raw") or fin.get("dividendYield", {}).get("raw")
            if dy is not None:
                merged["divYield"] = round(dy*100, 2) if dy < 1 else round(dy, 2)
            
            # Payout
            payout = summary.get("payoutRatio", {}).get("raw")
            if payout is not None:
                merged["payout"] = round(payout*100, 2) if payout < 1 else round(payout, 2)
            
            # Crescita ricavi
            cr = fin.get("revenueGrowth", {}).get("raw")
            if cr is not None:
                merged["crescitaRic"] = round(cr*100, 2) if abs(cr) < 1 else round(cr, 2)
            
            # EV/EBITDA
            ev = stats.get("enterpriseToEbitda", {}).get("raw")
            if ev:
                merged["evEbitda"] = round(ev, 2)
            
            # Pausa breve tra moduli per evitare 429
            time.sleep(random.uniform(0.5, 1.5))
            
        except Exception as e:
            print(f"    errore fondamentali {modules} {ticker}: {e}")
            continue
    
    return merged

def fetch_consenso_yahoo(ticker, session):
    """
    Scarica giudizio analisti e target price REALI da Yahoo
    Moduli: financialData (targetMeanPrice, recommendation) + recommendationTrend (buy/hold/sell)
    Ritorna dict formato collector.py: {tp, lo, hi, n, b, h, s, rating, upside}
    """
    consenso = {}
    try:
        modules = "financialData,recommendationTrend"
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules}&corsDomain=finance.yahoo.com&formatted=false"
        resp = session.get(url, timeout=20)
        if resp.status_code == 429:
            print(f"    429 consenso {ticker}, provo query2")
            url2 = url.replace("query1", "query2")
            resp = session.get(url2, timeout=20)
        if resp.status_code != 200:
            print(f"    HTTP {resp.status_code} consenso {ticker}")
            return None
        
        j = resp.json()
        result = j.get("quoteSummary", {}).get("result")
        if not result:
            return None
        result = result[0]
        
        fin = result.get("financialData", {})
        rec = result.get("recommendationTrend", {}).get("trend", [])
        
        # Target price
        tp = fin.get("targetMeanPrice", {}).get("raw")
        lo = fin.get("targetLowPrice", {}).get("raw")
        hi = fin.get("targetHighPrice", {}).get("raw")
        n_analysts = fin.get("numberOfAnalystOpinions", {}).get("raw")
        rec_mean = fin.get("recommendationMean", {}).get("raw")
        rec_key = fin.get("recommendationKey", "")
        
        # Recommendation trend - prendi ultimo mese
        b = h = s = 0
        if rec and len(rec) > 0:
            last = rec[-1]  # ultimo mese disponibile
            # Yahoo trend ha: strongBuy, buy, hold, sell, strongSell
            strongBuy = last.get("strongBuy", 0)
            buy = last.get("buy", 0)
            hold = last.get("hold", 0)
            sell = last.get("sell", 0)
            strongSell = last.get("strongSell", 0)
            b = strongBuy + buy
            h = hold
            s = sell + strongSell
        
        # Rating da recommendationMean o da buy/hold/sell
        rating = "Hold"
        if rec_key:
            if rec_key in ["buy", "strong_buy"]:
                rating = "Buy"
            elif rec_key in ["sell", "strong_sell", "underperform"]:
                rating = "Sell"
            else:
                rating = "Hold"
        else:
            if b > h and b > s:
                rating = "Buy"
            elif s > b and s > h:
                rating = "Sell"
        
        # Se abbiamo almeno target price, ritorna
        if tp:
            consenso = {
                "tp": round(tp, 2),
                "lo": round(lo, 2) if lo else round(tp*0.85, 2),
                "hi": round(hi, 2) if hi else round(tp*1.15, 2),
                "n": int(n_analysts) if n_analysts else (b+h+s if (b+h+s)>0 else 8),
                "b": int(b),
                "h": int(h),
                "s": int(s),
                "rating": rating,
                "upside": None  # calcolato dopo da prezzo attuale
            }
            return consenso
        elif (b+h+s) > 0:
            # Abbiamo solo rating senza target
            consenso = {
                "tp": None,
                "lo": None,
                "hi": None,
                "n": int(b+h+s),
                "b": int(b),
                "h": int(h),
                "s": int(s),
                "rating": rating,
                "upside": None
            }
            return consenso
            
    except Exception as e:
        print(f"    errore consenso {ticker}: {e}")
    
    return None

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
    print(f"Universo: {len(universo)} titoli")

    session = get_session()
    if not session:
        print("ERRORE: curl_cffi necessario", file=sys.stderr)
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
                    print(f"  {period} chart fail: {e}")
                    time.sleep(random.uniform(1,2))
            if df is None:
                raise last_err

            barre = df_to_barre(df)
            if len(barre) < 30:
                raise RuntimeError(f"solo {len(barre)} barre")

            # FONDAMENTALI REALI DA YAHOO
            print(f"  → scarico fondamentali Yahoo per {ticker}...")
            f_yahoo = fetch_fondamentali_yahoo(ticker, session)
            if f_yahoo:
                print(f"    Yahoo OK: {f_yahoo}")
            else:
                print(f"    Yahoo vuoto, uso solo universo.json")
            
            # Merge: universo.json come base, Yahoo sovrascrive se presente
            f_static = voce.get("f", {})
            f_merged = dict(f_static)
            f_merged.update({k:v for k,v in f_yahoo.items() if v is not None})
            if "cet1" not in f_merged and f_static.get("cet1") is not None:
                f_merged["cet1"] = f_static["cet1"]
            
            last = barre[-1]["c"]
            
            # CONSENSO ANALISTI E TARGET PRICE REALI DA YAHOO
            print(f"  → scarico consenso Yahoo per {ticker}...")
            con_yahoo = fetch_consenso_yahoo(ticker, session)
            if con_yahoo:
                if con_yahoo.get("tp") and last:
                    con_yahoo["upside"] = round((con_yahoo["tp"]/last - 1)*100, 1)
                print(f"    Consenso OK: {con_yahoo}")
            else:
                print(f"    Consenso vuoto per {ticker}")
            closes = pd.Series([b["c"] for b in barre])
            sma50 = closes.rolling(50).mean().iloc[-1] if len(closes)>=50 else last
            sma200 = closes.rolling(200).mean().iloc[-1] if len(closes)>=200 else last
            max52 = closes.tail(252).max() if len(closes)>=52 else last

            titoli.append({
                "ticker": ticker,
                "nome": voce["nome"],
                "settore": voce.get("settore","Industrials"),
                "barre": barre,
                "f": f_merged,
                "con": con_yahoo
            })
            flat.append({
                "Ticker": ticker.replace(".MI",""),
                "TickerYahoo": ticker,
                "Nome": voce["nome"],
                "Settore": voce.get("settore","Industrials"),
                "Prezzo": last,
                "PE": f_merged.get("pe"),
                "PB": f_merged.get("pb"),
                "ROE": f_merged.get("roe"),
                "DivYield": f_merged.get("divYield"),
                "DebtEquity": f_merged.get("debtEquity"),
                "MarketCapMld": f_merged.get("mcap"),
                "CET1": f_merged.get("cet1"),
                "SMA50": round(float(sma50),2) if pd.notna(sma50) else last,
                "SMA200": round(float(sma200),2) if pd.notna(sma200) else last,
                "DistSMA200": round((last/sma200-1)*100,2) if sma200 else 0,
                "Dist52w": round((last/max52-1)*100,2) if max52 else 0,
                "BarreCount": len(barre),
                "TargetPrice": con_yahoo.get("tp") if con_yahoo else None,
                "TargetLow": con_yahoo.get("lo") if con_yahoo else None,
                "TargetHigh": con_yahoo.get("hi") if con_yahoo else None,
                "NumAnalisti": con_yahoo.get("n") if con_yahoo else None,
                "Buy": con_yahoo.get("b") if con_yahoo else None,
                "Hold": con_yahoo.get("h") if con_yahoo else None,
                "Sell": con_yahoo.get("s") if con_yahoo else None,
                "Rating": con_yahoo.get("rating") if con_yahoo else None,
                "Upside": con_yahoo.get("upside") if con_yahoo else None,
                "FonteFondamentali": "Yahoo" if f_yahoo else "universo.json",
                "FonteConsenso": "Yahoo" if con_yahoo else "fallback"
            })
            print(f"  OK {ticker}: {len(barre)} barre, prezzo {last}, f={f_merged}")

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
        print("Nessun titolo nuovo, uso cache", file=sys.stderr)
        titoli = list(cache.values())

    if not titoli:
        print("Nessun dato", file=sys.stderr)
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

    print(f"\n✅ Scritto {OUT_DATi} ({len(titoli)} titoli)")
    print(f"✅ Fondamentali: Yahoo reali + fallback universo.json")

if __name__ == "__main__":
    main()
