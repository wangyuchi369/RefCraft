> Snapshot of `opensource/docs/reproducibility.md`. The repository references below are source paths, not files distributed with this project website.

# Reproducibility

The benchmark is fixed to **2026-09-18 reference-v2**. Public task IDs follow
the paper's **1–300** numbering, using the frozen `data/task_id_map.csv`.
Query text, image ordering, keypoints, core cases and the corrected reference
scoring groups are preserved. All 3,600 baseline records use the paper IDs;
their score values and leaderboard means are unchanged.
Dataset checksums live in `data/manifest.json`; source
fingerprints and validation records are consolidated in
reproducibility.json (source repository: `reproducibility.json`).

## Checks

Run from the repository root:

```bash
python scripts/validate.py --decode-images
python scripts/reproduce.py
python -m unittest discover -s tests -v

# Optional integration checks; no real model API calls.
python tests/smoke_browser.py
python tests/smoke_evaluation.py
python tests/smoke_inference.py
```

The inference smoke requires OpenCode 1.18.25 and Chromium. It runs a standalone
copy of the inference package against a scripted local endpoint, verifies prompt
and reference-image transport, executes a shell tool call, and collects a real
Chromium screenshot and HTML artifact. The evaluation smoke uses a local fake
judge with real browser evidence. These checks verify execution, not model or
judge quality. No full new-model 300-task run is included. The Docker recipe has
not been built in the packaging environment.

## Comparing results

- All 3,600 frozen records declare OpenCode 1.18.25, conversation-only reference
  attachments, and a 50-turn limit. The manuscript describes 100 turns; the runner
  defaults to 50. Report an explicit change to `--max-turns 100` separately.
- Most records declare a 7200-second wall limit and five infrastructure retries;
  a few use shorter limits or fewer retries. The portable runner defaults to no
  outer retries and does not reproduce the historical platform's recovery policy.
- The frozen scoring implementation uses depth caps and depth weights. It is
  more specific than the manuscript's simplified equal-keypoint description.
  Evaluation details (source repository: `evaluation.md`) give the exact arithmetic.
- The released evaluator preserves the frozen definitions and score arithmetic,
  but automated observation planning and new judgments do not replay historical
  manual adjudication. Matching a CLI version does not guarantee matching outputs.
- Historical results cover 12 models; the manuscript uses a nine-model subset.
  Model identifiers in `results/` are labels, not runtime routing aliases.

Use the same model, judge, reasoning settings, context/output limits, browser,
font environment and retry settings across comparisons. Public endpoints and
credentials are supplied explicitly; no internal service configuration is needed.
