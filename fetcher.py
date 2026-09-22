"""
Fetcher FTSE MIB / Euronext Milan - versione anti rate-limit
Fix per YFRateLimitError

Cambiamenti:
- Download prezzi in BATCH unico (1 richiesta invece di 40)
- Chunk da 15 ticker + pausa 3s tra chunk
- Retry con backoff esponenziale
- threads=False per non martellare Yahoo
- Fondamentali con cache e sleep lungo
"""
import yfinance as yf
import pandas as pd
import numpy as np
import json, os, time, random
from datetime import datetime

TICKERS = [
    "ENI.MI","ENEL.MI","ISP.MI","UCG.MI","G.MI","STM.MI","RACE.MI","LDO.MI",
    "PRY.MI","SRG.MI","TRN.MI","PST.MI","GASI.MI","MB.MI","NEXI.MI","CPR.MI",
    "BC.MI","MONC.MI","AMP.MI","BREM.MI","BZU.MI","IP.MI","AZM.MI","MED.MI",
    "SPM.MI","TEN.MI","TIT.MI","BPE.MI","BMPS.MI","BAMI.MI","CNHI.MI","STLA.MI",
    "REC.MI","DNLM.MI","DIA.MI","IG.MI","ITL.MI","HER.MI","ERG.MI","A2A.MI",
    "HOV.MI","CS.MI"
]

SECTORS = {
    "ENI.MI":"Energy","ENEL.MI":"Utilities","ISP.MI":"Financial","UCG.MI":"Financial",
    "G.MI":"Financial","STM.MI":"Technology","RACE.MI":"Auto Luxury","LDO.MI":"Defense",
}

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

def batch_download(tickers, period="2y"):
    all_data = {}
    # Yahoo limita, scarica a chunk
    chunk_size = 10
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i+chunk_size]
        print(f"Batch {i//chunk_size+1}: {chunk}")
        for attempt in range(4):
            try:
                # threads=False è fondamentale per evitare ban
                data = yf.download(chunk, period=period, interval="1d", auto_adjust=True, progress=False, threads=False, group_by='ticker')
                # salva
                if len(chunk)==1:
                    all_data[chunk[0]] = data
                else:
                    for t in chunk:
                        try:
                            if t in data.columns.get_level_values(0) or t in str(data.columns):
                                # estrai
                                if isinstance(data.columns, pd.MultiIndex):
                                    all_data[t] = data[t].dropna()
                                else:
                                    all_data[t] = data
                        except:
                            pass
                print(f"  OK {len(chunk)} tickers")
                break
            except Exception as e:
                wait = (2**attempt) + random.uniform(1,3)
                print(f"  Rate limit / errore: {e} -> retry in {wait:.1f}s")
                time.sleep(wait)
        time.sleep(random.uniform(2.5, 4.5))  # pausa tra chunk
    return all_data

def get_fundamentals_safe(ticker):
    try:
        # sleep lungo per non triggerare limit info
        time.sleep(random.uniform(1.0, 2.0))
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        return {
            "PE": info.get("trailingPE"),
            "PB": info.get("priceToBook"),
            "ROE": (info.get("returnOnEquity") or 0)*100 if info.get("returnOnEquity") else None,
            "DebtEquity": info.get("debtToEquity"),
            "DivYield": (info.get("dividendYield") or 0)*100 if info.get("dividendYield") else None,
            "MarketCap": info.get("marketCap"),
            "MargineNetto": (info.get("profitMargins") or 0)*100 if info.get("profitMargins") else None,
        }
    except Exception as e:
        print(f"  Fund {ticker} skip: {e}")
        return {}

def main():
    print("Download batch prezzi...")
    price_map = batch_download(TICKERS, period="2y")
    results = []
    for t in TICKERS:
        try:
            df = price_map.get(t)
            if df is None or df.empty or len(df)<200:
                print(f"{t}: no data, skip")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
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

            print(f"Fund {t}...")
            fund = get_fundamentals_safe(t)

            item = {
                "Ticker": t.replace(".MI",""),
                "TickerYahoo": t,
                "Nome": t.replace(".MI",""),
                "Settore": SECTORS.get(t, "Industrials"),
                "Indice": "FTSE MIB",
                "Prezzo": round(price,2),
                "MarketCapMld": round((fund.get("MarketCap") or 0)/1e9,2),
                "PE": round(fund.get("PE") or 0,2) if fund.get("PE") else None,
                "PB": round(fund.get("PB") or 0,2) if fund.get("PB") else None,
                "ROE": round(fund.get("ROE") or 0,2) if fund.get("ROE") else None,
                "DebtEquity": round((fund.get("DebtEquity") or 0)/100,2) if fund.get("DebtEquity") else None,
                "DivYield": round(fund.get("DivYield") or 0,2) if fund.get("DivYield") else None,
                "MargineNetto": round(fund.get("MargineNetto") or 0,2) if fund.get("MargineNetto") else None,
                "RSI": round(rsi,1),
                "SMA50": round(sma50,2),
                "SMA200": round(sma200,2),
                "DistSMA200": round(dist_sma200,2),
                "Dist52w": round(dist_52w,2),
                "Momentum3M": round(mom,2),
                "VolVsMedia": round(vol,1),
                "Pattern": pattern,
            }
            results.append(item)
        except Exception as e:
            print(f"Errore {t}: {e}")

    os.makedirs("data", exist_ok=True)
    with open("data/italian_stocks.json","w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    pd.DataFrame(results).to_csv("data/italian_stocks.csv", index=False)
    with open("data/last_update.json","w") as f:
        json.dump({"last_update": datetime.utcnow().isoformat(), "count": len(results)}, f, indent=2)
    print(f"Fatto: {len(results)} titoli -> data/")

if __name__ == "__main__":
    main()