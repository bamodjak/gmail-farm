"""Configuration: CLI arguments + environment variables + validation.

No personal details are hardcoded anywhere. Every setting has a sane
default and can be overridden with a flag or an env var.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    """All runtime settings for a gmail-farm run."""

    # How many accounts to attempt in this run (0 = unlimited loop).
    count: int = 5
    # Seconds to wait between attempts (pacing; Google rate-limits signups).
    delay_seconds: float = 60.0
    # Max retries per HTTP request (with exponential backoff).
    max_retries: int = 3
    # Base backoff in seconds (doubled on each retry).
    backoff_base: float = 2.0
    # Where created/attempted accounts are recorded (JSONL, append-only).
    out_file: str = "accounts.jsonl"
    # Where logs go (in addition to the console).
    log_file: str = "gmail-farm.log"
    # Optional HTTP(S) proxy URL, e.g. http://127.0.0.1:8080 (empty = direct).
    proxy: str = ""
    # Browser TLS fingerprint to impersonate (curl_cffi impersonation name).
    impersonate: str = "chrome131_android"
    # Password length for generated accounts (Google requires >= 8).
    password_length: int = 14
    # Earliest / latest birth year for generated adult identities.
    birth_year_min: int = 1985
    birth_year_max: int = 2005
    # Request timeout in seconds.
    timeout: float = 30.0
    # Stop the whole run after this many consecutive failures (0 = never).
    max_consecutive_failures: int = 5
    # Dry run: generate identities and do the session bootstrap only,
    # never submit a signup step.
    dry_run: bool = False
    # Verbose logging.
    verbose: bool = False
    # Browser route: use a real Chrome browser (Selenium) instead of
    # plain HTTP. Needed because Google's signup now requires a
    # BotGuard attestation that only a real browser can produce.
    browser: bool = False
    # The user's OWN phone number (E.164, e.g. +15551234567), used
    # only with --browser when Google asks for phone verification.
    # The tool never pays for a number: the SMS goes to the user's
    # own phone and the user types the code at the console prompt.
    # Empty = try to skip the phone step; if the skip fails the
    # attempt is abandoned as stage=phone_verification.
    phone: str = ""
    # Headed browser (show the Chrome window). Default is headless:
    # a farm runs on servers with no display. Needs a display.
    headed: bool = False
    # QR pause (browser route only): when Google shows the QR-code
    # verification wall, pause the attempt, save the QR code to a
    # file, and wait for the user to scan it with their own phone
    # and press Enter. Default on: without it the QR wall abandons
    # the attempt. Off = abandon immediately as stage
    # phone_verification (for non-interactive runs).
    qr_pause: bool = True
    # True when the user set --qr-pause / --no-qr-pause explicitly
    # (used only for validation; the value itself lives in qr_pause).
    _qr_pause_set: bool = False
    # QR downgrade (browser route only): at Google's QR / samedevice
    # verification wall, first try the google-qr-sms-helper downgrade
    # (DevicePhoneVerification MypxBd/YDtwf RPCs -> [3,null,4]
    # fallback -> SMS/voice selection page) before pausing for a QR
    # scan. It enters no phone number and pays nothing — it only
    # changes which verification Google offers. Default on.
    qr_downgrade: bool = True
    # True when the user set --qr-downgrade / --no-qr-downgrade
    # explicitly (used only for validation).
    _qr_downgrade_set: bool = False
    # Device profile for the browser route: 'desktop' (default, a
    # random desktop Chrome UA) or 'ipad-mini' (the iPad Mini spoof
    # from google-qr-sms-helper: iPad UA, no Sec-CH-UA headers,
    # navigator aligned to iPad, touch viewport) or 'android-phone'
    # (Pixel 8 / Android 14 Chrome UA, Android-consistent Sec-CH-UA
    # metadata, touch viewport). Google may offer SMS/voice
    # verification to a mobile device instead of the QR wall.
    # NOTE (verified live 2026-11-22, after the proxy fix): the
    # iPad Mini spoof does NOT change the signup verification page
    # — Google still shows the QR wall. Android variant untested.
    device: str = "desktop"
    # Bulk path (browser route only): alternative signup entry point
    # ('direct' default, or 'youtube' / 'drive' / 'photos' /
    # 'cloud-console' / 'play' / 'aistudio') — start the signup
    # from a Google service instead of the direct URL (ytgm).
    # 'direct' is the plain signup URL.
    entry_point: str = "direct"
    # Persistent Chrome profile directory (browser route only):
    # when set, cookies/history survive across attempts so the
    # profile warms up over time (ytgm aging) instead of starting
    # fresh every time. Unset = a fresh temporary profile.
    profile_dir: str = ""
    # Deep warm-up (browser route only): before the signup, run a
    # ytgm-style warm-up pass (Google searches, YouTube, Maps, News)
    # in the persistent profile. Only meaningful with --profile-dir.
    warm: bool = False

    def validate(self) -> None:
        """Raise ValueError on any invalid setting."""
        if self.count < 0:
            raise ValueError("--count must be >= 0")
        if self.delay_seconds < 0:
            raise ValueError("--delay must be >= 0")
        if self.max_retries < 0:
            raise ValueError("--max-retries must be >= 0")
        if self.password_length < 8 or self.password_length > 100:
            raise ValueError("--password-length must be between 8 and 100")
        if not (1900 <= self.birth_year_min <= self.birth_year_max <= 2010):
            raise ValueError("birth years must satisfy 1900 <= min <= max <= 2010")
        if self.timeout <= 0:
            raise ValueError("--timeout must be > 0")
        if self.max_consecutive_failures < 0:
            raise ValueError("--max-consecutive-failures must be >= 0")
        if self.proxy and not re.match(r"^https?://", self.proxy):
            raise ValueError("--proxy must be an http(s) URL, e.g. http://127.0.0.1:8080")
        if not self.out_file:
            raise ValueError("--out must not be empty")
        if not self.log_file:
            raise ValueError("--log-file must not be empty")
        if self.phone and not re.match(r"^\+\d{7,15}$", self.phone):
            raise ValueError("--phone must be in E.164 format, e.g. +15551234567")
        if self.phone and not self.browser:
            raise ValueError("--phone only applies with --browser")
        if self._qr_pause_set and not self.browser:
            raise ValueError("--qr-pause / --no-qr-pause only applies with --browser")
        if self._qr_downgrade_set and not self.browser:
            raise ValueError("--qr-downgrade / --no-qr-downgrade only applies with --browser")
        if self.device not in ("desktop", "ipad-mini", "android-phone"):
            raise ValueError("--device must be 'desktop', 'ipad-mini' "
                             "or 'android-phone'")
        if self.device != "desktop" and not self.browser:
            raise ValueError("--device only applies with --browser")
        if self.entry_point not in ("direct", "youtube", "drive", "photos",
                                   "cloud-console", "play", "aistudio"):
            raise ValueError("--entry-point must be direct, youtube, drive, "
                             "photos, cloud-console, play or aistudio")
        if self.entry_point != "direct" and not self.browser:
            raise ValueError("--entry-point only applies with --browser")
        if self.profile_dir and not self.browser:
            raise ValueError("--profile-dir only applies with --browser")
        if self.warm and not self.browser:
            raise ValueError("--warm only applies with --browser")


def build_config(argv: list[str] | None = None) -> Config:
    """Parse CLI args (env vars fill in defaults) and validate."""
    p = argparse.ArgumentParser(
        prog="gmail-farm",
        description=(
            "Create Google (Gmail) accounts in a loop. Plain HTTP is "
            "blocked by Google's BotGuard attestation, so use --browser "
            "for a real Chrome session. Stops cleanly at phone "
            "verification / CAPTCHA / blocks — with --phone it uses YOUR "
            "own number and you type the SMS code at the prompt; it "
            "never pays for a number and never solves a CAPTCHA."
        ),
    )
    p.add_argument("--count", type=int, default=_env_int("GMAIL_FARM_COUNT", 5),
                   help="accounts to attempt (0 = loop forever) [default: 5]")
    p.add_argument("--delay", type=float, default=_env_float("GMAIL_FARM_DELAY", 60.0),
                   help="seconds between attempts [default: 60]")
    p.add_argument("--max-retries", type=int, default=_env_int("GMAIL_FARM_MAX_RETRIES", 3),
                   help="retries per HTTP request [default: 3]")
    p.add_argument("--backoff-base", type=float, default=_env_float("GMAIL_FARM_BACKOFF", 2.0),
                   help="base backoff seconds [default: 2]")
    p.add_argument("--out", default=os.environ.get("GMAIL_FARM_OUT", "accounts.jsonl"),
                   help="JSONL accounts file [default: accounts.jsonl]")
    p.add_argument("--log-file", default=os.environ.get("GMAIL_FARM_LOG", "gmail-farm.log"),
                   help="log file [default: gmail-farm.log]")
    p.add_argument("--proxy", default=os.environ.get("GMAIL_FARM_PROXY", ""),
                   help="optional http(s) proxy URL")
    p.add_argument("--impersonate", default=os.environ.get("GMAIL_FARM_IMPERSONATE", "chrome131_android"),
                   help="curl_cffi browser impersonation [default: chrome131_android]")
    p.add_argument("--password-length", type=int, default=_env_int("GMAIL_FARM_PASSWORD_LENGTH", 14),
                   help="generated password length [default: 14]")
    p.add_argument("--birth-year-min", type=int, default=_env_int("GMAIL_FARM_BIRTH_MIN", 1985),
                   help="earliest birth year [default: 1985]")
    p.add_argument("--birth-year-max", type=int, default=_env_int("GMAIL_FARM_BIRTH_MAX", 2005),
                   help="latest birth year [default: 2005]")
    p.add_argument("--timeout", type=float, default=_env_float("GMAIL_FARM_TIMEOUT", 30.0),
                   help="HTTP timeout seconds [default: 30]")
    p.add_argument("--max-consecutive-failures", type=int,
                   default=_env_int("GMAIL_FARM_MAX_CONSEC_FAILURES", 5),
                   help="stop after N consecutive failures, 0 = never [default: 5]")
    p.add_argument("--dry-run", action="store_true",
                   help="bootstrap only; never submit a signup step")
    p.add_argument("--verbose", action="store_true", help="verbose logging")
    p.add_argument("--browser", action="store_true",
                   help="use a real Chrome browser (Selenium) instead of "
                   "plain HTTP; needed because Google's signup requires a "
                   "BotGuard attestation only a real browser can produce")
    p.add_argument("--phone", default=os.environ.get("GMAIL_FARM_PHONE", ""),
                   help="your OWN phone number in E.164 format "
                   "(e.g. +15551234567), used only with --browser when "
                   "Google asks for phone verification; the SMS goes to "
                   "your phone and you type the code at the prompt")
    p.add_argument("--headed", action="store_true",
                   help="show the Chrome window (needs a display); "
                   "default is headless")
    p.add_argument("--qr-pause", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="pause at Google's QR-verification wall: save "
                   "the QR code to a file and wait for you to scan it "
                   "with your phone and press Enter (browser route "
                   "only) [default: on]")
    p.add_argument("--qr-downgrade", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="at Google's QR-verification wall, first try "
                   "the google-qr-sms-helper downgrade (DevicePhone- "
                   "Verification RPCs -> SMS/voice selection page) "
                   "before pausing for a QR scan (browser route "
                   "only) [default: on]")
    p.add_argument("--device", choices=["desktop", "ipad-mini", "android-phone"],
                   default=os.environ.get("GMAIL_FARM_DEVICE", "desktop"),
                   help="browser device profile (browser route only): "
                   "desktop (default), ipad-mini — the iPad Mini "
                   "spoof from google-qr-sms-helper (NOTE: verified "
                   "live 2026-11-22, after the proxy fix, it does "
                   "NOT change the signup verification page — "
                   "Google still shows the QR wall), or "
                   "android-phone — Pixel 8 / Android 14 Chrome "
                   "[default: desktop]")
    p.add_argument("--entry-point",
                   choices=["direct", "youtube", "drive", "photos",
                            "cloud-console", "play", "aistudio"],
                   default=os.environ.get("GMAIL_FARM_ENTRY_POINT", "direct"),
                   help="browser signup entry point (browser route "
                   "only): start from a Google service instead of the "
                   "direct signup URL — youtube, drive or photos "
                   "(ytgm), cloud-console (the google-qr-sms-helper "
                   "entry point, untested for signup), play (Play "
                   "Store web), aistudio (AI Studio) [default: direct]")
    p.add_argument("--profile-dir", default=os.environ.get("GMAIL_FARM_PROFILE_DIR", ""),
                   help="persistent Chrome profile directory (browser "
                   "route only): cookies/history survive across "
                   "attempts so the profile warms up over time "
                   "(ytgm aging); unset = a fresh profile per attempt")
    p.add_argument("--warm", action="store_true",
                   help="deep warm-up pass before the signup (browser "
                   "route only): Google searches, YouTube, Maps, News "
                   "in the persistent profile (ytgm); only meaningful "
                   "with --profile-dir")
    p.add_argument("--version", action="version", version="gmail-farm 1.0.0")

    args = p.parse_args(argv)
    # --qr-pause / --no-qr-pause: None when not given (env or default
    # applies); True/False when given explicitly.
    qr_pause_env = os.environ.get("GMAIL_FARM_QR_PAUSE", "")
    if args.qr_pause is not None:
        qr_pause = args.qr_pause
        qr_pause_set = True
    elif qr_pause_env.lower() in ("0", "false", "no", "off"):
        qr_pause = False
        qr_pause_set = True
    elif qr_pause_env.lower() in ("1", "true", "yes", "on"):
        qr_pause = True
        qr_pause_set = True
    else:
        qr_pause = True
        qr_pause_set = False
    # --qr-downgrade / --no-qr-downgrade: same pattern (default on).
    qr_downgrade_env = os.environ.get("GMAIL_FARM_QR_DOWNGRADE", "")
    if args.qr_downgrade is not None:
        qr_downgrade = args.qr_downgrade
        qr_downgrade_set = True
    elif qr_downgrade_env.lower() in ("0", "false", "no", "off"):
        qr_downgrade = False
        qr_downgrade_set = True
    elif qr_downgrade_env.lower() in ("1", "true", "yes", "on"):
        qr_downgrade = True
        qr_downgrade_set = True
    else:
        qr_downgrade = True
        qr_downgrade_set = False
    cfg = Config(
        count=args.count,
        delay_seconds=args.delay,
        max_retries=args.max_retries,
        backoff_base=args.backoff_base,
        out_file=args.out,
        log_file=args.log_file,
        proxy=args.proxy,
        impersonate=args.impersonate,
        password_length=args.password_length,
        birth_year_min=args.birth_year_min,
        birth_year_max=args.birth_year_max,
        timeout=args.timeout,
        max_consecutive_failures=args.max_consecutive_failures,
        dry_run=args.dry_run,
        verbose=args.verbose,
        browser=args.browser or os.environ.get("GMAIL_FARM_BROWSER", "") in ("1", "true", "yes"),
        phone=args.phone,
        headed=args.headed,
        qr_pause=qr_pause,
        _qr_pause_set=qr_pause_set,
        qr_downgrade=qr_downgrade,
        _qr_downgrade_set=qr_downgrade_set,
        device=args.device,
        entry_point=args.entry_point,
        profile_dir=args.profile_dir,
        warm=args.warm,
    )
    cfg.validate()
    return cfg
