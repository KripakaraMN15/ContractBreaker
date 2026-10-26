"""Fault injection. Routes `requests` calls into a WSGI app in-process (no
sockets, fully deterministic) and can drop a response *after* the server has
already handled the request: the "ambiguous outcome" that makes retries risky."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import requests
from requests.adapters import BaseAdapter

from .events import EventLog


@dataclass
class FaultPlan:
    """Attempt numbers (1-based, per method+path) whose response is lost."""

    drop_response_on_attempts: set[int] = field(default_factory=lambda: {1})


class FaultInjectingAdapter(BaseAdapter):
    def __init__(self, wsgi_app, log: EventLog, plan: FaultPlan) -> None:
        super().__init__()
        self._client = wsgi_app.test_client()
        self._log = log
        self._plan = plan
        self._attempts: dict[tuple[str, str], int] = defaultdict(int)

    def send(self, request, **kwargs):
        key = (request.method, request.path_url)
        self._attempts[key] += 1
        attempt = self._attempts[key]
        self._log.record(
            "request_sent", method=request.method, path=request.path_url, attempt=attempt
        )

        server_resp = self._client.open(
            request.path_url,
            method=request.method,
            headers=dict(request.headers),
            data=request.body,
        )

        if attempt in self._plan.drop_response_on_attempts:
            self._log.record("response_dropped", attempt=attempt)
            raise requests.exceptions.ReadTimeout(request=request)

        response = requests.Response()
        response.status_code = server_resp.status_code
        response._content = server_resp.data
        response.headers.update(server_resp.headers)
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:  # required by BaseAdapter
        pass
