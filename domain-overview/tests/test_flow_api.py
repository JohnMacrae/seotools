"""/api/flow with GA4 faked at its HTTP boundary (seam 2)."""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import ga4, main  # noqa: E402

# chain of pages -> (users reaching the last page of the chain, {next page: users}, users leaving from it)
TREE = {
    ("/",): (100, {"/bridal": 60, "/book": 20}, 20),
    ("/", "/bridal"): (60, {"/dress/a": 30}, 30),
    ("/", "/book"): (20, {}, 20),
    ("/", "/bridal", "/dress/a"): (30, {}, 30),
}


class FakeGA4:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, version, method, body):
        self.calls.append((version, method, body))
        if self.fail:
            raise ga4.GA4Error(self.fail)
        if method == "runReport":
            return {"rows": [{"dimensionValues": [{"value": "/?utm=1"}], "metricValues": [{"value": "70"}]},
                             {"dimensionValues": [{"value": "/"}], "metricValues": [{"value": "30"}]}]}
        chain = tuple(self._page(s) for s in body["funnel"]["steps"])
        users, nexts, exits = TREE[chain]
        n = len(chain)
        rows = [{"dimensionValues": [{"value": f"{n}. s{n}"}, {"value": "RESERVED_TOTAL"}], "metricValues": [{"value": str(users)}]},
                {"dimensionValues": [{"value": f"{n}. s{n}"}, {"value": "(no next action)"}], "metricValues": [{"value": str(exits)}]},
                {"dimensionValues": [{"value": f"{n}. s{n}"}, {"value": chain[-1]}], "metricValues": [{"value": "5"}]}]
        rows += [{"dimensionValues": [{"value": f"{n}. s{n}"}, {"value": p + "?x=1"}], "metricValues": [{"value": str(u)}]}
                 for p, u in nexts.items()]
        return {"funnelVisualization": {"rows": rows}}

    @staticmethod
    def _page(step):
        expr = step["filterExpression"]
        f = (expr["andGroup"]["expressions"][0] if "andGroup" in expr else expr)["funnelFieldFilter"]
        assert f["fieldName"] == "pagePathPlusQueryString" and f["stringFilter"]["matchType"] == "FULL_REGEXP"
        return re.sub(r"\\(.)", r"\1", f["stringFilter"]["value"][1:-len(r"(\?.*)?$")])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "overview.db")
    monkeypatch.setattr(ga4, "GA4_PROPERTY", "123")
    monkeypatch.setattr(ga4, "KEY_FILE", __file__)
    fake = FakeGA4()
    monkeypatch.setattr(ga4, "_request", fake)
    ga4.CACHE.clear()
    return TestClient(main.app), fake


def labels(flow):
    return [[n["label"] for n in col] for col in flow["columns"]]


def test_organic_flow_with_source_and_as_of(env):
    client, fake = env
    r = client.get("/api/flow?start=2026-10-01&end=2026-10-28")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] == "GA4" and d["traffic"] == "organic" and d["fetched_at"]
    assert d["window"] == {"start": "2026-10-01", "end": "2026-10-28"}
    assert d["flow"]["total"] == 100  # "/" and "/?utm=1" are one arrival page
    assert labels(d["flow"]) == [["/"], ["/bridal", "/book", "Exit"], ["/dress/a", "Exit"], ["Exit"]]
    organic = [c for c in fake.calls if "Organic Search" in repr(c[2])]
    assert len(organic) == len(fake.calls)


def test_all_traffic_does_not_filter_on_channel(env):
    client, fake = env
    assert client.get("/api/flow?start=2026-10-01&end=2026-10-28&traffic=all").status_code == 200
    assert not any("Organic Search" in repr(c[2]) for c in fake.calls)


def test_ga4_error_is_shown_not_empty(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(ga4, "_request", FakeGA4(fail="GA4 API 403: no access"))
    r = client.get("/api/flow?start=2026-10-01&end=2026-10-28")
    assert r.status_code == 502 and "403" in r.json()["detail"]


def test_site_without_ga4_property(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(ga4, "GA4_PROPERTY", "")
    r = client.get("/api/flow?start=2026-10-01&end=2026-10-28")
    assert r.status_code == 503 and "GA4 property" in r.json()["detail"]
