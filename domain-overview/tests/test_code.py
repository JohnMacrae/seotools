"""Mechanical code rules (CODING_STANDARDS.md "One home per rule"): a module's _private names stay in that module.
A helper another module needs is public; tests are exempt."""
import ast
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
