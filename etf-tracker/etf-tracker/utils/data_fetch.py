from __future__ import annotations
import time
import random
from datetime import datetime, timedelta
from typing import Dict, List

import pandas as pd
import streamlit as st
import yfinance as yf
import pytz

from utils import sheets_db

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

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
    now_beijing = _now_beijing()
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
                    if row and len(row) >= 2 and row[1] == "1":
                        trading_days.append(row[0])
                bs.logout()
                if trading_days:
                    return max(trading_days)
    except Exception:
        pass
        
    dt = _now_beijing()
    if dt.hour < 17:
        dt -= timedelta(days=1)
    while dt.weekday() > 4:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")

def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3) -> pd.DataFrame:
    try:
        end_date_exclusive = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        end_date_exclusive = end
        
    for attempt in range(retries):
        try:
            df = yf.download(ticker_yf, start=start, end=end_date_exclusive, progress=False, timeout=15, auto_adjust=True)
            if df.empty:
                return pd.DataFrame()
            
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
            out = out[out["date"].dt.normalize() <= pd.Timestamp(end)]
            return out
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                return pd.DataFrame()
    return pd.DataFrame()

@st.cache_data(ttl=60, show_spinner=False)
def _fetch_missing_data_batch(requests: list[tuple[str, str, str, str]]) -> dict[str, pd.DataFrame]:
    import baostock as bs
    results = {}
    
    if not requests:
        return results
        
    stock_reqs = [(t, at, s, e) for t, at, s, e in requests if at == "stock"]
    etf_reqs = [(t, at, s, e) for t, at, s, e in requests if at != "stock"]
    
    session_ok = False
    if stock_reqs:
        try:
            lg = bs.login()
            session_ok = lg is not None and lg.error_code == "0"
            
            for ticker, asset_type, start_date, end_date in stock_reqs:
                bs_ticker = _to_baostock_ticker(ticker)
                df = pd.DataFrame()
                
                try:
                    rs = bs.query_history_k_data_plus(bs_ticker, "date,close", start_date=start_date, end_date=end_date, frequency="d", adjustflag="2")
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
                    
                if df.empty:
                    try:
                        rs = bs.query_history_k_data_plus(bs_ticker, "date,close", start_date=start_date, end_date=end_date, frequency="d", adjustflag="3")
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
                
    for ticker, asset_type, start_date, end_date in etf_reqs:
        yf_ticker = _to_yfinance_ticker(ticker)
        df = _retry_download_yfinance(yf_ticker, start_date, end_date)
        if not df.empty:
            results[ticker] = df
            
    return results

# ---- Google Sheets price cache functions ----
def _read_price_cache(tickers: list[str] = None) -> pd.DataFrame:
    """Load price cache from Google Sheets. If tickers given, filter."""
    df = sheets_db.read_df("price_cache")
    if df.empty:
        return pd.DataFrame(columns=["ticker", "asset_type", "date", "close"])
    if tickers:
        df = df[df["ticker"].isin(tickers)]
    return df

def _write_price_cache(new_rows: pd.DataFrame) -> None:
    """Merge new_rows into price_cache sheet and clear caches."""
    if new_rows.empty:
        return
    # Ensure columns
    required = ["ticker", "asset_type", "date", "close"]
    for col in required:
        if col not in new_rows.columns:
            new_rows[col] = None
    # Convert date to string
    new_rows["date"] = pd.to_datetime(new_rows["date"]).dt.strftime("%Y-%m-%d")
    # Read existing
    existing = sheets_db.read_df("price_cache")
    if not existing.empty:
        # Remove rows for tickers in new_rows (update)
        tickers_to_update = new_rows["ticker"].unique()
        existing = existing[~existing["ticker"].isin(tickers_to_update)]
    combined = pd.concat([existing, new_rows[required]], ignore_index=True)
    sheets_db.write_df("price_cache", combined)
    sheets_db.clear_caches()

