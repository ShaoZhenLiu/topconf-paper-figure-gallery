"""Local figure storage and export for the gallery."""
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".gallery-cache"
LOCAL = ROOT / "gallery-local"
VENUES = ("iclr", "icml", "neurips", "cvpr", "acl", "aaai", "tmlr")


def title_key(title):
    text = unicodedata.normalize("NFKD", html.unescape(title)).casefold()
    return "".join(char for char in text if char.isalnum())


def paper_key(paper):
    return f"{paper['venue']}:{paper['year']}:{title_key(paper['title'])}"


def author_list(value):
    if isinstance(value, str):
        value = re.split(r"\s*[,;]\s*", value)
    return [author.strip() for author in (value or []) if author.strip()]


def pattern_from_caption(caption):
    text = caption.casefold()
    for pattern, terms in (
        ("architecture", r"\b(architecture|encoder.decoder)\b"),
        ("pipeline", r"\b(pipeline|work.?flow)\b"),
        ("taxonomy", r"\b(taxonomy|benchmark overview)\b"),
        ("framework", r"\b(framework|method overview|system overview)\b"),
        ("conceptual", r"\b(motivation|conceptual illustration)\b"),
        ("comparison", r"\b(comparison|compared to)\b"),
        ("results", r"\b(qualitative results|visual results|generated samples)\b"),
        ("teaser", r"\bteaser\b"),
    ):
        if re.search(terms, text):
            return pattern
    return "untagged"


def atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(contents)
    try:
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.04 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def store_image(paper, image_bytes, source, origin, caption="", arxiv=None):
    """Write the entire image at the gallery's maximum display width."""
    with Image.open(io.BytesIO(image_bytes)) as raw:
        image = ImageOps.exif_transpose(raw)
        if "A" in image.getbands() or "transparency" in image.info:
            foreground = image.convert("RGBA")
            image = Image.new("RGB", foreground.size, "white")
            image.paste(foreground, mask=foreground.getchannel("A"))
        else:
            image = image.convert("RGB")
        if image.width < 20 or image.height < 20:
            raise ValueError("Figure is smaller than 20 pixels")
        if image.width > 1500:
            image.thumbnail((1500, image.height), Image.Resampling.LANCZOS)
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=90, optimize=True, progressive=True)
        width, height = image.size

    key = paper_key(paper)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    figure_id = f"ext-{paper['venue']}{paper['year']}-{digest}"
    image_path = LOCAL / "images" / source / paper["venue"] / f"{figure_id}.jpg"
    atomic_write(image_path, encoded.getvalue())
    row = {
        "id": figure_id,
        "venue": paper["venue"],
        "year": paper["year"],
        "title": paper["title"],
        "authors": author_list(paper.get("authors")),
        "pattern": pattern_from_caption(caption),
        "image": image_path.relative_to(ROOT).as_posix(),
        "paper": paper["page"],
        "pdf_source": paper["pdf"],
        "w": width,
        "h": height,
        "source": source,
        "image_source": origin,
    }
    if arxiv:
        row["arxiv"] = f"https://arxiv.org/abs/{arxiv}"
    if paper.get("tier") in {"oral", "spotlight"}:
        row["tier"] = paper["tier"]
    return key, row


class Catalog:
    def __init__(self):
        LOCAL.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(LOCAL / "catalog.sqlite3", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS figures (
                paper_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                paper_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                detail TEXT NOT NULL
            );
        """)
        self.curated = {
            paper_key(paper)
            for paper in json.loads((ROOT / "data" / "figures.json").read_text(encoding="utf-8"))
        }

    def existing(self):
        return self.curated | {
            key for key, record in self.db.execute("SELECT paper_key, record FROM figures")
            if (ROOT / json.loads(record)["image"]).is_file()
        }

    def save(self, result):
        key, row = result
        with self.db:
            self.db.execute("""
                INSERT INTO figures VALUES (?, ?, ?)
                ON CONFLICT(paper_key) DO UPDATE SET
                    source=excluded.source, record=excluded.record
                WHERE figures.source != 'diagram-bank' OR excluded.source = 'diagram-bank'
            """, (key, row["source"], json.dumps(row, ensure_ascii=False)))
            self.db.execute("DELETE FROM attempts WHERE paper_key=?", (key,))

    def record_attempt(self, key, status, detail):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?, ?)", (key, status, detail))

    def publish(self):
        # Serialize publication with catalog writers so a concurrent export cannot
        # replace a more recent snapshot with stale metadata.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = [
                json.loads(record) for key, record in self.db.execute("SELECT paper_key, record FROM figures")
                if key not in self.curated
            ]
            rows = [row for row in rows if (ROOT / row["image"]).is_file()]
            rows.sort(key=lambda row: (row["venue"], row["year"], row["id"]))
            bundle = "window.GALLERY_ADDITIONS = " + json.dumps(rows, ensure_ascii=True) + ";\n"
            atomic_write(LOCAL / "figures.js", bundle.encode("utf-8"))
        return rows

    def status(self):
        rows = self.publish()
        counts = Counter(row["source"] for row in rows)
        editions = Counter(f"{row['venue'].upper()} {row['year']}" for row in rows)
        attempts = dict(self.db.execute("SELECT status, COUNT(*) FROM attempts GROUP BY status"))
        roster_coverage = {}
        present = self.curated | {paper_key(row) for row in rows}
        for path in sorted((CACHE / "rosters").glob("*.json")):
            roster = json.loads(path.read_bytes())
            if not roster["rows"]:
                continue
            first = roster["rows"][0]
            edition = f"{first['venue'].upper()} {first['year']}"
            roster_coverage[edition] = {
                "with_figure": sum(paper_key(row) in present for row in roster["rows"]),
                "listed": len(roster["rows"]),
            }
        return {"curated": len(self.curated), "added": len(rows), "sources": dict(counts),
                "editions": dict(sorted(editions.items())), "pending_attempts": attempts,
                "roster_coverage": roster_coverage}
