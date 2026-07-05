"""
Core math for portfolio & ETF performance.

Key idea: for weight-based portfolios, we don't track literal share counts.
Instead we use each holding's dividend-and-split-adjusted price series
("hfq" in Chinese market data / akshare) which already bakes in the effect
of reinvesting that holding's own dividends. We blend these by target
weight to get a portfolio total-return index. See README for the full
explanation of this design choice.
"""
from __future__ import annotations
import pandas as pd
import numpy as np


def build_holding_return_factor(hfq_prices: pd.Series, inception_date) -> pd.Series:
    """
    Given a dividend-adjusted ('hfq') price series indexed by date, return a
    cumulative growth-factor series (1.0 at inception_date, growing/shrinking
    from there). Dates before inception_date are dropped.
    """
    hfq_prices = hfq_prices.sort_index()
    hfq_prices = hfq_prices[hfq_prices.index >= pd.Timestamp(inception_date)]
    if hfq_prices.empty:
        return pd.Series(dtype=float)
    base = hfq_prices.iloc[0]
    if base == 0 or pd.isna(base):
        raise ValueError("Base price at inception date is zero/NaN - bad data")
    return hfq_prices / base


def build_portfolio_index(
    holdings: list[dict],
    price_data: dict[str, pd.Series],
) -> pd.Series:
    """
    holdings: list of {"ticker": str, "weight": float (0-1), "inception_date": date-like}
    price_data: ticker -> hfq-adjusted close price Series indexed by date

    Returns a portfolio total-return index series, base = 1.0 at the
    portfolio's overall inception date (the earliest inception_date among
    current holdings). This implements a fixed-weight, buy-and-hold,
    dividends-reinvested-into-same-security model (no periodic rebalancing).
    """
    if not holdings:
        return pd.Series(dtype=float)

    weight_sum = sum(h["weight"] for h in holdings)
    if weight_sum <= 0:
        raise ValueError("Weights must sum to a positive number")

    factor_frames = {}
    for h in holdings:
        ticker = h["ticker"]
        if ticker not in price_data or price_data[ticker].empty:
            continue
        factor = build_holding_return_factor(price_data[ticker], h["inception_date"])
        factor_frames[ticker] = factor * (h["weight"] / weight_sum)

    if not factor_frames:
        return pd.Series(dtype=float)

    # Align on the union of dates; each holding only contributes from its own
    # inception date onward. Before a holding's inception, we hold it at its
    # starting weighted value (i.e. don't let it drag the index before it existed).
    combined = pd.DataFrame(factor_frames)
    # Forward-fill is wrong for "before inception" (no data at all there) -
    # instead, for dates where a holding hasn't started yet, treat its
    # contribution as its initial weight (flat), not NaN/zero, so the
    # portfolio index still sums close to 1.0 at t0 even with staggered
    # inception dates.
    for h in holdings:
        ticker = h["ticker"]
        if ticker not in combined.columns:
            continue
        col = combined[ticker]
        first_valid = col.first_valid_index()
        if first_valid is not None:
            flat_value = col.loc[first_valid]
            combined[ticker] = col.where(col.notna(), flat_value)
            combined.loc[combined.index < first_valid, ticker] = flat_value

    combined = combined.sort_index().ffill()
    portfolio_index = combined.sum(axis=1)
    # Renormalize so index = 1.0 exactly at the true overall inception date
    overall_start = min(pd.Timestamp(h["inception_date"]) for h in holdings)
    portfolio_index = portfolio_index[portfolio_index.index >= overall_start]
    if portfolio_index.empty:
        return portfolio_index
    portfolio_index = portfolio_index / portfolio_index.iloc[0]
    return portfolio_index


def generate_rebalance_dates(start_date, end_date, frequency: str) -> list[pd.Timestamp]:
    """
    Rebalance dates at fixed month-count intervals from start_date (e.g.
    quarterly from a Jan 15 start -> Apr 15, Jul 15, Oct 15, ...). frequency:
    "none", "monthly", "quarterly", "semiannual", "annual".
    """
    start_date = pd.Timestamp(start_date)
    end_date = pd.Timestamp(end_date)
    step_months = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}.get(frequency)
    if not step_months:
        return []
    dates = []
    k = 1
    while True:
        candidate = start_date + pd.DateOffset(months=step_months * k)
        if candidate > end_date:
            break
        dates.append(candidate)
        k += 1
    return dates


