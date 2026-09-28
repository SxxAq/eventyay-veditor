"""Mock VEditor service test harness using responses."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any

import responses
from django.urls import reverse


class MockVEditor:
    """Simulates the VEditor REST API for end-to-end integration tests.

    Intercepts outbound HTTP requests from VEditorClient via the `responses` library,
    validates headers/auth, tracks received talks and SSO requests, and provides helpers
    to emit signed incoming webhooks against Eventyay.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        api_key: str = "test-veditor-api-key-12345",
        webhook_secret: str = "test-webhook-secret-999",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.webhook_secret = webhook_secret

        # Recorded requests
        self.imported_schedules: list[dict[str, Any]] = []
        self.synced_talks: list[dict[str, Any]] = []
        self.sso_token_requests: list[dict[str, Any]] = []
        self.events_queries: list[dict[str, Any]] = []
        self.emitted_webhooks: list[dict[str, Any]] = []

        # Configurable responses
        self.events_list: list[dict[str, Any]] = [
            {
                "id": 1,
                "name": "Test Event",
                "external_id": "test-event",
            }
        ]
        self.next_token: str = "mock.jwt.token.12345"
        self._rsps: responses.RequestsMock | None = None

    def __enter__(self) -> MockVEditor:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def start(self) -> None:
        """Start intercepting HTTP calls to base_url."""
        if self._rsps is None:
            self._rsps = responses.RequestsMock(assert_all_requests_are_fired=False)
        self._rsps.start()
        self._register_default_routes()

    def stop(self) -> None:
        """Stop intercepting HTTP calls and reset registered routes."""
        if self._rsps is not None:
            try:
                self._rsps.stop()
                self._rsps.reset()
            except Exception:
                pass
            self._rsps = None

    def clear(self) -> None:
        """Clear all recorded request payloads and queries."""
        self.imported_schedules.clear()
        self.synced_talks.clear()
        self.sso_token_requests.clear()
        self.events_queries.clear()
        self.emitted_webhooks.clear()

    def _check_auth(self, request: Any) -> bool:
        auth_header = request.headers.get("X-API-Key") or request.headers.get("Authorization")
        if not auth_header:
            return False
        if auth_header == self.api_key or auth_header == f"Bearer {self.api_key}":
            return True
        return False

    def _register_default_routes(self) -> None:
        assert self._rsps is not None

        # GET /events
        def events_callback(request: Any) -> tuple[int, dict[str, str], str]:
            if not self._check_auth(request):
                return (401, {}, json.dumps({"detail": "Invalid or missing API key"}))
            self.events_queries.append({"url": request.url, "headers": dict(request.headers)})
            return (200, {"Content-Type": "application/json"}, json.dumps(self.events_list))

        self._rsps.add_callback(
            responses.GET,
            f"{self.base_url}/events",
            callback=events_callback,
            content_type="application/json",
        )

        # POST /talks/schedule/import
        def schedule_import_callback(request: Any) -> tuple[int, dict[str, str], str]:
            if not self._check_auth(request):
                return (401, {}, json.dumps({"detail": "Invalid or missing API key"}))
            body_bytes = request.body if isinstance(request.body, bytes) else (request.body or "").encode("utf-8")
            data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            self.imported_schedules.append(data)
            talks = data.get("talks", [])
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"status": "ok", "imported_count": len(talks)}),
            )

        self._rsps.add_callback(
            responses.POST,
            f"{self.base_url}/talks/schedule/import",
            callback=schedule_import_callback,
            content_type="application/json",
        )

        # POST /talks
        def single_talk_callback(request: Any) -> tuple[int, dict[str, str], str]:
            if not self._check_auth(request):
                return (401, {}, json.dumps({"detail": "Invalid or missing API key"}))
            body_bytes = request.body if isinstance(request.body, bytes) else (request.body or "").encode("utf-8")
            data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            self.synced_talks.append(data)
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"status": "ok", "id": 101}),
            )

        self._rsps.add_callback(
            responses.POST,
            f"{self.base_url}/talks",
            callback=single_talk_callback,
            content_type="application/json",
        )

        # POST /events/{event_id}/sso-token
        sso_event_pattern = re.compile(rf"^{re.escape(self.base_url)}/events/[^/]+/sso-token$")

        def sso_event_callback(request: Any) -> tuple[int, dict[str, str], str]:
            if not self._check_auth(request):
                return (401, {}, json.dumps({"detail": "Invalid or missing API key"}))
            body_bytes = request.body if isinstance(request.body, bytes) else (request.body or "").encode("utf-8")
            data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            self.sso_token_requests.append({"endpoint": "event", "url": request.url, "body": data})
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"token": self.next_token, "status": "ok"}),
            )

        self._rsps.add_callback(
            responses.POST,
            sso_event_pattern,
            callback=sso_event_callback,
            content_type="application/json",
        )

        # POST /talks/{talk_id}/sso-token
        sso_talk_pattern = re.compile(rf"^{re.escape(self.base_url)}/talks/[^/]+/sso-token$")

        def sso_talk_callback(request: Any) -> tuple[int, dict[str, str], str]:
            if not self._check_auth(request):
                return (401, {}, json.dumps({"detail": "Invalid or missing API key"}))
            body_bytes = request.body if isinstance(request.body, bytes) else (request.body or "").encode("utf-8")
            data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            self.sso_token_requests.append({"endpoint": "talk", "url": request.url, "body": data})
            return (
                200,
                {"Content-Type": "application/json"},
                json.dumps({"token": self.next_token, "status": "ok"}),
            )

        self._rsps.add_callback(
            responses.POST,
            sso_talk_pattern,
            callback=sso_talk_callback,
            content_type="application/json",
        )

    def generate_signature(self, body_bytes: bytes, secret: str | None = None, prefix: str = "sha256=") -> str:
        """Compute an HMAC-SHA256 signature for a webhook payload."""
        sec = secret or self.webhook_secret
        sig = hmac.new(sec.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        return f"{prefix}{sig}" if prefix else sig

    def emit_webhook(
        self,
        target_view_or_rf: Any,
        event: str,
        payload_data: dict[str, Any],
        secret: str | None = None,
        use_request_factory: bool = False,
        path: str | None = None,
    ) -> Any:
        """Dispatch a signed webhook to the plugin's WebhookView."""
        url = path or reverse("plugins:veditor:webhook")
        payload = dict(payload_data)
        payload["event"] = event
        if "timestamp" not in payload:
            payload["timestamp"] = time.time()

        raw_body = json.dumps(payload).encode("utf-8")
        sig = self.generate_signature(raw_body, secret=secret)
        self.emitted_webhooks.append({"event": event, "payload": payload, "signature": sig})

        if use_request_factory:
            from veditor.webhooks import WebhookView

            request = target_view_or_rf.post(
                url,
                data=raw_body,
                content_type="application/json",
                HTTP_X_VEDITOR_SIGNATURE=sig,
            )
            view = WebhookView.as_view()
            return view(request)

        return target_view_or_rf.post(
            url,
            data=raw_body,
            content_type="application/json",
            HTTP_X_VEDITOR_SIGNATURE=sig,
        )