def get_prices_batch(holdings: list[dict]) -> dict[str, pd.Series]:
    """
    Fetch price data for a list of holdings. Uses Google Sheets cache.
    Returns dict: ticker -> pd.Series of close prices (indexed by date).
    """
    tickers = [_clean_ticker(h["ticker"]) for h in holdings]
    if not tickers:
        return {}

    end_fetch = _get_last_trading_day()
    # Read entire cache for these tickers
    cache_df = _read_price_cache(tickers)
    results = {}
    missing_requests = []

    for h in holdings:
        ticker = _clean_ticker(h["ticker"])
        asset_type = h.get("asset_type", "stock")
        inception = pd.Timestamp(h["inception_date"]).strftime("%Y-%m-%d")

        # Filter cache for this ticker + asset_type
        sub = cache_df[(cache_df["ticker"] == ticker) & (cache_df["asset_type"] == asset_type)]
        if not sub.empty:
            sub = sub.copy()
            sub["date"] = pd.to_datetime(sub["date"])
            sub = sub.sort_values("date").set_index("date")["close"]
            results[ticker] = sub
            max_cached = sub.index.max()
            if max_cached < pd.Timestamp(end_fetch):
                start_missing = (max_cached + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                if start_missing <= end_fetch:
                    missing_requests.append((ticker, asset_type, start_missing, end_fetch))
        else:
            # No cache for this ticker
            missing_requests.append((ticker, asset_type, inception, end_fetch))

    if missing_requests:
        fetched = _fetch_missing_data_batch(missing_requests)
        # Prepare rows to update cache
        rows_to_write = []
        for ticker, asset_type, _, _ in missing_requests:
            if ticker in fetched and not fetched[ticker].empty:
                df = fetched[ticker].copy()
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df = df.dropna(subset=["close"])
                for _, row in df.iterrows():
                    rows_to_write.append({
                        "ticker": ticker,
                        "asset_type": asset_type,
                        "date": row["date"],
                        "close": float(row["close"])
                    })
        if rows_to_write:
            _write_price_cache(pd.DataFrame(rows_to_write))
            # Re-read cache to get updated data
            cache_df = _read_price_cache(tickers)

        # Update results with newly fetched data
        for h in holdings:
            ticker = _clean_ticker(h["ticker"])
            asset_type = h.get("asset_type", "stock")
            sub = cache_df[(cache_df["ticker"] == ticker) & (cache_df["asset_type"] == asset_type)]
            if not sub.empty:
                sub = sub.copy()
                sub["date"] = pd.to_datetime(sub["date"])
                sub = sub.sort_values("date").set_index("date")["close"]
                results[ticker] = sub

    return results

def get_watchlist_prices(watchlist_df: pd.DataFrame) -> Dict[str, pd.Series]:
    """
    For watchlist ETFs, return dict with label (name + ticker) -> normalized
    price series (starting at 1.0). Uses Google Sheets cache.
    """
    if watchlist_df.empty:
        return {}

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

    end_fetch = _get_last_trading_day()
    cache_df = _read_price_cache(tickers)  # all tickers, but we only need etf
    results = {}
    missing_requests = []

    for i, ticker in enumerate(tickers):
        label = labels[i]
        sub = cache_df[(cache_df["ticker"] == ticker) & (cache_df["asset_type"] == "etf")]
        if not sub.empty:
            sub = sub.copy()
            sub["date"] = pd.to_datetime(sub["date"])
            sub = sub.sort_values("date").set_index("date")["close"]
            # Normalize to 1.0 at first date
            if not sub.empty:
                results[label] = sub / sub.iloc[0]
            max_cached = sub.index.max() if not sub.empty else pd.NaT
            if pd.isna(max_cached) or max_cached < pd.Timestamp(end_fetch):
                start_missing = (max_cached + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if not pd.isna(max_cached) else "1990-01-01"
                if start_missing <= end_fetch:
                    missing_requests.append((ticker, "etf", start_missing, end_fetch))
        else:
            missing_requests.append((ticker, "etf", "1990-01-01", end_fetch))

    if missing_requests:
        fetched = _fetch_missing_data_batch(missing_requests)
        rows_to_write = []
        for ticker, asset_type, _, _ in missing_requests:
            if ticker in fetched and not fetched[ticker].empty:
                df = fetched[ticker].copy()
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df = df.dropna(subset=["close"])
                for _, row in df.iterrows():
                    rows_to_write.append({
                        "ticker": ticker,
                        "asset_type": "etf",
                        "date": row["date"],
                        "close": float(row["close"])
                    })
        if rows_to_write:
            _write_price_cache(pd.DataFrame(rows_to_write))
            # Re-read and update results
            cache_df = _read_price_cache(tickers)

        # Update results for all watchlist items
        for i, ticker in enumerate(tickers):
            label = labels[i]
            sub = cache_df[(cache_df["ticker"] == ticker) & (cache_df["asset_type"] == "etf")]
            if not sub.empty:
                sub = sub.copy()
                sub["date"] = pd.to_datetime(sub["date"])
                sub = sub.sort_values("date").set_index("date")["close"]
                if not sub.empty:
                    results[label] = sub / sub.iloc[0]

    return results

# Legacy functions for compatibility
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
