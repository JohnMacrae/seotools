import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

ROUTES = ["/", "/compare", "/linking", "/targets", "/mappack"]
SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S | re.I)


@pytest.mark.parametrize("route", ROUTES)
def test_page_scripts_parse(route, tmp_path):
    node = shutil.which("node")
    assert node, "node is required to syntax-check page scripts (no silent skip)"
    r = TestClient(main.app).get(route)
    assert r.status_code == 200
    scripts = [m.group("body") for m in SCRIPT_RE.finditer(r.text)
               if "src=" not in m.group("attrs").lower() and m.group("body").strip()]
    assert scripts, f"no inline script found on {route}"
    for i, body in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(body)
        p = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert p.returncode == 0, f"{route} script {i}: {p.stderr}"


# Guards found by the PR #35 retro: mistakes a reviewer kept catching by hand.
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
PAGES = sorted(STATIC.glob("*.html"))


def _href(page: Path) -> str:
    return "./" if page.stem == "index" else page.stem


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_nav_links_every_page(page):
    nav = re.search(r"<nav[^>]*>(.*?)</nav>", page.read_text(), re.S)
    assert nav, f"{page.name} has no <nav>"
    have = set(re.findall(r'href="([^"]+)"', nav.group(1)))
    missing = {_href(p) for p in PAGES} - have
    assert not missing, f"{page.name} nav is missing {sorted(missing)}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_hardcoded_dates(page):
    """ADR 0002: a number's as-of date comes from the API, never from a literal in the page."""
    dates = re.findall(r"\b20\d\d-[01]\d-[0-3]\d\b", page.read_text())
    assert not dates, f"{page.name} hard-codes {dates}"


def _unused_classes(html: str) -> list[str]:
    css = html[html.find("<style>"):html.find("</style>")]
    rest = html.replace(css, "")
    used = set()
    for m in re.finditer(r'class="([^"]*)"', rest):
        used |= set(re.sub(r"\$\{[^}]*\}", " ", m.group(1)).split())
    used |= set(re.findall(r"[\"'` ]([a-z][\w-]*)[\"'`]", rest))  # names set from JS strings
    for m in re.finditer(r"/\*\s*data classes:([^*]*)\*/", css):  # names that only arrive in API data
        used |= set(m.group(1).split())
    return sorted(set(re.findall(r"\.([a-zA-Z][\w-]*)", css)) - used)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_unused_css(page):
    """Pages start as copies of a sibling; drop the rules this page never uses.
    A class that only comes from API data is declared in a `/* data classes: a b */` comment."""
    unused = _unused_classes(page.read_text())
    assert not unused, f"{page.name} styles unused classes {unused}"
