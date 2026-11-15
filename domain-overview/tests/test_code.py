"""Mechanical code rules (CODING_STANDARDS.md "One home per rule"): a module's _private names stay in that module.
A helper another module needs is public; tests are exempt."""
import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
MODULES = {p.stem for p in APP.glob("*.py")}


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def test_no_module_uses_another_modules_private_names():
    found = []
    for f in sorted(APP.glob("*.py")):
        tree = ast.parse(f.read_text())
        aliases = {}  # local name -> app module
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level:
                for a in n.names:
                    if a.name in MODULES:
                        aliases[a.asname or a.name] = a.name
                    elif _private(a.name) and n.module != f.stem:
                        found.append(f"{f.name}:{n.lineno} imports {n.module}.{a.name}")
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in aliases
                    and aliases[n.value.id] != f.stem and _private(n.attr)):
                found.append(f"{f.name}:{n.lineno} uses {aliases[n.value.id]}.{n.attr}")
    assert not found, "make these public in their own module:\n" + "\n".join(found)


MOCK = APP / "mock"


def _literal(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _mock_names(call: ast.Call) -> str | None:
    """The mock file a DataForSEO call reads under DFS_MOCK=1, when it is fixed in the source (see dfs.post_path)."""
    fn = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
    kw = {k.arg: k.value for k in call.keywords}
    if fn == "post_path":
        if "mock_name" in kw:
            return _literal(kw["mock_name"])
        path = _literal(call.args[1]) if len(call.args) > 1 else None
        return path.replace("/", "_") if path else None
    if fn == "_post" and len(call.args) > 1:
        return _literal(call.args[1])
    return None


def test_every_dataforseo_call_has_a_mock_file():
    """DFS_MOCK=1 must serve every paid call; a missing file returns None, which callers store as real data."""
    missing = []
    for f in sorted(APP.glob("*.py")):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and (name := _mock_names(n)) and not (MOCK / f"{name}.json").exists():
                missing.append(f"{f.name}:{n.lineno} reads mock/{name}.json")
    assert not missing, "add these mock files:\n" + "\n".join(missing)


STATIC = APP / "static"
HEX = re.compile(r"(?<![\w&-])#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
ALLOWED_HEX = re.compile(r"--[\w-]+\s*:\s*#[0-9a-fA-F]+|var\(--[\w-]+,\s*#[0-9a-fA-F]+\)")


def test_page_colours_come_from_css_variables():
    """CODING_STANDARDS.md (Pages): colours come from the CSS variables so dark mode follows.
    Hex values are allowed only where a variable is defined (`--bad: #d64545`) or as a var() fallback."""
    found = []
    for f in sorted(STATIC.glob("*.html")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            for m in HEX.finditer(ALLOWED_HEX.sub("", line)):
                found.append(f"{f.name}:{i} {m.group()}")
    assert not found, "use a CSS variable (define it in :root and the dark block):\n" + "\n".join(found)
