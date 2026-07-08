import streamlit as st
import pandas as pd
import os

from utils import sheets_db, data_fetch, returns, auth

st.set_page_config(page_title="Manage - Portfolio Tracker", layout="wide")
auth.require_password()

st.title("🔧 Manage")

# ----- Rename portfolios for display -----
PORTFOLIO_LABELS = {
    "portfolio1_positions": "Возможности Китая",
    "portfolio2_positions": "Возможности Китая. Специальная 2",
}

section = st.sidebar.radio(
    "Section",
    [
        PORTFOLIO_LABELS["portfolio1_positions"],
        PORTFOLIO_LABELS["portfolio2_positions"],
        "Watchlist ETFs",
        "Backtest history upload",
    ],
)

SECTION_TAB_MAP = {
    PORTFOLIO_LABELS["portfolio1_positions"]: "portfolio1_positions",
    PORTFOLIO_LABELS["portfolio2_positions"]: "portfolio2_positions",
}

POSITION_COLS = {
    "ticker": st.column_config.TextColumn("Ticker", help="6-digit A-share code, e.g. 600519"),
    "name": st.column_config.TextColumn("Name"),
    "asset_type": st.column_config.SelectboxColumn("Type", options=["stock", "etf"]),
    "weight": st.column_config.NumberColumn("Target weight (%)", min_value=0.0, max_value=100.0, step=0.5),
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

    # Read and prepare data
    df = sheets_db.read_df(tab_name)
    for col in POSITION_COLS:
        if col not in df.columns:
            df[col] = None

    if not df.empty and "ticker" in df.columns:
        df["ticker"] = df["ticker"].astype(str).str.strip()

    if not df.empty:
        df["purchase_date"] = pd.to_datetime(df["purchase_date"], errors="coerce").dt.date
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce")

    if not df.empty:
        total_weight = df["weight"].sum()
        if abs(total_weight - 100) > 0.5:
            st.warning(f"Weights sum to {total_weight:.1f}% – adjust to 100% for accurate tracking.")
        else:
            st.caption(f"Weights sum to {total_weight:.1f}%. ✅")
    else:
        st.caption("No positions yet – add rows below using the editor.")

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
        sheets_db.write_df(tab_name, clean)
        sheets_db.clear_caches()
        st.success("Saved.")
        st.rerun()

    if not df.empty:
        st.divider()
        st.subheader("⚖️ Rebalance (save live performance to backtest)")
        st.caption(
            "Clicking this will save the current live tracking performance (from the "
            "last backtest date to today) into the backtest history. This freezes the "
            "current performance and resets the live tracking start date to today."
        )
        if st.button(f"Rebalance {label}", key=f"rebalance_{tab_name}"):
            holdings = []
            for _, row in df.iterrows():
                try:
                    holdings.append({
                        "ticker": str(row["ticker"]).strip(),
                        "asset_type": str(row.get("asset_type", "stock")).strip().lower() or "stock",
                        "weight": float(row["weight"]) / 100.0,
                        "inception_date": pd.to_datetime(row["purchase_date"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue

            if not holdings:
                st.warning("No valid positions to rebalance.")
            else:
                # FIX: Use batch fetching to avoid multiple API logins
                price_data = data_fetch.get_prices_batch(holdings)
                
                backtest_index_values = load_backtest(label)
                rebalance_freq = sheets_db.get_rebalance_frequency(label)
                live_start_date = backtest_index_values.index[-1] if not backtest_index_values.empty else None

                live_index = returns.compute_live_index(
                    holdings, price_data,
                    rebalance_frequency=rebalance_freq,
                    live_start_date=live_start_date,
                )
                combined = returns.chain_link_backtest(backtest_index_values, live_index)
                if combined.empty:
                    st.warning("Could not compute combined index.")
                else:
                    rebalance_df = combined.reset_index()
                    rebalance_df.columns = ["date", "index_value"]
                    rebalance_df["portfolio"] = label
                    rebalance_df["date"] = pd.to_datetime(rebalance_df["date"]).dt.strftime("%Y-%m-%d")
                    rebalance_df["index_value"] = rebalance_df["index_value"].apply(
                        lambda x: f"{x:.8f}" if pd.notnull(x) else ""
                    )
                    existing = sheets_db.read_df("backtest_history")
                    if not existing.empty:
                        existing = existing[existing["portfolio"] != label]
                    combined = pd.concat([existing, rebalance_df[["date", "portfolio", "index_value"]]], ignore_index=True)
                    sheets_db.write_df("backtest_history", combined)
                    sheets_db.clear_caches()
                    st.success(f"✅ Rebalance complete! {label} backtest now includes performance up to today.")
                    st.rerun()


def load_backtest(portfolio_label: str) -> pd.Series:
    df = sheets_db.read_df("backtest_history")
    if df.empty:
        return pd.Series(dtype=float)
    df = df[df["portfolio"] == portfolio_label].copy()
    if df.empty:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    return pd.Series(pd.to_numeric(df["index_value"], errors="coerce").values, index=df["date"])


if section in SECTION_TAB_MAP:
    tab_name = SECTION_TAB_MAP[section]
    label = section
    positions_editor(tab_name, label)

elif section == "Watchlist ETFs":
    st.subheader("Watchlist ETFs")
    df = sheets_db.read_df("watchlist_etfs")
    cols = {
        "ticker": st.column_config.TextColumn("Ticker", help="e.g. 510300"),
        "name": st.column_config.TextColumn("Name"),
    }
    for col in cols:
        if col not in df.columns:
            df[col] = None
    if not df.empty and "ticker" in df.columns:
        df["ticker"] = df["ticker"].astype(str).str.strip()

    edited = st.data_editor(
        df[list(cols.keys())], column_config=cols, num_rows="dynamic",
        use_container_width=True, key="editor_watchlist",
    )
    if st.button("Save changes", key="save_watchlist"):
        clean = edited.dropna(subset=["ticker"]).copy()
        clean["ticker"] = clean["ticker"].astype(str).str.strip()
        sheets_db.write_df("watchlist_etfs", clean)
        sheets_db.clear_caches()
        st.success("Saved.")
        st.rerun()

elif section == "Backtest history upload":
    st.subheader("Upload historical backtest returns")
    st.caption("Upload an Excel file with columns: Date, Portfolio (exactly 'Возможности Китая' or 'Возможности Китая. Специальная 2'), Index Value (starting at 100).")

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
                ["3. 'Portfolio' must be exactly 'Возможности Китая' or 'Возможности Китая. Специальная 2'."],
                ["4. 'Index Value' = a performance index starting at 100."]
            ]).to_excel(writer, sheet_name="Instructions", index=False, header=False)

    with open(template_path, "rb") as f:
        st.download_button("Download blank template", f, file_name="backtest_template.xlsx")

    uploaded = st.file_uploader("Upload filled-in template", type=["xlsx"])
    if uploaded is not None:
        try:
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
                allowed = {"Возможности Китая", "Возможности Китая. Специальная 2"}
                bad = set(new_data["Portfolio"]) - allowed
                if bad:
                    st.error(f"Unrecognized portfolio(s): {bad}. Must be exactly 'Возможности Китая' or 'Возможности Китая. Специальная 2'.")
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
                        combined["index_value"] = combined["index_value"].apply(
                            lambda x: f"{x:.8f}" if pd.notnull(x) else ""
                        )
                        sheets_db.write_df("backtest_history", combined)
                        sheets_db.clear_caches()
                        st.success("Backtest history saved.")
                        st.rerun()

    st.divider()
    st.write("Current stored backtest history:")
    st.dataframe(sheets_db.read_df("backtest_history"), use_container_width=True)
