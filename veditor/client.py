"""HTTP client for communicating with the standalone VEditor service."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import (
    VEditorAuthError,
    VEditorConfigError,
    VEditorError,
    VEditorNetworkError,
    VEditorSyncError,
)
from .mappers import serialize_talk, serialize_talks


class VEditorClient:
    """Single outbound API gateway for interacting with VEditor."""

    DEFAULT_TIMEOUT: float = 10.0

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
    ):
        # 1. Resolve configuration from parameters, settings, or environment
        def _get_conf(name: str) -> Any:
            if getattr(settings, "configured", False):
                return getattr(settings, name, None)
            return None

        self.base_url = base_url or _get_conf("VEDITOR_API_BASE_URL") or os.environ.get("VEDITOR_API_BASE_URL")
        self.api_key = api_key or _get_conf("VEDITOR_API_KEY") or os.environ.get("VEDITOR_API_KEY")

        resolved_timeout = timeout if timeout is not None else _get_conf("VEDITOR_REQUEST_TIMEOUT") or os.environ.get("VEDITOR_REQUEST_TIMEOUT")
        if resolved_timeout is not None:
            try:
                self.timeout = float(resolved_timeout)
            except (ValueError, TypeError) as exc:
                raise VEditorConfigError(f"Invalid timeout '{resolved_timeout}'. Must be a positive number.") from exc
            if self.timeout <= 0:
                raise VEditorConfigError(f"Invalid timeout '{self.timeout}'. Must be strictly greater than 0.")
        else:
            self.timeout = self.DEFAULT_TIMEOUT

        # 2. Validate configuration
        self._validate_config()

        # 3. Setup persistent session with default auth headers and retry strategy
        self.session = session or requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.session.headers.update(
            {
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _validate_config(self) -> None:
        """Validate base_url and api_key."""
        if not self.base_url:
            raise VEditorConfigError("VEditor base URL is not configured. Set VEDITOR_API_BASE_URL.")

        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise VEditorConfigError(f"Invalid VEditor base URL '{self.base_url}'. Must start with http:// or https://.")

        self.base_url = self.base_url.rstrip("/")

        if not self.api_key or not self.api_key.strip():
            raise VEditorConfigError("VEditor API key is not configured. Set VEDITOR_API_KEY.")

    def _build_url(self, path: str) -> str:
        """Construct a clean absolute URL for an endpoint path."""
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Execute an HTTP request against the VEditor API and handle exceptions."""
        url = self._build_url(path)
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", False)

        try:
            response = self.session.request(method=method, url=url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectTimeout) as exc:
            raise VEditorNetworkError(f"VEditor request timed out: {exc}") from exc
        except (requests.exceptions.ConnectionError, requests.exceptions.ProxyError) as exc:
            raise VEditorNetworkError(f"Failed to connect to VEditor at {self.base_url}: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise VEditorNetworkError(f"VEditor network transport error: {exc}") from exc

        return self._handle_response(response)

    def _handle_response(self, response: requests.Response) -> Any:
        """Parse response status code and body into appropriate exceptions or data."""
        try:
            data = response.json()
        except ValueError:
            data = {"raw_text": response.text}

        status = response.status_code

        if 200 <= status < 300:
            return data

        error_message = None
        if isinstance(data, dict):
            error_message = data.get("detail") or data.get("message")
        elif isinstance(data, (list, str)):
            error_message = str(data)

        if not error_message:
            error_message = response.text or f"HTTP {status}"

        if status in (401, 403):
            raise VEditorAuthError(
                f"VEditor authentication failed: {error_message}",
                status_code=status,
                response_data=data,
            )
        if status in (400, 422):
            raise VEditorSyncError(
                f"VEditor sync/validation error: {error_message}",
                status_code=status,
                response_data=data,
            )
        if 500 <= status < 600:
            raise VEditorNetworkError(
                f"VEditor server error: {error_message}",
                status_code=status,
                response_data=data,
            )

        raise VEditorError(
            f"VEditor request failed: {error_message}",
            status_code=status,
            response_data=data,
        )

    def sync_talk(self, talk_slot: Any, event_id: str | None = None) -> dict[str, Any]:
        """Synchronize a single talk with VEditor."""
        payload = serialize_talk(talk_slot, event_id=event_id)
        return self._request("POST", "/talks", json=payload)

    def sync_talks(self, event_id: int | str, talk_slots: list[Any]) -> dict[str, Any]:
        """Atomically upsert talks in bulk for an event via POST /talks/schedule/import.

        Note:
            The live VEditor schedule import endpoint matches and upserts talks based on
            `(event_id, title, start)` and returns `{"status": "ok", "imported_count": N}`.
        """
        serialized = serialize_talks(talk_slots, event_id=event_id)
        payload = {"event_id": event_id, "talks": serialized}
        return self._request("POST", "/talks/schedule/import", json=payload)

    def request_sso_jwt(
        self,
        event_id: str,
        talk_id: str | None = None,
        role: str = "organiser",
    ) -> str:
        """Request a scoped SSO JWT token for browser handoff or speaker review."""
        if role == "organiser":
            endpoint = f"/events/{event_id}/sso-token"
            payload = {"role": "organiser"}
        elif role == "speaker":
            if not talk_id:
                raise ValueError("talk_id is required when requesting a speaker SSO token")
            endpoint = f"/talks/{talk_id}/sso-token"
            payload = {"role": "speaker"}
        else:
            raise ValueError(f"Unsupported role '{role}'. Allowed roles are 'organiser' and 'speaker'.")

        response_data = self._request("POST", endpoint, json=payload)

        if isinstance(response_data, dict):
            token = response_data.get("token") or response_data.get("sso_token") or response_data.get("jwt")
            if not token:
                raise VEditorError("No token received in SSO response", response_data=response_data)
            return str(token)
        if isinstance(response_data, str):
            return response_data

        raise VEditorError("Unexpected response type from SSO token endpoint", response_data=response_data)
