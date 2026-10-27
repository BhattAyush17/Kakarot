"""Gemini credentials use the shared keyring and encrypted fallback store.

The daemon validates the credential before it opens audio.  A syntactically
non-empty string is not enough: AI Studio now issues both ``AIza`` and ``AQ.``
keys, and Google rejects an expired, restricted, or service-account-bound key
with a 401/403.  Surfacing that before the microphone is opened prevents the
old failure mode where the UI showed "listening" but every request was dead.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.error
import urllib.request

from core.secret_store import get_secret, set_secret

GEMINI_API_KEY_SECRET = "gemini_api_key"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"
VERIFY_TIMEOUT_SECONDS = 8.0


class GeminiCredentialError(RuntimeError):
    """A Gemini credential was missing, unreadable, or rejected by Google."""


def get_gemini_api_key() -> str:
    """Return the first configured Gemini credential, without logging it."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return get_secret(GEMINI_API_KEY_SECRET)


def set_gemini_api_key(api_key: str) -> str:
    value = str(api_key or "").strip()
    if not value:
        raise ValueError("an API key is required")
    return set_secret(GEMINI_API_KEY_SECRET, value)


def _http_error_message(code: int) -> str:
    return {
        400: "Gemini rejected the request (HTTP 400). Check the key format.",
        401: "Gemini rejected this API key (HTTP 401 Unauthorized). Create a new key in Google AI Studio.",
        403: "Gemini denied this API key (HTTP 403 Forbidden). Check API access and billing restrictions.",
        404: "Gemini returned HTTP 404 for the models endpoint. Check the API configuration.",
        429: "Gemini rate limited the credential check (HTTP 429). Try again shortly.",
    }.get(code, f"Gemini returned HTTP {code}.")


def verify_gemini_api_key(
    api_key: str,
    timeout: float = VERIFY_TIMEOUT_SECONDS,
    opener: object | None = None,
) -> None:
    """Probe the models endpoint and raise an actionable error if rejected.

    The key is sent only in the ``x-goog-api-key`` header.  It is never placed
    in the URL, an exception message, or the logs.  ``opener`` exists so the
    front-end's compatibility tests can patch a module-local HTTP function
    without duplicating the validation logic.
    """
    value = str(api_key or "").strip()
    if not value:
        raise GeminiCredentialError(
            "No Gemini API key is configured. Open Settings or run "
            "`python -m kakarot_daemon.daemon configure`."
        )
    request = urllib.request.Request(MODELS_URL, headers={"x-goog-api-key": value})
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536))
        if not isinstance(payload, dict) or not payload.get("models"):
            raise GeminiCredentialError(
                "Gemini accepted the connection but returned no models for this key."
            )
    except urllib.error.HTTPError as exc:
        # Do not echo the response body: Google sometimes includes request
        # metadata, and the key must never be copied into an error string.
        raise GeminiCredentialError(_http_error_message(exc.code)) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            raise GeminiCredentialError(
                f"DNS lookup failed for generativelanguage.googleapis.com: {reason.strerror or reason}."
            ) from None
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise GeminiCredentialError("Gemini request timed out after 8 seconds.") from None
        if isinstance(reason, ssl.SSLError):
            detail = getattr(reason, "reason", None) or reason
            raise GeminiCredentialError(f"TLS connection to Gemini failed: {detail}.") from None
        raise GeminiCredentialError(f"Could not connect to Gemini: {reason}.") from None
    except (TimeoutError, socket.timeout):
        raise GeminiCredentialError("Gemini request timed out after 8 seconds.") from None
    except ssl.SSLError as exc:
        detail = getattr(exc, "reason", None) or exc
        raise GeminiCredentialError(f"TLS connection to Gemini failed: {detail}.") from None
    except (ValueError, json.JSONDecodeError) as exc:
        raise GeminiCredentialError(f"Gemini returned invalid JSON: {exc}") from exc

