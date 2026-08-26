import json
from typing import Any

import pytest
import requests

from scripts import fls_audit_issue as audit


def response(status: int, value: object, *, link: str | None = None) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(value).encode()
    if link:
        result.headers["Link"] = link
    return result


class FakeSession:
    def __init__(self, responses: list[requests.Response]):
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


def client(session: FakeSession) -> audit.GitHubClient:
    return audit.GitHubClient("token", "owner", "repo", session=session)


def test_paginate_follows_link_header() -> None:
    second_url = "https://api.github.com/repositories/1/issues?page=2"
    session = FakeSession(
        [
            response(200, [{"number": 1}], link=f'<{second_url}>; rel="next"'),
            response(200, [{"number": 2}]),
        ]
    )

    values = client(session).issues()

    assert [value["number"] for value in values] == [1, 2]
    assert session.requests[1][1] == second_url
    assert session.requests[1][2]["params"] is None


def test_ensure_label_url_encodes_name_and_creates_missing_label() -> None:
    session = FakeSession([response(404, {}), response(201, {"name": "FLS audit"})])

    client(session).ensure_label("FLS audit")

    assert session.requests[0][1].endswith("/labels/FLS%20audit")
    assert session.requests[1][0] == "POST"
    assert session.requests[1][2]["json"]["name"] == "FLS audit"


def test_api_error_includes_response_context() -> None:
    session = FakeSession([response(422, {"message": "invalid"})])

    with pytest.raises(audit.AuditIssueError, match="422"):
        client(session).issue(42)
