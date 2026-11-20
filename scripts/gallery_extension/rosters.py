"""Read accepted-paper rosters and public arXiv cross-references by edition."""
import html
import json
import re
from collections import defaultdict
from urllib.parse import quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from build_pool_new import build_aaai, build_acl, build_cvpr

from .arxiv import paper_arxiv
from .catalog import CACHE, ROOT, author_list, title_key, atomic_write

OPENREVIEW = "https://openreview.net/"
ICLR_2026_SNAPSHOT = "https://raw.githubusercontent.com/gisbi-kim/iclr2026-explorer/master/output/iclr2026_papers.json"
COPILOT = "https://raw.githubusercontent.com/papercopilot/paperlists/main/"
POOL_SOURCE = "https://github.com/qwdwqfwq/topconf-paper-figure-gallery/tree/main/data/pool"
ICML_VOLUMES = {
    2013: 28, 2014: 32, 2015: 37, 2016: 48, 2017: 70, 2018: 80,
    2019: 97, 2020: 119, 2021: 139, 2022: 162, 2023: 202, 2024: 235, 2025: 267,
}


def seed_pools():
    editions = defaultdict(list)
    for file in sorted((ROOT / "data" / "pool").glob("*.jsonl")):
        with file.open(encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("title"):
                    editions[(row["venue"], int(row["year"]))].append(row)
    return editions


def web_url(*values):
    return next((value for value in values
                 if isinstance(value, str) and urlsplit(value).scheme in {"http", "https"}), "")


def accepted_tier(string):
    value = string.lower()
    if "oral" in value:
        return "oral"
    if "spotlight" in value:
        return "spotlight"
    return ""


def normalized(rows, venue, year):
    unique = {}
    for row in rows:
        title = " ".join(str(row.get("title") or "").split())
        if not title:
            continue
        arxiv = paper_arxiv(row)
        arxiv_pdf = f"https://arxiv.org/pdf/{arxiv}" if arxiv else ""
        arxiv_page = f"https://arxiv.org/abs/{arxiv}" if arxiv else ""
        item = {
            "title": title, "venue": venue, "year": year,
            "authors": author_list(row.get("authors") or row.get("author")),
            "pdf": web_url(row.get("pdf"), row.get("pdf_source"), arxiv_pdf),
            "page": web_url(row.get("page"), row.get("paper"), row.get("openreview"), arxiv_page, row.get("site")),
            "tier": accepted_tier(str(row.get("status") or row.get("tier") or "")),
        }
        if not item["page"]:
            item["page"] = item["pdf"]
        if arxiv:
            item["arxiv"] = arxiv
        unique.setdefault(title_key(title), item)
    return list(unique.values())


def copilot(client, venue, year):
    slug = "nips" if venue == "neurips" else venue
    url = f"{COPILOT}{slug}/{slug}{year}.json"
    cached = CACHE / "cross-references" / f"{slug}{year}.json"
    if cached.exists():
        return json.loads(cached.read_bytes()), url
    contents, _, _ = client.get(url)
    if contents.startswith(b"version https://git-lfs.github.com/spec/v1"):
        media = f"https://media.githubusercontent.com/media/papercopilot/paperlists/main/{slug}/{slug}{year}.json"
        contents, _, _ = client.get(media, 128 * 1024 * 1024)
    rows = json.loads(contents)
    if not isinstance(rows, list):
        raise ValueError("Paper Copilot returned an unexpected list")
    atomic_write(cached, contents)
    return rows, url


def arxiv_hints(client, venue, year, seed, allowed):
    hints = {title_key(row["title"]): paper_arxiv(row) for row in seed if row.get("title")}
    try:
        extra, _ = copilot(client, venue, year)
    except (requests.RequestException, ValueError):
        extra = []
    for row in extra:
        key = title_key(row.get("title") or "")
        identifier = paper_arxiv(row)
        if key in allowed and identifier and not hints.get(key):
            hints[key] = identifier
    return hints


def openreview(client, venue, year):
    prefix = {"iclr": "ICLR.cc", "icml": "ICML.cc"}[venue]
    group = f"{prefix}/{year}/Conference"
    options = {"content.venueid": group, "limit": 1000, "offset": 0}
    entries = []
    endpoint = "https://api2.openreview.net/notes"
    while True:
        payload, _, _ = client.get(endpoint, params=options)
        page = json.loads(payload)["notes"]
        entries.extend(page)
        options["offset"] += len(page)
        if len(page) < options["limit"]:
            break
    rows = []
    for entry in entries:
        content = {key: (value.get("value") if isinstance(value, dict) else value)
                   for key, value in entry["content"].items()}
        forum = quote(entry["forum"])
        rows.append({
            "title": content.get("title"), "authors": content.get("authors"),
            "page": OPENREVIEW + "forum?id=" + forum,
            "pdf": OPENREVIEW + "pdf?id=" + forum, "arxiv": content.get("arxiv"),
            "status": content.get("venue"),
        })
    return rows, f"https://openreview.net/group?id={group}"


def pmlr(client, year):
    volume = ICML_VOLUMES.get(year)
    if not volume:
        return openreview(client, "icml", year)
    url = f"https://proceedings.mlr.press/v{volume}/"
    payload, _, _ = client.get(url)
    document = BeautifulSoup(payload, "html.parser")
    rows = []
    for block in document.select(".paper"):
        title = block.select_one(".title")
        pdf = block.select_one('a[href$=".pdf"]')
        landing = block.select_one('a[href$=".html"]')
        authors = block.select_one(".authors")
        if title and pdf:
            rows.append({"title": title.get_text(" ", strip=True),
                         "authors": authors.get_text(" ", strip=True) if authors else "",
                         "pdf": urljoin(url, pdf["href"]),
                         "page": urljoin(url, landing["href"]) if landing else ""})
    return rows, url


def neurips(client, year):
    url = f"https://proceedings.neurips.cc/paper_files/paper/{year}"
    raw, _, _ = client.get(url)
    landing = BeautifulSoup(raw, "html.parser")
    volumes = {}
    for node in landing.find_all("a", href=True):
        target = urljoin(url, node["href"])
        if re.search(r"/vol\d+-(?:main-conference|datasets-and-benchmarks)(?:/|$)", target):
            volumes[target] = None
    pages = []
    for link in volumes:
        raw, _, _ = client.get(link)
        pages.append(BeautifulSoup(raw, "html.parser"))
    if not pages:
        pages = [landing]
    rows = []
    for page in pages:
        for link in page.find_all("a", href=True):
            href = link["href"]
            if not re.search(r"/hash/[^/]+-Abstract[^/]*\.html$", href, re.I):
                continue
            paper = urljoin(url, href)
            pdf = paper.replace("/hash/", "/file/").replace("-Abstract", "-Paper").removesuffix(".html") + ".pdf"
            authors = link.parent.find("i")
            rows.append({"title": link.get_text(" ", strip=True),
                         "authors": authors.get_text(" ", strip=True) if authors else "",
                         "page": paper, "pdf": pdf})
    return rows, url


def acl_history(client, year):
    url = f"https://aclanthology.org/events/acl-{year}/"
    payload, _, _ = client.get(url)
    document = BeautifulSoup(payload, "html.parser")
    legacy = re.compile(rf"^/?(?:P{year % 100:02d}-[12]\d{{3}}|{year}\.acl-main\.\d+)/?$")
    rows = []
    for anchor in document.find_all("a", href=legacy):
        identifier = anchor["href"].strip("/")
        if identifier.endswith(("000", ".0")):
            continue
        landing = urljoin(url, f"/{identifier}/")
        block = anchor.find_parent(class_="d-sm-flex") or anchor.parent
        authors = [tag.get_text(" ", strip=True) for tag in block.select('a[href^="/people/"]')]
        rows.append({"title": anchor.get_text(" ", strip=True), "authors": authors,
                     "page": landing, "pdf": landing.rstrip("/") + ".pdf"})
    return rows, url


def aaai_published(client, year):
    # Crossref serves the article metadata deposited by AAAI Press, including
    # the PDF galley URLs for older issues that are difficult to enumerate.
    endpoint = "https://api.crossref.org/journals/2374-3468/works"
    params = {
        "filter": f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31,type:journal-article",
        "select": "DOI,title,author,volume,link,URL",
        "rows": 500,
        "cursor": "*",
    }
    volume = str(year - 1986)
    rows, seen = [], set()
    while True:
        raw, _, _ = client.get(endpoint, params=params)
        page = json.loads(raw)["message"]
        items = page["items"]
        new = 0
        for item in items:
            doi = item["DOI"]
            if doi in seen:
                continue
            seen.add(doi)
            new += 1
            if item.get("volume") != volume or not doi.startswith(f"10.1609/aaai.v{volume}i"):
                continue
            title = (item.get("title") or [""])[0]
            title = BeautifulSoup(html.unescape(title), "html.parser").get_text(" ", strip=True)
            authors = [
                " ".join(filter(None, (author.get("given"), author.get("family"))))
                for author in item.get("author", [])
            ]
            pdf = next((link["URL"] for link in item.get("link", []) if link.get("content-type") == "application/pdf"), "")
            rows.append({"title": title, "authors": authors, "pdf": pdf, "page": item.get("URL") or f"https://doi.org/{doi}"})
        if len(items) < params["rows"] or not new or not page.get("next-cursor"):
            break
        params["cursor"] = page["next-cursor"]
    return rows, f"https://aaai.org/proceeding/aaai-{volume}-{year}/"


def iclr_2026_snapshot(client):
    path = CACHE / "iclr2026-explorer.json"
    if path.exists():
        raw = path.read_bytes()
    else:
        raw, _, _ = client.get(ICLR_2026_SNAPSHOT)
    data = json.loads(raw)
    rows = [
        {
            "title": row["title"],
            "authors": row.get("authors"),
            "page": row.get("openreview_url"),
            "pdf": row.get("pdf_url"),
            "status": row.get("status"),
            "arxiv": row.get("resource_url"),
        }
        for row in data["papers"]
        if row.get("status", "").lower() in {"oral", "poster", "spotlight"}
    ]
    if rows and not path.exists():
        atomic_write(path, raw)
    return rows, ICLR_2026_SNAPSHOT


def official_roster(client, venue, year):
    if venue == "iclr":
        try:
            rows, source = openreview(client, venue, year)
            if rows or year != 2026:
                return rows, source
        except requests.RequestException:
            if year != 2026:
                raise
        return iclr_2026_snapshot(client)
    if venue == "icml":
        return pmlr(client, year)
    if venue == "neurips":
        return neurips(client, year)
    if venue == "cvpr":
        return build_cvpr(year), f"https://openaccess.thecvf.com/CVPR{year}?day=all"
    if venue == "acl":
        if year <= 2020:
            return acl_history(client, year)
        return build_acl(year), f"https://aclanthology.org/events/acl-{year}/"
    if venue == "aaai":
        try:
            articles, source = aaai_published(client, year)
            if articles:
                return articles, source
        except (requests.RequestException, ValueError, KeyError):
            pass
        return build_aaai(year), "https://ojs.aaai.org/index.php/AAAI/issue/archive"
    raise ValueError(f"Unsupported conference: {venue}")


def accepted_copilot(rows):
    accepted = re.compile(r"\b(accept(?:ed)?|poster|spotlight|oral|highlight|award)\b", re.I)
    rejected = re.compile(r"\b(reject(?:ed)?|withdrawn|submitted|desk reject)\b", re.I)
    for row in rows:
        status = str(row.get("status") or "")
        if not accepted.search(status) or rejected.search(status):
            continue
        row = dict(row)
        row["page"] = web_url(row.get("oa"), row.get("openreview"), row.get("site"))
        if not row.get("pdf") and "openreview.net/forum" in row["page"]:
            row["pdf"] = row["page"].replace("/forum?", "/pdf?")
        yield row


def load(client, venue, year, seed, refresh=False):
    cache = CACHE / "rosters" / f"{venue}{year}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_bytes())
    warnings = []
    # These committed pools are already snapshots of the official accepted
    # proceedings. Refresh explicitly when the publisher has a newer list.
    if seed and not refresh and year < 2026 and not (venue == "neurips" and year == 2025):
        rows, source = seed, POOL_SOURCE
    else:
        rows, source = [], ""
        try:
            rows, source = official_roster(client, venue, year)
        except (requests.RequestException, ValueError, KeyError) as error:
            warnings.append(str(error))
        if not rows:
            try:
                source_rows, source = copilot(client, venue, year)
                rows = list(accepted_copilot(source_rows))
                if rows:
                    warnings.append("Using Paper Copilot's accepted-paper roster")
            except (requests.RequestException, ValueError) as error:
                warnings.append(str(error))
        if not rows and seed:
            rows, source = seed, POOL_SOURCE
            if year >= 2026:
                warnings.append("Upstream pool is a topic-prioritized subset for this edition")
            elif venue == "neurips" and year == 2025:
                warnings.append("Upstream NeurIPS 2025 snapshot includes only arXiv-matched papers")
    result = {"source": source, "warnings": warnings, "rows": normalized(rows, venue, year)}
    if result["rows"]:
        atomic_write(cache, json.dumps(result, ensure_ascii=False).encode("utf-8"))
    return result
