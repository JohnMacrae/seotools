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
