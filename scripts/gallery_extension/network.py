"""Bounded HTTP requests shared by the roster and figure importers."""
import threading
import time
from urllib.parse import urlsplit

import requests

# Keep bulk imports gentle on public proceedings and arXiv's legacy API.
INTERVAL = {"export.arxiv.org": 3.2, "arxiv.org": 1.0, "ar5iv.labs.arxiv.org": 1.0,
            "api.openreview.net": 1.0, "api2.openreview.net": 1.0, "openreview.net": 1.0}
DEFAULT_INTERVAL = 0.5
USER_AGENT = "topconf-paper-figure-gallery local figure importer/1.0"


class Client:
    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._next = {}

    def _session(self):
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
            self._local.session.headers["User-Agent"] = USER_AGENT
        return self._local.session

    def _wait(self, host):
        interval = INTERVAL.get(host, DEFAULT_INTERVAL)
        while True:
            with self._lock:
                now = time.monotonic()
                delay = self._next.get(host, 0) - now
                if delay <= 0:
                    self._next[host] = now + interval
                    return
            time.sleep(delay)

    def get(self, url, max_size=32 * 1024 * 1024, params=None):
        hostname = urlsplit(url).hostname
        self._wait(hostname)
        with self._session().get(url, params=params, stream=True, timeout=(12, 30)) as response:
            if response.status_code == 429:
                delay = response.headers.get("Retry-After", "")
                delay = int(delay) if delay.isdecimal() else 30
                with self._lock:
                    self._next[hostname] = max(self._next.get(hostname, 0), time.monotonic() + delay)
            response.raise_for_status()
            advertised = response.headers.get("Content-Length", "")
            if advertised.isdecimal() and int(advertised) > max_size:
                raise ValueError(f"Resource exceeds the download limit: {url}")
            content = bytearray()
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > max_size:
                    raise ValueError(f"Resource exceeds the download limit: {url}")
            return bytes(content), response.url, response.headers.get("Content-Type", "")
