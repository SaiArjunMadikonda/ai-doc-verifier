import json
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

import meeting_sources as ms


def test_scoped_collection_paginates_and_skips_unsupported_items(monkeypatch):
    graph = ms.GRAPH
    meeting = graph + "/users/u/onlineMeetings/m%2F1/transcripts"
    drive = graph + "/drives/d/items"
    responses = {
        meeting: {"value": [], "@odata.nextLink": meeting + "?page=2"},
        meeting + "?page=2": {"value": [{"id": "t/1"}]},
        meeting + "/t%2F1/content": b"00:00:01.000 We renamed --old to --new.\n",
        drive + "/f/children": {
            "value": [
                {"name": "export.txt", "id": "n", "file": {}},
                {"name": "meeting.mp4", "id": "v", "file": {}},
                {"name": "another folder", "id": "child", "folder": {}},
            ]
        },
        drive + "/n/content": b"Bob: We removed /retired.",
    }

    def request(url, *, token, accept="application/json"):
        assert token == "fixture"
        if "/t%2F1/content" in url:
            assert accept == "application/vnd.microsoft.graph.transcript+text"
        response = responses.pop(url)
        return response if isinstance(response, bytes) else json.dumps(response).encode()

    monkeypatch.setattr(ms, "_access_token", lambda env: "fixture")
    monkeypatch.setattr(ms, "_request", request)
    notes = dict(
        ms.graph_notes(
            {
                "TEAMS_ORGANIZER_ID": "u",
                "TEAMS_MEETING_ID": "m/1",
                "ONEDRIVE_DRIVE_ID": "d",
                "ONEDRIVE_FOLDER_ID": "f",
            }
        )
    )
    assert not responses
    assert "--old to --new" in notes[meeting + "/t%2F1"][0].text
    assert notes[drive + "/n"][0].speaker == "Bob"


def test_external_page_and_redirects_protect_authorization():
    with pytest.raises(ValueError, match="Unexpected"):
        ms._request("https://unrelated.example/page", token="fixture")
    req = Request(
        ms.GRAPH + "/drives/d/items/n/content", headers={"Authorization": "Bearer fixture"}
    )
    target = ms._Redirect().redirect_request(
        req, None, 302, "", {}, "https://download.example/content"
    )
    assert target.get_header("Authorization") is None
    with pytest.raises(ValueError, match="redirect"):
        ms._Redirect().redirect_request(req, None, 302, "", {}, "http://download.example/content")


def test_incomplete_configuration_does_not_make_a_request():
    with pytest.raises(ValueError, match="Configure"):
        list(ms.graph_notes({"TEAMS_ORGANIZER_ID": "u"}))


def test_application_token_is_requested_from_tenant(monkeypatch):
    tenant = "12345678-1234-1234-1234-123456789abc"

    def request(url, *, data):
        assert url == f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        assert parse_qs(data.decode()) == {
            "client_id": ["app"],
            "client_secret": ["test & key"],
            "grant_type": ["client_credentials"],
            "scope": ["https://graph.microsoft.com/.default"],
        }
        return b'{"access_token":"test-token"}'

    monkeypatch.setattr(ms, "_request", request)
    assert (
        ms._access_token(
            {"MS_TENANT_ID": tenant, "MS_CLIENT_ID": "app", "MS_CLIENT_SECRET": "test & key"}
        )
        == "test-token"
    )


def test_loop_uses_graph_html_conversion_and_preserves_bullet_boundaries(monkeypatch):
    monkeypatch.setattr(ms, "_access_token", lambda env: "token")
    monkeypatch.setattr(
        ms, "_pages", lambda url, token: [{"name": "Meeting.loop", "file": {}, "id": "n"}]
    )

    def content(url, *, token, accept):
        assert url == ms.GRAPH + "/drives/d/items/n/content?format=html" and accept == "text/html"
        return b"<html><head><title>Private title</title></head><body><ul><li>We renamed <b>--old</b> to --new.</li><li>We removed /old.</li></ul><script>do not import</script></body></html>"

    monkeypatch.setattr(ms, "_request", content)
    [(source, remarks)] = ms.graph_notes({"ONEDRIVE_DRIVE_ID": "d", "ONEDRIVE_FOLDER_ID": "f"})
    assert source.endswith("/items/n")
    assert [r.text for r in remarks] == ["We renamed --old to --new.", "We removed /old."]


def test_tenant_rotates_attempts_and_resumes_successful_organizers(monkeypatch):
    people = [str(ms.uuid.UUID(int=i)) for i in range(1, 28)]
    cursors = {}
    monkeypatch.setattr(ms, "_access_token", lambda env: "token")
    visited = []

    def lists(url, token):
        if "/users?" in url:
            return [{"id": u, "accountEnabled": True, "userType": "Member"} for u in people] + [
                {"id": "ignored", "accountEnabled": True, "userType": "Guest"},
                {"id": "disabled", "accountEnabled": False, "userType": "Member"},
            ]
        user = url.split("/users/")[1].split("/")[0]
        visited.append((user, url))
        if user == people[0]:
            raise ms.GraphError(403)
        return [{"id": "t", "meetingId": "m"}]

    monkeypatch.setattr(ms, "_pages", lists)
    monkeypatch.setattr(ms, "_transcript", lambda key, token: [])

    def collect():
        return list(
            ms.graph_notes(
                {"TEAMS_SCOPE": "tenant"}, last_sync=cursors.get, checkpoint=cursors.__setitem__
            )
        )

    assert len(collect()) == 24
    assert len(visited) == 25 and "organizer:" + people[0] not in cursors
    visited.clear()
    collect()
    assert [u for u, _ in visited[:2]] == people[-2:]
    assert any("startDateTime=" in url for _, url in visited)
    assert "startDateTime=" not in visited[0][1]


def test_explicit_organizer_does_not_enumerate_users(monkeypatch):
    user = str(ms.uuid.UUID(int=3))
    monkeypatch.setattr(ms, "_access_token", lambda env: "token")

    def pages(url, token):
        assert f"/users/{user}/onlineMeetings/getAllTranscripts(" in url
        assert f"meetingOrganizerUserId='{user}'" in url
        raise ms.GraphError(403)

    monkeypatch.setattr(ms, "_pages", pages)
    with pytest.raises(ms.GraphError):
        list(ms.graph_notes({"TEAMS_SCOPE": "organizer", "TEAMS_ORGANIZER_ID": user}))
