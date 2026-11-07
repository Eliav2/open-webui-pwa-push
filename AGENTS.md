# AGENTS.md

Rules for anyone (human or agent) changing this repo.

- **No magic numbers or strings.** A literal compared with `==`, `<`, `in`, `switch`... gets a name (a constant, or a frozen object of the values). Allowed without a name: `0`, `1`, `-1` and `""`. CI runs `python3 scripts/magic_baseline.py` (Python, ruff PLR2004, baseline in `scripts/magic-baseline.json`) and `scripts/lint-js/run.sh` (JS, ESLint, baseline in `scripts/lint-js/eslint-suppressions.json`); both fail only on hits the baseline does not hold.
- **Tests first.** A fix comes with a test that fails without it: `tests/` (the function, pytest), `web/*.test.js` (the service worker and page script, `node --test`), `e2e/` (real Chrome, real push service).
- **The served files are the product.** `web/oc-sw.js` and `web/oc-push.js` are served as they are, with only their placeholders replaced. Keep comments accurate: they are the reference for what each path does.
- **Docs follow the code.** A behaviour change updates `README.md` (and its screenshot, `docs/screenshots.py`) in the same PR.
