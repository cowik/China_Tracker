"""
Data fetching:
- Stocks: BaoStock primary, akshare secondary, yfinance fallback
- ETFs: akshare primary, yfinance secondary, BaoStock fallback
"""
from __future__ import annotations
import time
import random
import pandas as pd
import streamlit as st


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


def _retry_download_akshare(ticker: str, start_date: str, end_date: str, retries=4, delay=3):
    """Fetch daily data using akshare (Eastmoney) – best for ETFs."""
    import akshare as ak
    for attempt in range(retries):
        try:
            # Try ETF function first
            df = ak.fund_etf_hist_em(
                symbol=ticker,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="hfq"  # dividend-adjusted
            )
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"日期": "date", "收盘": "close"})
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close'])
            return df[['date', 'close']]
        except Exception as e:
            st.warning(f"akshare ETF attempt {attempt+1} failed: {e}")
            time.sleep(delay * (1 + random.random()))
    return pd.DataFrame()


def _retry_download_akshare_stock(ticker: str, start_date: str, end_date: str, retries=4, delay=3):
    """Fetch stock data via akshare (fallback for stocks)."""
    import akshare as ak
    for attempt in range(retries):
        try:
            df = ak.stock_zh_a_hist(
                symbol=ticker,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="hfq"
            )
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.rename(columns={"日期": "date", "收盘": "close"})
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close'])
            return df[['date', 'close']]
        except Exception as e:
            st.warning(f"akshare stock attempt {attempt+1} failed: {e}")
            time.sleep(delay * (1 + random.random()))
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
                adjustflag="3"
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
    asset_type determines priority:
    - 'etf': akshare (ETF) -> yfinance -> BaoStock
    - 'stock': BaoStock -> akshare (stock) -> yfinance
    """
    ticker = _clean_ticker(ticker)
    df = pd.DataFrame()

    if asset_type == 'etf':
        # 1. akshare ETF function (most reliable for ETFs)
        df = _retry_download_akshare(ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (ETF) loaded via akshare")
            return df.set_index("date")["close"].sort_index()

        # 2. yfinance
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
            if not df.empty:
                st.info(f"✅ {ticker} (ETF) loaded via yfinance")
                return df.set_index("date")["close"].sort_index()
        except Exception:
            pass

        # 3. BaoStock
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (ETF) loaded via BaoStock")
            return df.set_index("date")["close"].sort_index()

    else:  # stock
        # 1. BaoStock
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (stock) loaded via BaoStock")
            return df.set_index("date")["close"].sort_index()

        # 2. akshare stock function
        df = _retry_download_akshare_stock(ticker, start_date, "2050-01-01")
        if not df.empty:
            st.info(f"✅ {ticker} (stock) loaded via akshare")
            return df.set_index("date")["close"].sort_index()

        # 3. yfinance
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
            if not df.empty:
                st.info(f"✅ {ticker} (stock) loaded via yfinance")
                return df.set_index("date")["close"].sort_index()
        except Exception:
            pass

    st.warning(f"❌ No data for {ticker} (asset_type={asset_type})")
    return pd.Series(dtype=float)


# Backward compatibility
def get_stock_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_price_series(ticker, asset_type="stock", start_date=start_date)
    if s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})


def get_etf_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_price_series(ticker, asset_type="etf", start_date=start_date)
    if s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    return pd.DataFrame()
