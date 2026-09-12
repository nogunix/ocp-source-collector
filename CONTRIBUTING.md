# Contributing to ocp-source-collector

Thank you for your interest in contributing. This guide covers the basics.

## Getting started

1. Fork the repository and clone your fork.
2. Create a feature branch from `main`.
3. Make your changes, following the conventions below.
4. Open a pull request against `main`.

## Prerequisites

- Bash 4+ and standard coreutils
- Python 3.9+
- [ShellCheck](https://www.shellcheck.net/) (shell linting)
- [Ruff](https://docs.astral.sh/ruff/) (`pip install ruff`, Python linting)
- `pytest` and `pyyaml` (`pip install pytest pyyaml`, for Python tests)

The full pipeline additionally requires `oc`, `jq`, `mksquashfs`, `xz`, and a
registry pull secret, but these are **not** needed for linting or running the
test suite.

## Running checks locally

```bash
# Shell: syntax check + lint (mirrors CI)
shellcheck -S error -e SC1091 scripts/*.sh

# Python: syntax check + lint
ruff check scripts/*.py mcp/*.py

# Tests (no network, no registry needed)
bash tests/test_resolve.sh
bash tests/test_stage.sh
bash tests/test_reclaim.sh
bash tests/test_freshness.sh
bash tests/test_artifact_path.sh
python3 -m pytest tests/ -q
```

All of these run in CI on every push and pull request.

## Code conventions

- **Shell scripts** source `scripts/lib.sh` for argument parsing, logging
  (`log`/`die`), and path resolution. New scripts should do the same.
- **`CASKET_WORK`** must always be derived dynamically (from `lib.sh` or
  `$(dirname ...)` / `os.path.dirname(__file__)`), never hardcoded to a
  specific directory name.
- **Environment variables** with sensible defaults live in `lib.sh`. Do not
  bury new knobs inside individual scripts.
- **Idempotency**: pipeline stages skip work whose output already exists.
  Preserve this property.
- Prefer minimal dependencies. The collection scripts target a single RHEL 9
  host with standard packages.

## Commit messages

Use the `type: description` format. Examples from the history:

```
docs: add repository layout section to READMEs
fix: handle empty CSV in B-operand discover
feat: add submodule expansion to the pipeline
```

Keep the subject line under 72 characters. Use the body for "why", not "what".

## Adding a new OCP minor

Edit `config/minors.txt` (one minor per line). All phases read from this file;
no script changes are needed.

## License

By contributing you agree that your contributions will be licensed under the
[MIT License](LICENSE).
