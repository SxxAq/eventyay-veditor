"""Unit and integration tests for VEditor Celery tasks and speaker email dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse

from veditor.exceptions import VEditorConfigError, VEditorError, VEditorNetworkError
from veditor.tasks import process_talk_approved
from veditor.views import ConnectView


def setup_request(request):
    """Attach session and messages storage to a RequestFactory request."""
    middleware = SessionMiddleware(lambda req: None)
    middleware.process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    return request


class MockEventSettings:
    """Mock event settings storage avoiding database queries."""

    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def configured_event():
    """Create a fully configured mock Event with VEditor settings."""
    organizer = SimpleNamespace(name="FOSSASIA Org", slug="fossasia-org")
    settings = MockEventSettings(
        {
            "veditor_api_key": "test-veditor-api-key-12345",
            "veditor_api_base_url": "http://localhost:8080",
            "mail_from": "summit@fossasia.org",
        }
    )
    return SimpleNamespace(
        id=42,
        slug="fossasia-2026",
        name="FOSSASIA Summit 2026",
        organizer=organizer,
        settings=settings,
        live=True,
    )


@pytest.fixture
def speaker_user():
    """Create a test speaker user mock."""
    return SimpleNamespace(
        id=101,
        email="speaker@example.com",
        fullname="Jane Speaker",
        name="Jane Speaker",
    )


@pytest.fixture
def co_speaker_user():
    """Create a second test speaker user mock."""
    return SimpleNamespace(
        id=102,
        email="cospeaker@example.com",
        fullname="Alex CoSpeaker",
        name="Alex CoSpeaker",
    )


@pytest.fixture
def submission(configured_event, speaker_user):
    """Create a test talk submission with a registered speaker."""
    speakers_list = [speaker_user]
    speakers_mgr = MagicMock()
    speakers_mgr.all.side_effect = lambda: list(speakers_list)
    speakers_mgr.add.side_effect = lambda u: speakers_list.append(u) if u not in speakers_list else None

    sub = SimpleNamespace(
        id=101,
        event=configured_event,
        code="TALK-101",
        title="Keynote: Open Source AI Studio",
        speakers=speakers_mgr,
        slots=MagicMock(),
    )
    sub.slots.first.return_value = None
    return sub


@pytest.fixture(autouse=True)
def mock_tasks_orm(configured_event, submission):
    """Automatically mock Event, Submission, TalkSlot, and QueuedMail queries for task tests."""
    cache.clear()
    mail.outbox.clear()

    mock_mails = []

    def create_queued_mail(**kwargs):
        m = MagicMock()
        m.kwargs = kwargs
        m.to = kwargs.get("to")
        m.subject = kwargs.get("subject")
        m.text = kwargs.get("text")
        m.locale = kwargs.get("locale")
        m.to_users = MagicMock()
        m.submissions = MagicMock()
        mock_mails.append(m)
        return m

    with (
        patch("veditor.tasks.Event.objects.filter") as mock_event_qs,
        patch("veditor.tasks.Submission.objects.filter") as mock_sub_qs,
        patch("veditor.tasks.TalkSlot.objects.filter") as mock_slot_qs,
        patch("veditor.tasks.QueuedMail.objects.create", side_effect=create_queued_mail) as mock_qm_create,
    ):

        def filter_event(**kwargs):
            m = MagicMock()
            if kwargs.get("id") == configured_event.id or kwargs.get("slug") == configured_event.slug:
                m.first.return_value = configured_event
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

        def filter_slot(**kwargs):
            m = MagicMock()
            m.select_related.return_value = m
            m.first.return_value = None
            return m

        mock_event_qs.side_effect = filter_event
        mock_sub_qs.side_effect = filter_sub
        mock_slot_qs.side_effect = filter_slot

        yield {
            "event": mock_event_qs,
            "submission": mock_sub_qs,
            "slot": mock_slot_qs,
            "queued_mail_create": mock_qm_create,
            "queued_mails": mock_mails,
        }


# ============================================================================
# process_talk_approved Unit & Integration Tests
# ============================================================================


def test_process_talk_approved_success_single_speaker(configured_event, submission, speaker_user, mock_tasks_orm):
    """Verify successful JWT minting and QueuedMail dispatch to a single speaker."""
    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "jwt.test.token.speaker123"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id=submission.code,
        )

    assert result["status"] == "success"
    assert result["event_id"] == configured_event.id
    assert result["talk_id"] == "42"
    assert result["external_id"] == submission.code
    assert result["sent_count"] == 1
    assert result["recipients"] == [speaker_user.email]

    mock_jwt.assert_called_once_with(
        event_id=str(configured_event.id),
        talk_id="42",
        role="speaker",
        email=speaker_user.email,
        display_name=speaker_user.fullname,
    )

    # Inspect dispatched email via QueuedMail
    assert len(mock_tasks_orm["queued_mails"]) == 1
    sent_mail = mock_tasks_orm["queued_mails"][0]
    assert sent_mail.to == speaker_user.email
    assert f"[{configured_event.name}] Video Review Ready: {submission.title}" in sent_mail.subject
    assert "jwt.test.token.speaker123" in sent_mail.text
    sent_mail.to_users.add.assert_called_once_with(speaker_user)
    sent_mail.submissions.add.assert_called_once_with(submission)
    sent_mail.send.assert_called_once()


def test_process_talk_approved_success_multiple_speakers(configured_event, submission, speaker_user, co_speaker_user, mock_tasks_orm):
    """Verify email dispatch to all co-speakers of a talk."""
    submission.speakers.add(co_speaker_user)

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "jwt.test.token.multi"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id=submission.code,
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 2
    assert set(result["recipients"]) == {speaker_user.email, co_speaker_user.email}
    assert len(mock_tasks_orm["queued_mails"]) == 2


def test_process_talk_approved_idempotency_and_force_bypass(configured_event, submission, speaker_user, mock_tasks_orm):
    """Verify idempotency cache skips redundant dispatch and force=True bypasses cache."""
    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "jwt.token"

        # 1. First run dispatches
        result1 = process_talk_approved(event_id=configured_event.id, talk_id="42", external_id=submission.code)
        assert result1["status"] == "success"
        assert result1["sent_count"] == 1
        assert len(mock_tasks_orm["queued_mails"]) == 1

        # 2. Second run without force skips and tracks in skipped
        result2 = process_talk_approved(event_id=configured_event.id, talk_id="42", external_id=submission.code)
        assert result2["status"] == "skipped"
        assert result2["sent_count"] == 0
        assert result2["skipped"] == [speaker_user.email]
        assert len(mock_tasks_orm["queued_mails"]) == 1

        # 3. Third run with force=True bypasses delivery cache
        result3 = process_talk_approved(event_id=configured_event.id, talk_id="42", external_id=submission.code, force=True)
        assert result3["status"] == "success"
        assert result3["sent_count"] == 1
        assert result3["recipients"] == [speaker_user.email]
        assert len(mock_tasks_orm["queued_mails"]) == 2


def test_process_talk_approved_no_speakers_returns_skipped(configured_event, submission, mock_tasks_orm):
    """Verify talk without confirmed speakers returns skipped without failing."""
    submission.speakers.all.side_effect = lambda: []

    result = process_talk_approved(
        event_id=configured_event.id,
        talk_id="42",
        external_id=submission.code,
    )

    assert result["status"] == "skipped"
    assert "No speakers registered" in result["message"]
    assert result["sent_count"] == 0
    assert len(mock_tasks_orm["queued_mails"]) == 0


def test_process_talk_approved_missing_submission_returns_error(configured_event, mock_tasks_orm):
    """Verify unknown submission external_id returns error dict without unhandled crash."""

    def not_found(**kwargs):
        m = MagicMock()
        m.first.return_value = None
        return m

    mock_tasks_orm["submission"].side_effect = not_found

    result = process_talk_approved(
        event_id=configured_event.id,
        talk_id="555",
        external_id="NONEXISTENT-CODE",
    )

    assert result["status"] == "error"
    assert result["error"] == "Submission not found"
    assert len(mock_tasks_orm["queued_mails"]) == 0


def test_process_talk_approved_unconfigured_veditor_client(submission, mock_tasks_orm):
    """Verify VEditorConfigError is raised when event has no VEditor credentials configured."""
    organizer = SimpleNamespace(name="Bare Org", slug="bare-org")
    bare_event = SimpleNamespace(
        id=99,
        slug="bare-event",
        name="Bare Event",
        organizer=organizer,
        settings=MockEventSettings(),
        live=True,
    )
    submission.event = bare_event

    def filter_event(**kwargs):
        m = MagicMock()
        m.first.return_value = bare_event
        return m

    mock_tasks_orm["event"].side_effect = filter_event

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(VEditorConfigError, match="not configured"):
            process_talk_approved(
                event_id=bare_event.id,
                talk_id="12",
                external_id=submission.code,
            )


def test_process_talk_approved_network_error_raises_for_celery_retry(configured_event, submission):
    """Verify VEditorNetworkError bubbles up unhandled so Celery's autoretry_for triggers."""
    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.side_effect = VEditorNetworkError("Connection refused by VEditor backend", status_code=502)

        with pytest.raises(VEditorNetworkError):
            process_talk_approved(
                event_id=configured_event.id,
                talk_id="42",
                external_id=submission.code,
            )


