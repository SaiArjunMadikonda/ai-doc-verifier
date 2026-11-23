# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `[verify-api]`: deterministic API Contract Verifier with verified / contradicted / uncertain verdicts and evidence.
- Static route extraction for FastAPI, Flask, Django and Express (`api-code-path`), with honest uncertainty flags.
- Suggested fixes, optional AI-drafted rewrites (`--ai`), and a documentation health score with trend history.
- `.rst` and `.adoc` documentation support with original line numbers.
- Code example checks (curl, requests/httpx, fetch/axios, raw HTTP) and JSON sample response checks.
- Project checks (environment variables, CLI flags, file/function references, version claims) and the combined `[verify-docs]` / `src/docaudit.py` audit.
- Hand-labeled accuracy evaluation (`evaluation/`) with a precision/recall gate in CI.
- Local web dashboard (`web/server.py`) and demo project (`examples/shop-demo`).
- PR-scoped audits (`--changed-since`), baseline (`--update-baseline`) and ignore rules (`.docaudit.json`), SARIF output (`--sarif`).
- Meeting-notes checks (`src/meeting_notes.py`, `--meeting`): Teams `.vtt` / `.docx` / text transcripts, rule-based decision finder with optional `--ai`, verdicts from the code, action items and draft decision records; dashboard Meeting tab and a made-up demo meeting.
- Infrastructure checks (Dockerfile `EXPOSE`, compose services and ports, Helm values, Terraform variables), anchor-link checks and runbook-command checks (Makefile targets, package scripts, script paths).
- Breaking-change detector between git refs (`src/api_diff.py`, `--compare-to`, `--fail-on-breaking`) that names the affected documentation, and deprecation tracking for endpoints the API marks deprecated.
- Static route readers for Go, Spring (Java/Kotlin), Rails and ASP.NET.

### Fixed
- `post_pr_comment` reports a failed `gh pr comment` instead of returning success; the dashboard no longer scrolls sideways on phone widths.
- Routes declared through string constants or `A + B` path expressions are now read; routes whose path or prefix cannot be determined statically make unmatched claims *uncertain* instead of *contradicted*.
- A parameter type behind an unresolved external `$ref` is reported *uncertain*, never *verified*.
- `docaudit.py`: a named `--spec` / `--code` that cannot be read exits `2`; a run that checked nothing fails `--fail-under`.
- Action: `[verify-api]` / `[verify-docs]` no longer require model settings or a docs repository URL; with docs in a separate repository, `[verify-docs]` checks them against the code repository; `pr-created` is only `true` when a PR was created.
- Dashboard: undocumented operations are listed even when the docs contain no endpoint claims.
- Evaluation: `never_verified` labels catch false confirmations; three cases for the above were added.

## [1.1.2] - 2026-08-26

### Fixed

- Normalize `DOCS_SUBFOLDER` trailing slash and resolve paths with
  `os.path.normpath` before comparing against the current working directory
- Clean up stale semantic indexes when their documentation files are deleted,
  fetching the base branch ref for accurate change detection
- Build git `ls-tree` / `cat-file` / `show` pathspecs relative to the current
  working directory using `git rev-parse --show-prefix`

## [0.1.0] - 2026-08-16

Initial tagged release. The action has been in use since September 2025;
this release captures the current feature set for stable pinning.

### Added

- AI-powered documentation review and update via PR comments
  (`[review-docs]`, `[update-docs]`)
- Spec-vs-code gap analysis via Jira integration (`[review-feature]`)
- Support for Markdown, AsciiDoc, and reStructuredText doc formats
- Same-repo and separate-docs-repo configurations
- Semantic folder indexes for faster file discovery
- Persistent style guidelines via `.code-to-docs/style.md`
- Repository configuration via `.code-to-docs/config.json`
- Interactive review with checkboxes for accepting/rejecting file suggestions
- Fork PR detection with suggested-changes fallback
- Per-file and global reviewer instructions in `[update-docs]` comments
- CI pipeline with ruff linting, formatting, and 60% coverage threshold
