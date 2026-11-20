"""Import numbered Figure 1 images from the DiagramBank release."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from queue import Empty, Queue
from urllib.parse import quote

from PIL import Image

from .catalog import CACHE, VENUES, paper_key, store_image

DATASET = "ghzlmc/DiagramBank"
REVISION = "8d8d408b4ccff03d76e8cbd7b2f39a0914405c7e"
METADATA_SHA256 = "6b8eb310827342405eafc4f383054aa6357b78c2f97613675f09b55870f19708"
RANGE_SIZE = 16 * 1024 * 1024
CURL = shutil.which("curl.exe") or shutil.which("curl")
NONFINAL_DECISION = re.compile(r"\b(?:reject\w*|withdraw\w*|conditional|declin\w*)\b", re.I)


def curl_args():
    if not CURL:
        raise RuntimeError("curl is required to download DiagramBank")
    return [CURL, "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "15"]


def download_metadata(url, path):
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        subprocess.run(curl_args() + [
            "--continue-at", "-", "--max-time", "900", "--retry", "2",
            "--output", str(partial), url,
        ], check=True)
        os.replace(partial, path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != METADATA_SHA256:
        raise ValueError(f"DiagramBank metadata does not match release SHA-256: {path}")
    return path


class ArchiveStream:
    """Read from a local archive or append verified HTTP ranges as needed."""

    def __init__(self, url, archive, local_only):
        self.archive = archive
        self.partial = archive.with_name(archive.name + ".part")
        self.chunk = archive.with_name(archive.name + ".range")
        self.headers = archive.with_name(archive.name + ".range-headers")
        self.url = url
        self.remote_size = None
        self.range_size = RANGE_SIZE
        self.cached = archive.is_file()
        if self.cached:
            self.reader = archive.open("rb")
            self.available = archive.stat().st_size
        else:
            if local_only:
                raise FileNotFoundError(archive)
            archive.parent.mkdir(parents=True, exist_ok=True)
            self.partial.touch(exist_ok=True)
            self.available = self.partial.stat().st_size
            self.reader = self.partial.open("rb")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.reader.close()
        self.chunk.unlink(missing_ok=True)
        self.headers.unlink(missing_ok=True)

    def extend(self):
        if self.remote_size is not None and self.available == self.remote_size:
            return False
        sizes = (self.range_size, min(self.range_size, 4 * 1024 * 1024), min(self.range_size, 256 * 1024))
        for attempt, chunk_size in enumerate(sizes):
            start = self.available
            end = start + chunk_size - 1
            if self.remote_size is not None:
                end = min(end, self.remote_size - 1)
            response = self.request_range(start, end)
            if response:
                size, length = response
                self.remote_size = size
                self.available += length
                self.range_size = min(max(chunk_size, length * 2), RANGE_SIZE)
                return True
            time.sleep(2 ** attempt)
        raise OSError(f"No usable range received for {self.archive.name} at byte {self.available:,}")

    def request_range(self, start, end):
        self.chunk.unlink(missing_ok=True)
        self.headers.unlink(missing_ok=True)
        # Bind the public redirect to the requested offset. Some mirrors
        # otherwise reuse a redirect cached for an earlier byte range.
        requested = end - start + 1
        separator = "&" if "?" in self.url else "?"
        url = f"{self.url}{separator}range_start={start}&range_size={requested}"
        subprocess.run(curl_args() + [
            "--max-time", "65" if requested >= 1024 * 1024 else "32",
            "--max-filesize", str(requested),
            "--range", f"{start}-{end}", "--dump-header", str(self.headers),
            "--output", str(self.chunk), url,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if not self.headers.exists() or not self.chunk.exists():
            return None
        headers = self.headers.read_bytes()
        matches = re.findall(rb"(?im)^content-range:\s*bytes (\d+)-(\d+)/(\d+)", headers)
        if not matches:
            return None
        first, last, total = [int(value) for value in matches[-1]]
        length = self.chunk.stat().st_size
        if first != start or not first <= last <= min(end, total - 1) or not 0 < length <= last - first + 1:
            return None
        if self.remote_size is not None and total != self.remote_size:
            return None
        with self.partial.open("ab") as output, self.chunk.open("rb") as source:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        return total, length

    def read(self, size):
        contents = self.reader.read(size)
        while len(contents) < size and not self.cached and self.extend():
            contents += self.reader.read(size - len(contents))
        return contents

    def finish(self, keep):
        if self.cached:
            return
        if keep and self.remote_size == self.available:
            os.replace(self.partial, self.archive)
        elif not keep:
            self.partial.unlink(missing_ok=True)


def accepted_decision(venue, year, label, archive_group):
    normalized = " ".join(label.casefold().split())
    if NONFINAL_DECISION.search(f"{normalized} {archive_group.replace('_', ' ')}"):
        return False
    if re.match(r"^accept(?:ed)?\b", normalized):
        return True
    # The release uses ICML's presentation track as the decision in several years.
    return venue == "icml" and re.fullmatch(
        rf"icml {year} (?:poster|oral(?: ?poster)?|spotlight(?: ?poster)?)", normalized,
    ) is not None


def release_figures(metadata):
    groups = defaultdict(dict)
    excluded = set()
    accepted = set()
    with metadata.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if str(record.get("figure_number", "")).strip() != "1":
                continue
            venue = str(record.get("venue", "")).lower()
            title = str(record.get("title") or "").strip()
            path = PurePosixPath(record["figure_path"])
            if len(path.parts) < 6 or not title or venue not in VENUES:
                continue
            archive_venue, _, archive_year, decision, forum, filename = path.parts[-6:]
            if not (archive_year.isdecimal() and decision.replace("_", "").isalnum()):
                continue
            label = str(record.get("decision") or "").strip()
            page = record.get("url") or "https://openreview.net/forum?id=" + quote(forum)
            pdf = "https://openreview.net/pdf?id=" + quote(forum)
            tier = next((tier for tier in ("oral", "spotlight") if tier in decision.lower()), "")
            paper = {"venue": venue, "year": int(record["year"]), "title": title,
                     "authors": record.get("authors"), "page": page, "pdf": pdf, "tier": tier,
                     "decision": label}
            identity = paper_key(paper)
            if not accepted_decision(venue, archive_year, label, decision):
                excluded.add(identity)
                continue
            accepted.add(identity)
            archive = f"{archive_venue}_{archive_year}_{decision}.tar.gz"
            groups[archive][(forum, filename)] = (paper, record.get("figure_caption") or "")
    return groups, excluded - accepted


def candidates(metadata):
    return release_figures(metadata)[0]


def read_archive(archive, file, targets, on_result):
    errors = []
    missing = set(targets)
    with tarfile.open(fileobj=file, mode="r|gz") as contents:
        for member in contents:
            key = tuple(PurePosixPath(member.name).parts[-2:])
            if not member.isfile() or key not in missing:
                continue
            missing.remove(key)
            paper, caption = targets[key]
            try:
                if member.size > 80 * 1024 * 1024:
                    raise ValueError("Archived figure is larger than 80 MiB")
                with contents.extractfile(member) as source:
                    data = source.read()
                origin = f"https://huggingface.co/datasets/{DATASET}/blob/{REVISION}/data/{archive}"
                on_result(store_image(paper, data, "diagram-bank", origin, caption))
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                errors.append((paper_key(paper), str(error)))
            if not missing:
                break
    errors.extend((paper_key(targets[key][0]), f"Figure missing from {archive}") for key in missing)
    return errors


def archive_rank(archive, records, directory):
    if (directory / archive).is_file():
        return 0, -len(records)
    if "poster" not in archive and len(records) < 150:
        return 1, len(records)
    return 2, -len(records)


def run(args, catalog):
    CACHE.mkdir(parents=True, exist_ok=True)
    # Prevent two import commands from appending to the same partial archive.
    lock = sqlite3.connect(CACHE / "diagram-bank-import.sqlite3", timeout=0)
    try:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower():
                raise
            raise SystemExit("Another DiagramBank import is already running.") from error
        return import_release(args, catalog)
    finally:
        lock.close()


def import_release(args, catalog):
    base = args.hub.rstrip("/") + f"/datasets/{DATASET}/resolve/{REVISION}"
    metadata = args.metadata or CACHE / "diagram-bank.jsonl"
    if args.metadata is None:
        if args.local_only and not metadata.is_file():
            raise FileNotFoundError("Use --metadata or cache the DiagramBank index before --local-only")
        download_metadata(base + "/data.jsonl?download=true", metadata)
    groups, excluded = release_figures(metadata)
    if not args.plan:
        catalog.exclude_unaccepted_diagram_bank(excluded)
    existing = catalog.existing()
    release = set()
    todo = defaultdict(dict)
    for archive, records in sorted(groups.items()):
        for key, record in records.items():
            identity = paper_key(record[0])
            if identity not in release and identity not in existing:
                todo[archive][key] = record
            release.add(identity)
    print(f"DiagramBank Figure 1: {len(release):,} unique papers; "
          f"{len(release & existing):,} already represented; {len(todo)} pending archives", flush=True)
    if args.plan:
        for archive, records in todo.items():
            print(f"{archive}\t{len(records)}")
        return 0

    results = Queue()
    counts = Counter()

    def collect(archive, records):
        file = args.archives / archive
        print(f"{archive}: {len(records)} pending figures", flush=True)
        stream = ArchiveStream(base + f"/data/{quote(archive)}?download=true", file, args.local_only)
        with stream:
            errors = read_archive(archive, stream, records, results.put)
        if not errors:
            stream.finish(args.keep_archives)
        return errors

    jobs = [(archive, records) for archive, records in todo.items()
            if not args.local_only or (args.archives / archive).is_file()]
    jobs.sort(key=lambda job: archive_rank(*job, args.archives))
    if args.max_archives is not None:
        jobs = jobs[:args.max_archives]
    last_publish = time.monotonic()
    updated = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect, archive, records): archive for archive, records in jobs}
        while futures or not results.empty():
            try:
                catalog.save(results.get(timeout=1))
                updated = True
                counts["added"] += 1
            except Empty:
                pass
            for future in [future for future in futures if future.done()]:
                archive = futures.pop(future)
                try:
                    errors = future.result()
                except (OSError, tarfile.TarError, EOFError) as error:
                    counts["archives_failed"] += 1
                    print(f"{archive}: {error}", flush=True)
                    continue
                for key, error in errors:
                    catalog.record_attempt(key, "diagram-image-error", error)
                counts["image_errors"] += len(errors)
                print(f"{archive}: finished, {len(errors)} image errors", flush=True)
            if updated and time.monotonic() - last_publish >= 5:
                catalog.publish()
                updated = False
                last_publish = time.monotonic()
    catalog.publish()
    print(f"DiagramBank run: {dict(counts)}; {len(todo) - len(jobs)} archives deferred", flush=True)
    return int(bool(counts["archives_failed"] or counts["image_errors"]))
