# RefCraft project website

An English-language academic project site based on the supplied **ICLR27_RefCraft.pdf** and the released benchmark. The manuscript, data, scoring code, and recorded results were treated as read-only.

The main page follows the paper identity with Abstract, Benchmark & evaluation, Results, Interactive examples, and BibTeX, in that reading order. The abstract is set in an 880px centered measure rather than the full reading column. Results contains the nine-model leaderboard, a capability breakdown, and a single agreement block; alignment is not a separate numbered section. Benchmark & evaluation leads with the paper’s full Figure 1 overview — domain composition, the reference-guided task example, and the score-versus-tokens plot — above a four-item dataset stat row; the original Figure 2 method diagram remains available on demand. The headline-findings strip, the reference/revision ablation panel, the two-card validation layout with its aspect-breakdown table, the per-example scores, and the expandable reproducibility notes are all omitted from the page by request.

The homepage uses a paper-inspired pale-blue masthead with a large left-aligned RefCraft title, the paper’s original transparent mascot, an enlarged reference-composition illustration, and numbered research sections. Results and Interactive examples sit on tinted full-width bands, so the page reads as alternating chapters rather than one continuous surface; white result, validation, and dataset surfaces keep blue accents focused. Larger text, a local DM Sans Medium weight, and tighter spacing improve reading density. The navigation includes the official Qwen mark; resource buttons use official GitHub and Hugging Face marks. The complete paper title remains visible beneath the wordmark. Scoring equations use native MathML and a local DejaVu Math font; no external math renderer is required. Original generated artifacts retain their own designs.

### Motion

Section headings and sub-headings are set in title case. Section blocks rise into view on scroll, the dataset figures count up, capability bars grow to their value, the masthead composition assembles itself, Pareto points fade in with their chart, leaderboard bars ease to new lengths when sorted, and the header gains a shadow once the page leaves the masthead. Cards, gallery tiles, and ablation rows lift on hover. All of it is governed by one self-contained inline script in `index.html`’s `<head>` plus a `@media(prefers-reduced-motion:no-preference)` block at the end of `visual.css`. Nothing depends on `app.js`: without `IntersectionObserver`, under reduced-motion, or after a script error, the `js-reveal` class is never applied (or is removed by a 4-second safety timer) and every element renders in its final state. A warm accent (`--accent-warm`) marks the weakest value in a chart — the only non-blue hue, and it is data-driven rather than decorative.

## Preview

Open **`RefCraft.html`** for the self-contained offline version: it includes images, fonts, results, and citation. It can be moved to a different folder or machine without its asset directory. `index.html` is the editable multi-file source entry point, with assets beside it. The arXiv button currently opens the arXiv homepage and is labeled “coming soon” until the paper-specific URL is available. The supplied manuscript remains in `assets/RefCraft.pdf` in the multi-file package.

No npm install, API key, server, or external CDN is required. The Pareto plot and leaderboard work through `file://`. Preserved example HTML files also remain usable separately. Share the local `RefCraft.html` file, rather than a remote preview URL.

## Update links and content

- Add the future HTTPS GitHub and Hugging Face URLs to `data/config.json`. Until supplied, these resources display **Coming soon** without a fake destination.
- Author links are configured in `data/authors.json`. Nine personal homepages are identified, including the user-supplied Shuai Bai and Tiancheng Gu pages. The user-supplied Scholar profile is provisionally assigned to Hang Zhang and remains labeled for author-mapping review because the profile could not be fetched from this machine. The editable HTML also contains the initial author links.
- Update preserved case descriptions and artifact-matched scores in `data/examples.json`; keep exact task briefs and recorded output provenance intact. The per-example scores are no longer displayed on the page, but they stay in the data and still gate the build: Every score includes the displayed HTML’s SHA-256. The build rejects mismatched artifact hashes. `data/example-scores.csv` provides exact values and source labels for all four examples.
- `data/leaderboard.json` contains only the nine models in the supplied paper, Table 1. Scores are reported out of 100; `tokens` is in millions per task. The downloadable CSV explicitly names this column `tokens_millions`.
- The interactive Pareto plot was retired in v20: panel (c) of the teaser figure now carries it. `pareto.js` and `pareto.css` stay in the repository but are no longer linked from `index.html` or packaged; re-adding the link, script, and a `<div id="pareto-chart">` restores it, and `leaderboard.js` already calls it through optional chaining.
- Edit the page layout and author block in `index.html`, the base layout in `styles.css` and current visual theme in `visual.css`, and interactions in `app.js`. The interactive SVG plot is implemented in `pareto.js` and `pareto.css`; `leaderboard.js` and `leaderboard.css` implement table views, sorting, search, and model details.
- After changing a JSON data file, rebuild the small browser bundle:

