"""Find a paper's arXiv version and take Figure 1 from its HTML when available."""
import base64
import gzip
import mimetypes
import json
import os
import re
import threading
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from .catalog import title_key

ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?"
ID_RE = re.compile(rf"^{ARXIV_ID}$", re.I)
LINK_RE = re.compile(rf"(?:arxiv\.org/(?:abs|html|pdf)/|arxiv\s*:\s*)({ARXIV_ID})", re.I)
FIGURE_RE = re.compile(r"^\s*(?:figure|fig\.?)\s+1(?=\s|[.:–—(]|$)", re.I)
FIGURE_ID_RE = re.compile(r"(?:^|\.)F1$", re.I)
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA_HOSTS = {"arxiv.org", "www.arxiv.org", "ar5iv.org", "ar5iv.labs.arxiv.org"}
BROWSER_LIMIT = threading.Semaphore(2)
TITLE_LOOKUP_LOCK = threading.Lock()


class FigureUnavailable(Exception):
    """The source provided no usable Figure 1 or teaser."""


def arxiv_id(*values):
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        match = LINK_RE.search(value)
        if match:
            return match.group(1)
        if ID_RE.fullmatch(value):
            return value
    return None


def paper_arxiv(paper):
    return arxiv_id(*(paper.get(field) for field in ("arxiv", "arxiv_pdf", "pdf", "page", "site")))


def metadata_index(path, wanted):
    """Read only requested titles from an arXiv bulk snapshot."""
    if not path:
        return {}
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".json")]
            names += [name for name in archive.namelist() if name.endswith(".jsonl")]
            if len(names) != 1:
                raise ValueError("Expected a single JSONL snapshot in the arXiv zip")
            with archive.open(names[0]) as source:
                return matching_titles(source, wanted)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as source:
        return matching_titles(source, wanted)


def matching_titles(source, wanted):
    index = {}
    ambiguous = set()
    for line in source:
        record = json.loads(line)
        key = title_key(record.get("title") or "")
        identifier = arxiv_id(record.get("id"))
        if key in wanted and identifier:
            if key in index and index[key] != identifier:
                ambiguous.add(key)
            index[key] = identifier
    return {key: value for key, value in index.items() if key not in ambiguous}


def lookup_title(client, title):
    cleaned = " ".join(title.replace('"', "").split())
    with TITLE_LOOKUP_LOCK:
        data, _, _ = client.get("https://export.arxiv.org/api/query", params={
            "search_query": f'ti:"{cleaned}"', "max_results": 8,
        })
    identifiers = {
        arxiv_id(entry.findtext(ATOM + "id"))
        for entry in ElementTree.fromstring(data).findall(ATOM + "entry")
        if title_key(entry.findtext(ATOM + "title") or "") == title_key(title)
    }
    identifiers.discard(None)
    if len(identifiers) == 1:
        return identifiers.pop()
    return None


def find_figure(document):
    teaser = None
    for figure in document.select("figure, div.ltx_figure"):
        if "ltx_table" in figure.get("class", []):
            continue
        caption = figure.find("figcaption", recursive=False)
        if caption is None:
            caption = figure.find(class_="ltx_caption", recursive=False)
        label = caption.get_text(" ", strip=True) if caption else ""
        if FIGURE_RE.match(label) or FIGURE_ID_RE.search(figure.get("id", "")):
            return figure, caption, label
        if teaser is None and (label.lower().startswith("teaser") or
                               "teaser" in figure.get("id", "").lower() or
                               "ltx_figure_teaser" in figure.get("class", [])):
            teaser = (figure, caption, label)
    return teaser


def installed_browser():
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if not os.environ.get(variable):
                continue
            root = Path(os.environ[variable])
            for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                candidate = root / relative
                if candidate.is_file():
                    return candidate
    return None