def compute_live_index(
    holdings: list[dict],
    price_data: dict[str, pd.Series],
    rebalance_frequency: str = "none",
    live_start_date=None,
    end_date=None,
) -> pd.Series:
    """
    Builds the "live tracking" portion of the index (i.e. from the point
    current positions/weights take over, e.g. right after a backtest ends).

    - live_start_date: the single cutoff date at which ALL current holdings
      are allocated at their target weight. If None, defaults to the
      earliest holding's inception_date (staggered buy-in is only preserved
      when rebalancing is "none" AND live_start_date is None - see below).
    - rebalance_frequency: "none" keeps a fixed-weight buy-and-hold from
      live_start_date (or per-holding staggered dates if live_start_date is
      left as None, preserving each position's own purchase date). Any other
      frequency forces a common start (rebalancing needs one) and resets all
      holdings to target weight on every scheduled rebalance date.
    """
    if not holdings:
        return pd.Series(dtype=float)
    if end_date is None:
        end_date = pd.Timestamp.today().normalize()
    else:
        end_date = pd.Timestamp(end_date)

    if rebalance_frequency in (None, "none") and live_start_date is None:
        # No forced cutoff, no rebalancing: preserve original staggered,
        # buy-as-you-add-positions behaviour.
        return build_portfolio_index(holdings, price_data)

    if live_start_date is None:
        live_start_date = min(pd.Timestamp(h["inception_date"]) for h in holdings)
    live_start_date = pd.Timestamp(live_start_date)

    if rebalance_frequency in (None, "none"):
        forced = [{**h, "inception_date": live_start_date} for h in holdings]
        return build_portfolio_index(forced, price_data)

    rebal_dates = generate_rebalance_dates(live_start_date, end_date, rebalance_frequency)
    bounds = [live_start_date] + rebal_dates + [end_date]

    combined = pd.Series(dtype=float)
    running_value = 1.0
    for seg_start, seg_end in zip(bounds[:-1], bounds[1:]):
        seg_holdings = [{**h, "inception_date": seg_start} for h in holdings]
        seg_index = build_portfolio_index(seg_holdings, price_data)
        seg_index = seg_index[(seg_index.index >= seg_start) & (seg_index.index <= seg_end)]
        if seg_index.empty:
            continue
        seg_index = seg_index / seg_index.iloc[0]
        seg_chained = seg_index * running_value
        if not combined.empty:
            seg_chained = seg_chained[seg_chained.index > combined.index[-1]]
        combined = pd.concat([combined, seg_chained])
        if not combined.empty:
            running_value = combined.iloc[-1]
    return combined


def normalize_to_factor(index_values: pd.Series) -> pd.Series:
    """Rescale any index series (e.g. one starting at 100) to start at 1.0,
    based on its own first value - robust even if it doesn't start at exactly 100."""
    s = index_values.sort_index().dropna()
    if s.empty:
        return s
    return s / s.iloc[0]


def chain_link_backtest(backtest_index_values: pd.Series, live_index: pd.Series) -> pd.Series:
    """
    backtest_index_values: raw index values from the user's uploaded backtest
        (e.g. 100, 102.15, 98.7, ...), indexed by date, ending at the cutoff
        where live tracking takes over.
    live_index: Series, base 1.0 at its first date (from compute_live_index).

    Returns one continuous growth-factor series, base 1.0 at the earliest
    date present (backtest start if there is one, else live start).
    """
    backtest_factor = normalize_to_factor(backtest_index_values)
    if backtest_factor.empty and live_index.empty:
        return pd.Series(dtype=float)
    if backtest_factor.empty:
        return live_index
    if live_index.empty:
        return backtest_factor

    junction_value = backtest_factor.iloc[-1]
    live_chained = junction_value * live_index
    live_chained = live_chained[live_chained.index > backtest_factor.index[-1]]
    combined = pd.concat([backtest_factor, live_chained]).sort_index()
    return combined


def period_return(index_series: pd.Series, as_of, lookback_days: int | None, ytd: bool = False) -> float | None:
    """
    % return of index_series from a lookback point to the latest available
    date on/before `as_of`. Uses the last available observation on or before
    each target date (handles weekends/holidays without special-casing them).
    Returns None if there isn't enough history.
    """
    if index_series.empty:
        return None
    s = index_series.sort_index()
    as_of = pd.Timestamp(as_of)
    end_slice = s[s.index <= as_of]
    if end_slice.empty:
        return None
    end_val = end_slice.iloc[-1]
    end_date = end_slice.index[-1]

    if ytd:
        start_target = pd.Timestamp(year=end_date.year, month=1, day=1)
    elif lookback_days is not None:
        start_target = end_date - pd.Timedelta(days=lookback_days)
    else:
        start_target = s.index[0]  # "Max"

    start_slice = s[s.index <= start_target]
    if start_slice.empty:
        # requested lookback predates all history -> use earliest point we have
        start_val = s.iloc[0]
    else:
        start_val = start_slice.iloc[-1]

    if start_val == 0 or pd.isna(start_val) or pd.isna(end_val):
        return None
    return (end_val / start_val - 1) * 100.0


PERIOD_DEFS = {
    "1D": 1,
    "1W": 7,
    "1M": 30,
    "3M": 91,
    "6M": 182,
    "1Y": 365,
}


def comparison_row(index_series: pd.Series, as_of) -> dict:
    """Returns a dict of {period_label: pct_return} for the standard comparison-table periods."""
    return {label: period_return(index_series, as_of, days) for label, days in PERIOD_DEFS.items()}
