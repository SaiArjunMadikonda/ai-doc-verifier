import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_notes import Corpus
from meeting_review import conflicts, owners_file, owners_for, publish_conflicts, request_reviews
from meeting_sync import Decision, plan


def test_owner_matching_and_empty_configuration(tmp_path):
    assert owners_file(tmp_path) == {}
    folder = tmp_path / ".code-to-docs"
    folder.mkdir()
    file = folder / "meeting-owners.json"
    file.write_text(json.dumps({"*": ["@Default"], "docs/": ["@org/writers"], "docs/x.md": []}))
    routes = owners_file(tmp_path)
    assert owners_for(["docs/x.md"], routes) == []
    assert owners_for(["docs/y.md", "docset/z.md"], routes) == ["@default", "@org/writers"]
    assert owners_for([], routes) == ["@default"]
    file.write_text('{"../escape": []}')
    with pytest.raises(ValueError):
        owners_file(tmp_path)


def test_requests_skip_author_and_previously_requested_reviewers(tmp_path):
    posted = []
    timeline = [{"event": "review_requested", "requested_reviewer": {"login": "Ana"}}]

    def api(cmd, root):
        if "--input" in cmd:
            posted.append(json.loads(Path(cmd[-1]).read_bytes()))
            timeline.append({"event": "review_requested", "requested_team": {"slug": "tech-docs"}})
        else:
            return SimpleNamespace(stdout=json.dumps([[], timeline]))

    pull = {"number": 3, "state": "open", "user": {"login": "Author"}}
    for _ in range(2):
        request_reviews(api, tmp_path, "org/repo", pull, ["@author", "@ana", "@org/tech-docs"])
    assert posted == [{"reviewers": [], "team_reviewers": ["tech-docs"]}]


def test_conflict_issue_updates_once_preserves_human_notes_and_respects_closure(tmp_path):
    decisions = [Decision("rename", "--old", flag, "") for flag in ["--one", "--two"]]
    corpus = Corpus({}, {"docs/api.md": "Use --old."})

    def findings():
        return conflicts(plan(decisions, corpus), corpus, {"docs/": ["@owner"]}, "main")

    issues, writes = [], []

    def api(cmd, root):
        if "--input" not in cmd:
            return SimpleNamespace(stdout=json.dumps([issues]))
        payload = json.loads(Path(cmd[-1]).read_bytes())
        writes.append(cmd[cmd.index("--method") + 1])
        if not issues:
            issues.append({"number": 4, "state": "open", **payload})
        else:
            issues[0].update(payload)

    for _ in range(2):
        publish_conflicts(api, tmp_path, "org/app", findings())
    assert "@owner" in issues[0]["body"] and "docs/api.md" in issues[0]["body"]
    issues[0]["body"] += "\nHuman note"
    decisions.append(Decision("remove", "--old", "", ""))
    publish_conflicts(api, tmp_path, "org/app", findings())
    assert "Removed `--old`" in issues[0]["body"] and issues[0]["body"].endswith("Human note")
    issues[0]["state"] = "closed"
    decisions.pop()
    publish_conflicts(api, tmp_path, "org/app", findings())
    assert writes == ["POST", "PATCH"]
