"""Route meeting decisions to documentation owners on GitHub."""

import hashlib
import json
import re
import tempfile
from collections import defaultdict
from pathlib import Path


def owners_file(root):
    path = root / ".code-to-docs" / "meeting-owners.json"
    if not path.exists():
        return {}
    routes = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(routes, dict):
        raise ValueError("meeting-owners.json must map document paths to lists of GitHub owners.")
    for pattern, owners in routes.items():
        parts = pattern.removesuffix("/").split("/")
        if pattern != "*" and (
            any(p in {"", ".", ".."} for p in parts) or re.search(r"[\\:*?\[\]]", pattern)
        ):
            raise ValueError(f"Invalid documentation owner path: {pattern}")
        if not isinstance(owners, list) or not all(
            isinstance(o, str)
            and re.fullmatch(r"@[A-Za-z\d][A-Za-z\d-]*(?:/[A-Za-z\d][A-Za-z\d_-]*)?", o)
            for o in owners
        ):
            raise ValueError(f"Use a list of @user or @organization/team owners for {pattern}.")
    return routes


def owners_for(paths, routes):
    owners = set()
    for path in set(paths) or {""}:
        matches = [p for p in routes if p == path or (p.endswith("/") and path.startswith(p))]
        owners.update(o.lower() for o in routes.get(max(matches, key=len, default="*"), []))
    return sorted(owners)


def release_sentence(d):
    if d.kind == "change":
        return f"Changed `{d.subject}` from `{d.old}` to `{d.new}`."
    if d.kind == "remove":
        return f"Removed `{d.old}`."
    return f"Renamed `{d.old}` to `{d.new}`."


def pages(run, root, endpoint):
    data = json.loads(run(["gh", "api", "--paginate", "--slurp", endpoint], root).stdout)
    return [item for page in data for item in page]


def write_api(run, root, endpoint, method, fields):
    with tempfile.TemporaryDirectory() as tmp:
        body = Path(tmp) / "body.json"
        body.write_text(json.dumps(fields), encoding="utf-8")
        run(["gh", "api", "--method", method, endpoint, "--input", str(body)], root)


def request_reviews(run, root, repo, pull, owners):
    if not owners or pull["state"] != "open":
        return
    number = pull["number"]
    org = repo.split("/")[0].lower()
    seen = {"@" + pull["user"]["login"].lower()}
    seen.update("@" + u["login"].lower() for u in pull.get("requested_reviewers", []))
    seen.update(f"@{org}/{t['slug'].lower()}" for t in pull.get("requested_teams", []))
    for event in pages(run, root, f"repos/{repo}/issues/{number}/timeline?per_page=100"):
        if event.get("event") != "review_requested":
            continue
        if user := event.get("requested_reviewer"):
            seen.add("@" + user["login"].lower())
        if team := event.get("requested_team"):
            seen.add(f"@{org}/{team['slug'].lower()}")
    missing = sorted(set(owners) - seen)
    if missing:
        teams = [o[1:].split("/") for o in missing if "/" in o]
        if any(group != org for group, _ in teams):
            raise ValueError("Review teams must belong to the repository organization.")
        write_api(
            run,
            root,
            f"repos/{repo}/pulls/{number}/requested_reviewers",
            "POST",
            {
                "reviewers": [o[1:] for o in missing if "/" not in o],
                "team_reviewers": [team for _, team in teams],
            },
        )


def conflicts(proposals, corpus, routes, base):
    groups = defaultdict(list)
    for p in proposals:
        d = p.decision
        if p.status == "conflicting decisions":
            groups[(d.kind == "change", d.subject or d.old)].append(d)
    findings = {}
    for (value_change, subject), decisions in groups.items():
        paths = sorted(
            {
                hit.file
                for d in decisions
                for hit in (
                    corpus.find_value_in_docs(subject, d.old)
                    if value_change
                    else corpus.find_in_docs(d.old)
                )
            }
        )
        owners = owners_for(paths, routes)
        key = hashlib.sha256(json.dumps([base, value_change, subject]).encode()).hexdigest()[:24]
        marker = f"<!-- meeting-docs-conflict:{key} -->"
        detail = "\n".join("- " + release_sentence(d) for d in sorted(decisions))
        body = f"{marker}\nMeeting notes disagree about `{subject}` on `{base}`. The proposed changes are:\n\n{detail}"
        if paths:
            body += "\n\nAffected documentation: " + ", ".join(f"`{p}`" for p in paths)
        if owners:
            body += "\n\nDocumentation owners: " + " ".join(owners)
        body += "\n\nCorrect the source notes and reimport them, then close this issue.\n<!-- end-meeting-docs-conflict -->"
        findings[marker] = (f"Resolve conflicting meeting decisions for {subject}", body)
    return findings


def publish_conflicts(run, root, repo, findings):
    if not findings:
        return
    endpoint = f"repos/{repo}/issues"
    existing = {}
    for issue in pages(run, root, endpoint + "?state=all&per_page=100"):
        if "pull_request" in issue:
            continue
        body = issue.get("body") or ""
        for marker in re.findall(r"<!-- meeting-docs-conflict:[0-9a-f]{24} -->", body):
            existing.setdefault(marker, issue)
    for marker, (title, body) in findings.items():
        issue = existing.get(marker)
        if issue is None:
            write_api(run, root, endpoint, "POST", {"title": title, "body": body})
        elif issue["state"] == "open":
            old = issue["body"]
            updated = re.sub(
                re.escape(marker) + r".*?<!-- end-meeting-docs-conflict -->",
                lambda _, replacement=body: replacement,
                old,
                count=1,
                flags=re.S,
            )
            if updated != old:
                write_api(
                    run, root, endpoint + "/" + str(issue["number"]), "PATCH", {"body": updated}
                )
