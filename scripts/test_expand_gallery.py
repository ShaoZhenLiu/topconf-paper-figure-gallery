"""Regression checks for the local gallery's acquisition paths."""
import io
import json
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pymupdf
from PIL import Image

from gallery_extension import arxiv, catalog, conferences, diagrambank, rosters


def png():
    output = io.BytesIO()
    Image.new("RGB", (180, 90), "blue").save(output, "PNG")
    return output.getvalue()


class FakeClient:
    def __init__(self, sources):
        self.sources = sources

    def get(self, url, *args, **kwargs):
        data, content_type = self.sources[url]
        return data, url, content_type


class ArxivFigureTest(unittest.TestCase):
    def test_numbered_figure_matches_one_not_ten(self):
        url = "https://arxiv.org/html/2501.00001"
        html = b"""
        <figure><img src='/unused.png'><figcaption>Figure 10: wrong</figcaption></figure>
        <figure id='S1.F1'><img src='2501.00001v1/overview.png'><figcaption>Figure 1: overview</figcaption></figure>
        """
        client = FakeClient({
            url: (html, "text/html"),
            "https://arxiv.org/html/2501.00001v1/overview.png": (png(), "image/png"),
        })
        result, page, caption = arxiv.html_figure(client, url)
        self.assertEqual(result, png())
        self.assertEqual(page, url)
        self.assertEqual(caption, "Figure 1: overview")

    def test_multi_panel_keeps_every_image_including_svg_object(self):
        url = "https://arxiv.org/html/2501.00002"
        html = b"""
        <figure id='S1.F1'><div><img src='/panel.png'></div>
        <object type='image/svg+xml' data='/panel.svg'></object>
        <figcaption>Figure 1: two panels</figcaption></figure>
        """
        client = FakeClient({
            url: (html, "text/html"),
            "https://arxiv.org/panel.png": (png(), "image/png"),
            "https://arxiv.org/panel.svg": (b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
        })
        with patch.object(arxiv, "capture_panels", return_value=png()) as renderer:
            arxiv.html_figure(client, url)
        figure = renderer.call_args.args[0]
        self.assertEqual(len(figure.find_all("img")), 2)
        self.assertTrue(figure.find_all("img")[1]["src"].startswith("data:image/svg+xml;base64,"))
        self.assertIsNone(figure.find("figcaption"))


class PdfFallbackTest(unittest.TestCase):
    def test_existing_caption_crop_can_render_from_buffer(self):
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_image(pymupdf.Rect(70, 80, 400, 240), stream=png())
            page.insert_text((70, 260), "Figure 1: A complete example image used to validate page extraction.")
            result = conferences.pdf_figure(document.tobytes())
        with Image.open(io.BytesIO(result)) as image:
            self.assertGreater(image.width, 700)
            self.assertGreater(image.height, 400)


class DiagramBankTest(unittest.TestCase):
    def test_archive_import_reads_only_selected_member(self):
        data = png()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as output:
            for name in ("Figure10.png", "Figure1.png"):
                member = tarfile.TarInfo(f"OpenReview/ICLR/figures/2024/accept_oral/forum/{name}")
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        archive.seek(0)
        results = []
        with patch.object(diagrambank, "store_image", return_value=("paper", {})) as store:
            errors = diagrambank.read_archive("sample.tar.gz", archive, {("forum", "Figure1.png"): ({}, "")}, results.append)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(store.call_args.args[1], data)

    def test_archive_resumes_partially_received_verified_range(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.tar.gz"
            path.with_name(path.name + ".part").write_bytes(b"abc")
            with diagrambank.ArchiveStream("https://example.org/archive", path, False) as stream:
                def transfer(*args, **kwargs):
                    stream.headers.write_bytes(b"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 3-5/6\r\n")
                    stream.chunk.write_bytes(b"de")
                with patch.object(diagrambank.subprocess, "run", side_effect=transfer):
                    self.assertEqual(stream.read(5), b"abcde")
                    self.assertEqual(stream.available, 5)


class CatalogTest(unittest.TestCase):
    def test_export_retries_when_windows_reader_holds_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "figures.js"
            path.write_bytes(b"old")
            replace = catalog.os.replace

            def unlock_after_first_attempt(source, destination):
                if snapshot_replace.call_count == 1:
                    raise PermissionError("snapshot in use")
                replace(source, destination)

            with patch.object(catalog.os, "replace", side_effect=unlock_after_first_attempt) as snapshot_replace:
                catalog.atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"new")
            self.assertEqual(snapshot_replace.call_count, 2)

    def test_reimport_keeps_reviewed_label_for_same_image(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(catalog, "LOCAL", Path(directory)):
            gallery = catalog.Catalog()
            with closing(gallery.db):
                row = {"source": "diagram-bank", "image": "figure.jpg", "pattern": "framework"}
                gallery.save(("paper", {**row, "pattern_source": "visual-review"}))
                gallery.save(("paper", {**row, "pattern": "untagged"}))
                record, = gallery.db.execute("SELECT record FROM figures").fetchone()
            self.assertEqual(json.loads(record)["pattern"], "framework")
            self.assertEqual(json.loads(record)["pattern_source"], "visual-review")


class RosterTest(unittest.TestCase):
    def test_neurips_2025_uses_main_volume(self):
        base = "https://proceedings.neurips.cc/paper_files/paper/2025"
        html = b'<a href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">Main track</a>'
        client = FakeClient({
            base: (b'<a href="/paper_files/paper/2025/vol38-main-conference">Proceedings</a>', "text/html"),
            base + "/vol38-main-conference": (html, "text/html"),
        })
        rows, source = rosters.neurips(client, 2025)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pdf"], base + "/file/abc-Paper-Conference.pdf")


if __name__ == "__main__":
    unittest.main()
