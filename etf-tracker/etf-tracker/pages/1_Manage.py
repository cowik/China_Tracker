import streamlit as st
import pandas as pd
from datetime import date
import os

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
        help="How often to reset all positions back to their target weights.",
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
        if "ticker" in df.columns:
            df["ticker"] = df["ticker"].astype(str).str.strip()
        df["purchase_date"] = pd.to_datetime(df["purchase_date"], errors="coerce").dt.date
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
        df["cost_basis"] = pd.to_numeric(df["cost_basis"], errors="coerce")

    total_weight = df["weight"].sum() if not df.empty else 0
    if df.empty:
        st.caption("No positions yet - add rows below.")
    elif abs(total_weight - 100) > 0.5:
        st.warning(f"Weights sum to {total_weight:.1f}% – adjust to 100% for accurate tracking.")
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
        if "ticker" in df.columns:
            df["ticker"] = df["ticker"].astype(str).str.strip()

    edited = st.data_editor(
        df[list(cols.keys())], column_config=cols, num_rows="dynamic",
        use_container_width=True, key="editor_watchlist",
    )
    if st.button("Save changes", key="save_watchlist"):
        clean = edited.dropna(subset=["ticker"]).copy()
        clean["ticker"] = clean["ticker"].astype(str).str.strip()
        clean["added_date"] = clean["added_date"].fillna(pd.Timestamp.today().date())
        for col in clean.select_dtypes(include=['datetime64', 'datetime']).columns:
            clean[col] = clean[col].apply(lambda x: x.strftime('%Y-%m-%d') if hasattr(x, 'strftime') else str(x))
        sheets_db.write_df("watchlist_etfs", clean)
        sheets_db.clear_caches()
        st.success("Saved.")
        st.rerun()

elif section == "Backtest history upload":
    st.subheader("Upload historical backtest returns")
    
    os.makedirs("data", exist_ok=True)
    template_path = "data/backtest_template.xlsx"
    if not os.path.exists(template_path):
        pd.DataFrame(columns=["Date", "Portfolio", "Index Value"]).to_excel(
            template_path, index=False, sheet_name="Backtest Data"
        )
        with pd.ExcelWriter(template_path, mode='a', engine='openpyxl') as writer:
            pd.DataFrame([
                ["How to fill in this template"],
                ["1. One row per date, per portfolio."],
                ["2. 'Date' = the date of that data point."],
                ["3. 'Portfolio' must be 'Portfolio 1' or 'Portfolio 2'."],
                ["4. 'Index Value' = a performance index starting at 100."]
            ]).to_excel(writer, sheet_name="Instructions", index=False, header=False)

    with open(template_path, "rb") as f:
        st.download_button("Download blank template", f, file_name="backtest_template.xlsx")

    uploaded = st.file_uploader("Upload filled-in template", type=["xlsx"])
    if uploaded is not None:
        try:
            # Read as text to handle Russian decimals
            new_data = pd.read_excel(uploaded, sheet_name="Backtest Data", dtype=str)
            if "Index Value" in new_data.columns:
                new_data["Index Value"] = new_data["Index Value"].str.replace(",", ".").str.replace(" ", "").str.replace("'", "")
                new_data["Index Value"] = new_data["Index Value"].str.replace(r"[^\d.\-]", "", regex=True)
                new_data["Index Value"] = pd.to_numeric(new_data["Index Value"], errors="coerce")
            if "Date" in new_data.columns:
                new_data["Date"] = pd.to_datetime(new_data["Date"], errors="coerce")
            new_data = new_data.dropna(subset=["Date", "Index Value", "Portfolio"])
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")
            new_data = None

        if new_data is not None:
            required = {"Date", "Portfolio", "Index Value"}
            if not required.issubset(new_data.columns):
                st.error(f"Missing columns. Found: {list(new_data.columns)}")
            else:
                bad = set(new_data["Portfolio"]) - {"Portfolio 1", "Portfolio 2"}
                if bad:
                    st.error(f"Unrecognized portfolio(s): {bad}")
                else:
                    st.dataframe(new_data, use_container_width=True)
                    if st.button("Confirm and save"):
                        existing = sheets_db.read_df("backtest_history")
                        uploaded_pf = set(new_data["Portfolio"])
                        if not existing.empty:
                            existing = existing[~existing["portfolio"].isin(uploaded_pf)]
                        new_data = new_data.rename(columns={
                            "Date": "date", "Portfolio": "portfolio",
                            "Index Value": "index_value",
                        })
                        new_data["date"] = pd.to_datetime(new_data["date"]).dt.strftime("%Y-%m-%d")
                        new_data["index_value"] = pd.to_numeric(new_data["index_value"], errors="coerce")
                        combined = pd.concat([existing, new_data[["date", "portfolio", "index_value"]]], ignore_index=True)
                        # Store as strings with dot to avoid Google Sheets decimal corruption
                        combined["index_value"] = combined["index_value"].apply(
                            lambda x: f"{x:.8f}" if pd.notnull(x) else ""
                        )
                        sheets_db.write_df("backtest_history", combined)
                        sheets_db.clear_caches()
                        st.success("Saved.")
                        st.rerun()

    st.divider()
    st.write("Current stored backtest history:")
    st.dataframe(sheets_db.read_df("backtest_history"), use_container_width=True)

elif section == "Dividend log":
    st.subheader("Dividend log")
    # ... (unchanged, keep as before)
    st.write("Dividends are auto-detected...")
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
            st.success(f"Found {len(new_rows)} new dividend record(s).")
        else:
            st.info("No new dividends found.")

    st.divider()
    div_df = sheets_db.read_df("dividends")
    edited = st.data_editor(div_df, num_rows="dynamic", use_container_width=True, key="editor_dividends")
    if st.button("Save corrections", key="save_dividends"):
        sheets_db.write_df("dividends", edited)
        sheets_db.clear_caches()
        st.success("Saved.")
        st.rerun()