def capture_panels(figure, browser_path, style):
    """Render the full HTML element, including the original panel arrangement."""
    from playwright.sync_api import Error, sync_playwright

    with BROWSER_LIMIT, sync_playwright() as playwright:
        browser = None
        try:
            options = {"headless": True}
            executable = browser_path or installed_browser()
            if executable:
                options["executable_path"] = str(executable)
            browser = playwright.chromium.launch(**options)
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                """<!doctype html><meta charset="utf-8"><style>
                body{margin:0;background:white;font:16px serif}
                #main-figure{display:block;width:fit-content;min-width:20px;max-width:1420px;padding:8px}
                figure{margin:0;text-align:center} img,svg{max-width:100%;height:auto}
                .ltx_inline-block,.ltx_figure_panel{display:inline-block;vertical-align:top}
                table{border-collapse:collapse;margin:auto}
                </style>""" + style + '<main id="main-figure">' + str(figure) + '</main>',
                wait_until="load", timeout=18000,
            )
            page.wait_for_function(
                "Array.from(document.images).every(img => img.complete && img.naturalWidth > 0)",
                timeout=10000,
            )
            return page.locator("#main-figure").screenshot(animations="disabled", timeout=18000)
        except Error as error:
            raise FigureUnavailable(f"Could not render the entire HTML figure: {error}") from error
        finally:
            if browser:
                browser.close()


def safe_markup(figure):
    for element in figure.select("script, iframe, object, embed, link, meta, form, source"):
        element.decompose()
    for element in [figure, *figure.find_all(True)]:
        for attribute in list(element.attrs):
            if attribute.startswith("on") or attribute in {"srcset", "autofocus", "loading"}:
                del element.attrs[attribute]


def html_figure(client, url, browser_path=None):
    document_data, final_url, _ = client.get(url, 20 * 1024 * 1024)
    document = BeautifulSoup(document_data, "html.parser")
    candidate = find_figure(document)
    if candidate is None:
        raise FigureUnavailable("HTML has no numbered Figure 1 or explicitly marked teaser")
    figure, caption, caption_text = candidate
    if caption:
        caption.decompose()
    for element in figure.select('object[type^="image/"][data], embed[type^="image/"][src]'):
        attrs = dict(element.attrs)
        attrs["src"] = attrs.pop("data", None) or attrs.get("src")
        element.replace_with(document.new_tag("img", attrs=attrs))
    safe_markup(figure)
    base = document.select_one("head base[href]")
    base_url = urljoin(final_url, base["href"]) if base else final_url
    images = []
    for img in figure.find_all("img"):
        source = img.get("src") or img.get("data-src")
        if not source:
            raise FigureUnavailable("HTML figure has an image without a source")
        if source.startswith("data:") and ";base64," in source:
            prefix, encoded = source.split(",", 1)
            media = prefix[5:].split(";", 1)[0]
            payload = base64.b64decode(encoded, validate=True)
        else:
            absolute = urljoin(base_url, source)
            if urlsplit(absolute).hostname not in MEDIA_HOSTS:
                raise FigureUnavailable("HTML figure references media outside the arXiv hosts")
            payload, _, media = client.get(absolute)
            media = media.split(";", 1)[0].strip()
            if media in {"", "application/octet-stream"}:
                media = mimetypes.guess_type(urlsplit(absolute).path)[0] or media
        if not media.startswith("image/"):
            raise FigureUnavailable(f"Unexpected HTML figure media type: {media}")
        images.append((img, payload, media))
    has_subcaptions = figure.select_one(".ltx_subcaption, figcaption") is not None
    if len(images) == 1 and not has_subcaptions and images[0][2] != "image/svg+xml":
        return images[0][1], final_url, caption_text
    if not images and not figure.find("svg"):
        raise FigureUnavailable("HTML figure has no image panels")
    for img, payload, media in images:
        encoded = base64.b64encode(payload).decode("ascii")
        img["src"] = f"data:{media};base64,{encoded}"
    # Keep the arXiv document's inline layout rules. External requests and
    # executable content are excluded from the isolated rendering page.
    style = "".join(str(el) for el in document.select("head style"))
    return capture_panels(figure, browser_path, style), final_url, caption_text
