import re
import streamlit as st
import pandas as pd

from utils import sheets_db, auth

st.set_page_config(page_title="Repair - Portfolio Tracker", layout="wide")
auth.require_password()

st.title("🩹 Repair portfolios")
st.caption(
    "Restores portfolios whose position tabs still exist in Google Sheets but "
    "are missing from the portfolios_meta registry (the list the dashboard "
    "reads). Only the registry is modified: position tabs, backtest history, "
    "rebalancing settings and all other tabs are never touched."
)

if sheets_db.read_failed("portfolios_meta"):
    st.error(
        "Google Sheets is not responding right now, so the registry cannot be "
        "checked safely. Wait a minute, reload this page, and try again."
    )
    st.stop()


@st.cache_data(ttl=60, show_spinner=False)
def _snapshot() -> dict:
    return sheets_db.diagnose_registry()


snap = _snapshot()

# ------------------------------------------------------------------ registry --
st.subheader("Current registry (portfolios_meta)")
st.caption(
    "Unregister removes a portfolio from the registry ONLY — its positions tab "
    "and backtest history stay in Google Sheets, and it can be re-registered "
    "below at any time. Use it to fix a mistake."
)
if snap["meta_broken"]:
    st.warning(
        "The portfolios_meta tab could not be parsed (it is empty or its "
        "columns are damaged). Every position tab found in the sheet is listed "
        "below for re-registration."
    )
if not snap["registered"]:
    if not snap["meta_broken"]:
        st.info("The registry is empty — no portfolios are registered.")
else:
    for tab, lbl in snap["registered"]:
        c1, c2, c3 = st.columns([3, 2.4, 1])
        c1.write(f"**{lbl}**")
        c2.code(tab, language=None)
        if c3.button("Unregister", key=f"unreg_{tab}", help="Registry row only — positions and backtest are kept."):
            try:
                sheets_db.remove_registry_row(tab)
                st.toast(f"Unregistered {lbl} (tab kept: {tab})")
                st.rerun()
            except sheets_db.SheetsWriteBlockedError:
                st.error("Google Sheets could not be read just now. Reload the page and try again.")

st.divider()

# ------------------------------------------------------------------- repair --
st.subheader("Missing portfolios")
if not snap["orphans"]:
    st.success(
        "All portfolio tabs in Google Sheets are registered — nothing to repair. "
        "If portfolios are still missing from the dashboard, they were deleted "
        "earlier together with their tabs (delete removes the tab too)."
    )
else:
    st.warning(
        f"Found {len(snap['orphans'])} position tab(s) in Google Sheets that are "
        "not in the registry. Their contents are intact. Check the tickers below, "
        "adjust the portfolio names if needed, and press Restore."
    )

    rows = []
    for t in snap["orphans"]:
        num = re.search(r"\d+", t)
        fallback = f"Портфель {num.group()}" if num else t
        suggested = snap["proposal"].get(t, "") or fallback
        rows.append({
            "Positions tab": t,
            "Tickers held": snap["previews"].get(t, ""),
            "Portfolio name": suggested,
        })
    df_map = pd.DataFrame(rows)
    edited = st.data_editor(
        df_map,
        num_rows="fixed",
        disabled=["Positions tab", "Tickers held"],
        use_container_width=True,
        key="repair_mapping",
    )

    if st.button("Restore portfolios", type="primary"):
        if sheets_db.read_failed("portfolios_meta"):
            st.error("Google Sheets could not be read just now — nothing was written. Reload the page and retry.")
        else:
            seen = {lbl for _, lbl in snap["registered"]}
            to_write, skipped, dups = [], [], []
            for _, r in edited.iterrows():
                tab = str(r["Positions tab"]).strip()
                lbl = str(r["Portfolio name"]).strip()
                if not lbl:
                    skipped.append(tab)
                    continue
                if lbl in seen:
                    dups.append(lbl)
                    continue
                seen.add(lbl)
                to_write.append({"tab_name": tab, "label": lbl})
            if dups:
                st.error(f"Duplicate portfolio names (must be unique): {', '.join(sorted(set(dups)))}")
            elif not to_write:
                st.info("Nothing to restore — enter at least one portfolio name.")
            else:
                written = sheets_db.restore_registry_rows(to_write)
                msg = f"Restored {len(written)} portfolio(s): {', '.join(r['label'] for r in written)}."
                if skipped:
                    msg += f" Skipped (no name entered): {', '.join(skipped)}."
                st.success(msg)
                st.rerun()

# --------------------------------------------------------- reference tables --
if snap["orphans"]:
    st.divider()
    with st.expander("Where the suggested names come from (backtest history / settings / display order)"):
        if snap["bt_stats"]:
            ref_rows = [
                {"Portfolio name (from saved data)": name,
                 "Backtest rows": n,
                 "First date": f,
                 "Last date": l}
                for name, (n, f, l) in sorted(snap["bt_stats"].items())
            ]
            st.dataframe(pd.DataFrame(ref_rows), use_container_width=True, hide_index=True)
        else:
            st.write("No backtest history found — type the portfolio names manually.")
        if snap["leftover"]:
            st.write("Names recovered but not matched to any tab above (check them manually):")
            st.write(", ".join(snap["leftover"]))
