"""End-to-end integration tests for the Eventyay VEditor plugin lifecycle.

Exercises the complete user journey across:
1. Organiser Handoff & Schedule Synchronization (ConnectView -> Mock VEditor)
2. Talk Approval & Speaker Email Dispatch (VEditor Webhook -> Celery -> Mail)
3. Media Published & Public Schedule Rendering (VEditor Webhook -> Resource -> Recording Provider)
4. Privacy Filters (do_not_record) & Multi-Speaker Magic Links
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse

from tests.mock_veditor import MockVEditor
from veditor.recording import VEditorRecordingProvider
from veditor.tasks import process_talk_approved, process_talk_published
from veditor.views import ConnectView
from veditor.webhooks import WebhookView


def setup_request(request):
    """Attach session and messages storage to a RequestFactory request."""
    middleware = SessionMiddleware(lambda req: None)
    middleware.process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    return request


class MockSettingsStorage:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


@pytest.fixture
def mock_veditor_service():
    """Context manager fixture providing a running mock VEditor service."""
    with MockVEditor() as mock:
        yield mock


@pytest.fixture
def integrated_event():
    """Event mock configured with VEditor credentials."""
    organizer = SimpleNamespace(name="FOSSASIA Org", slug="fossasia")
    settings = MockSettingsStorage(
        {
            "veditor_api_key": "test-veditor-api-key-12345",
            "veditor_api_base_url": "http://localhost:8080",
            "veditor_webhook_secret": "test-webhook-secret-999",
            "mail_from": "notifications@eventyay.com",
        }
    )
    event_obj = SimpleNamespace(
        id=42,
        slug="summit-2026",
        name="FOSSASIA Summit 2026",
        organizer=organizer,
        settings=settings,
        live=True,
    )
    event_obj.submissions = MagicMock()
    return event_obj


@pytest.fixture
def integrated_talk(integrated_event):
    """Talk submission mock with speakers and room slot."""
    speaker1 = SimpleNamespace(
        id=201,
        name="Alice Speaker",
        fullname="Alice Speaker",
        email="alice@example.org",
    )
    speakers = [speaker1]
    speakers_mgr = MagicMock()
    speakers_mgr.all.side_effect = lambda: list(speakers)

    submission = SimpleNamespace(
        id=101,
        code="TALK101",
        title="Keynote: Open Source AI",
        event=integrated_event,
        event_id=integrated_event.id,
        speakers=speakers_mgr,
        do_not_record=False,
        recording_url=None,
    )

    room = SimpleNamespace(id=1, name="Auditorium Main")
    slot = SimpleNamespace(
        id=301,
        submission=submission,
        room=room,
        start=MagicMock(isoformat=lambda: "2026-09-28T09:00:00Z"),
        end=MagicMock(isoformat=lambda: "2026-09-28T09:45:00Z"),
    )
    submission.slots = MagicMock()
    submission.slots.first.return_value = slot

    # Setup event_obj.submissions query behavior
    def filter_event_submissions(**kwargs):
        m = MagicMock()
        code = kwargs.get("code__iexact") or kwargs.get("code")
        sub_id = kwargs.get("id")
        if (code and str(code).upper() == submission.code) or (sub_id and int(sub_id) == submission.id):
            m.first.return_value = submission
        else:
            m.first.return_value = None
        return m

    integrated_event.submissions.filter.side_effect = filter_event_submissions
    integrated_event.current_schedule = SimpleNamespace(scheduled_talks=[slot])

    return SimpleNamespace(submission=submission, slot=slot, speakers=speakers)


# ============================================================================
# Scenario A: Organiser Handoff & Talk Sync
# ============================================================================


def test_integration_scenario_a_organiser_handoff(mock_veditor_service, integrated_event, integrated_talk):
    """Verify organizer handoff: syncs talk schedule and redirects with valid SSO token."""
    rf = RequestFactory()
    user = MagicMock()
    user.is_authenticated = True
    user.email = "organizer@example.org"
    user.name = "Organizer Name"
    user.fullname = "Organizer Name"
    user.get_display_name = lambda: "Organizer Name"
    user.has_event_permission.return_value = True

    mock_veditor_service.events_list = [
        {
            "id": integrated_event.id,
            "external_id": integrated_event.slug,
            "name": integrated_event.name,
        }
    ]

    integrated_event.get_talk_slots = lambda: [integrated_talk.slot]

    request = rf.post(
        reverse(
            "plugins:veditor:connect",
            kwargs={
                "organizer": integrated_event.organizer.slug,
                "event": integrated_event.slug,
            },
        ),
        data={"action": "sync"},
    )
    request.user = user
    request.event = integrated_event
    request.organizer = integrated_event.organizer
    setup_request(request)

    view = ConnectView.as_view()
    response = view(
        request,
        organizer=integrated_event.organizer.slug,
        event=integrated_event.slug,
    )

    # 1. Assert VEditor received bulk schedule import
    assert len(mock_veditor_service.imported_schedules) == 1
    import_payload = mock_veditor_service.imported_schedules[0]
    assert import_payload["event_id"] == integrated_event.id
    assert len(import_payload["talks"]) == 1
    synced_talk = import_payload["talks"][0]
    assert synced_talk["external_id"] == "TALK101"
    assert synced_talk["title"] == "Keynote: Open Source AI"
    assert synced_talk["room"] == "Auditorium Main"

    # 2. Assert SSO Token was requested for organizer
    assert len(mock_veditor_service.sso_token_requests) == 1
    sso_req = mock_veditor_service.sso_token_requests[0]
    assert sso_req["endpoint"] == "event"
    assert sso_req["body"]["role"] == "organizer"

    # 3. Assert HTTP 302 Redirect to VEditor with SSO token parameter
    assert response.status_code == 302
    assert f"http://localhost:8080/studio?event_id={integrated_event.id}" in response.url
    assert "sso_token=mock.jwt.token.12345" in response.url


# ============================================================================
# Scenario B: Approval Webhook & Speaker Email Dispatch
# ============================================================================


def test_integration_scenario_b_talk_approved_email_dispatch(mock_veditor_service, integrated_event, integrated_talk):
    """Verify talk.approved webhook: validates HMAC and sends speaker review magic link email."""
    cache.clear()
    mail.outbox.clear()
    rf = RequestFactory()

    submission = integrated_talk.submission
    speaker = integrated_talk.speakers[0]

    queued_mails = []

    def mock_create_mail(**kwargs):
        m = MagicMock()
        m.kwargs = kwargs
        m.to = kwargs.get("to")
        m.subject = kwargs.get("subject")
        m.text = kwargs.get("text")
        m.to_users = MagicMock()
        m.submissions = MagicMock()
        queued_mails.append(m)
        return m

    def filter_event(**kwargs):
        m = MagicMock()
        if kwargs.get("id") == integrated_event.id or kwargs.get("slug") == integrated_event.slug:
            m.first.return_value = integrated_event
        else:
            m.first.return_value = None
        return m

    def filter_sub(**kwargs):
        m = MagicMock()
        code = kwargs.get("code")
        sub_id = kwargs.get("id")
        if code == submission.code or sub_id == submission.id:
            m.first.return_value = submission
        else:
            m.first.return_value = None
        return m

    payload = {
        "event": "talk.approved",
        "talk_id": 55,
        "event_id": integrated_event.id,
        "external_id": submission.code,
        "timestamp": time.time(),
    }

    with (
        patch("veditor.webhooks.process_talk_approved") as mock_webhook_task,
        patch("veditor.tasks.Event.objects.filter", side_effect=filter_event),
        patch("veditor.tasks.Submission.objects.filter", side_effect=filter_sub),
        patch("veditor.tasks.QueuedMail.objects.create", side_effect=mock_create_mail),
        patch("eventyay.base.models.Event.objects") as mock_webhook_event_mgr,
    ):
        mock_webhook_event_mgr.filter.side_effect = filter_event

        # Step 1: Webhook ingestion
        response = mock_veditor_service.emit_webhook(
            rf,
            event="talk.approved",
            payload_data=payload,
            use_request_factory=True,
        )
        assert response.status_code == 200
        data = json.loads(response.content.decode("utf-8"))
        assert data["status"] == "accepted"
        mock_webhook_task.delay.assert_called_once_with(
            event_id=integrated_event.id,
            talk_id=55,
            external_id=submission.code,
            raw_payload=payload,
        )

        # Step 2: Celery task execution
        result = process_talk_approved(
            event_id=integrated_event.id,
            talk_id=55,
            external_id=submission.code,
        )
        assert result["status"] == "success"
        assert result["sent_count"] == 1
        assert speaker.email in result["recipients"]

        # Step 3: Verify email queued in outbox
        assert len(queued_mails) == 1
        sent_mail = queued_mails[0]
        assert sent_mail.to == speaker.email
        assert submission.title in sent_mail.subject
        assert "sso_token=mock.jwt.token.12345" in sent_mail.text

        # Verify MockVEditor received speaker SSO token request
        assert len(mock_veditor_service.sso_token_requests) == 1
        sso_req = mock_veditor_service.sso_token_requests[0]
        assert sso_req["endpoint"] == "talk"
        assert sso_req["body"]["role"] == "speaker"
        assert sso_req["body"]["email"] == speaker.email


# ============================================================================
# Scenario C: Published Webhook & Public Schedule Playback
# ============================================================================


def test_integration_scenario_c_talk_published_public_schedule(mock_veditor_service, integrated_event, integrated_talk):
    """Verify talk.published webhook: updates Resource and displays video player on public schedule."""
    rf = RequestFactory()
    submission = integrated_talk.submission

    video_url = "https://cdn.example.org/videos/keynote-open-source-ai.mp4"
    payload = {
        "event": "talk.published",
        "talk_id": 55,
        "event_id": integrated_event.id,
        "external_id": submission.code,
        "video_url": video_url,
        "timestamp": time.time(),
    }

    mock_resource = MagicMock()
    mock_resource.id = 888
    mock_resource.link = video_url
    mock_resource.description = "Video Recording"
    mock_resource.kind = "generic"

    submission.resources = MagicMock()
    submission.resources.filter.return_value.first.return_value = mock_resource

    def filter_event(**kwargs):
        m = MagicMock()
        if kwargs.get("id") == integrated_event.id or kwargs.get("slug") == integrated_event.slug:
            m.first.return_value = integrated_event
        else:
            m.first.return_value = None
        return m

    with (
        patch("veditor.webhooks.process_talk_published") as mock_pub_task,
        patch("eventyay.base.models.Event.objects") as mock_event_mgr,
        patch("eventyay.base.models.Resource.objects.filter") as mock_res_filter,
    ):
        mock_event_mgr.filter.side_effect = filter_event
        mock_res_filter.return_value.order_by.return_value.first.return_value = mock_resource

        # Step 1: Ingest webhook
        response = mock_veditor_service.emit_webhook(
            rf,
            event="talk.published",
            payload_data=payload,
            use_request_factory=True,
        )
        assert response.status_code == 200
        mock_pub_task.delay.assert_called_once_with(
            event_id=integrated_event.id,
            talk_id=55,
            video_url=video_url,
            external_id=submission.code,
            raw_payload=payload,
        )

        # Step 2: Execute task
        task_res = process_talk_published(**mock_pub_task.delay.call_args.kwargs)
        assert task_res["status"] == "success"
        assert task_res["submission_code"] == submission.code

        # Step 3: Public schedule recording provider
        provider = VEditorRecordingProvider(integrated_event)
        recording_output = provider.get_recording(submission)
        assert "iframe" in recording_output
        assert "csp_header" in recording_output
        assert f'src="{video_url}"' in recording_output["iframe"]
        assert "<video controls" in recording_output["iframe"]
        assert recording_output["csp_header"] == "https://cdn.example.org"


# ============================================================================
# Scenario D: Privacy Opt-Out (do_not_record)
# ============================================================================


def test_integration_scenario_d_privacy_opt_out_respected(mock_veditor_service, integrated_event, integrated_talk):
    """Verify speaker privacy: do_not_record skips attachment and hides player from public schedule."""
    rf = RequestFactory()
    submission = integrated_talk.submission
    submission.do_not_record = True

    video_url = "https://cdn.example.org/videos/private-session.mp4"
    payload = {
        "event": "talk.published",
        "talk_id": 99,
        "event_id": integrated_event.id,
        "external_id": submission.code,
        "video_url": video_url,
        "timestamp": time.time(),
    }

    def filter_event(**kwargs):
        m = MagicMock()
        if kwargs.get("id") == integrated_event.id or kwargs.get("slug") == integrated_event.slug:
            m.first.return_value = integrated_event
        else:
            m.first.return_value = None
        return m

    with (
        patch("veditor.webhooks.process_talk_published") as mock_pub_task,
        patch("eventyay.base.models.Event.objects") as mock_event_mgr,
    ):
        mock_event_mgr.filter.side_effect = filter_event

        # Step 1: Ingest webhook
        response = mock_veditor_service.emit_webhook(
            rf,
            event="talk.published",
            payload_data=payload,
            use_request_factory=True,
        )
        assert response.status_code == 200
        mock_pub_task.delay.assert_called_once_with(
            event_id=integrated_event.id,
            talk_id=99,
            video_url=video_url,
            external_id=submission.code,
            raw_payload=payload,
        )

        # Step 2: Execute published task
        task_res = process_talk_published(
            event_id=integrated_event.id,
            talk_id=99,
            external_id=submission.code,
            video_url=video_url,
            raw_payload=payload,
        )
        assert task_res["status"] == "skipped"
        assert task_res["reason"] == "do_not_record"

        # Step 3: Public schedule must remain completely blank
        provider = VEditorRecordingProvider(integrated_event)
        assert provider.get_recording(submission) == {}


# ============================================================================
# Scenario E: Multi-Speaker Magic Links
# ============================================================================


def test_integration_scenario_e_multi_speaker_distinct_magic_links(mock_veditor_service, integrated_event, integrated_talk):
    """Verify multi-speaker talks: each co-speaker receives individual magic link with their own SSO token."""
    cache.clear()
    mail.outbox.clear()

    submission = integrated_talk.submission
    speaker1 = integrated_talk.speakers[0]
    speaker2 = SimpleNamespace(
        id=202,
        name="Bob CoSpeaker",
        fullname="Bob CoSpeaker",
        email="bob@example.org",
    )
    all_speakers = [speaker1, speaker2]
    submission.speakers.all.side_effect = lambda: list(all_speakers)

    queued_mails = []

    def mock_create_mail(**kwargs):
        m = MagicMock()
        m.to = kwargs.get("to")
        m.text = kwargs.get("text")
        m.subject = kwargs.get("subject")
        queued_mails.append(m)
        return m

    def filter_event(**kwargs):
        m = MagicMock()
        if kwargs.get("id") == integrated_event.id or kwargs.get("slug") == integrated_event.slug:
            m.first.return_value = integrated_event
        else:
            m.first.return_value = None
        return m

    def filter_sub(**kwargs):
        m = MagicMock()
        code = kwargs.get("code")
        sub_id = kwargs.get("id")
        if code == submission.code or sub_id == submission.id:
            m.first.return_value = submission
        else:
            m.first.return_value = None
        return m

    with (
        patch("veditor.tasks.Event.objects.filter", side_effect=filter_event),
        patch("veditor.tasks.Submission.objects.filter", side_effect=filter_sub),
        patch("veditor.tasks.QueuedMail.objects.create", side_effect=mock_create_mail),
    ):
        result = process_talk_approved(
            event_id=integrated_event.id,
            talk_id=55,
            external_id=submission.code,
        )

        assert result["status"] == "success"
        assert result["sent_count"] == 2
        assert set(result["recipients"]) == {"alice@example.org", "bob@example.org"}
        assert len(queued_mails) == 2
        recipients = [m.to for m in queued_mails]
        assert "alice@example.org" in recipients
        assert "bob@example.org" in recipients


# ============================================================================
# Scenario F: Webhook Security & Tampering Rejection
# ============================================================================


def test_integration_scenario_f_tampered_signature_rejected(mock_veditor_service, integrated_event):
    """Verify security boundary: forged or tampered HMAC signature is rejected with HTTP 401."""
    rf = RequestFactory()
    payload = {
        "event": "talk.approved",
        "talk_id": 42,
        "event_id": integrated_event.id,
        "external_id": "TALK101",
        "timestamp": time.time(),
    }
    raw_body = json.dumps(payload).encode("utf-8")

    request = rf.post(
        reverse("plugins:veditor:webhook"),
        data=raw_body,
        content_type="application/json",
        HTTP_X_VEDITOR_SIGNATURE="sha256=invalid_tampered_hash_0000000000000000000",
    )

    def filter_event(**kwargs):
        m = MagicMock()
        if kwargs.get("id") == integrated_event.id or kwargs.get("slug") == integrated_event.slug:
            m.first.return_value = integrated_event
        else:
            m.first.return_value = None
        return m

    with patch("eventyay.base.models.Event.objects") as mock_event_mgr:
        mock_event_mgr.filter.side_effect = filter_event
        view = WebhookView.as_view()
        response = view(request)

        assert response.status_code == 401
        data = json.loads(response.content.decode("utf-8"))
        assert "Invalid webhook signature" in data["error"]