```bash
python3 scripts/build_data.py
```

The checked-in `data/site-data.js` avoids fetch restrictions when opening the site directly from disk. The bibliography download is `data/refcraft.bib`; keep it aligned with the visible BibTeX block if publication metadata changes.

Regenerate the self-contained HTML after edits with `python3 scripts/build_standalone.py` (requires `beautifulsoup4` for packaging only). This does not call a service or publish anything.

## Package or deploy

```bash
python3 scripts/package_site.py
```

This creates `dist/` and `refcraft-project-site.zip`. The package contains the working site, its editable JSON/configuration, local paper, fonts with their OFL licenses, the selected case images, and source credits. Research notes, browser screenshots, absolute-path provenance, unused paper figures, and internal repository material are excluded.

The contents of `dist/` can be served by any static host, including GitHub Pages. Use the folder contents as the document root. Relative URLs support project subpaths. Add the final site URL to Open Graph metadata and a canonical link when a public address is chosen. No external publication was performed. For review on remote machines, share the HTML file path rather than a server URL.

## Content provenance

- Identity, authors, affiliations, abstract, and nine-model results: user-supplied PDF, checked against the manuscript tables where applicable.
- Public task IDs use the released 1–300 numbering. Four preserved example tasks: **280**, **35**, **125**, and **273**. Tasks 125 and 273 were added from `project-site-handoff/nonpaper-examples-20261019/`, which supplied byte-identical copies of the frozen GPT-6 Astra artifacts with their reference frames, scores, attribution, and provenance; `build_data.py` re-verified each `htmlSha256` against the embedded bytes and each Overall against `min(B, C, R)`. Tasks 96 and 163 were removed because both appear in the manuscript (Task 96 in Figure 4a, Task 163 in §A.7 and Figure 9); 125 and 273 appear in neither the text nor the figure text; their frozen artifacts stay in `demos/` and their scores stay in `data/examples.json` history, but `scripts/package_site.py` derives the shipped demo and image set from `data/examples.json`, so neither is packaged.
- The nine model marks in the leaderboard and the Pareto plot are rendered from Table 1 of the manuscript by `scripts/extract_model_marks.py` into `assets/brands/marks/`, recorded with page rectangles and hashes in `data/model-marks.json`, and embedded as data URIs in the browser bundle so the plot's SVG and the standalone file need no separate request. `assets/brands/qwen.png` remains the official navigation mark and is unrelated to the extracted set.
- Task 280 is a separately recorded OpenCode rerun. Its 79.56989247311827 task score is tied to the displayed checkpoint’s exact hash in the recorded run’s `analysis/harness-cells.json`; B = 79.56989247311827, C = R = 100. It is labeled as a rerun and excluded from the frozen leaderboard.
- The three frozen example HTML hashes match the original score rows exactly: Task 96 scores 97.83 (B = 97.83, C = R = 100); Task 35 scores 60.44 (B = 60.44, C = 100, R = 66.66666666666667); Task 163 scores 0 (B = 64.47, C = 100, R = 0). No scores were rerun or altered. Local-only `review/v10/score-provenance.json` records source hashes and matched rows.
- The full paper is an unchanged copy of the attachment. The site does not imply conference acceptance or invent an arXiv identifier.
- The mascot is the native image and alpha mask embedded in Figure 1(a), extracted without resampling or recoloring. The domain sunburst is a lossless crop of the original Figure 1(a) panel, including its labels and reference-distribution bars. Extraction records are in local-only `review/v7/mascot-source.json` and `review/v7/domain-wheel-source.json`.
- The optional Figure 2 construction diagram is rendered directly from the supplied PDF; its crop coordinates and source hash are recorded in local-only `data/paper-figures.json`.
- Human validation uses the supplied PDF’s §5.3.3: 100 artifacts, 10 web-development experts, task-level Pearson r = 0.747, and all-keypoint weighted Cohen’s κ = 0.64. The expandable aspect table follows Table 3.
- Arena alignment distinguishes sources: the PDF reports Spearman ρ = 0.89 without specifying its matched model set; the archived September 24, 2026 comparison of seven shared models recomputes to ρ = 0.786. The page labels the first value as paper-reported and presents the archive result in a visible source note. `data/arena-alignment.csv` is an exact copy of the frozen comparison CSV. Detailed source reconciliation remains in local-only `research/validation-content-v8.md`.
- Model-level Overall averages `min(B, C, R)` **per task**; it cannot be recomputed by taking the minimum of the displayed model averages.
- `credits.html` documents image and font attribution. Reference screenshots retain their source terms; screenshots were resized and encoded as WebP for this site.

