"""Unit and integration tests for VEditor Celery tasks and speaker email dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scopes_disabled
from eventyay.base.models import Event, Organizer, Submission, SubmissionType, TalkSlot, User

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


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture
def configured_event(db):
    """Create a fully configured Event with VEditor settings."""
    with scopes_disabled():
        organizer = Organizer.objects.create(name="FOSSASIA Org", slug="fossasia-org")
        event = Event.objects.create(
            organizer=organizer,
            name="FOSSASIA Summit 2026",
            slug="fossasia-2026",
            live=True,
            date_from=now(),
            plugins="veditor",
        )
        if hasattr(event, "settings"):
            event.settings.set("veditor_api_key", "test-veditor-api-key-12345")
            event.settings.set("veditor_api_base_url", "http://localhost:8080")
            event.settings.set("mail_from", "summit@fossasia.org")
        return event


@pytest.fixture
def submission_type(db, configured_event):
    """Create a default talk submission type for the event."""
    with scopes_disabled():
        return SubmissionType.objects.create(event=configured_event, name="Talk")


@pytest.fixture
def speaker_user(db):
    """Create a test speaker user."""
    return User.objects.create_user(
        email="speaker@example.com",
        password="secretpassword",
        fullname="Jane Speaker",
    )


@pytest.fixture
def co_speaker_user(db):
    """Create a second test speaker user."""
    return User.objects.create_user(
        email="cospeaker@example.com",
        password="secretpassword",
        fullname="Alex CoSpeaker",
    )


@pytest.fixture
def submission(db, configured_event, submission_type, speaker_user):
    """Create a test talk submission with a registered speaker."""
    with scopes_disabled():
        sub = Submission.objects.create(
            event=configured_event,
            submission_type=submission_type,
            code="TALK-101",
            title="Keynote: Open Source AI Studio",
        )
        sub.speakers.add(speaker_user)
        return sub


# ============================================================================
# process_talk_approved Unit & Integration Tests
# ============================================================================


@pytest.mark.django_db
def test_process_talk_approved_success_single_speaker(configured_event, submission, speaker_user):
    """Verify successful JWT minting and email dispatch to a single speaker."""
    mail.outbox.clear()

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "jwt-magic-token-xyz"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id=42,
            external_id="TALK-101",
        )

    assert result["status"] == "success"
    assert result["event_id"] == configured_event.id
    assert result["talk_id"] == "42"
    assert result["external_id"] == "TALK-101"
    assert result["sent_count"] == 1
    assert result["recipients"] == [speaker_user.email]
    assert result["failed"] == []

    mock_jwt.assert_called_once_with(
        event_id=str(configured_event.id),
        talk_id="42",
        role="speaker",
        email=speaker_user.email,
        display_name="Jane Speaker",
    )

    # Inspect dispatched email in Django test outbox
    assert len(mail.outbox) == 1
    sent_msg = mail.outbox[0]
    assert sent_msg.to == [speaker_user.email]
    assert configured_event.name in sent_msg.subject
    assert "Keynote: Open Source AI Studio" in sent_msg.subject
    assert sent_msg.from_email == "summit@fossasia.org"

    # Verify plain text body contains magic link and greeting
    assert "Jane Speaker" in sent_msg.body
    assert "http://localhost:8080/studio/talks/42?sso_token=jwt-magic-token-xyz" in sent_msg.body

    # Verify HTML body contains magic link and styled CTA button
    assert len(sent_msg.alternatives) == 1
    html_content, mime_type = sent_msg.alternatives[0]
    assert mime_type == "text/html"
    assert "Review Your Video in VEditor Studio" in html_content
    assert "http://localhost:8080/studio/talks/42?sso_token=jwt-magic-token-xyz" in html_content


@pytest.mark.django_db
def test_process_talk_approved_multiple_speakers(configured_event, submission, speaker_user, co_speaker_user):
    """Verify email dispatch to all co-speakers of a talk."""
    with scopes_disabled():
        submission.speakers.add(co_speaker_user)
    mail.outbox.clear()

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "token-multi-123"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="77",
            external_id="TALK-101",
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 2
    assert set(result["recipients"]) == {speaker_user.email, co_speaker_user.email}
    assert len(mail.outbox) == 2
    assert mock_jwt.call_count == 2


@pytest.mark.django_db
def test_process_talk_approved_no_speakers(configured_event, submission_type):
    """Verify task skips gracefully when a submission has no speakers registered."""
    with scopes_disabled():
        Submission.objects.create(
            event=configured_event,
            submission_type=submission_type,
            code="TALK-999",
            title="Unassigned Panel",
        )
    mail.outbox.clear()

    result = process_talk_approved(
        event_id=configured_event.id,
        talk_id="999",
        external_id="TALK-999",
    )

    assert result["status"] == "skipped"
    assert result["sent_count"] == 0
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_process_talk_approved_submission_not_found(configured_event):
    """Verify error status when external_id and talk_id cannot be resolved to any talk."""
    mail.outbox.clear()

    result = process_talk_approved(
        event_id=configured_event.id,
        talk_id="555",
        external_id="NONEXISTENT-CODE",
    )

    assert result["status"] == "error"
    assert result["error"] == "Submission not found"
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_process_talk_approved_unconfigured_veditor_client(submission):
    """Verify VEditorConfigError is raised when event has no VEditor credentials configured."""
    with scopes_disabled():
        organizer = Organizer.objects.create(name="Bare Org", slug="bare-org")
        bare_event = Event.objects.create(
            organizer=organizer,
            name="Bare Event",
            slug="bare-event",
            live=True,
            date_from=now(),
        )
        bare_sub_type = SubmissionType.objects.create(event=bare_event, name="Talk")
        submission.event = bare_event
        submission.submission_type = bare_sub_type
        submission.save()

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(VEditorConfigError, match="not configured"):
            process_talk_approved(
                event_id=bare_event.id,
                talk_id="12",
                external_id=submission.code,
            )


@pytest.mark.django_db
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


@pytest.mark.django_db
def test_process_talk_approved_partial_failure_logs_and_continues(configured_event, submission, speaker_user, co_speaker_user):
    """Verify partial failure (one speaker raises VEditorError) allows other speakers to succeed."""
    with scopes_disabled():
        submission.speakers.add(co_speaker_user)
    mail.outbox.clear()

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
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_process_talk_approved_resolves_by_talk_slot_id(configured_event, submission, speaker_user):
    """Verify lookup resolves correctly when external_id points to a TalkSlot ID."""
    from eventyay.base.models import Schedule

    with scopes_disabled():
        schedule = Schedule.objects.create(event=configured_event)
        slot = TalkSlot.objects.create(
            submission=submission,
            schedule=schedule,
            start=now(),
            end=now(),
        )
    mail.outbox.clear()

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "token-by-slot-id"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id=str(slot.id),
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_process_talk_approved_resolves_by_talk_id_when_no_external_id(configured_event, submission, speaker_user):
    """Verify fallback lookup when external_id is omitted and talk_id matches submission code."""
    mail.outbox.clear()

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "token-fallback"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id=submission.code,
            external_id=None,
        )

    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_process_talk_approved_speaker_without_email_skipped(configured_event, submission, db):
    """Verify speakers with null or empty emails are skipped without breaking dispatch."""
    with scopes_disabled():
        no_email_user = User.objects.create(
            email="",
            fullname="Anonymous Speaker",
        )
        submission.speakers.add(no_email_user)
    mail.outbox.clear()

    with patch("veditor.tasks.VEditorClient.request_sso_jwt") as mock_jwt:
        mock_jwt.return_value = "token-valid"

        result = process_talk_approved(
            event_id=configured_event.id,
            talk_id="42",
            external_id=submission.code,
        )

    # Only the speaker with valid email was sent to
    assert result["status"] == "success"
    assert result["sent_count"] == 1
    assert len(mail.outbox) == 1


# ============================================================================
# Manual Organizer Resend Action Tests
# ============================================================================


@pytest.mark.django_db
def test_manual_resend_speaker_link_view_success(configured_event, submission, rf):
    """Verify organizer can manually trigger review link dispatch from ConnectView."""
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
    )


@pytest.mark.django_db
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
