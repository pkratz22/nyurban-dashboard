import sys
from pathlib import Path

import pytest

SCRAPER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRAPER_DIR))

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html():
    """Load a saved HTML page from tests/fixtures by file name."""
    return lambda name: (FIXTURES / name).read_text()


@pytest.fixture
def team_tables(fixture_html):
    from bs4 import BeautifulSoup
    """Top-level tables of the team page, in document order (nested tables excluded)."""
    soup = BeautifulSoup(fixture_html("team_page.html"), "html.parser")
    return [t for t in soup.find_all("table") if not t.find_parent("table")]
