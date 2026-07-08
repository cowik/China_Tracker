import streamlit as st
import pandas as pd
import os

from utils import sheets_db, data_fetch, returns, auth

st.set_page_config(page_title="Manage - Portfolio Tracker", layout="wide")
auth.require_password()

st.title("🔧 Manage")

# Fetch portfolios dynamically
PORTFOLIOS = sheets_db.get_portfolios()

sections = list(PORTFOLIOS.values()) + ["Watchlist ETFs", "Backtest history upload", "Manage Portfolios", "Reorder Items"]
section = st.sidebar.radio("Section", sections)

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
            "Clicking this will save the current live tracking performance into the backtest history. "
            "This freezes the current performance and resets the live tracking start date to today."
        )
        if st.button(f"Rebalance {label}", key=f"rebalance_{tab_name}"):
            holdings = []
            for _, row in df.iterrows():
                try:
                    ticker = str(row["ticker"]).strip()
                    
                    # Auto-detect ETFs based on ticker prefix if Type is missing
                    asset_type = str(row.get("asset_type", "")).strip().lower()
                    if not asset_type or asset_type not in ["stock", "etf"]:
                        asset_type = "etf" if ticker.startswith(("1", "5")) else "stock"
                        
                    holdings.append({
                        "ticker": ticker,
                        "asset_type": asset_type,
                        "weight": float(row["weight"]) / 100.0,
                        "inception_date": pd.to_datetime(row["purchase_date"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue

            if not holdings:
                st.warning("No valid positions to rebalance.")
            else:
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
                    
                    # FIX: Keep index_value as a pure float (JSON number) so Google Sheets 
                    # doesn't misinterpret dots as thousands separators based on locale.
                    rebalance_df["index_value"] = pd.to_numeric(rebalance_df["index_value"], errors="coerce")
                    
                    existing = sheets_db.read_df("backtest_history")
                    if not existing.empty:
                        existing = existing[existing["portfolio"] != label]
                        
                    combined_df = pd.concat([existing, rebalance_df[["date", "portfolio", "index_value"]]], ignore_index=True)
                    sheets_db.write_df("backtest_history", combined_df)
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

# --- Routing ---
if section in PORTFOLIOS.values():
    tab_name = [k for k, v in PORTFOLIOS.items() if v == section][0]
    positions_editor(tab_name, section)

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
    st.caption("Upload an Excel file with columns: Date, Portfolio, Index Value (starting at 100).")

    allowed_portfolios = list(PORTFOLIOS.values())
    st.write(f"**Allowed Portfolio names:** {', '.join(allowed_portfolios)}")

    os.makedirs("data", exist_ok=True)
    template_path = "data/backtest_template.xlsx"
    if not os.path.exists(template_path):
        pd.DataFrame(columns=["Date", "Portfolio", "Index Value"]).to_excel(
            template_path, index=False, sheet_name="Backtest Data"
        )

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
                bad = set(new_data["Portfolio"]) - set(allowed_portfolios)
                if bad:
                    st.error(f"Unrecognized portfolio(s): {bad}. Must match an existing portfolio name exactly.")
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
                        combined_df = pd.concat([existing, new_data[["date", "portfolio", "index_value"]]], ignore_index=True)
                        
                        # FIX: Keep index_value as a pure float (JSON number) so Google Sheets 
                        # doesn't misinterpret dots as thousands separators based on locale.
                        combined_df["index_value"] = pd.to_numeric(combined_df["index_value"], errors="coerce")
                        
                        sheets_db.write_df("backtest_history", combined_df)
                        sheets_db.clear_caches()
                        st.success("Backtest history saved.")
                        st.rerun()

    st.divider()
    st.write("Current stored backtest history:")
    st.dataframe(sheets_db.read_df("backtest_history"), use_container_width=True)

elif section == "Manage Portfolios":
    st.subheader("Manage Portfolios")
    st.caption("Add new portfolios or delete existing ones. Deleted portfolios cannot be recovered.")
    
    with st.form("add_portfolio_form"):
        new_label = st.text_input("New Portfolio Name")
        submitted = st.form_submit_button("Add Portfolio")
        if submitted and new_label:
            sheets_db.add_portfolio(new_label)
            st.success(f"Added portfolio: {new_label}")
            st.rerun()
            
    st.divider()
    st.write("**Existing Portfolios:**")
    for tab, label in PORTFOLIOS.items():
        col1, col2 = st.columns([4, 1])
        col1.write(f"{label} (`{tab}`)")
        if col2.button("Delete", key=f"del_{tab}"):
            sheets_db.delete_portfolio(tab, label)
            st.warning(f"Deleted {label}")
            st.rerun()

elif section == "Reorder Items":
    st.subheader("Reorder Portfolios & ETFs")
    st.caption("Set the display order for the main dashboard dropdown and comparison table.")
    
    all_items = list(PORTFOLIOS.values())
    watchlist = sheets_db.read_df("watchlist_etfs")
    if not watchlist.empty:
        for _, row in watchlist.iterrows():
            name = str(row.get("name", "")).strip() or row["ticker"]
            all_items.append(f"{name} ({row['ticker']})")
            
    if not all_items:
        st.info("No portfolios or ETFs found to reorder.")
    else:
        current_order = sheets_db.get_display_order()
        order_list = [current_order.get(item, 99) for item in all_items]
        df_order = pd.DataFrame({"Item": all_items, "Sort Order": order_list})
        
        edited = st.data_editor(
            df_order, 
            num_rows="fixed",
            use_container_width=True,
            key="order_editor"
        )
        
        if st.button("Save Order"):
            sorted_df = edited.sort_values("Sort Order")
            sheets_db.save_display_order(sorted_df["Item"].tolist())
            st.success("Display order saved!")
            st.rerun()
