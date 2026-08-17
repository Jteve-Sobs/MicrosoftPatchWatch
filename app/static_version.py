"""Cache-busting for /static assets. StaticFiles serves them with no
cache-busting by default, so a browser that already cached app.js/style.css
can keep running the old version indefinitely after a deploy — confusing
when a JS fix silently does nothing because the browser never re-fetched it.
Appending the file's own mtime as a query string forces a re-fetch exactly
when the file actually changed, no version bump/build step required.
"""

from __future__ import annotations

import os


def static_version(url_path: str) -> str:
    """url_path is the request path (e.g. "/static/js/app.js"), which maps
    1:1 onto app/static/... on disk per the StaticFiles mount in main.py."""
    disk_path = os.path.join("app", url_path.lstrip("/"))
    try:
        mtime = int(os.path.getmtime(disk_path))
    except OSError:
        mtime = 0
    return f"{url_path}?v={mtime}"
