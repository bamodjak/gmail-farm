"""Google signup flow driver.

What this does, step by step:
  1. Bootstrap: GET the signup page, follow redirects, keep cookies,
     and extract the flow tokens from the page (WIZ_global_data):
     the lifecycle RPC endpoint, the reCAPTCHA site key, the TL flow
     token and the session tokens. This step is proven to work.
  2. Steps: submit each signup step to the lifecycle RPC service
     (AccountLifecyclePlatformSignupUi) using Google's WIZ
     "batchexecute" format (POST .../data/batchexecute with
     f.req=[[["<rpcId>","<json>",null,"generic"]]]). Steps in order:
     name -> username -> password -> birthday/gender -> phone
     verification -> terms. The name step uses the first of the 8
     tM7Xoe candidate method ids from the page (unverified — see
     the note below).
  3. Stage detection: after every response, classify where the flow
     is — created / phone_verification / captcha / selfie / blocked /
     username_taken / error — from the response body and status, and
     stop cleanly at any verification step. This module NEVER enters
     a phone number and NEVER attempts to solve a CAPTCHA.

Why the RPC layer stops where it does (read this before assuming it works):
  The RPC endpoint and wire format ARE now known (reverse-engineered
  2026-11-22, see RPC-REVERSE-2026-11-22.md): the service is a Google
  WIZ "batchexecute" RPC at
  .../AccountLifecyclePlatformSignupUi/data/batchexecute, with
  f.req=[[["<rpcId>","<json>",null,"generic"]]] and at=<SNlM0e>.
  The XSRF token (SNlM0e) is accepted — the server's own error
  responses prove it (no at -> 400 XSRF; with at -> past that check).
  But the call then returns 401 UNAUTHENTICATED (gRPC code 16) for
  ANY rpcId/payload: the service requires a BotGuard attestation
  produced by the bscframe JS challenge running in a real browser.
  Plain HTTP cannot produce it (the bscframe returns an empty shell,
  the page sets no cookies, and the challenge needs JS execution).
  So the HTTP path is gated at the attestation layer, not at the
  method-name layer. The rpcId used here is the first of the 8
  tM7Xoe candidate method ids from the page — a candidate, not a
  verified method, because no method id can be confirmed live until
  the attestation is satisfied.

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
# (window.WIZ_global_data: Im6cmf / eptZe). The service is a Google
# WIZ "batchexecute" RPC: the method is NOT a URL path segment (that
# 404s) — it is an rpcId inside a triple-nested f.req posted to the
# .../data/batchexecute endpoint. See RPC-REVERSE-2026-11-22.md.
RPC_BASE_PATH = "/lifecycle/_/AccountLifecyclePlatformSignupUi"
RPC_PATH = RPC_BASE_PATH + "/data/batchexecute"


@dataclass
class FlowState:
    """Per-attempt session + extracted flow tokens."""

    cookies: dict[str, str] = field(default_factory=dict)
    rpc_endpoint: str = ""          # full URL of the lifecycle service
    rpc_batchexecute_url: str = ""  # .../data/batchexecute (the RPC endpoint)
    recaptcha_site_key: str = ""    # reCAPTCHA site key embedded in the page
    tl_token: str = ""              # TL flow token (URL-decoded)
    session_token: str = ""         # SNlM0e token from WIZ_global_data
    f_sid: str = ""                 # FdrFJe (batchexecute f.sid query param)
    bl_token: str = ""              # cfb2h (batchexecute bl query param)
    soc_app: str = ""               # MUE6Ne (batchexecute soc-app query param)
    rpc_ids: list[str] = field(default_factory=list)  # tM7Xoe: 8 candidate
    # method ids (as strings) for the signup steps. The name step is
    # CANDIDATE rpc_ids[0] — unverified, because the RPC is gated by a
    # BotGuard attestation that plain HTTP cannot produce (see
    # RPC-REVERSE-2026-11-22.md), so no method id can be confirmed live.
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

        # RPC endpoint: Im6cmf in WIZ_global_data (no trailing slash).
        m = re.search(r'"Im6cmf"\s*:\s*"([^"]+)"', body)
        if m:
            path = m.group(1)
            state.rpc_endpoint = "https://accounts.google.com" + path.rstrip("/") + "/"
        else:
            state.rpc_endpoint = "https://accounts.google.com" + RPC_BASE_PATH + "/"
        # The actual RPC endpoint is the batchexecute path under it.
        state.rpc_batchexecute_url = state.rpc_endpoint + "data/batchexecute"

        # Batchexecute query params from WIZ_global_data.
        m = re.search(r'"FdrFJe"\s*:\s*"([^"]+)"', body)
        if m:
            state.f_sid = m.group(1)
        m = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', body)
        if m:
            state.bl_token = m.group(1)
        m = re.search(r'"MUE6Ne"\s*:\s*"([^"]+)"', body)
        if m:
            state.soc_app = m.group(1)
        # Candidate RPC method ids: tM7Xoe is a list of 8 numbers in
        # WIZ_global_data (one per signup step, order unverified).
        m = re.search(r'"tM7Xoe"\s*:\s*\[([0-9,\s]+)\]', body)
        if m:
            state.rpc_ids = [s.strip() for s in m.group(1).split(",") if s.strip()]

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

        log.info("bootstrap ok: rpc=%s recaptcha=%s tl=%s rpc_ids=%d",
                 state.rpc_endpoint,
                 bool(state.recaptcha_site_key),
                 bool(state.tl_token),
                 len(state.rpc_ids))
        return state

    # -- stage detection --------------------------------------------------
    @staticmethod
    def detect_stage(body: str, status: int) -> tuple[str, str]:
        """Classify a response into a stage + detail string."""
        if status in (429,):
            return "blocked", f"HTTP {status} (rate limited)"
        if status >= 500:
            return "error", f"HTTP {status} (server error)"
        if status == 401:
            # The batchexecute endpoint returns 401 UNAUTHENTICATED
            # (gRPC code 16) when the XSRF token passes but the
            # BotGuard attestation is missing. Plain HTTP cannot
            # produce that attestation (it needs the bscframe JS
            # challenge to run in a real browser), so this is the
            # terminal gate for the HTTP path.
            return "blocked", (
                "HTTP 401 (unauthenticated — BotGuard attestation "
                "required; not producible over plain HTTP)"
            )
        if status in (400, 403):
            # A 400 on batchexecute with an "xsrf" error means the at
            # token was missing/rejected; a 400 without it is a bad
            # request (wrong rpcId / payload shape).
            if "xsrf" in (body or "").lower():
                return "blocked", "HTTP 400 (XSRF token rejected)"
            return "blocked", f"HTTP {status}"
        if status == 404:
            # A 404 on the lifecycle RPC means the method path does not
            # exist — the step call was rejected at the routing layer.
            return "rpc_rejected", "HTTP 404 (RPC method path not found)"
        text = (body or "").lower()
        # A successful batchexecute call returns a )]}' chunked body
        # with a "wrb.fr" result frame for our rpcId.
        if "wrb.fr" in text:
            for stage, markers in _STAGE_MARKERS.items():
                for marker in markers:
                    if marker in text:
                        return stage, f"marker: {marker!r}"
            return "unknown", (
                f"HTTP {status}, RPC accepted (wrb.fr) but no known "
                f"stage marker in the result"
            )
        for stage, markers in _STAGE_MARKERS.items():
            for marker in markers:
                if marker in text:
                    return stage, f"marker: {marker!r}"
        if not body:
            return "error", "empty response"
        return "unknown", f"HTTP {status}, no known stage marker"

    # -- RPC --------------------------------------------------------------
    def _rpc(self, state: FlowState, rpc_id: str, payload: dict) -> tuple[int, str]:
        """One batchexecute RPC call to the lifecycle service.

        Google WIZ "batchexecute" format (verified against the live
        service 2026-11-22, and against the public NotebookLM
        batchexecute reference):
          POST <rpc_batchexecute_url>?f.sid=..&bl=..&hl=en_US
               &soc-app=..&soc-platform=1&soc-device=1&_reqid=..&rt=c
          form: f.req=[[["<rpcId>","<json>",null,"generic"]]]
                at=<SNlM0e>  TL=<tl>
        Returns (http_status, response_text). The response is a )]}'
        length-prefixed chunk stream; a "wrb.fr" frame is a successful
        RPC result for our rpcId, an "er" frame is the error.
        """
        qs = {
            "f.sid": state.f_sid,
            "bl": state.bl_token,
            "hl": "en_US",
            "soc-app": state.soc_app,
            "soc-platform": "1",
            "soc-device": "1",
            "_reqid": "100000",
            "rt": "c",
        }
        url = state.rpc_batchexecute_url + "?" + urllib.parse.urlencode(qs)
        form = {
            "f.req": json.dumps([[[rpc_id, json.dumps(payload), None, "generic"]]]),
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

        # Step 1: name, via the batchexecute RPC. The rpcId is the
        # first of the 8 tM7Xoe candidate method ids from the page
        # (CANDIDATE — unverified, because the RPC is gated by a
        # BotGuard attestation that plain HTTP cannot produce, so no
        # method id can be confirmed live; see
        # RPC-REVERSE-2026-11-22.md). The payload is the name as a
        # JSON string, matching the batchexecute "json_params" slot.
        rpc_id = state.rpc_ids[0] if state.rpc_ids else "105698215"
        try:
            status, text = self._rpc(state, rpc_id, {
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
