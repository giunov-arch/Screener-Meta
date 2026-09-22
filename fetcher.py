"""
Fetcher FTSE MIB / Euronext Milan - sorgenti gratuite
- Prezzi storici + indicatori tecnici: yfinance (gratis, illimitato)
- Fondamentali: yfinance info + Alpha Vantage (opzionale, free tier)
Output: data/italian_stocks.json + .csv + data/last_update.json
Formato compatibile con Screener Italia Pro
"""
import yfinance as yf
import pandas as pd
import numpy as np
import json, os, time
from datetime import datetime

# LISTA TITOLI - puoi estendere
TICKERS = [
    "ENI.MI","ENEL.MI","ISP.MI","UCG.MI","G.MI","STM.MI","RACE.MI","LDO.MI",
    "PRY.MI","SRG.MI","TRN.MI","PST.MI","GASI.MI","MB.MI","NEXI.MI","CPR.MI",
    "BC.MI","MONC.MI","AMP.MI","BREM.MI","BZU.MI","IP.MI","AZM.MI","MED.MI",
    "SPM.MI","TEN.MI","TIT.MI","BPE.MI","BMPS.MI","BAMI.MI","CNHI.MI","STLA.MI",
    "REC.MI","DNLM.MI","DIA.MI","IG.MI","ITL.MI","HER.MI","ERG.MI","A2A.MI",
    "HOV.MI","CS.MI","MT.MI","LUX.MI"
]

# Mappatura settori semplificata
SECTORS = {
    "ENI.MI":"Energy","ENEL.MI":"Utilities","ISP.MI":"Financial","UCG.MI":"Financial",
    "G.MI":"Financial","STM.MI":"Technology","RACE.MI":"Auto Luxury","LDO.MI":"Defense",
    "PRY.MI":"Industrials","SRG.MI":"Utilities","TRN.MI":"Utilities","PST.MI":"Financial",
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

def get_fundamentals(ticker, alpha_key=None):
    # prova yfinance info
    try:
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
        print(f"Fundamentals fail {ticker}: {e}")
        return {}

def main():
    results = []
    alpha_key = os.getenv("ALPHAVANTAGE_API_KEY")  # opzionale
    print(f"Fetch {len(TICKERS)} tickers...")
    for t in TICKERS:
        try:
            print(f"-> {t}")
            df = yf.download(t, period="2y", interval="1d", auto_adjust=True, progress=False)
            if df.empty or len(df)<200:
                print(f"  skip, pochi dati")
                continue
            # yfinance con multiindex fix
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = compute_technical(df)
            last = df.iloc[-1]
            prev = df.iloc[-2]
            price = float(last['Close'])
            sma50 = float(last['SMA50']) if not np.isnan(last['SMA50']) else price
            sma200 = float(last['SMA200']) if not np.isnan(last['SMA200']) else price
            rsi = float(last['RSI']) if not np.isnan(last['RSI']) else 50
            mom = float(last['Momentum3M']) if not np.isnan(last['Momentum3M']) else 0
            vol = float(last['VolVsAvg']) if not np.isnan(last['VolVsAvg']) else 0

            dist_sma200 = (price/sma200 -1)*100 if sma200 else 0
            # max 52w
            max52 = float(df['Close'].tail(252).max())
            dist_52w = (price/max52 -1)*100 if max52 else 0

            fund = get_fundamentals(t, alpha_key)
            # pattern detection semplice
            pattern = "Neutrale"
            if price > sma50 > sma200 and rsi>50: pattern="Golden Cross"
            elif abs(price-sma50)/sma50 <0.02 and rsi<55: pattern="Pullback a SMA50"
            elif vol>80 and mom>5: pattern="Breakout volumi"
            elif abs(mom)<3 and vol< -20: pattern="Base stretta"
            elif df['Close'].tail(20).min() > df['Close'].tail(40).head(20).min(): pattern="Minimi crescenti"

            clean_ticker = t.replace(".MI","")
            item = {
                "Ticker": clean_ticker,
                "TickerYahoo": t,
                "Nome": clean_ticker,
                "Settore": SECTORS.get(t, "Industrials"),
                "Indice": "FTSE MIB" if t in TICKERS[:20] else "Mid Cap",
                "Prezzo": round(price,2),
                "MarketCapMld": round((fund.get("MarketCap") or 0)/1e9,2),
                "PE": round(fund.get("PE") or 0,2) if fund.get("PE") else None,
                "PB": round(fund.get("PB") or 0,2) if fund.get("PB") else None,
                "ROE": round(fund.get("ROE") or 0,2) if fund.get("ROE") else None,
                "DebtEquity": round((fund.get("DebtEquity") or 0)/100,2) if fund.get("DebtEquity") else None,
                "DivYield": round(fund.get("DivYield") or 0,2) if fund.get("DivYield") else None,
                "CrescRicavi": None,
                "MargineNetto": round(fund.get("MargineNetto") or 0,2) if fund.get("MargineNetto") else None,
                "FCFYield": None,
                "RSI": round(rsi,1),
                "SMA50": round(sma50,2),
                "SMA200": round(sma200,2),
                "DistSMA200": round(dist_sma200,2),
                "Dist52w": round(dist_52w,2),
                "Momentum3M": round(mom,2),
                "VolVsMedia": round(vol,1),
                "Pattern": pattern,
                "Data": datetime.utcnow().isoformat()
            }
            results.append(item)
            time.sleep(0.6) # rispetta rate limit
        except Exception as e:
            print(f"Errore {t}: {e}")

    # Salva JSON per app
    os.makedirs("data", exist_ok=True)
    with open("data/italian_stocks.json","w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # Salva CSV compatibile con Screener
    df_out = pd.DataFrame(results)
    # mapping colonne attese da app
    csv_cols = ["Ticker","Prezzo","PE","PB","ROE","DebtEquity","DivYield","MargineNetto","RSI","SMA50","SMA200","Momentum3M","VolVsMedia"]
    df_out.to_csv("data/italian_stocks.csv", index=False, encoding="utf-8")

    with open("data/last_update.json","w") as f:
        json.dump({"last_update": datetime.utcnow().isoformat(), "count": len(results)}, f, indent=2)

    print(f"Fatto: {len(results)} titoli salvati in data/")

if __name__ == "__main__":
    main()