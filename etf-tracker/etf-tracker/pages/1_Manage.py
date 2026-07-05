import streamlit as st
import pandas as pd
from datetime import date

from utils import sheets_db, data_fetch, auth

st.set_page_config(page_title="Manage - Portfolio Tracker", layout="wide")
auth.require_password()

st.title("🔧 Manage")

section = st.sidebar.radio(
    "Section",
    ["Portfolio 1", "Portfolio 2", "Watchlist ETFs", "Backtest history upload", "Dividend log"],
)

POSITION_COLS = {
    "ticker": st.column_config.TextColumn("Ticker", help="6-digit A-share code, e.g. 600519"),
    "name": st.column_config.TextColumn("Name"),
    "asset_type": st.column_config.SelectboxColumn("Type", options=["stock", "etf"]),
    "weight": st.column_config.NumberColumn("Target weight (%)", min_value=0.0, max_value=100.0, step=0.5),
    "cost_basis": st.column_config.NumberColumn("Cost basis (price paid)", min_value=0.0, step=0.01),
    "purchase_date": st.column_config.DateColumn("Purchase date"),
}


REBALANCE_OPTIONS = {
    "none": "No rebalancing (buy & hold at target weights)",
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "semiannual": "Every 6 months",
    "annual": "Annually",
}


def positions_editor(tab_name: str, label: str):
    st.subheader(f"{label} positions")

    current_freq = sheets_db.get_rebalance_frequency(label)
    chosen_freq = st.selectbox(
        "Rebalancing", options=list(REBALANCE_OPTIONS.keys()),
        format_func=lambda k: REBALANCE_OPTIONS[k],
        index=list(REBALANCE_OPTIONS.keys()).index(current_freq),
        key=f"rebal_{tab_name}",
        help="How often to reset all positions back to their target weights. "
             "Applies from your backtest's hand-off date (or your earliest "
             "position's purchase date if you haven't uploaded a backtest) "
             "up to today.",
    )
    if chosen_freq != current_freq:
        sheets_db.save_rebalance_frequency(label, chosen_freq)
        st.success(f"Rebalancing set to: {REBALANCE_OPTIONS[chosen_freq]}")
        st.rerun()

    df = sheets_db.read_df(tab_name)
    for col in POSITION_COLS:
        if col not in df.columns:
            df[col] = None
    if not df.empty:
        df["purchase_date"] = pd.to_datetime(df["purchase_date"], errors="coerce").dt.date
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
        df["cost_basis"] = pd.to_numeric(df["cost_basis"], errors="coerce")

    total_weight = df["weight"].sum() if not df.empty else 0
    if df.empty:
        st.caption("No positions yet - add rows below.")
    elif abs(total_weight - 100) > 0.5:
        st.warning(f"Weights currently sum to {total_weight:.1f}%, not 100%. That's OK while you're editing, but double-check before relying on the numbers.")
    else:
        st.caption(f"Weights sum to {total_weight:.1f}%. ✅")

    edited = st.data_editor(
        df[list(POSITION_COLS.keys())],
        column_config=POSITION_COLS,
        num_rows="dynamic",
        use_container_width=True,
        key=f"editor_{tab_name}",
    )
    if st.button("Save changes", key=f"save_{tab_name}"):
        clean = edited.dropna(subset=["ticker"]).copy()
        clean["ticker"] = clean["ticker"].astype(str).str.strip()
        # --- FIX: convert all datetime/date columns to strings ---
        for col in clean.select_dtypes(include=['datetime64', 'datetime']).columns:
            clean[col] = clean[col].apply(lambda x: x.strftime('%Y-%m-%d') if hasattr(x, 'strftime') else str(x))
        sheets_db.write_df(tab_name, clean)
        sheets_db.clear_caches()
        st.success("Saved.")
        st.rerun()


if section == "Portfolio 1":
    positions_editor("portfolio1_positions", "Portfolio 1")

