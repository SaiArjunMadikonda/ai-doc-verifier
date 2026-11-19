"""Behaviour of entrypoint.sh, run for real with stubbed git/python."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "entrypoint.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run_entrypoint(tmp_path, env, python_body="exit 0"):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    for name, body in {"git": "exit 0", "python": python_body}.items():
        path = bindir / name
        path.write_text(f"#!/bin/bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "github_output"
    out.write_text("")
    full_env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
        "GH_TOKEN": "t",
        **env,
    }
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT)], env=full_env, capture_output=True, text=True, cwd=tmp_path
    )
    return proc, out.read_text()


def test_verify_commands_need_no_model_settings(tmp_path):
    for body in ("[verify-docs]", "please run [VERIFY-API]"):
        proc, outputs = run_entrypoint(
            tmp_path / body[1:5], {"COMMENT_BODY": body, "DOCS_SUBFOLDER": "docs"}
        )
        assert proc.returncode == 0, proc.stdout
        assert "status=success" in outputs and "pr-created=false" in outputs


def test_other_commands_still_require_model_settings(tmp_path):
    proc, _ = run_entrypoint(tmp_path, {"COMMENT_BODY": "[review-docs]", "DOCS_SUBFOLDER": "docs"})
    assert proc.returncode == 1 and "model-api-base input is required" in proc.stdout


def test_docs_location_is_still_required(tmp_path):
    proc, _ = run_entrypoint(tmp_path, {"COMMENT_BODY": "[verify-docs]"})
    assert proc.returncode == 1 and "docs-repo-url input is required" in proc.stdout


def test_pr_created_reflects_what_happened(tmp_path):
    env = {
        "COMMENT_BODY": "[update-docs]",
        "MODEL_API_BASE": "x",
        "MODEL_NAME": "m",
        "DOCS_REPO_URL": "u",
    }
    proc, outputs = run_entrypoint(tmp_path / "none", env)
    assert proc.returncode == 0 and "pr-created=false" in outputs
    body = 'echo "pr-created=true" >> "$CTD_OUTPUT_FILE"'
    proc, outputs = run_entrypoint(tmp_path / "made", env, body)
    assert proc.returncode == 0 and "pr-created=true" in outputs


def test_failed_run_reports_failure(tmp_path):
    env = {"COMMENT_BODY": "[verify-docs]", "DOCS_SUBFOLDER": "docs"}
    proc, outputs = run_entrypoint(tmp_path, env, "exit 3")
    assert proc.returncode == 1 and "status=failed" in outputs and "pr-created=false" in outputs