Detailed local-only audit notes are in `research/`; image provenance is in `data/assets-provenance.json`. Neither is included in the deployment package.

## Design references

The redesign follows the user's references: [BenchCAD](https://benchcad.com/), [InstructAvatar](https://wangyuchi369.github.io/InstructAvatar/), [PixArt-Σ](https://pixart-alpha.github.io/PixArt-sigma-project/), and [SynCamMaster](https://jianhongbai.github.io/SynCamMaster/). It adopts their emphasis on complete paper identity, readable scientific text, clear results, and interactive research examples. A later refinement also studies [BlenderWorld](https://blenderworld.github.io/#leaderboard): concise findings, score bars on a fixed scale, and switching between scores and resources. RefCraft uses a distinct pale-blue research layout, left-aligned masthead, original reference-composition illustration, numbered sections, and sans-serif statistics. Following that study, the homepage carries two of BlenderWorld’s devices in RefCraft’s own palette: fixed-scale score bars for the capability breakdown, and alternating tinted section bands. Its headline-findings strip was built and then removed by request. Section titles stay label-style rather than adopting BlenderWorld’s editorial headlines, and category-specific scores and model groupings are not imported. The implementation and layout are original. `research/redesign-references-a.md` and `research/redesign-references-b.md` document the actual source and visual research, including resource-loading limitations.

## Preserved interactive examples

The examples are present on the homepage as section 04 and in the navigation. `fragments/interactive-examples.html` retains the original gallery section and demo dialog as a reference copy; the live markup is in `index.html`, with the gallery before `#citation` and the dialog before `#image-dialog`. The example initializer activates only when its DOM is present, and the standalone builder embeds demo payloads and reference images only while the viewer is present — removing the section again therefore shrinks `RefCraft.html` without further edits. Rebuild the package and standalone HTML after changing the section.

`demos/task-035.html`, `task-096.html`, and `task-163.html` are byte-identical copies of frozen GPT-6 Astra artifacts. `task-280.html` is a byte-identical copy of the recorded rerun's final checkpoint. The local audit manifest, `data/demo-manifest.json`, records their SHA-256 hashes and original paths; it is excluded from the public package.

`build_data.py` embeds their exact bytes as base64 in the browser data bundle. The viewer decodes them into isolated `srcdoc` frames with `sandbox="allow-scripts allow-forms"`; the form permission permits the artifacts' local JavaScript submit handlers. The demos do not need external resources. They cannot access the parent document. The viewer injects a small Escape-key message bridge at runtime so an expanded example can be closed while the frame has focus; the downloaded HTML remains unchanged. Task 96's saved stories work for the current demo session; persistent localStorage is unavailable in this frame. Resetting returns an example to its original state.

When the gallery is restored, each example has a stable fragment link, such as `RefCraft.html#example-35`, which opens the selected task directly. Desktop (1440×900), mobile (390×844), fit, and 100% views operate on the same frame. Expanding promotes the existing dialog to a modal without moving or recreating the frame, preserving demo state. Downloading an example returns its original HTML bytes. The Pareto frontier is computed from the nine observed token/Overall pairs and currently contains Qwen3.8-Max and GPT-6 Astra. Plot selection and leaderboard selection are linked.

