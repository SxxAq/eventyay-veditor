"""Unit and integration tests for the Connect view and navigation signals."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django.urls import reverse

from veditor.exceptions import VEditorNetworkError, VEditorSyncError
from veditor.signals import control_nav_veditor
from veditor.views import ConnectView


def setup_request(request):
    """Helper to attach session and messages storage to a RequestFactory request."""
    middleware = SessionMiddleware(lambda req: None)
    middleware.process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    return request


class MockEventSettings:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


@pytest.fixture
def event():
    """Mock event fixture avoiding database connections in CI."""
    organizer = SimpleNamespace(slug="test-org")
    schedule = SimpleNamespace(scheduled_talks=[])
    return SimpleNamespace(
        id=42,
        slug="test-conf",
        name="Test Conference 2026",
        organizer=organizer,
        settings=MockEventSettings(),
        current_schedule=schedule,
        wip_schedule=None,
        get_talk_slots=lambda: [],
    )


@pytest.fixture
def user():
    """Mock unprivileged user fixture."""
    mock_u = MagicMock()
    mock_u.is_authenticated = True
    mock_u.has_event_permission.return_value = False
    return mock_u


@pytest.fixture
def organizer_user():
    """Mock organizer user fixture with can_change_event_settings permission."""
    mock_u = MagicMock()
    mock_u.is_authenticated = True
    mock_u.has_event_permission.return_value = True
    return mock_u


@pytest.fixture
def rf():
    return RequestFactory()


# ============================================================================
# Navigation Signal Tests
# ============================================================================


def test_signals_nav_unauthenticated(event, rf):
    request = setup_request(rf.get("/"))
    request.user = SimpleNamespace(is_authenticated=False)
    items = control_nav_veditor(sender=event, request=request)
    assert items == []


def test_signals_nav_no_permission(event, user, rf):
    request = setup_request(rf.get("/"))
    request.user = user
    items = control_nav_veditor(sender=event, request=request)
    assert items == []


def test_signals_nav_with_permission(event, organizer_user, rf):
    request = setup_request(rf.get("/"))
    request.user = organizer_user
    items = control_nav_veditor(sender=event, request=request)
    assert len(items) == 1
    assert items[0]["label"] == "Video Editor"
    assert items[0]["icon"] == "video-camera"
    assert reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug}) in items[0]["url"]


# ============================================================================
# Connect View Tests
# ============================================================================


def test_connect_view_get_unauthorized(event, user, rf):
    request = setup_request(rf.get(reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug})))
    request.user = user
    request.event = event
    request.organizer = event.organizer

    view = ConnectView.as_view()
    with pytest.raises(PermissionDenied):
        view(request, organizer=event.organizer.slug, event=event.slug)


def test_connect_view_get_authorized(event, organizer_user, rf):
    request = setup_request(rf.get(reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug})))
    request.user = organizer_user
    request.event = event
    request.organizer = event.organizer

    view = ConnectView.as_view()
    response = view(request, organizer=event.organizer.slug, event=event.slug)

    assert response.status_code == 200
    assert response.context_data["event"] == event
    assert response.context_data["talks_count"] == 0


def test_connect_view_post_success(event, organizer_user, rf):
    request = setup_request(rf.post(reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug})))
    request.user = organizer_user
    request.event = event
    request.organizer = event.organizer

    with patch("veditor.views.VEditorClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.base_url = "https://editor.example.com"
        mock_client.sync_talks.return_value = {"status": "ok", "imported_count": 0}
        mock_client.request_sso_jwt.return_value = "mock_signed_jwt_token"

        view = ConnectView.as_view()
        response = view(request, organizer=event.organizer.slug, event=event.slug)

        assert response.status_code == 302
        assert response.url == f"https://editor.example.com/studio?event_id={event.id}&sso_token=mock_signed_jwt_token"
        assert mock_client.sync_talks.called
        assert mock_client.request_sso_jwt.called


def test_connect_view_post_sync_error_blocks_redirect(event, organizer_user, rf):
    request = setup_request(rf.post(reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug})))
    request.user = organizer_user
    request.event = event
    request.organizer = event.organizer

    with patch("veditor.views.VEditorClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.base_url = "https://editor.example.com"
        mock_client.sync_talks.side_effect = VEditorSyncError("Invalid talk structure", status_code=422)

        view = ConnectView.as_view()
        response = view(request, organizer=event.organizer.slug, event=event.slug)

        # Must stay on the connect page with 200, NOT redirect to VEditor
        assert response.status_code == 200
        assert not mock_client.request_sso_jwt.called
        messages = [m.message for m in request._messages]
        assert any("Failed to synchronize talks" in str(m) for m in messages)


def test_connect_view_post_network_error_blocks_redirect(event, organizer_user, rf):
    request = setup_request(rf.post(reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug})))
    request.user = organizer_user
    request.event = event
    request.organizer = event.organizer

    with patch("veditor.views.VEditorClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.base_url = "https://editor.example.com"
        mock_client.sync_talks.side_effect = VEditorNetworkError("VEditor server connection refused")

        view = ConnectView.as_view()
        response = view(request, organizer=event.organizer.slug, event=event.slug)

        # Must stay on the connect page with 200, NOT redirect to VEditor
        assert response.status_code == 200
        assert not mock_client.request_sso_jwt.called
        messages = [m.message for m in request._messages]
        assert any("Failed to synchronize talks" in str(m) for m in messages)


def test_connect_view_post_save_settings(event, organizer_user, rf):
    request = setup_request(
        rf.post(
            reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug}),
            data={
                "action": "save_settings",
                "veditor_api_base_url": "http://localhost:8080/",
                "veditor_api_key": "new-secret-key",
                "veditor_event_id": "99105",
            },
        )
    )
    request.user = organizer_user
    request.event = event
    request.organizer = event.organizer

    view = ConnectView.as_view()
    response = view(request, organizer=event.organizer.slug, event=event.slug)

    assert response.status_code == 302
    assert event.settings.get("veditor_api_base_url") == "http://localhost:8080"
    assert event.settings.get("veditor_api_key") == "new-secret-key"
    assert event.settings.get("veditor_event_id") == "99105"


def test_connect_view_post_with_custom_event_id(event, organizer_user, rf):
    event.settings.set("veditor_api_base_url", "http://localhost:8080")
    event.settings.set("veditor_api_key", "valid-key")
    event.settings.set("veditor_event_id", "99105")

    request = setup_request(
        rf.post(
            reverse("plugins:veditor:connect", kwargs={"organizer": event.organizer.slug, "event": event.slug}),
            data={"action": "sync_and_launch"},
        )
    )
    request.user = organizer_user
    request.event = event
    request.organizer = event.organizer

    with patch("veditor.views.VEditorClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.base_url = "http://localhost:8080"
        mock_client.sync_talks.return_value = {"status": "ok"}
        mock_client.request_sso_jwt.return_value = "jwt_token_123"

        view = ConnectView.as_view()
        response = view(request, organizer=event.organizer.slug, event=event.slug)

        assert response.status_code == 302
        assert response.url == "http://localhost:8080/studio?event_id=99105&sso_token=jwt_token_123"
        mock_client.sync_talks.assert_called_once_with(event_id=99105, talk_slots=[])
        mock_client.request_sso_jwt.assert_called_once_with(event_id=99105, role="organiser")
