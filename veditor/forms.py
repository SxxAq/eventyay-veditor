"""Forms for VEditor integration settings."""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _


class VEditorSettingsForm(forms.Form):
    """Form to configure per-event VEditor connection credentials."""

    veditor_api_base_url = forms.URLField(
        label=_("VEditor Service URL"),
        required=True,
        initial="http://localhost:8080",
        widget=forms.URLInput(attrs={"class": "form-control", "placeholder": "http://localhost:8080"}),
        help_text=_("URL where the VEditor service is running."),
    )
    veditor_api_key = forms.CharField(
        label=_("VEditor API Key"),
        required=True,
        widget=forms.PasswordInput(
            render_value=True,
            attrs={"class": "form-control", "placeholder": _("API key generated in VEditor")},
        ),
        help_text=_("The client API key generated in VEditor for this event."),
    )
    veditor_event_id = forms.IntegerField(
        label=_("VEditor Event ID"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": _("Optional: Leave blank to use Eventyay event ID")}),
        help_text=_("Optional: Specify if the event has a different ID in VEditor."),
    )

    def clean_veditor_api_base_url(self) -> str:
        """Validate base URL scheme and optional origin allowlist."""
        from urllib.parse import urlparse

        from django.conf import settings

        url = self.cleaned_data.get("veditor_api_base_url", "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise forms.ValidationError(_("Invalid URL. Must begin with http:// or https://."))

        allowed = getattr(settings, "VEDITOR_ALLOWED_ORIGINS", None)
        if allowed:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in allowed:
                raise forms.ValidationError(_("The VEditor URL origin is not in the allowed list."))

        return url.rstrip("/")
