"""
Data fetching: 
- ETFs: efinance primary, yfinance fallback, BaoStock last
- Stocks: BaoStock primary, efinance fallback, yfinance last
"""
from __future__ import annotations
import pandas as pd
import streamlit as st
import time
import random

# Try to import efinance; if not available, fall back
try:
    import efinance as ef
    EFINANCE_AVAILABLE = True
except ImportError:
    EFINANCE_AVAILABLE = False
    st.warning("efinance not installed – falling back to other sources.")


def _clean_ticker(ticker: str) -> str:
    """Remove suffixes and spaces."""
    ticker = str(ticker).strip()
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    return ticker.replace('.', '')


def _to_baostock_ticker(ticker: str) -> str:
    """Convert to baostock format: sh.xxxxxx or sz.xxxxxx."""
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"sh.{ticker}"
    else:
        return f"sz.{ticker}"


def _to_yfinance_ticker(ticker: str) -> str:
    """Convert to yfinance format: xxxxxx.SS or xxxxxx.SZ."""
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"{ticker}.SS"
    else:
        return f"{ticker}.SZ"


def _retry_download_efinance(ticker: str, start_date: str, end_date: str, retries=3):
    """Fetch daily data using efinance (Eastmoney)."""
    if not EFINANCE_AVAILABLE:
        return pd.DataFrame()
    for attempt in range(retries):
        try:
            df = ef.stock.get_quote_history(
                code=ticker,
                start=start_date,
                end=end_date
            )
            if df is not None and not df.empty:
                df = df.rename(columns={"日期": "date", "收盘": "close"})
                df['date'] = pd.to_datetime(df['date'])
                df['close'] = pd.to_numeric(df['close'], errors='coerce')
                df = df.dropna(subset=['close'])
                return df[['date', 'close']]
        except Exception as e:
            st.warning(f"efinance attempt {attempt+1} failed: {e}")
            time.sleep(2 * (1 + random.random()))
    return pd.DataFrame()


def _retry_download_baostock(ticker: str, start_date: str, end_date: str, retries=3):
    """Fetch daily data using BaoStock."""
    import baostock as bs
    for attempt in range(retries):
        try:
            lg = bs.login()
            if lg is None or lg.error_code != '0':
                st.warning(f"BaoStock login failed: {lg.error_msg if lg else 'None'}")
                time.sleep(2)
                continue

            rs = bs.query_history_k_data_plus(
                ticker,
                "date,close",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3"  # 3 = no adjustment
            )
            if rs.error_code != '0':
                st.warning(f"BaoStock query failed: {rs.error_msg}")
                bs.logout()
                time.sleep(2)
                continue

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())
            bs.logout()

            if not data_list:
                return pd.DataFrame()

            df = pd.DataFrame(data_list, columns=rs.fields)
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close'])
            return df[['date', 'close']]

        except Exception as e:
            st.warning(f"BaoStock attempt {attempt+1} failed: {e}")
            time.sleep(2 * (1 + random.random()))
    return pd.DataFrame()


def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3):
    """Fallback to yfinance."""
    import yfinance as yf
    for attempt in range(retries):
        try:
            df = yf.download(ticker_yf, start=start, end=end, progress=False, timeout=15)
            if df.empty:
                return pd.DataFrame()
            close = df['Close']
            out = close.reset_index()
            out.columns = ['date', 'close']
            out['date'] = pd.to_datetime(out['date'])
            return out
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                raise e
    return pd.DataFrame()


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    """
    Returns date-indexed close price series.
    asset_type is used to prioritize data source:
    - 'stock': try BaoStock first, then efinance, then yfinance
    - 'etf': try efinance first, then yfinance, then BaoStock
    """
    ticker = _clean_ticker(ticker)
    df = pd.DataFrame()
    
    if asset_type == 'stock':
        # Try BaoStock first for stocks
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (stock) loaded via BaoStock")
            return df.set_index("date")["close"].sort_index()
        
        # Fallback to efinance
        df = _retry_download_efinance(ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (stock) loaded via efinance")
            return df.set_index("date")["close"].sort_index()
        
        # Finally yfinance
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
            if not df.empty:
                st.info(f"✅ {ticker} (stock) loaded via yfinance")
                return df.set_index("date")["close"].sort_index()
        except Exception:
            pass

    elif asset_type == 'etf':
        # Try efinance first for ETFs
        df = _retry_download_efinance(ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (ETF) loaded via efinance")
            return df.set_index("date")["close"].sort_index()
        
        # Fallback to yfinance
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
            if not df.empty:
                st.info(f"✅ {ticker} (ETF) loaded via yfinance")
                return df.set_index("date")["close"].sort_index()
        except Exception:
            pass
        
        # Last try BaoStock
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (ETF) loaded via BaoStock")
            return df.set_index("date")["close"].sort_index()

    # If all failed
    st.warning(f"❌ No data for {ticker} (asset_type={asset_type})")
    return pd.Series(dtype=float)


# For backward compatibility with existing code
def get_stock_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    df = get_price_series(ticker, asset_type="stock", start_date=start_date)
    if df.empty:
        return pd.DataFrame()
    return df.reset_index().rename(columns={"index": "date"})


def get_etf_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    df = get_price_series(ticker, asset_type="etf", start_date=start_date)
    if df.empty:
        return pd.DataFrame()
    return df.reset_index().rename(columns={"index": "date"})


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    # Placeholder – dividends can be fetched if needed
    return pd.DataFrame()
