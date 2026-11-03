# Project website

This directory is the complete static website. Open `index.html` locally, or
deploy it with GitHub Pages: **Deploy from a branch → main → /docs**.
No server or frontend build is required.

Edit `index.html` and the CSS for the page, and `data/*.json` for links and content.
After editing JSON, run from the repository root:

```bash
python3 docs/scripts/build_data.py
```

To create a single offline HTML file (requires `beautifulsoup4`):

```bash
python3 docs/scripts/build_standalone.py
```

This writes `docs/RefCraft.html`. Benchmark code and data are in the repository
root; the `data/` inside this directory is only for the website.
