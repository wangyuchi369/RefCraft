"""Extract the nine model marks from Table 1 of the supplied manuscript.

The marks are vector glyphs, not embedded raster images, so each one is rendered
from its exact page rectangle and its row shading is removed by an edge-connected
flood fill. Filling only from the border keeps enclosed light areas opaque, which
matters for marks such as Kimi K3 (a white glyph inside a dark square).

Requires PyMuPDF, NumPy and Pillow:
    python3 -m pip install pymupdf numpy pillow
    python3 scripts/extract_model_marks.py
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "assets" / "RefCraft.pdf"
OUT_DIR = ROOT / "assets" / "brands" / "marks"
RECORD = ROOT / "data" / "model-marks.json"

TABLE_PAGE = 6  # zero-based; Table 1 of the manuscript
MARK_X = (108.6, 119.6)  # between the row edge and the model name at x = 120.23
ZOOM = 40
FINAL = 128
TOLERANCE = 14

# Row order in Table 1, paired with the logo key already used in leaderboard.json.
MODELS = [
    ("GPT-6 Astra", "openai"),
    ("Qwen3.8-Max", "qwen"),
    ("Grok 4.6", "grok"),
    ("Kimi K3", "kimi"),
    ("Claude Fable 5", "claude"),
    ("GLM-5.3", "zai"),
    ("DeepSeek-V4-Pro", "deepseek"),
    ("Seed2.1 Pro", "doubao"),
    ("Gemini 3.1 Pro", "gemini"),
]


def flood_background(rgb, tolerance=TOLERANCE):
    """Return a boolean mask of background pixels reachable from the border."""
    h, w, _ = rgb.shape
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    bg = np.median(border, axis=0)
    close = (np.abs(rgb.astype(np.int16) - bg.astype(np.int16)).max(axis=2) <= tolerance)

    out = np.zeros((h, w), dtype=bool)
    stack = []
    for x in range(w):
        for y in (0, h - 1):
            if close[y, x]:
                stack.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if close[y, x]:
                stack.append((y, x))
    while stack:
        y, x = stack.pop()
        if out[y, x] or not close[y, x]:
            continue
        out[y, x] = True
        if y > 0:
            stack.append((y - 1, x))
        if y + 1 < h:
            stack.append((y + 1, x))
        if x > 0:
            stack.append((y, x - 1))
        if x + 1 < w:
            stack.append((y, x + 1))
    return out, bg


def extract(page, rect, key):
    pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=rect, alpha=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)

    background, bg = flood_background(rgb)
    alpha = np.where(background, 0, 255).astype(np.uint8)

    rows = np.any(alpha > 0, axis=1)
    cols = np.any(alpha > 0, axis=0)
    if not rows.any() or not cols.any():
        raise SystemExit(f"{key}: nothing left after background removal")
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    rgb = rgb[y0:y1 + 1, x0:x1 + 1]
    alpha = alpha[y0:y1 + 1, x0:x1 + 1]

    # Pad to a square so every mark shares one optical size on the page.
    h, w = alpha.shape
    side = max(h, w)
    pad_rgb = np.zeros((side, side, 3), dtype=np.uint8)
    pad_rgb[:] = bg
    pad_a = np.zeros((side, side), dtype=np.uint8)
    oy, ox = (side - h) // 2, (side - w) // 2
    pad_rgb[oy:oy + h, ox:ox + w] = rgb
    pad_a[oy:oy + h, ox:ox + w] = alpha

    # Premultiply before resizing so the removed background cannot bleed a halo.
    af = pad_a.astype(np.float32) / 255.0
    premul = (pad_rgb.astype(np.float32) * af[..., None]).astype(np.uint8)
    img = Image.fromarray(np.dstack([premul, pad_a]), "RGBA").resize(
        (FINAL, FINAL), Image.LANCZOS
    )
    arr = np.asarray(img).astype(np.float32)
    a = np.clip(arr[..., 3:4], 1e-6, 255.0)
    straight = np.clip(arr[..., :3] / (a / 255.0), 0, 255).astype(np.uint8)
    final = Image.fromarray(
        np.dstack([straight, arr[..., 3].astype(np.uint8)]), "RGBA"
    )

    path = OUT_DIR / f"{key}.png"
    final.save(path, optimize=True)
    return path, (side / ZOOM)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(PDF.read_bytes()).hexdigest()
    doc = pymupdf.open(PDF)
    page = doc[TABLE_PAGE]

    record = {
        "source_pdf": str(PDF.relative_to(ROOT)),
        "source_pdf_sha256": digest,
        "source_page": TABLE_PAGE + 1,
        "method": (
            "Direct PyMuPDF rendering of each Table 1 row's mark rectangle at "
            f"{ZOOM}x, row shading removed by an edge-connected flood fill, padded "
            f"square and resized to {FINAL}px. No recoloring or redrawing."
        ),
        "marks": [],
    }

    for name, key in MODELS:
        hits = page.search_for(name)
        if not hits:
            raise SystemExit(f"{name}: not found on page {TABLE_PAGE + 1}")
        row = hits[0]
        rect = pymupdf.Rect(MARK_X[0], row.y0 - 1.4, MARK_X[1], row.y1 + 1.4)
        path, pt = extract(page, rect, key)
        record["marks"].append({
            "model": name,
            "key": key,
            "src": str(path.relative_to(ROOT)),
            "page_rect": [round(v, 2) for v in (rect.x0, rect.y0, rect.x1, rect.y1)],
            "source_size_pt": round(pt, 2),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
        print(f"{key:9s} <- {name:16s} {path.relative_to(ROOT)}")

    RECORD.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {len(record['marks'])} marks and {RECORD.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
