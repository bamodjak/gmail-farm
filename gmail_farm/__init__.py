"""gmail-farm — create Google (Gmail) accounts over HTTP, in a loop.

General-purpose, reusable, no hardcoded personal details. All settings
come from CLI arguments or environment variables (see config.py).

Layout (one job per module):
    config.py        CLI/env config + validation
    identities.py    random identity generation (name, username, password, DOB, gender)
    http_client.py   curl_cffi session: browser impersonation, retries, cookies, proxy
    signup.py        Google signup flow driver (bootstrap + steps + stage detection)
    browser.py       Browser fallback (Selenium + Chrome, anti-detection,
                     phone-skip) — used when the HTTP RPC route is blocked
    storage.py       resume-safe JSONL account store
    logging_setup.py logging to console + file
    gmail_farm.py    CLI entrypoint (thin wrapper)

Honest status (2026-11-22, verified by research + a live probe):
    * There is NO public API to create consumer @gmail.com accounts.
      The Gmail API and Identity Platform signUp do not register
      @gmail.com addresses. The only path is the web signup flow.
    * That flow is Google's proprietary "lifecycle" JS app
      (AccountLifecyclePlatformSignupUi) at accounts.google.com.
      Steps are submitted as undocumented gRPC-web RPCs (f.req format),
      the page embeds a reCAPTCHA site key, and the flow ends at phone
      verification (SMS / QR / same-device) — and can also require a
      selfie check. There is no way to complete it over HTTP without a
      phone number, and this tool never enters one.
    * So this tool implements the flow as far as HTTP allows: session
      bootstrap (proven to work), step submission attempts, and precise
      detection/reporting of the stage reached (phone verification,
      CAPTCHA, selfie, blocked, or created). It stops cleanly instead
      of pretending to succeed.

Run:  python3 gmail_farm.py --count 5
See README.md for full usage.
"""

__version__ = "1.0.0"
