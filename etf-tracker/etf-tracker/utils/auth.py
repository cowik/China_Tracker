"""Simple single-password gate for the Manage page. Password lives in
Streamlit secrets (never in code), so it's safe even if the repo is public."""
import streamlit as st


def require_password():
    """Call at the top of the Manage page. Renders a password box and
    stops the rest of the page from rendering until the correct password
    is entered. Uses session_state so you don't have to re-enter it on
    every widget interaction within the same browser session."""
    if st.session_state.get("authenticated"):
        return

    st.title("🔒 Manage (password required)")
    with st.form("login_form"):
        pw = st.text_input("Admin password", type="password")
        submitted = st.form_submit_button("Log in")
    if submitted:
        if pw == st.secrets.get("admin_password"):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()
