import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.match import contains_variant, first_sentence, url_has  # noqa: E402


def test_contains_variant():
    assert contains_variant("Shop our Lace Wedding Dress range", "lace wedding dresses")  # plural
    assert contains_variant("dresses for a wedding in lace", "lace wedding dresses") is False  # extra words inside
    assert contains_variant("wedding dresses lace", "lace wedding dresses")  # word order
    assert not contains_variant("lace and wedding dresses", "lace wedding dresses")  # inserted word
    assert not contains_variant("", "x") and not contains_variant("x", "")


def test_first_sentence():
    assert first_sentence("  \nWelcome to Veni. Second one!") == "Welcome to Veni."
    assert first_sentence("Line one\nLine two. Three") == "Line one"
    assert first_sentence("") == ""


def test_url_has():
    assert url_has("https://x.com/collections/bridal/lace", "lace wedding dresses")
    assert url_has("https://x.com/collections/bridal", "wedding dresses")
    assert url_has("https://x.com/collections/bridal/ready-to-wear", "ready to wear wedding dresses")
    assert not url_has("https://x.com/collections/bridal/under-500", "affordable wedding dresses")
    assert not url_has("https://x.com/", "wedding dress shop Colchester")