def test_process_talk_approved_partial_failure_logs_and_continues(configured_event, submission, speaker_user, co_speaker_user, mock_tasks_orm):
    """Verify partial failure (one speaker raises VEditorError) allows other speakers to succeed."""
    submission.speakers.add(co_speaker_user)

    def sso_side_effect(**kwargs):
        if kwargs.get("email") == speaker_user.email:
            raise VEditorError("Speaker user blacklisted in studio")
        return "token-ok"

    with patch("veditor.tasks.VEditorClient.request_sso_jwt", side_effect=sso_side_effect):
        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id=submission.code,
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert result["recipients"] == [co_speaker_user.email]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["email"] == speaker_user.email
    assert len(mock_tasks_orm["queued_mails"]) == 1


def test_process_talk_approved_resolves_by_talk_slot_id(configured_event, submission, mock_tasks_orm):
    """Verify lookup resolves correctly when external_id points to a TalkSlot ID."""
    slot = SimpleNamespace(
        id=789,
        submission=submission,
    )

    def filter_sub(**kwargs):
        m = MagicMock()
        m.first.return_value = None
        return m

    def filter_slot(**kwargs):
        m = MagicMock()
        m.select_related.return_value = m
        if kwargs.get("id") == 789:
            m.first.return_value = slot
        else:
            m.first.return_value = None
        return m

    mock_tasks_orm["submission"].side_effect = filter_sub
    mock_tasks_orm["slot"].side_effect = filter_slot

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "token-by-slot-id"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id="789",
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert len(mock_tasks_orm["queued_mails"]) == 1