elif section == "Portfolio 2":
    positions_editor("portfolio2_positions", "Portfolio 2")

elif section == "Watchlist ETFs":
    st.subheader("Watchlist ETFs")
    df = sheets_db.read_df("watchlist_etfs")
    cols = {
        "ticker": st.column_config.TextColumn("Ticker", help="e.g. 510300"),
        "name": st.column_config.TextColumn("Name"),
        "added_date": st.column_config.DateColumn("Added on"),
    }
    for col in cols:
        if col not in df.columns:
            df[col] = None
    if not df.empty:
        df["added_date"] = pd.to_datetime(df["added_date"], errors="coerce").dt.date
        # --- FIX: ensure ticker is string before data_editor ---
        if "ticker" in df.columns:
            df["ticker"] = df["ticker"].astype(str).str.strip()

    edited = st.data_editor(
        df[list(cols.keys())], column_config=cols, num_rows="dynamic",
        use_container_width=True, key="editor_watchlist",
    )
    if st.button("Save changes", key="save_watchlist"):
        clean = edited.dropna(subset=["ticker"]).copy()
        clean["ticker"] = clean["ticker"].astype(str).str.strip()
        # Fill missing added_date with today's date
        clean["added_date"] = clean["added_date"].fillna(pd.Timestamp.today().date())
        # Convert all date columns to strings (fixes JSON serialization)
        for col in clean.select_dtypes(include=['datetime64', 'datetime', 'date']).columns:
            clean[col] = clean[col].apply(lambda x: x.strftime('%Y-%m-%d') if hasattr(x, 'strftime') else str(x))
        sheets_db.write_df("watchlist_etfs", clean)
        sheets_db.clear_caches()
        st.success("Saved.")
        st.rerun()

elif section == "Backtest history upload":
    st.subheader("Upload historical backtest returns")
    
    # --- Ensure the template file exists ---
    import os
    import pandas as pd
    os.makedirs("data", exist_ok=True)
    template_path = "data/backtest_template.xlsx"
    if not os.path.exists(template_path):
        # Create a minimal template with the required columns
        dummy_df = pd.DataFrame(columns=["Date", "Portfolio", "Index Value"])
        dummy_df.to_excel(template_path, index=False, sheet_name="Backtest Data")
        # Add an instructions sheet (optional)
        with pd.ExcelWriter(template_path, mode='a', engine='openpyxl') as writer:
            instructions = pd.DataFrame([
                ["How to fill in this template"],
                ["1. One row per date, per portfolio."],
                ["2. 'Date' = the date of that data point (any consistent format like YYYY-MM-DD works)."],
                ["3. 'Portfolio' must be exactly 'Portfolio 1' or 'Portfolio 2'."],
                ["4. 'Index Value' = a performance index starting at 100."]
            ])
            instructions.to_excel(writer, sheet_name="Instructions", index=False, header=False)

    # Now offer the download
    with open(template_path, "rb") as f:
        st.download_button(
            "Download blank template", f, file_name="backtest_template.xlsx",
            help="Fill this in with your own historical returns, then upload it below.",
        )
    
    # ----- The rest of the original upload logic (unchanged) -----
    uploaded = st.file_uploader("Upload filled-in template", type=["xlsx"])
    if uploaded is not None:
        try:
            new_data = pd.read_excel(uploaded, sheet_name="Backtest Data")
        except Exception as e:
            st.error(f"Couldn't read that file - is it based on the template? ({e})")
            new_data = None

        if new_data is not None:
            required = {"Date", "Portfolio", "Index Value"}
            if not required.issubset(new_data.columns):
                st.error(f"Missing expected columns. Found: {list(new_data.columns)}")
            else:
                new_data = new_data.dropna(subset=["Date", "Portfolio", "Index Value"])
                bad_portfolios = set(new_data["Portfolio"]) - {"Portfolio 1", "Portfolio 2"}
                if bad_portfolios:
                    st.error(f"Unrecognized portfolio name(s): {bad_portfolios}. Must be exactly 'Portfolio 1' or 'Portfolio 2'.")
                else:
                    st.dataframe(new_data, use_container_width=True)
                    for pf in set(new_data["Portfolio"]):
                        first_val = new_data[new_data["Portfolio"] == pf].sort_values("Date")["Index Value"].iloc[0]
                        if abs(first_val - 100) > 0.01:
                            st.info(f"Note: {pf}'s first Index Value is {first_val}, not 100. That's fine - it'll be auto-scaled to start at 100.")
                    if st.button("Confirm and save this backtest data"):
                        existing = sheets_db.read_df("backtest_history")
                        uploaded_portfolios = set(new_data["Portfolio"])
                        if not existing.empty:
                            existing = existing[~existing["portfolio"].isin(uploaded_portfolios)]
                        new_data = new_data.rename(columns={
                            "Date": "date", "Portfolio": "portfolio",
                            "Index Value": "index_value",
                        })
                        new_data["date"] = pd.to_datetime(new_data["date"]).dt.strftime("%Y-%m-%d")
                        combined = pd.concat([existing, new_data[["date", "portfolio", "index_value"]]], ignore_index=True)
                        sheets_db.write_df("backtest_history", combined)
                        sheets_db.clear_caches()
                        st.success("Backtest history saved.")
                        st.rerun()

    st.divider()
    st.write("Current stored backtest history:")
    st.dataframe(sheets_db.read_df("backtest_history"), use_container_width=True)

