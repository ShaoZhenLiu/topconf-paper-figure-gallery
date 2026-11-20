"""Extend coverage from accepted proceedings, using arXiv HTML first."""
import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymupdf
import requests
from PIL import Image
from xml.etree import ElementTree

from extract_figures import content_above, find_captions

from . import arxiv, rosters
from .catalog import VENUES, paper_key, store_image, title_key
from .network import Client

PDF_LOCK = threading.Lock()
ARXIV_MODERN = re.compile(r"^\d{4}\.\d{4,5}")
SOURCE_ERRORS = (requests.RequestException, OSError, ValueError, arxiv.FigureUnavailable,
                 Image.DecompressionBombError)


def year_list(spec):
    years = set()
    for part in spec.split(","):
        start, separator, end = part.strip().partition(":")
        first = int(start)
        last = int(end) if separator else first
        if not 1900 <= first <= last <= 2100:
            raise ValueError("Year range must be increasing and between 1900 and 2100")
        years.update(range(first, last + 1))
    return sorted(years)


def pdf_figure(data):
    if not data.startswith(b"%PDF"):
        raise arxiv.FigureUnavailable("Source did not return a PDF")
    # PyMuPDF pages are not safe to access from concurrent threads.
    with PDF_LOCK, pymupdf.open(stream=data, filetype="pdf") as document:
        for pages in (range(min(4, len(document))), range(4, len(document))):
            regions = []
            for number in pages:
                page = document[number]
                for caption in find_captions(page):
                    region = content_above(page, caption)
                    if region and region.width >= 50 and region.height >= 25 and caption.y0 - region.y1 <= 50:
                        regions.append((region.width * region.height, number, region, caption.y0))
            if regions:
                _, number, region, caption_top = max(regions, key=lambda entry: entry[0])
                page = document[number]
                bottom = min(region.y1 + 3, caption_top)
                clip = pymupdf.Rect(region.x0 - 4, region.y0 - 4, region.x1 + 4, bottom) & page.rect
                return page.get_pixmap(dpi=216, clip=clip, alpha=False).tobytes("png")
    raise arxiv.FigureUnavailable("Could not identify a Figure 1 region in the PDF")


def extract_one(client, paper, hints, args):
    identifier = arxiv.paper_arxiv(paper) or hints.get(title_key(paper["title"]))
    errors = []
    if not identifier and args.arxiv_lookup:
        try:
            identifier = arxiv.lookup_title(client, paper["title"])
        except (requests.RequestException, ElementTree.ParseError, ValueError) as error:
            errors.append(f"arXiv lookup: {error}")
    if identifier:
        paper = {**paper,
                 "page": paper.get("page") or f"https://arxiv.org/abs/{identifier}",
                 "pdf": paper.get("pdf") or f"https://arxiv.org/pdf/{identifier}"}
        html_urls = [f"https://arxiv.org/html/{identifier}", f"https://ar5iv.labs.arxiv.org/html/{identifier}"]
        for url in html_urls:
            try:
                data, source_url, caption = arxiv.html_figure(client, url, args.browser)
                return store_image(paper, data, "arxiv-html", source_url, caption, identifier), "", ""
            except SOURCE_ERRORS as error:
                errors.append(f"HTML: {error}")
    pdf_urls = dict.fromkeys(url for url in (
        paper.get("pdf"), f"https://arxiv.org/pdf/{identifier}" if identifier else None,
    ) if url)
    for url in pdf_urls:
        try:
            content, source_url, _ = client.get(url, 80 * 1024 * 1024)
            data = pdf_figure(content)
            update = {**paper, "pdf": paper.get("pdf") or source_url,
                      "page": paper.get("page") or f"https://arxiv.org/abs/{identifier}"}
            return store_image(update, data, "proceedings-pdf", source_url, arxiv=identifier), "", ""
        except SOURCE_ERRORS + (pymupdf.FileDataError,) as error:
            errors.append(f"PDF: {error}")
    return None, "conference-image-unavailable", " | ".join(errors)[:1600] or "No public figure source found"


def run(args, catalog):
    try:
        years = year_list(args.years)
    except ValueError as error:
        raise SystemExit(f"Invalid --years: {error}") from error
    venues = list(dict.fromkeys(item.strip().lower() for item in args.venues.split(",")))
    if not all(venue in VENUES[:-1] for venue in venues):
        raise SystemExit(f"--venues must be selected from {', '.join(VENUES[:-1])}")
    client = Client()
    seed = rosters.seed_pools()
    existing = catalog.existing()
    editions = [(venue, year) for year in years for venue in venues]
    pending = {}
    unavailable = []
    with ThreadPoolExecutor(max_workers=min(args.workers, 6)) as pool:
        futures = {
            pool.submit(rosters.load, client, venue, year, seed.get((venue, year), []), args.refresh_rosters):
            (venue, year) for venue, year in editions
        }
        for future in as_completed(futures):
            key = futures[future]
            result = future.result()
            remaining = [paper for paper in result["rows"] if paper_key(paper) not in existing]
            label = f"{key[0].upper()} {key[1]}"
            print(f"{label}: {len(result['rows']):,} listed; {len(remaining):,} still need a figure; "
                  f"source {result['source'] or 'unavailable'}", flush=True)
            for warning in result["warnings"][-2:]:
                print(f"{label}: {warning}", flush=True)
            if result["rows"]:
                pending[key] = remaining
            else:
                unavailable.append(key)
    if args.plan:
        return int(bool(unavailable))

    wanted = {title_key(row["title"]) for rows in pending.values() for row in rows}
    hints = arxiv.metadata_index(args.arxiv_metadata, wanted)
    total = Counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for key in editions:
            rows = pending.get(key, [])
            if not rows:
                continue
            edition_hints = rosters.arxiv_hints(client, *key, seed.get(key, []), wanted)
            hints.update({title: identifier for title, identifier in edition_hints.items() if identifier})
            rows.sort(key=lambda row: not (arxiv.paper_arxiv(row) or hints.get(title_key(row["title"]))))
            if args.limit_per_edition:
                rows = rows[:args.limit_per_edition]
            for offset in range(0, len(rows), 32):
                batch = rows[offset:offset + 32]
                futures = {pool.submit(extract_one, client, row, hints, args): row for row in batch}
                counters = Counter()
                for future in as_completed(futures):
                    row = futures[future]
                    extracted, status, detail = future.result()
                    if extracted:
                        catalog.save(extracted)
                        counters[extracted[1]["source"]] += 1
                    else:
                        catalog.record_attempt(paper_key(row), status, detail)
                        counters["unavailable"] += 1
                catalog.publish()
                total.update(counters)
                print(f"{key[0].upper()} {key[1]} {offset + len(batch)}/{len(rows)}: "
                      + ", ".join(f"{name} {count}" for name, count in counters.items()), flush=True)
    print(json.dumps({"conference_run": dict(total), "rosters_unavailable": unavailable}), flush=True)
    return int(bool(unavailable or total["unavailable"]))
