# downloader.py

from pathlib import Path
import re
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from canvasapi import Canvas


def download_canvas_courses(
    api_url: str,
    api_key: str,
    course_ids: list[int] | None = None,
    output_dir: str | Path = "CanvasDownloads",
    logger=None,
    progress_cb=None,
    allowed_exts: set[str] | None = None,
):
    """
    Download module content (Files, Pages, and linked files) from Canvas courses.

    Parameters
    ----------
    api_url : str
    api_key : str
    course_ids : list[int] | None
        If None, downloads all active/invited courses.
    output_dir : str | Path
    logger : callable | None
    progress_cb : callable | None
        Called as progress_cb(done, total, message)
    allowed_exts : set[str] | None
        Set of allowed file extensions including leading dot, e.g.
        {".pdf", ".docx", ".ipynb"}. If None, a sensible default set is used.

    Returns
    -------
    list[dict]:
        Per-course summaries.
    """

    # ---- small logger helper ----
    def log(msg: str):
        if logger is not None:
            logger(msg)
        else:
            print(msg)
            
        
    # ---- Configuration ----
    api_url_clean = api_url.rstrip("/")

    DEFAULT_EXTS = {
        ".pdf", ".doc", ".docx", ".txt", ".rtf", ".ppt", ".pptx",
        ".ipynb", ".py", ".jpg", ".jpeg", ".png", ".gif",
        ".zip", ".rar", ".7z", ".tar", ".gz",
    }
    ALLOWED_EXTS = allowed_exts or DEFAULT_EXTS

    # ---- Helpers ----
    def safe_name(name: str, maxlen: int = 120) -> str:
        if not name:
            return "untitled"
        s = re.sub(r'[\\/*?:"<>|]+', "_", name)
        s = re.sub(r"\s+", " ", s).strip()
        return s[:maxlen]

    def unique_path(dirpath: Path, filename: str) -> Path:
        p = dirpath / filename
        if not p.exists():
            return p
        stem, suffix = p.stem, p.suffix
        i = 1
        while (dirpath / f"{stem} ({i}){suffix}").exists():
            i += 1
        return dirpath / f"{stem} ({i}){suffix}"

    def is_same_host(url: str) -> bool:
        return urlparse(url).netloc == urlparse(api_url_clean).netloc

    def infer_filename_from_headers(url: str, resp: requests.Response, fallback: str) -> str:
        cd = resp.headers.get("Content-Disposition") or resp.headers.get("content-disposition")
        if cd:
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
            if m:
                return safe_name(m.group(1))
        name_from_path = Path(urlparse(url).path).name
        return safe_name(name_from_path or fallback)

    file_id_re = re.compile(r"/files/(\d+)(?:/|$)")

    def extract_file_id(u: str) -> str | None:
        m = file_id_re.search(urlparse(u).path)
        return m.group(1) if m else None

    session = requests.Session()
    BASE_HEADERS = {"User-Agent": "Mozilla/5.0 (CanvasDownloader)"}

    def authed_headers_for(url: str) -> dict:
        h = dict(BASE_HEADERS)
        if is_same_host(url) and api_key:
            h["Authorization"] = f"Bearer {api_key}"
            h["Referer"] = api_url_clean
        return h

    def resolve_file_via_api(file_id: str):
        endpoint = f"{api_url_clean}/api/v1/files/{file_id}"
        try:
            r = session.get(endpoint, headers=authed_headers_for(endpoint), timeout=30)
            r.raise_for_status()
            j = r.json()
            best = j.get("url") or j.get("download_url")
            name = j.get("display_name") or j.get("filename")
            return best, name
        except Exception:
            return None, None

    def allowed_file_type(filename: str) -> bool:
        return Path(filename).suffix.lower() in ALLOWED_EXTS

    def download_binary(url: str, out_dir: Path, preferred_name: str | None = None) -> Path | None:
        try:
            with session.get(url, headers=authed_headers_for(url), stream=True, timeout=60, allow_redirects=True) as r:
                r.raise_for_status()
                fname = safe_name(preferred_name or infer_filename_from_headers(url, r, "download"))
                if not allowed_file_type(fname):
                    return None
                target = unique_path(out_dir, fname)
                with open(target, "wb") as f:
                    for chunk in r.iter_content(262_144):
                        if chunk:
                            f.write(chunk)
            return target
        except Exception as e:
            log(f"    ❌ Failed to download: {url} -> {e}")
            return None

    def extract_all_links_from_html(html_body: str, page_base_url: str) -> list[str]:
        soup = BeautifulSoup(html_body or "", "html.parser")
        urls: list[str] = []

        # anchor href
        for a in soup.find_all("a", href=True):
            urls.append(urljoin(page_base_url, a["href"].strip()))
        # images / embeds / objects
        for tag, attr in (("img", "src"), ("embed", "src"), ("object", "data")):
            for t in soup.find_all(tag):
                val = t.get(attr)
                if val:
                    urls.append(urljoin(page_base_url, val.strip()))
        # Canvas-specific data-api-endpoint
        for el in soup.find_all(attrs={"data-api-endpoint": True}):
            api_ep = el.get("data-api-endpoint", "").strip()
            if api_ep:
                urls.append(api_ep)

        # dedupe (ignore fragments)
        seen, deduped = set(), []
        for u in urls:
            normalized = urlparse(u)._replace(fragment="").geturl()
            if normalized not in seen:
                seen.add(normalized)
                deduped.append(normalized)
        return deduped

    # ---- Main logic ----
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)

    canvas = Canvas(api_url_clean, api_key)

    courses = (
        [canvas.get_course(cid) for cid in course_ids]
        if course_ids
        else list(canvas.get_courses(enrollment_state="active,invited_or_pending"))
    )

    num_courses = len(courses)
    summaries: list[dict] = []

    log(f"Found {num_courses} course(s).")

    # Initialize progress at 0
    if progress_cb is not None:
        progress_cb(0, max(num_courses, 1), "Starting download...")

    done_courses = 0

    for c in courses:
        try:
            course = canvas.get_course(c.id)
            course_dir = root / f"{safe_name(course.name or f'course_{course.id}')} ({course.id})"
            course_dir.mkdir(parents=True, exist_ok=True)

            log(f"\n=== {course.id} – {course.name} ===")

            modules = list(course.get_modules())
            modules.sort(key=lambda m: getattr(m, "position", 0))
            downloaded_ids: set[str] = set()

            # per-course counters
            files_downloaded = 0
            pages_saved = 0
            linked_files_downloaded = 0

            for module in modules:
                mpos = getattr(module, "position", 0)
                mname = safe_name(getattr(module, "name", "Module"))
                module_dir = course_dir / f"{mpos:02d} - {mname}"
                module_dir.mkdir(parents=True, exist_ok=True)

                for item in module.get_module_items():
                    itype = getattr(item, "type", None)
                    title = getattr(item, "title", None) or itype or "Item"

                    # ---- File items ----
                    if itype == "File":
                        file_id = getattr(item, "content_id", None)
                        if not file_id or file_id in downloaded_ids:
                            continue
                        best, disp = resolve_file_via_api(file_id)
                        if best and disp and allowed_file_type(disp):
                            path = download_binary(best, module_dir, preferred_name=disp)
                            if path:
                                downloaded_ids.add(file_id)
                                files_downloaded += 1
                                log(f"  📥 {path.name}")
                        continue

                    # ---- Pages ----
                    if itype == "Page":
                        slug = getattr(item, "page_url", None)
                        if not slug:
                            continue
                        try:
                            page = course.get_page(slug)
                            ptitle = getattr(page, "title", None) or title or slug
                            body = getattr(page, "body", "") or ""
                            html_out = unique_path(module_dir, f"{safe_name(ptitle)}.html")
                            html_out.write_text(
                                f"<!-- Saved from /courses/{course.id}/pages/{slug} -->\n"
                                f"<!-- Title: {ptitle} -->\n{body}",
                                encoding="utf-8",
                            )
                            pages_saved += 1
                            log(f"  📝 {html_out.name}")

                            base_url = f"{api_url_clean}/courses/{course.id}/pages/{slug}"
                            for link in extract_all_links_from_html(body, base_url):
                                fid = extract_file_id(link)
                                if fid and fid not in downloaded_ids:
                                    best, disp = resolve_file_via_api(fid)
                                    if best and disp and allowed_file_type(disp):
                                        path = download_binary(best, module_dir, preferred_name=disp)
                                        if path:
                                            downloaded_ids.add(fid)
                                            linked_files_downloaded += 1
                                            log(f"    📎 {path.name}")

                        except Exception as e:
                            log(f"  ❌ Page '{title}': {e}")

            summaries.append(
                {
                    "course_id": course.id,
                    "course_name": course.name,
                    "files_downloaded": files_downloaded,
                    "pages_saved": pages_saved,
                    "linked_files_downloaded": linked_files_downloaded,
                }
            )

            # ✅ Update progress AFTER finishing a course
            done_courses += 1
            if progress_cb is not None:
                progress_cb(done_courses, max(num_courses, 1), f"Finished {course.name}")

        except Exception as e:
            log(f"\n❌ Skipping course {c.id}: {e}")

    # Ensure we end at 100%
    if progress_cb is not None:
        progress_cb(max(num_courses, 1), max(num_courses, 1), "All courses finished")

    return summaries