elif section == "Dividend log":
    st.subheader("Dividend log")
    st.write(
        "Dividends are auto-detected for stocks/ETFs held in your two portfolios "
        "(not the watchlist, since those aren't real holdings). Detection folds "
        "into total-return figures via dividend-adjusted price data automatically - "
        "this log is for your visibility and to correct any mistakes."
    )

    if st.button("🔍 Scan for new dividends now"):
        existing = sheets_db.read_df("dividends")
        existing_keys = set()
        if not existing.empty:
            existing_keys = set(zip(existing["portfolio"], existing["ticker"], existing["ex_date"].astype(str)))

        new_rows = []
        for tab_name, label in [("portfolio1_positions", "Portfolio 1"), ("portfolio2_positions", "Portfolio 2")]:
            pos_df = sheets_db.read_df(tab_name)
            for _, row in pos_df.iterrows():
                ticker = str(row.get("ticker", "")).strip()
                if not ticker:
                    continue
                asset_type = str(row.get("asset_type", "stock")).strip().lower() or "stock"
                purchase_date = pd.to_datetime(row.get("purchase_date"), errors="coerce")
                divs = data_fetch.get_dividends(ticker, asset_type)
                if divs.empty:
                    continue
                if pd.notna(purchase_date):
                    divs = divs[divs["ex_date"] >= purchase_date]
                for _, d in divs.iterrows():
                    ex_date_str = str(d["ex_date"].date())
                    key = (label, ticker, ex_date_str)
                    if key in existing_keys:
                        continue
                    new_rows.append({
                        "portfolio": label,
                        "ticker": ticker,
                        "ex_date": ex_date_str,
                        "pay_date": str(d["pay_date"].date()) if pd.notna(d["pay_date"]) else "",
                        "amount_per_share": d["amount_per_share"],
                        "detected_on": str(date.today()),
                    })
        if new_rows:
            sheets_db.append_rows("dividends", new_rows)
            sheets_db.clear_caches()
            st.success(f"Found and logged {len(new_rows)} new dividend record(s).")
        else:
            st.info("No new dividends found since last scan.")

    st.divider()
    div_df = sheets_db.read_df("dividends")
    edited = st.data_editor(div_df, num_rows="dynamic", use_container_width=True, key="editor_dividends")
    if st.button("Save corrections", key="save_dividends"):
        sheets_db.write_df("dividends", edited)
        sheets_db.clear_caches()
        st.success("Saved.")
        st.rerun()
