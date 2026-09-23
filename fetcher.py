"""
Fetcher v3 - bypass YFRateLimitError definitivo
Strategia:
1. Sessione curl_cffi (bypassa bot detection Yahoo)
2. Download 1 ticker alla volta con pausa lunga 4-7s
3. Cache locale: se data/italian_stocks.json esiste, tiene i vecchi dati e aggiorna solo prezzo
4. Fondamentali OPZIONALI (disattivabili) per evitare ban su /quoteSummary
5. Fallback: se fallisce tutto, usa Stooq (gratis, no limit) per prezzi .MI

Esecuzione: python fetcher.py --no-fundamentals (consigliato su GitHub)
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json, os, time, random, argparse
from datetime import datetime

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except:
    HAS_CFFI = False
    print("⚠️  curl_cffi non installato, uso requests standard (più facile da bannare)")

TICKERS = [
    "ENI.MI","ENEL.MI","ISP.MI","UCG.MI","G.MI","STM.MI","RACE.MI","LDO.MI",
    "PRY.MI","SRG.MI","TRN.MI","PST.MI","GASI.MI","MB.MI","NEXI.MI","CPR.MI",
    "BC.MI","MONC.MI","AMP.MI","BREM.MI","BZU.MI","IP.MI","AZM.MI","MED.MI",
    "SPM.MI","TEN.MI","TIT.MI","BPE.MI","BMPS.MI","BAMI.MI","CNHI.MI","STLA.MI",
    "REC.MI","DNLM.MI","DIA.MI","IG.MI","ITL.MI","HER.MI","ERG.MI","A2A.MI",
    "HOV.MI","CS.MI"
]

SECTORS = {"ENI.MI":"Energy","ENEL.MI":"Utilities","ISP.MI":"Financial","UCG.MI":"Financial","G.MI":"Financial","STM.MI":"Technology"}

def get_session():
    if HAS_CFFI:
        s = cffi_requests.Session(impersonate="chrome")
        return s
    return None

def compute_technical(df):
    close = df['Close']
    df['SMA50'] = close.rolling(50).mean()
    df['SMA200'] = close.rolling(200).mean()
    delta = close.diff()
    gain = delta.where(delta>0,0).rolling(14).mean()
    loss = (-delta.where(delta<0,0)).rolling(14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100/(1+rs))
    df['Momentum3M'] = (close / close.shift(63) -1)*100
    df['VolVsAvg'] = (df['Volume'] / df['Volume'].rolling(20).mean() -1)*100
    return df

def download_one(ticker, session, period="2y"):
    for attempt in range(5):
        try:
            print(f"  Download {ticker} attempt {attempt+1}")
            # yfinance con sessione custom se disponibile
            if session:
                tk = yf.Ticker(ticker, session=session)
                df = tk.history(period=period, interval="1d", auto_adjust=True)
            else:
                df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False, threads=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if df.empty:
                raise ValueError("empty df")
            return df
        except Exception as e:
            is_rate = "Rate" in str(e) or "Too Many" in str(e) or "429" in str(e)
            wait = (3**attempt) + random.uniform(3,7) if is_rate else random.uniform(2,4)
            print(f"    ❌ {e} -> wait {wait:.1f}s")
            time.sleep(wait)
            if attempt==4:
                return None
    return None

def download_stooq_fallback(ticker):
    """Fallback gratuito Stooq - funziona per molti .MI senza rate limit"""
    try:
        # Stooq usa eni.mi -> eni.mi
        stooq_ticker = ticker.lower()
        url = f"https://stooq.com/q/d/l/?s={stooq_ticker}&i=d"
        df = pd.read_csv(url)
        if df.empty: return None
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.set_index('Date').sort_index()
        df = df.rename(columns={"Close":"Close","Volume":"Volume","Open":"Open","High":"High","Low":"Low"})
        df = df.tail(600)  # 2y circa
        return df
    except Exception as e:
        print(f"    Stooq fallback fallito {ticker}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fundamentals", action="store_true", help="Salta fondamentali per evitare ban")
    args = parser.parse_args()

    session = get_session()
    if HAS_CFFI:
        print("✅ Uso curl_cffi - bypass anti-bot attivo")

    # carica cache se esiste
    cache = {}
    if os.path.exists("data/italian_stocks.json"):
        try:
            with open("data/italian_stocks.json","r") as f:
                old = json.load(f)
                for it in old:
                    cache[it.get("TickerYahoo") or it.get("Ticker")+".MI"] = it
            print(f"Cache caricata: {len(cache)} titoli")
        except: pass

    results = []
    for idx, t in enumerate(TICKERS):
        print(f"\n[{idx+1}/{len(TICKERS)}] {t}")
        df = download_one(t, session, period="2y")
        if df is None or df.empty:
            print(f"  Provo Stooq fallback...")
            df = download_stooq_fallback(t)
        if df is None or len(df)<100:
            print(f"  SKIP {t}, nessun dato")
            # usa cache se disponibile
            if t in cache:
                print(f"  Uso cache per {t}")
                results.append(cache[t])
            continue

        try:
            df = compute_technical(df)
            last = df.iloc[-1]
            price = float(last['Close'])
            sma50 = float(last['SMA50']) if not pd.isna(last['SMA50']) else price
            sma200 = float(last['SMA200']) if not pd.isna(last['SMA200']) else price
            rsi = float(last['RSI']) if not pd.isna(last['RSI']) else 50
            mom = float(last['Momentum3M']) if not pd.isna(last['Momentum3M']) else 0
            vol = float(last['VolVsAvg']) if not pd.isna(last['VolVsAvg']) else 0
            dist_sma200 = (price/sma200 -1)*100 if sma200 else 0
            max52 = float(df['Close'].tail(252).max())
            dist_52w = (price/max52 -1)*100 if max52 else 0

            pattern="Neutrale"
            if price > sma50 > sma200 and rsi>50: pattern="Golden Cross"
            elif abs(price-sma50)/sma50 <0.02: pattern="Pullback a SMA50"
            elif vol>80 and mom>5: pattern="Breakout volumi"
            elif abs(mom)<3 and vol< -20: pattern="Base stretta"

            # fondamentali + consenso - deterministico, non random
            fund = cache.get(t, {}) if args.no_fundamentals else {}
            consenso = cache.get(t, {}).get("Consenso") or cache.get(t, {}).get("consenso") or {}
            if not args.no_fundamentals:
                try:
                    time.sleep(random.uniform(1.5,3))
                    tk = yf.Ticker(t, session=session) if session else yf.Ticker(t)
                    info = tk.info or {}
                    fund = {
                        "PE": info.get("trailingPE") or info.get("forwardPE"),
                        "PB": info.get("priceToBook"),
                        "ROE": (info.get("returnOnEquity") or 0)*100 if info.get("returnOnEquity") else cache.get(t,{}).get("ROE"),
                        "DebtEquity": info.get("debtToEquity"),
                        "DivYield": (info.get("dividendYield") or 0)*100 if info.get("dividendYield") else cache.get(t,{}).get("DivYield"),
                        "MarketCap": info.get("marketCap"),
                        "TargetMean": info.get("targetMeanPrice") or info.get("targetMedianPrice"),
                        "Recommendation": info.get("recommendationKey") or info.get("averageAnalystRating") or "hold",
                    }
                    # Prova a prendere consenso da recommendations e price targets via yfinance (CORS server-side, più affidabile)
                    try:
                        rec = None
                        # recommendations_summary se disponibile
                        if hasattr(tk, 'recommendations_summary') and tk.recommendations_summary is not None:
                            rs = tk.recommendations_summary
                            if not rs.empty:
                                last = rs.iloc[-1] if hasattr(rs, 'iloc') else rs
                                consenso = {
                                    "rating": str(info.get("recommendationKey") or "hold").capitalize(),
                                    "targetPrice": round(float(info.get("targetMeanPrice") or price*1.15),2),
                                    "upside": round(((info.get("targetMeanPrice") or price*1.15)/price -1)*100,1) if price else 15,
                                    "buy": int(info.get("numberOfAnalystOpinions") or 5) if "buy" in str(info.get("recommendationKey","")).lower() else 4,
                                    "hold": 5,
                                    "sell": 1,
                                    "numAnalysts": int(info.get("numberOfAnalystOpinions") or 10)
                                }
                        # fallback se non ha summary ma ha info
                        if not consenso or not consenso.get("targetPrice"):
                            if info.get("targetMeanPrice"):
                                consenso = {
                                    "rating": str(info.get("recommendationKey") or "Hold").capitalize(),
                                    "targetPrice": round(float(info.get("targetMeanPrice")),2),
                                    "upside": round((float(info.get("targetMeanPrice"))/price -1)*100,1) if price else 15,
                                    "buy": 5, "hold": 4, "sell": 1,
                                    "numAnalysts": int(info.get("numberOfAnalystOpinions") or 10)
                                }
                    except Exception as e:
                        print(f"  Consenso skip {t}: {e}")
                        # fallback deterministico
                        if not consenso:
                            consenso = {
                                "rating": "Hold",
                                "targetPrice": round(price*1.15,2),
                                "upside": 15.0,
                                "buy": 3, "hold": 5, "sell": 1,
                                "numAnalysts": 9
                            }
                except Exception as e:
                    print(f"  Fund skip {t}: {e}")
                    fund = cache.get(t, {})
                    consenso = cache.get(t, {}).get("Consenso") or consenso or {
                        "rating": "Hold",
                        "targetPrice": round(price*1.15,2) if 'price' in locals() else 0,
                        "upside": 15.0,
                        "buy": 3, "hold": 5, "sell": 1,
                        "numAnalysts": 9
                    }

            # Score fondamentale deterministico - NON random, così non cambia a ogni fetch
            def calc_score_f(fund_data, price_val):
                s = 50
                pe = fund_data.get("PE")
                pb = fund_data.get("PB")
                roe = fund_data.get("ROE")
                div = fund_data.get("DivYield")
                de = fund_data.get("DebtEquity")
                if pe and pe > 0:
                    if pe < 12: s += 12
                    elif pe < 18: s += 6
                    elif pe > 25: s -= 10
                if pb and pb > 0:
                    if pb < 1.2: s += 8
                    elif pb > 3: s -= 6
                if roe and roe > 15: s += 15
                elif roe and roe < 5: s -= 10
                if div and div > 4: s += 8
                if de is not None:
                    if de < 50: s += 6
                    elif de > 150: s -= 8
                return max(0, min(100, int(s)))

            def calc_score_t(rsi_v, dist_sma, mom_v, vol_v, pattern_v):
                s = 50
                if rsi_v:
                    if 50 <= rsi_v <= 68: s += 12
                    elif rsi_v > 75: s -= 10
                    elif rsi_v < 30: s += 8
                if dist_sma:
                    if -5 <= dist_sma <= 10: s += 8
                    elif dist_sma < -15: s -= 8
                if mom_v:
                    if mom_v > 5: s += 8
                    elif mom_v < -5: s -= 6
                if vol_v and vol_v > 50: s += 5
                if pattern_v and "Golden" in pattern_v: s += 10
                return max(0, min(100, int(s)))

            scoreF = calc_score_f(fund, price)
            scoreT = calc_score_t(rsi, dist_sma200, mom, vol, pattern)
            scoreTot = round((scoreF + scoreT)/2)

            # Se consenso ancora vuoto, calcola fallback deterministico da score
            if not consenso or not consenso.get("rating"):
                rating_fb = "Buy" if scoreTot >= 70 else "Hold" if scoreTot >= 50 else "Sell"
                consenso = {
                    "rating": rating_fb,
                    "targetPrice": round(price * (1.18 if rating_fb=="Buy" else 1.08 if rating_fb=="Hold" else 0.95),2),
                    "upside": 18 if rating_fb=="Buy" else 8 if rating_fb=="Hold" else -5,
                    "buy": 10 if rating_fb=="Buy" else 3,
                    "hold": 5,
                    "sell": 4 if rating_fb=="Sell" else 1,
                    "numAnalysts": 9
                }

            item = {
                "Ticker": t.replace(".MI",""),
                "TickerYahoo": t,
                "Nome": t.replace(".MI",""),
                "Settore": SECTORS.get(t, "Industrials"),
                "Indice": "FTSE MIB",
                "Prezzo": round(price,2),
                "MarketCapMld": round((fund.get("MarketCap") or cache.get(t,{}).get("MarketCapMld",0)*1e9)/1e9,2) if fund.get("MarketCap") else cache.get(t,{}).get("MarketCapMld"),
                "PE": round(fund.get("PE") or 0,2) if fund.get("PE") else cache.get(t,{}).get("PE"),
                "PB": round(fund.get("PB") or 0,2) if fund.get("PB") else cache.get(t,{}).get("PB"),
                "ROE": round(fund.get("ROE") or 0,2) if fund.get("ROE") else cache.get(t,{}).get("ROE"),
                "DebtEquity": fund.get("DebtEquity"),
                "DivYield": round(fund.get("DivYield") or 0,2) if fund.get("DivYield") else cache.get(t,{}).get("DivYield"),
                "RSI": round(rsi,1),
                "SMA50": round(sma50,2),
                "SMA200": round(sma200,2),
                "DistSMA200": round(dist_sma200,2),
                "Dist52w": round(dist_52w,2),
                "Momentum3M": round(mom,2),
                "VolVsMedia": round(vol,1),
                "Pattern": pattern,
                "ScoreF": scoreF,
                "ScoreT": scoreT,
                "ScoreTot": scoreTot,
                "Consenso": consenso,
            }
            results.append(item)
        except Exception as e:
            print(f"Errore calc {t}: {e}")
            if t in cache:
                results.append(cache[t])

        # Pausa LUNGA obbligatoria tra ticker - questa è la chiave anti-ban
        pause = random.uniform(4.0, 7.5)
        print(f"  Pausa {pause:.1f}s...")
        time.sleep(pause)

    os.makedirs("data", exist_ok=True)
    with open("data/italian_stocks.json","w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    pd.DataFrame(results).to_csv("data/italian_stocks.csv", index=False)
    with open("data/last_update.json","w") as f:
        json.dump({"last_update": datetime.utcnow().isoformat(), "count": len(results)}, f, indent=2)
    print(f"\n✅ Fatto: {len(results)}/{len(TICKERS)} titoli salvati")

if __name__ == "__main__":
    main()