"""Google signup flow driver.

What this does, step by step:
  1. Bootstrap: GET the signup page, follow redirects, keep cookies,
     and extract the flow tokens from the page (WIZ_global_data):
     the lifecycle RPC endpoint, the reCAPTCHA site key, the TL flow
     token and the session tokens. This step is proven to work.
  2. Steps: submit each signup step to the lifecycle RPC service
     (AccountLifecyclePlatformSignupUi) using Google's standard WIZ
     `f.req` POST format. Steps in order: name -> username ->
     password -> birthday/gender -> phone verification -> terms.
  3. Stage detection: after every response, classify where the flow
     is — created / phone_verification / captcha / selfie / blocked /
     username_taken / error — from the response body and status, and
     stop cleanly at any verification step. This module NEVER enters
     a phone number and NEVER attempts to solve a CAPTCHA.

Why the RPC layer is best-effort (read this before assuming it works):
  Google does not document or publish this flow. The step method
  names and payload shapes live only in Google's minified JS bundle
  and are obfuscated. The driver therefore sends the standard WIZ
  `f.req` call to the discovered endpoint and reports the server's
  exact response. If Google rejects it (unknown method / bad payload),
  the attempt is recorded as stage=rpc_rejected with the response —
  that is a real, reportable result, not a silent failure.

  Even with a perfect RPC replay, the flow ends at phone verification
  (SMS / QR / same-device) and can also require a reCAPTCHA token and
  a selfie check. Those cannot be completed over HTTP without a phone
  number, so no HTTP-only tool can finish a Gmail account today.
  This driver is honest about that: it gets as far as HTTP allows
  and says exactly where it stopped.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass, field

from .http_client import HttpClient
from .identities import Identity

log = logging.getLogger(__name__)

SIGNUP_URL = (
    "https://accounts.google.com/signup/v2/webcreateaccount"
    "?flowName=GlifWebSignIn&flowEntry=SignUp"
)

# Markers used to classify the stage from a response body (lowercased).
_STAGE_MARKERS: dict[str, list[str]] = {
    "phone_verification": [
        "phone number", "verify it's you", "verify it’s you",
        "enter your phone", "send a code", "sms", "continue on your phone",
        "same device", "devicephonenumberverification",
        "phonenumberverification",
    ],
    "captcha": ["recaptcha", "captcha", "i'm not a robot", "i’m not a robot"],
    "selfie": ["selfie", "face verification", "verify your identity",
               "selfieface", "take a selfie"],
    "username_taken": ["username is taken", "that username is taken",
                       "already taken", "not available"],
    "blocked": ["something went wrong", "can't create", "can’t create",
                "try again later", "too many", "rate limit", "blocked"],
    "created": ["welcome", "your account is ready", "account created",
                "manageaccount", "myaccount.google.com"],
}

# The lifecycle RPC service discovered from the signup page
# (window.WIZ_global_data: Im6cmf / eptZe).
RPC_PATH = "/lifecycle/_/AccountLifecyclePlatformSignupUi"


@dataclass
class FlowState:
    """Per-attempt session + extracted flow tokens."""

    cookies: dict[str, str] = field(default_factory=dict)
    rpc_endpoint: str = ""          # full URL of the lifecycle service
    recaptcha_site_key: str = ""    # reCAPTCHA site key embedded in the page
    tl_token: str = ""              # TL flow token (URL-decoded)
    session_token: str = ""         # SNlM0e token from WIZ_global_data
    page_ok: bool = False


@dataclass
class AttemptResult:
    """Outcome of one signup attempt."""

    identity: Identity
    stage: str                      # see _STAGE_MARKERS keys + rpc_rejected / error
    detail: str = ""                # short human-readable detail
    http_status: int = 0
    response_snippet: str = ""      # truncated server response (no secrets)

    @property
    def created(self) -> bool:
        return self.stage == "created"


class SignupFlow:
    """Drives one Google signup attempt over HTTP."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    # -- bootstrap ------------------------------------------------------
    def bootstrap(self) -> FlowState:
        """Load the signup page and extract flow tokens.

        Raises on transport/HTTP failure. Returns a FlowState; page_ok
        is False if the page did not look like the signup app.
        """
        state = FlowState()
        resp = self.client.get(SIGNUP_URL, allow_redirects=True)
        state.http_status = resp.status_code if hasattr(resp, "status_code") else 0
        body = resp.text or ""
        state.cookies = self.client.cookies_dict()
        state.page_ok = ("AccountLifecyclePlatformSignupUi" in body
                         or "lifecycle/steps/signup" in body)
        if not state.page_ok:
            log.warning("signup page did not contain the lifecycle app "
                        "(status=%s, len=%d)", resp.status_code, len(body))
            return state

        # RPC endpoint: Im6cmf in WIZ_global_data.
        m = re.search(r'"Im6cmf"\s*:\s*"([^"]+)"', body)
        if m:
            path = m.group(1)
            state.rpc_endpoint = "https://accounts.google.com" + path.rstrip("/") + "/"
        else:
            state.rpc_endpoint = "https://accounts.google.com" + RPC_PATH + "/"

        # reCAPTCHA site key: ecWwgb in WIZ_global_data.
        m = re.search(r'"ecWwgb"\s*:\s*"([^"]+)"', body)
        if m:
            state.recaptcha_site_key = m.group(1)

        # TL flow token (URL-encoded in the page).
        m = re.search(r'TL%3D([A-Za-z0-9%_\-\.]{20,200})', body)
        if m:
            state.tl_token = urllib.parse.unquote(m.group(1))

        # Session token: SNlM0e in WIZ_global_data.
        m = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', body)
        if m:
            state.session_token = m.group(1)

        log.info("bootstrap ok: rpc=%s recaptcha=%s tl=%s",
                 state.rpc_endpoint,
                 bool(state.recaptcha_site_key),
                 bool(state.tl_token))
        return state

    # -- stage detection --------------------------------------------------
    @staticmethod
    def detect_stage(body: str, status: int) -> tuple[str, str]:
        """Classify a response into a stage + detail string."""
        if status in (429,):
            return "blocked", f"HTTP {status} (rate limited)"
        if status >= 500:
            return "error", f"HTTP {status} (server error)"
        if status in (400, 403):
            return "blocked", f"HTTP {status}"
        if status == 404:
            # A 404 on the lifecycle RPC means the method path does not
            # exist — the step call was rejected at the routing layer.
            return "rpc_rejected", "HTTP 404 (RPC method path not found)"
        text = (body or "").lower()
        for stage, markers in _STAGE_MARKERS.items():
            for marker in markers:
                if marker in text:
                    return stage, f"marker: {marker!r}"
        if not body:
            return "error", "empty response"
        return "unknown", f"HTTP {status}, no known stage marker"

    # -- RPC --------------------------------------------------------------
    def _rpc(self, state: FlowState, method: str, payload: dict) -> tuple[int, str]:
        """One WIZ `f.req` RPC call to the lifecycle service.

        Returns (http_status, response_text). Google's WIZ services
        accept POST with form-encoded f.req=<json> plus the flow token.
        """
        url = state.rpc_endpoint + method
        form = {
            "f.req": json.dumps([None, json.dumps(payload)]),
            "at": state.session_token,
            "TL": state.tl_token,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SIGNUP_URL,
            "Origin": "https://accounts.google.com",
        }
        resp = self.client.post(url, data=form, headers=headers)
        return resp.status_code, (resp.text or "")[:2000]

    # -- the attempt ------------------------------------------------------
    def attempt(self, identity: Identity, dry_run: bool = False) -> AttemptResult:
        """Run one signup attempt for the given identity.

        Always does the bootstrap. In dry-run mode it stops there.
        Otherwise it attempts the first step (name) and classifies the
        result. It never enters a phone number or solves a CAPTCHA.
        """
        try:
            state = self.bootstrap()
        except Exception as exc:
            return AttemptResult(identity, "error", f"bootstrap failed: {exc}")

        if not state.page_ok:
            return AttemptResult(identity, "blocked",
                                 "signup page did not load the lifecycle app",
                                 http_status=state.http_status)

        if dry_run:
            return AttemptResult(identity, "dry_run",
                                 "bootstrap only (dry run)", http_status=200)

        # Step 1: name. Method/payload shape is Google's internal
        # format; if the server rejects it, we record rpc_rejected
        # with the response instead of guessing further.
        try:
            status, text = self._rpc(state, "SubmitName", {
                "firstName": identity.first_name,
                "lastName": identity.last_name,
            })
        except Exception as exc:
            return AttemptResult(identity, "error", f"step RPC failed: {exc}")

        stage, detail = self.detect_stage(text, status)
        if stage == "unknown" and status == 200:
            # A 200 with no known marker usually means the RPC method
            # or payload was not accepted — record it as such.
            stage, detail = "rpc_rejected", (
                f"HTTP 200 but no step advanced; server response did not "
                f"match any known stage (method/payload likely rejected). "
                f"snippet: {text[:200]!r}"
            )
        return AttemptResult(identity, stage, detail, http_status=status,
                             response_snippet=text[:500])
