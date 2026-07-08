"""
Data fetching with persistent local SQLite cache.
- Stocks & ETFs: BaoStock (adjustflag='2') -> yfinance fallback.
- Uses batch fetching to minimize BaoStock login/logout cycles.
"""
from __future__ import annotations
import time
import random
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Dict, List

import pandas as pd
import streamlit as st
import yfinance as yf
import pytz

from utils import sheets_db

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

# ----------------------------------------------------------------- SQLite Cache --
def _get_db_conn():
    """Returns a fresh SQLite connection. Ensures table exists and WAL mode is enabled."""
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/prices.db", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_cache (
            ticker TEXT,
            asset_type TEXT,
            date TEXT,
            close REAL,
            PRIMARY KEY (ticker, asset_type, date)
        )
    """)
    conn.commit()
    return conn

# ---------------------------------------------------------------- helpers --
def _now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)

def _is_trading_day(dt: datetime) -> bool:
    return dt.weekday() < 5

def _clean_ticker(ticker: str) -> str:
    ticker = str(ticker).strip()
    for suffix in (".SH", ".SZ", ".SS"):
        if ticker.upper().endswith(suffix):
            ticker = ticker[: -len(suffix)]
    return ticker.replace(".", "")

def _to_baostock_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    return f"sh.{ticker}" if ticker[0] in ("5", "6") else f"sz.{ticker}"

def _to_yfinance_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    return f"{ticker}.SS" if ticker[0] in ("5", "6") else f"{ticker}.SZ"

@st.cache_data(ttl=300, show_spinner=False)
def _get_last_trading_day() -> str:
    """Cached — only computed once per 5 minutes."""
    now_beijing = _now_beijing()
    
    # BaoStock delay: Daily closing data isn't available until ~17:30 Beijing time.
    # If it's before 17:30, we must look for the *previous* trading day.
    cutoff_time = now_beijing.replace(hour=17, minute=30, second=0, microsecond=0)
    if now_beijing < cutoff_time:
        now_beijing -= timedelta(days=1)
        
    today = now_beijing.strftime("%Y-%m-%d")
    start = (now_beijing - timedelta(days=10)).strftime("%Y-%m-%d")
    
    try:
        import baostock as bs
        lg = bs.login()
        if lg is not None and lg.error_code == '0':
            rs = bs.query_trade_dates(start_date=start, end_date=today)
            if rs.error_code == '0':
                trading_days = []
                while rs.next():
                    row = rs.get_row_data()
                    # BaoStock returns [date, is_trading_day]. 
                    # We must check if is_trading_day == "1" to skip holidays/weekends!
                    if row and len(row) >= 2 and row[1] == "1":
                        trading_days.append(row[0])
                bs.logout()
                if trading_days:
                    return max(trading_days)
    except Exception:
        pass
        
    # Fallback: just step back over weekends if API fails
    dt = _now_beijing()
    if dt.hour < 17:
        dt -= timedelta(days=1)
    while dt.weekday() > 4:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")

# ------------------------------------------------------------ raw fetchers --
def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3) -> pd.DataFrame:
    # yfinance uses EXCLUSIVE end dates. We must add 1 day to actually get the "end" day.
    try:
        end_date_exclusive = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        end_date_exclusive = end
        
    for attempt in range(retries):
        try:
            df = yf.download(
                ticker_yf, start=start, end=end_date_exclusive,
                progress=False, timeout=15,
                auto_adjust=True,
            )
            if df.empty:
                return pd.DataFrame()
            
            # Handle yfinance MultiIndex columns (fixes KeyError crash)
            if isinstance(df.columns, pd.MultiIndex):
                if "Adj Close" in df.columns.get_level_values(0):
                    close = df["Adj Close"].iloc[:, 0]
                elif "Close" in df.columns.get_level_values(0):
                    close = df["Close"].iloc[:, 0]
                else:
                    return pd.DataFrame()
            else:
                close = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
                
            out = close.reset_index()
            out.columns = ["date", "close"]
            out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
            
            # Filter out intraday data to sync with 17:30 BaoStock rule
            out = out[out["date"].dt.normalize() <= pd.Timestamp(end)]
            
            return out
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                return pd.DataFrame()
    return pd.DataFrame()

def _fetch_missing_data_batch(requests: list[tuple[str, str, str, str]]) -> dict[str, pd.DataFrame]:
    """Fetch multiple tickers using a single BaoStock session."""
    import baostock as bs
    results = {}
    
    if not requests:
        return results
        
    session_ok = False
    try:
        lg = bs.login()
        session_ok = lg is not None and lg.error_code == "0"
        
        for ticker, asset_type, start_date, end_date in requests:
            bs_ticker = _to_baostock_ticker(ticker)
            df = pd.DataFrame()
            
            # Try BaoStock adjustflag="2" (forward adjusted for splits/dividends)
            try:
                rs = bs.query_history_k_data_plus(
                    bs_ticker, "date,close",
                    start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag="2",
                )
                if rs.error_code == "0":
                    data_list = []
                    while rs.next():
                        data_list.append(rs.get_row_data())
                    if data_list:
                        df = pd.DataFrame(data_list, columns=rs.fields)
                        df["date"] = pd.to_datetime(df["date"])
                        df["close"] = pd.to_numeric(df["close"], errors="coerce")
                        df = df.dropna(subset=["close"])[["date", "close"]]
            except Exception:
                pass
                
            # Try BaoStock adjustflag="3" if empty
            if df.empty:
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_ticker, "date,close",
                        start_date=start_date, end_date=end_date,
                        frequency="d", adjustflag="3",
                    )
                    if rs.error_code == "0":
                        data_list = []
                        while rs.next():
                            data_list.append(rs.get_row_data())
                        if data_list:
                            df = pd.DataFrame(data_list, columns=rs.fields)
                            df["date"] = pd.to_datetime(df["date"])
                            df["close"] = pd.to_numeric(df["close"], errors="coerce")
                            df = df.dropna(subset=["close"])[["date", "close"]]
                except Exception:
                    pass
                    
            # Fallback to yfinance if BaoStock fails completely
            if df.empty:
                yf_ticker = _to_yfinance_ticker(ticker)
                df = _retry_download_yfinance(yf_ticker, start_date, end_date)
                
            if not df.empty:
                results[ticker] = df
                
    except Exception:
        pass
    finally:
        if session_ok:
            bs.logout()
            
    return results

# --------------------------------------------------------------- batch watchlist --
@st.cache_data(ttl=300, show_spinner=False)
def get_watchlist_prices(watchlist_df: pd.DataFrame) -> Dict[str, pd.Series]:
    if watchlist_df.empty:
        return {}
        
    conn = _get_db_conn()
    
    tickers = []
    labels = []
    for _, row in watchlist_df.iterrows():
        ticker = str(row["ticker"]).strip()
        if not ticker:
            continue
        name = str(row.get("name", "")).strip() or ticker
        tickers.append(ticker)
        labels.append(f"{name} ({ticker})")
        
    if not tickers:
        return {}
        
    end_date = _get_last_trading_day()
    start_date = (datetime.now(BEIJING_TZ) - timedelta(days=3650)).strftime("%Y-%m-%d")
    
    # Check what we have in SQLite
    results = {}
    missing_tickers = []
    missing_labels = []
    missing_requests = []
    
    for i, t in enumerate(tickers):
        df_cache = pd.read_sql(
            "SELECT date, close FROM price_cache WHERE ticker=? AND asset_type='etf'",
            conn, params=[t]
        )
        if not df_cache.empty:
            df_cache["date"] = pd.to_datetime(df_cache["date"])
            s = df_cache.sort_values("date").set_index("date")["close"]
            if not s.empty:
                max_date = s.index.max()
                if max_date >= pd.Timestamp(end_date) - pd.Timedelta(days=4):
                    results[labels[i]] = s / s.iloc[0]
                    continue
                    
        missing_tickers.append(t)
        missing_labels.append(labels[i])
        missing_requests.append((t, 'etf', start_date, end_date))
        
    if missing_requests:
        fetched_data = _fetch_missing_data_batch(missing_requests)
        
        rows_to_write = []
        for i, req in enumerate(missing_requests):
            ticker = req[0]
            label = missing_labels[i]
            
            if ticker in fetched_data and not fetched_data[ticker].empty:
                df = fetched_data[ticker]
                
                # Filter out intraday data to sync with 17:30 BaoStock rule
                df = df[df['date'].dt.normalize() <= pd.Timestamp(end_date)]
                
                if not df.empty:
                    close_series = df.set_index('date')['close']
                    results[label] = close_series / close_series.iloc[0]
                    
                    for dt, price in close_series.items():
                        rows_to_write.append((ticker, 'etf', dt.strftime("%Y-%m-%d"), float(price)))
                        
        if rows_to_write:
            conn.executemany(
                "INSERT OR REPLACE INTO price_cache (ticker, asset_type, date, close) VALUES (?, ?, ?, ?)",
                rows_to_write
            )
            conn.commit()
            
    conn.close()
    return results

# ------------------------------------------------------------------ public --
@st.cache_data(ttl=300, show_spinner=False)
def get_prices_batch(holdings: list[dict]) -> dict[str, pd.Series]:
    """
    Fetches prices for a list of holdings using a single BaoStock session 
    and batches writes to SQLite.
    """
    conn = _get_db_conn()
    
    tickers = [_clean_ticker(h["ticker"]) for h in holdings]
    if not tickers:
        return {}
        
    placeholders = ','.join(['?'] * len(tickers))
    df_cache = pd.read_sql(
        f"SELECT ticker, asset_type, date, close FROM price_cache WHERE ticker IN ({placeholders})",
        conn, params=tickers
    )
    
    end_fetch = _get_last_trading_day()
    missing_requests = []
    results = {}
    
    for h in holdings:
        ticker = _clean_ticker(h["ticker"])
        asset_type = h.get("asset_type", "stock")
        start_date = pd.Timestamp(h["inception_date"]).strftime("%Y-%m-%d")
        
        if not df_cache.empty:
            mask = (df_cache["ticker"] == ticker) & (df_cache["asset_type"] == asset_type)
            cached = df_cache[mask].copy()
        else:
            cached = pd.DataFrame()
            
        if not cached.empty:
            cached["date"] = pd.to_datetime(cached["date"])
            cached_series = cached.sort_values("date").set_index("date")["close"]
            results[ticker] = cached_series
            
            max_cached_date = cached_series.index.max()
            start_fetch = (max_cached_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if start_fetch <= end_fetch:
                missing_requests.append((ticker, asset_type, start_fetch, end_fetch))
        else:
            missing_requests.append((ticker, asset_type, start_date, end_fetch))
            
    if not missing_requests:
        conn.close()
        return results
        
    fetched_data = _fetch_missing_data_batch(missing_requests)
    
    rows_to_write = []
    for ticker, asset_type, _, _ in missing_requests:
        if ticker in fetched_data and not fetched_data[ticker].empty:
            df = fetched_data[ticker]
            for _, row in df.iterrows():
                rows_to_write.append((
                    ticker, asset_type, 
                    pd.to_datetime(row["date"]).strftime("%Y-%m-%d"), 
                    float(row["close"])
                ))
                
    if rows_to_write:
        conn.executemany(
            "INSERT OR REPLACE INTO price_cache (ticker, asset_type, date, close) VALUES (?, ?, ?, ?)",
            rows_to_write
        )
        conn.commit()
        
    df_cache = pd.read_sql(
        f"SELECT ticker, asset_type, date, close FROM price_cache WHERE ticker IN ({placeholders})",
        conn, params=tickers
    )
    
    conn.close()
    for h in holdings:
        ticker = _clean_ticker(h["ticker"])
        asset_type = h.get("asset_type", "stock")
        if not df_cache.empty:
            mask = (df_cache["ticker"] == ticker) & (df_cache["asset_type"] == asset_type)
            cached = df_cache[mask]
            if not cached.empty:
                cached["date"] = pd.to_datetime(cached["date"])
                results[ticker] = cached.sort_values("date").set_index("date")["close"]
                
    return results

# ---- backward compatibility ----
def get_stock_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_prices_batch([{"ticker": ticker, "asset_type": "stock", "inception_date": start_date}]).get(ticker)
    if s is None or s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})

def get_etf_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_prices_batch([{"ticker": ticker, "asset_type": "etf", "inception_date": start_date}]).get(ticker)
    if s is None or s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})

def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    return pd.DataFrame()
