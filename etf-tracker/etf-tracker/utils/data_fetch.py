"""
Data fetching:
- Stocks: BaoStock primary (adjusted), fallback to yfinance
- ETFs: yfinance primary, fallback to BaoStock
"""
from __future__ import annotations
import time
import random
import pandas as pd
import streamlit as st
import yfinance as yf


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


def _retry_download_baostock(ticker: str, start_date: str, end_date: str, adjustflag='2', retries=3):
    """Fetch daily data using BaoStock with given adjustflag."""
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
                adjustflag=adjustflag  # '2' = forward-adjusted (total return)
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
    """Fetch daily data using yfinance."""
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
    - For stocks: try BaoStock (adjusted) → BaoStock (unadjusted) → yfinance
    - For ETFs: try yfinance → BaoStock
    """
    ticker = _clean_ticker(ticker)
    df = pd.DataFrame()

    if asset_type == 'stock':
        # 1. BaoStock adjusted (total return)
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01", adjustflag='2')
        if not df.empty:
            st.info(f"✅ {ticker} (stock) loaded via BaoStock (adjusted)")
            return df.set_index("date")["close"].sort_index()

        # 2. BaoStock unadjusted
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01", adjustflag='3')
        if not df.empty:
            st.info(f"✅ {ticker} (stock) loaded via BaoStock (unadjusted)")
            return df.set_index("date")["close"].sort_index()

        # 3. yfinance fallback
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
            if not df.empty:
                st.info(f"✅ {ticker} (stock) loaded via yfinance (fallback)")
                return df.set_index("date")["close"].sort_index()
        except Exception:
            pass

    else:  # 'etf'
        # 1. yfinance
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
            if not df.empty:
                st.info(f"✅ {ticker} (ETF) loaded via yfinance")
                return df.set_index("date")["close"].sort_index()
        except Exception:
            pass

        # 2. BaoStock fallback for ETFs (if yfinance fails)
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01", adjustflag='2')
        if not df.empty:
            st.info(f"✅ {ticker} (ETF) loaded via BaoStock (fallback)")
            return df.set_index("date")["close"].sort_index()

    # If all failed
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
    """Optional dividend fetching via yfinance (for both stocks and ETFs)."""
    ticker = _clean_ticker(ticker)
    yf_ticker = _to_yfinance_ticker(ticker)
    try:
        tkr = yf.Ticker(yf_ticker)
        divs = tkr.dividends
        if divs.empty:
            return pd.DataFrame()
        df = divs.reset_index()
        df.columns = ['ex_date', 'amount_per_share']
        df['pay_date'] = df['ex_date']
        df['ex_date'] = pd.to_datetime(df['ex_date'])
        df['pay_date'] = pd.to_datetime(df['pay_date'])
        return df[['ex_date', 'pay_date', 'amount_per_share']]
    except Exception:
        return pd.DataFrame()
