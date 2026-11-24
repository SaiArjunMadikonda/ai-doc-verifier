import json
import subprocess
from types import SimpleNamespace

import pytest

import meeting_release as release
from meeting_review import release_sentence
from meeting_sync import Decision


@pytest.mark.parametrize(
    "d",
    [
        Decision("change", "20", "50", "limit"),
        Decision("remove", "/gone", "", ""),
        Decision("rename", "--old", "--new", ""),
    ],
)
def test_release_wording_is_bound_to_the_reviewed_decision(d):
    text = release_sentence(d)
    pr = {"body": "### Release note\r\n\r\n" + text + "\r\n"}
    assert release.note_for(pr, d.key, {}) == text
    assert release.note_for({"body": ""}, d.key, {d.key: d}) == text
    assert release.note_for(pr, "wrong", {}) != text


def test_only_approved_same_repo_merges_in_range_are_in_draft(tmp_path, monkeypatch):
    d = Decision("remove", "/retired", "", "")

    def pull(n, **override):
        return {
            "number": n,
            "base": {"ref": "main"},
            "head": {
                "ref": "meeting-docs/" + d.key,
                "sha": "final",
                "repo": {"full_name": "org/app"},
            },
            "merge_commit_sha": "merge",
            "merged_at": "2026-01-15T18:00:00Z",
            "user": {"login": "author"},
            **override,
        }

    def review(state="APPROVED", commit_id="final", user="maintainer", **extra):
        return {
            "id": 2,
            "commit_id": commit_id,
            "state": state,
            "user": {"type": "User", "login": user},
            "submitted_at": "2026-01-14T18:00:00Z",
            **extra,
        }

    for_review = [pull(n) for n in range(1, 8)]
    other = [
        pull(10, merged_at=None),
        pull(11, merge_commit_sha="older"),
        pull(12, base={"ref": "other"}),
        pull(13, head={**pull(13)["head"], "repo": {"full_name": "fork/app"}}),
    ]
    reviews = {
        1: [review()],
        2: [],
        3: [review(commit_id="previous")],
        4: [review(), review(state="DISMISSED", id=3)],
        5: [review(), review(state="CHANGES_REQUESTED", user="second")],
        6: [review(user="author")],
        7: [review(submitted_at="2026-01-16T18:00:00Z")],
    }

    def api(cmd, root):
        url = cmd[-1]
        if "/reviews?" in url:
            result = [reviews[int(url.split("/")[-2])]]
        else:
            assert "base=main" in url
            result = [for_review, other]
        return SimpleNamespace(stdout=json.dumps(result))

    monkeypatch.setattr(release, "command", api)
    md = release.draft(tmp_path, "org/app", "main", {"merge"}, {d.key: d})
    assert md.splitlines()[-1] == "- Removed `/retired`. (#1)" and md.count("(#") == 1


def test_real_git_release_range_and_first_release(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    git("commit", "--allow-empty", "-m", "first")
    first = git("rev-parse", "HEAD")
    git("tag", "v1.0.0")
    assert release.release_commits(tmp_path, None, "v1.0.0") == {first}
    git("commit", "--allow-empty", "-m", "second")
    second = git("rev-parse", "HEAD")
    git("tag", "-a", "v1.1.0", "-m", "version")
    assert release.release_commits(tmp_path, None, "v1.1.0") == {second}
    assert release.release_commits(tmp_path, first, second) == {second}
    with pytest.raises(ValueError):
        release.release_commits(tmp_path, second, first)
