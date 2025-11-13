# app.py

import streamlit as st
from pathlib import Path
from canvasapi import Canvas
import tempfile
import shutil
import pandas as pd
from my_krml_24999690.data.sets import download_canvas_courses


def load_courses(api_url: str, api_key: str):
    """Return list of (id, name) tuples for active/invited courses."""
    api_url = api_url.rstrip("/")
    canvas = Canvas(api_url, api_key)
    courses = canvas.get_courses(enrollment_state="active,invited_or_pending")
    return [(c.id, c.name) for c in courses]

def main():
    st.set_page_config(
        page_title="Canvas Module Downloader",
        page_icon="📚",
        layout="centered",
    )

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

    # --- File type filters (Sidebar or main area) ---
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
                courses = load_courses(api_url, api_key)
                st.session_state.courses = courses
                st.success(f"Loaded {len(courses)} subject(s).")
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

    # --- Footer / Copyright ---
    st.markdown("---")
    st.caption("© 2025 Muhammad Iqbal")


if __name__ == "__main__":
    main()