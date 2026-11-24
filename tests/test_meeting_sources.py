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
