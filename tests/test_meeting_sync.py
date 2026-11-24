import json
import subprocess
from pathlib import Path

import pytest

import meeting_sync as sync
from meeting_notes import Corpus, parse_text

RENAME = "Ada: We renamed `--format` to `--output-format`."


def test_pending_decision_survives_and_becomes_one_edit(tmp_path):
    db = tmp_path / "state.db"
    source = ("meeting-123", parse_text(RENAME))
    (decision,) = sync.remember(db, [source, ("notes-copy", source[1])])
    before = Corpus({"cli.py": "--format"}, {"guide.md": "Use --format."})
    assert sync.plan([decision], before)[0].status == "pending"
    assert sync.remember(db, []) == [decision]
    assert sync.remember(db, [source]) == [decision]
    after = Corpus({"cli.py": "--output-format"}, {"guide.md": "Use --format.\nFormerly --format."})
    (p,) = sync.plan([decision], after)
    assert p.changes == {"guide.md": {1: ("Use --format.", "Use --output-format.")}}
    assert b"meeting-123" not in db.read_bytes() and b"Ada:" not in db.read_bytes()


def test_revised_notes_and_failed_batch(tmp_path):
    db = tmp_path / "state.db"
    original = sync.remember(db, [("a", parse_text(RENAME))])

    def broken():
        yield "a", []
        raise ValueError("unavailable")

    with pytest.raises(ValueError):
        sync.remember(db, broken())
    assert sync.remember(db, []) == original
    assert sync.remember(db, [("a", [])]) == []


def test_conflicts_values_and_removals():
    corpus = Corpus(
        {"app.py": "limit = 50"},
        {"api.md": "### GET /retired\nDefault limit is 20.\nFor 20 users set the limit to 20."},
    )
    decisions = [
        sync.Decision("remove", "/retired", "", ""),
        sync.Decision("change", "20", "50", "limit"),
        sync.Decision("rename", "--old", "--first", ""),
        sync.Decision("rename", "--old", "--second", ""),
    ]
    assert sync.plan(decisions, corpus)[0].status == "manual review"
    removal, value, *conflicts = sync.plan(decisions, corpus, can_check_removed_routes=True)
    assert removal.changes["api.md"][1][1] == "### Removed GET endpoint `/retired`"
    assert value.changes == {"api.md": {2: ("Default limit is 20.", "Default limit is 50.")}}
    assert all(p.status == "conflicting decisions" and not p.changes for p in conflicts)


def test_graph_cursor_and_decisions_commit_together(tmp_path, monkeypatch):
    state = tmp_path / "state.db"
    cursors = []

    def feed(env, *, last_sync, checkpoint):
        cursors.append(last_sync("organizer:private"))
        yield "notes", parse_text(RENAME)
        checkpoint("organizer:private", env["next"])
        if env.get("fail"):
            raise ValueError("incomplete page")

    monkeypatch.setattr(sync, "graph_notes", feed)
    assert len(sync.remember(state, [], graph_env={"next": "one"})) == 1
    with pytest.raises(ValueError):
        sync.remember(state, [], graph_env={"next": "two", "fail": True})
    sync.remember(state, [], graph_env={"next": "three"})
    assert cursors == [None, "one", "one"]
    assert b"organizer:private" not in state.read_bytes()


def test_full_publish_retries_without_duplicate_or_checkout_changes(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    origin = tmp_path / "origin.git"
    db = tmp_path / "decisions.db"
    notes = tmp_path / "export.txt"
    notes.write_text(RENAME, encoding="utf-8")

    def git(*args):
        return subprocess.check_output(["git", *map(str, args)], cwd=root, text=True).strip()

    git("init", "--bare", origin)
    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("config", "core.autocrlf", "false")
    git("remote", "add", "origin", origin.as_uri())
    (root / "docs").mkdir()
    config = root / ".code-to-docs"
    config.mkdir()
    (config / "meeting-owners.json").write_text('{"docs/": ["@reviewer"]}')
    doc = root / "docs" / "guide.md"
    doc.write_bytes(b"Guide\r\nUse --format.\r\n")
    (root / "cli.py").write_text("option = '--format'\n")
    git("add", ".")
    git("commit", "-m", "before")
    git("push", "origin", "main")
    args = [
        "--root",
        str(root),
        "--state",
        str(db),
        "--repo",
        "example/app",
        "--base",
        "main",
        "--publish",
    ]
    assert sync.main([*args, "--notes", str(notes)]) == 0
    assert not git("ls-remote", "--heads", "origin", "refs/heads/meeting-docs/*")
    (root / "cli.py").write_text("option = '--output-format'\n")
    git("add", "cli.py")
    git("commit", "-m", "ship rename")
    git("push", "origin", "main")

    original = sync.command
    created = []
    attempts = []
    requests = []

    def gh_or_git(cmd, cwd, **kwargs):
        if cmd[0] != "gh":
            return original(cmd, cwd, **kwargs)
        if cmd[1] == "api":
            if "--input" in cmd:
                requests.append(json.loads(Path(cmd[-1]).read_bytes()))
                if len(requests) == 1:
                    raise subprocess.CalledProcessError(1, cmd)
                return subprocess.CompletedProcess(cmd, 0, "{}")
            if cmd[-1].endswith("/timeline?per_page=100"):
                timeline = (
                    [{"event": "review_requested", "requested_reviewer": {"login": "reviewer"}}]
                    if len(requests) > 1
                    else []
                )
                return subprocess.CompletedProcess(cmd, 0, json.dumps([timeline]))
            if cmd[-1].endswith("/pulls/42"):
                return subprocess.CompletedProcess(cmd, 0, json.dumps(created[0]))
            assert "state=all" in cmd and "base=main" in cmd
            return subprocess.CompletedProcess(cmd, 0, json.dumps(created))
        assert "--draft" in cmd
        body = Path(cmd[cmd.index("--body-file") + 1]).read_text(encoding="utf-8")
        assert "guide.md:2" in body and "Ada" not in body and "cli.py:1" in body
        attempts.append(cmd[cmd.index("--head") + 1])
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(1, cmd)
        assert "Renamed `--format` to `--output-format`." in body
        created.append({"number": 42, "state": "open", "user": {"login": "author"}})
        return subprocess.CompletedProcess(cmd, 0, "https://github.com/example/app/pull/42")

    monkeypatch.setattr(sync, "command", gh_or_git)
    assert sync.main(args) == 1  # branch exists, PR creation interrupted
    assert sync.main(args) == 1  # PR exists, reviewer request interrupted
    assert sync.main(args) == 0  # recover using the PR and saved decision
    assert sync.main([*args, "--notes", str(notes)]) == 0
    assert requests == [{"reviewers": ["reviewer"], "team_reviewers": []}] * 2
    assert len(created) == 1 and attempts[0] == attempts[1]
    assert (
        git("--git-dir", origin, "show", attempts[0] + ":docs/guide.md")
        == "Guide\nUse --output-format."
    )
    assert doc.read_bytes() == b"Guide\r\nUse --format.\r\n" and not git("status", "--porcelain")
