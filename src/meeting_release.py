"""Draft release notes from approved, merged meeting documentation pull requests."""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlencode

from meeting_review import pages, release_sentence
from meeting_sync import Decision, command
from security_utils import sanitize_output


def release_commits(root, start, end):
    def resolve(ref):
        return command(
            ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], root
        ).stdout.strip()

    end = resolve(end)
    if start:
        start = resolve(start)
        if command(
            ["git", "merge-base", "--is-ancestor", start, end], root, check=False
        ).returncode:
            raise ValueError("The start of the release range must be an ancestor of the end.")
    else:
        # Search full version tags on the release line, excluding the tag on the end commit.
        tags = set(
            command(["git", "tag", "--list", "v*.*.*", "--merged", end], root).stdout.splitlines()
        )
        at_end = command(
            ["git", "tag", "--list", "v*.*.*", "--points-at", end], root
        ).stdout.splitlines()
        if tags.difference(at_end):
            args = ["git", "describe", "--tags", "--abbrev=0", "--match", "v*.*.*"]
            for tag in at_end:
                args.extend(["--exclude", tag])
            start = resolve(command([*args, end], root).stdout.strip())
    selected = [end] if not start else [end, "^" + start]
    return set(command(["git", "rev-list", *selected], root).stdout.splitlines())


def approved(pull, reviews):
    latest = {}
    for review in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r["id"])):
        user = review.get("user") or {}
        state = review["state"]
        if (
            not user.get("login")
            or user.get("type") != "User"
            or not review.get("submitted_at")
            or review["submitted_at"] > pull["merged_at"]
            or user["login"] == pull["user"]["login"]
        ):
            continue
        if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[user["login"].lower()] = (state, review.get("commit_id"))
    return ("APPROVED", pull["head"]["sha"]) in latest.values() and all(
        state != "CHANGES_REQUESTED" for state, _ in latest.values()
    )


def saved_decisions(state):
    if state is None:
        return {}
    with closing(sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return {
            d.key: d
            for (records,) in db.execute("SELECT decisions FROM sources")
            for d in (Decision(*fields) for fields in json.loads(records))
        }


def note_for(pull, key, saved):
    if key in saved:
        return release_sentence(saved[key])
    # New drafts carry the normalized decision. Verify its fields against the branch digest.
    body = (pull.get("body") or "").replace("\r\n", "\n")
    section = re.search(r"^### Release note\n\n([^\n]+)$", body, re.M)
    if section:
        sentence = section[1]
        field = r"`([\w./{}:%$@+-]{1,160})`"
        if m := re.fullmatch(f"Renamed {field} to {field}\\.", sentence):
            d = Decision("rename", m[1], m[2], "")
        elif m := re.fullmatch(f"Changed {field} from {field} to {field}\\.", sentence):
            d = Decision("change", m[2], m[3], m[1])
        elif m := re.fullmatch(f"Removed {field}\\.", sentence):
            d = Decision("remove", m[1], "", "")
        else:
            d = None
        if d and d.key == key:
            return release_sentence(d)
    return "Updated documentation following a meeting decision."


def draft(root, repo, base, commits, saved):
    prefix = f"repos/{repo}/pulls"
    query = urlencode({"base": base, "state": "closed", "per_page": 100})
    entries = []
    for pr in pages(command, root, prefix + "?" + query):
        head = pr["head"]
        branch = re.fullmatch(r"meeting-docs/([0-9a-f]{24})", head["ref"])
        if (
            not branch
            or not pr.get("merged_at")
            or pr["merge_commit_sha"] not in commits
            or pr["base"]["ref"] != base
            or (head.get("repo") or {}).get("full_name", "").lower() != repo.lower()
        ):
            continue
        number = pr["number"]
        if approved(pr, pages(command, root, f"{prefix}/{number}/reviews?per_page=100")):
            entries.append((number, note_for(pr, branch[1], saved)))
    lines = [f"- {note} (#{number})" for number, note in sorted(entries)]
    return (
        "## Meeting documentation updates\n\n"
        + "\n".join(lines or ["No approved meeting documentation changes in this release range."])
        + "\n"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--base", required=True)
    parser.add_argument(
        "--from-ref", help="Exclusive start (default: previous reachable version tag)"
    )
    parser.add_argument("--to-ref", default="HEAD", help="Inclusive end")
    parser.add_argument("--state", type=Path, help="Optional decision database for older PRs")
    parser.add_argument("--output", type=Path, help="Write Markdown to this file (default: stdout)")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo OWNER/REPO is required")
    try:
        commits = release_commits(args.root, args.from_ref, args.to_ref)
        result = draft(args.root, args.repo, args.base, commits, saved_decisions(args.state))
        if args.output:
            args.output.write_text(result, encoding="utf-8")
        else:
            print(result, end="")
    except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(
            "Release draft failed: " + sanitize_output(detail or "Command failed"), file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