## Verification

Chromium review covers direct local HTML loading, actual interactions inside all four demos, plot and table controls, expanded-view state preservation, viewport controls, image zoom, downloads, and narrow-screen layout. The final standalone file is checked with networking blocked. Research QA receipts are kept in `research/`; current structure, motion, and offline verification is in `review/v19/`; resource-link verification is in `review/v16/`; author-link verification is in `review/v15/`, and earlier layout screenshots are in `review/v14/`. The current standalone review file is `RefCraft-v19.html`. The v19 revision reorders the page to Abstract → Benchmark → Results → Examples → BibTeX, restores the interactive examples, adds the capability breakdown to Results, sets section sub-headings as uppercase labels, widens the abstract to an 880px centered measure, leads the dataset statistics with a stat row and stacks Figure 1(b)'s task example beneath them, replaces the two validation cards with one three-number agreement block, removes the per-example scores, adds the nine model marks extracted from the manuscript's Table 1 to the leaderboard and the Pareto plot, drops the two example tasks that appear in the manuscript, places Benchmark and Examples on tinted bands, and introduces the reduced-motion-aware animation layer; `review/v19/qa.md` records the measured checks, the paper source of every added number, and the degradation paths. The v18 revision centers the Abstract heading above its text, with desktop and mobile layout review in `review/v18/`. The v17 review covers the four-section order, removal of the specified content, resource spacing, remaining interactions, and preservation of source files. The v16 standalone review file is `RefCraft-v16.html`. The v15 revision updates the three user-supplied author links; its standalone review file is `RefCraft-v15.html`. The v14 revision uses the official Qwen icon in place of the navigation R mark, directly before RefCraft, and removes the separate Qwen label. Its standalone review file is `RefCraft-v14.html`. The v13 revision slightly increases outer page margins using an 1180px maximum content width, with typography and internal spacing retained from v12. Its standalone review file is `RefCraft-v13.html`. The v12 revision balances regular-weight body text with stronger headings, restores comfortable paragraph and card spacing, and preserves the 1240px page width. Its standalone review file is `RefCraft-v12.html`. The v11 review verifies 19px/600 reading text, the restored 1240px content width, reduced internal spacing, header alignment, readable chart labels, and offline example interactions. Header asset URLs carry a revision query to avoid mixing cached CSS with new markup. A copy of the standalone file is delivered as `RefCraft-v11.html` for a fresh preview. The v10 review checks all four displayed task scores against exact HTML hashes, author link types, official icons, larger typography, and responsive offline layout and interactions. Earlier verification is retained in `review/v9/`. Earlier review snapshots are retained under `review/`. The v9 check focuses on the two-step scoring formula: native MathML with a bundled OpenType MATH font, readable fractions and summation limits, and responsive offline rendering. The v8 validation section retains its source and responsive verification records. Earlier mascot motion, paper-figure controls, plot/table interactions, direct example fragments, and iframe-focused Escape retain their verification records.

## v21

The interactive examples are back to four tasks, none of which appear in the manuscript:
**280** (air quality, recorded rerun), **35** (bilingual seismic observatory), **125**
(Two Rivers clothing collection) and **273** (Silent Radio mini-game arcade). The two new
artifacts, their reference frames, scores, attribution and provenance came from the
handoff directory and were merged without overwriting anything already on the page; the
per-example scores stay hidden, and `credits.html` gained a case for each new task.
`app.js` now falls back to an empty step list when a task has no scripted "Try it yourself"
entries, so adding a further example cannot break its panel again.

Headings are title case, not upper case: `Benchmark & Evaluation`, `Leaderboard & Analysis`,
`Interactive Examples`, `Model Comparison`, `Capability Breakdown`,
`Agreement with Human Judgment`, `Dataset Statistics`, `Evaluating the Complete Task`,
and the two capability captions.

Review receipts for this revision are in `review/v21/`; `review/v20/qa.md` covers the
teaser-led benchmark section, the retired plot and the press-feedback layer.