def test_process_talk_approved_resolves_by_talk_id_when_no_external_id(configured_event, submission, mock_tasks_orm):
    """Verify fallback lookup when external_id is omitted and talk_id matches submission code."""
    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "token-fallback"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id=submission.code,
            external_id=None,
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert len(mock_tasks_orm["queued_mails"]) == 1


def test_process_talk_approved_speaker_without_email_skipped(configured_event, submission, mock_tasks_orm):
    """Verify speakers with null or empty emails are skipped without breaking dispatch."""
    no_email_user = SimpleNamespace(
        id=103,
        email="",
        fullname="Anonymous Speaker",
        name="Anonymous Speaker",
    )
    submission.speakers.add(no_email_user)

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "token-valid"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id=submission.code,
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert len(mock_tasks_orm["queued_mails"]) == 1


def test_process_talk_approved_fallback_email_multi_alternatives(configured_event, submission, speaker_user):
    """Verify fallback to EmailMultiAlternatives when QueuedMail model is not available."""
    with (
        patch("veditor.tasks.QueuedMail", None),
        patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt,
    ):
        mock_jwt.return_value = "token-fallback-mail"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id=submission.code,
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [speaker_user.email]


# ============================================================================
# Manual Organizer Resend Action Tests
# ============================================================================


def test_manual_resend_speaker_link_view_success(configured_event, submission, rf):
    """Verify organizer can manually trigger review link dispatch from ConnectView with force=True."""
    user = MagicMock()
    user.is_authenticated = True
    user.has_event_permission.return_value = True

    request = rf.post(
        reverse("plugins:veditor:connect", kwargs={"organizer": configured_event.organizer.slug, "event": configured_event.slug}),
        data={
            "action": "resend_speaker_link",
            "submission_code": submission.code,
            "external_id": submission.code,
        },
    )
    request.user = user
    request.event = configured_event
    request.organizer = configured_event.organizer
    setup_request(request)

    with patch("veditor.views.process_talk_approved.delay") as mock_delay:
        view = ConnectView.as_view()
        response = view(request, organizer=configured_event.organizer.slug, event=configured_event.slug)

    assert response.status_code == 302
    assert response.url == reverse("plugins:veditor:connect", kwargs={"organizer": configured_event.organizer.slug, "event": configured_event.slug})
    mock_delay.assert_called_once_with(
        event_id=configured_event.id,
        talk_id=submission.code,
        external_id=submission.code,
        force=True,
    )


def test_manual_resend_speaker_link_view_no_talk_selected(configured_event, rf):
    """Verify error message when organizer attempts to resend link without selecting a talk."""
    user = MagicMock()
    user.is_authenticated = True
    user.has_event_permission.return_value = True

    request = rf.post(
        reverse("plugins:veditor:connect", kwargs={"organizer": configured_event.organizer.slug, "event": configured_event.slug}),
        data={
            "action": "resend_speaker_link",
        },
    )
    request.user = user
    request.event = configured_event
    request.organizer = configured_event.organizer
    setup_request(request)

    with patch("veditor.views.process_talk_approved.delay") as mock_delay:
        view = ConnectView.as_view()
        response = view(request, organizer=configured_event.organizer.slug, event=configured_event.slug)

    assert response.status_code == 302
    mock_delay.assert_not_called()
