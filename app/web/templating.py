"""The Jinja2 templates every page renders with."""

import functools
import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@functools.cache
def static_url(name: str) -> str:
    """A static file's URL, versioned by its content ("style.css?v=1a2b3c4d") so browsers
    fetch a changed file instead of reusing a cached copy of the old one."""
    digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:8]
    return f"/static/{name}?v={digest}"


templates.env.globals["static_url"] = static_url
