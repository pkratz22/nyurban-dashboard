# NYUrban Basketball Dashboard

Scrapes your team's stats from nyurban.com and displays them in a local dashboard.

## Setup

```bash
cd scraper
pip install -r requirements.txt
```

## Tests

After installing the scraper dependencies, run the offline unit tests from the repository root:

```bash
python3 -m unittest discover -s scraper/tests -v
```

## Run

**Step 1 — Scrape the data:**
```bash
cd scraper
python scraper.py
```

**Step 2 — Start the dashboard:**
```bash
cd web
python serve.py
```

Then open http://localhost:8080 in your browser.

## What it scrapes
- Team name, division, record
- Game schedule & results
- Player scoring stats (names are stripped from published data)
- Division standings
- Division leaders
