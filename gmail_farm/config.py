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


def build_config(argv: list[str] | None = None) -> Config:
    """Parse CLI args (env vars fill in defaults) and validate."""
    p = argparse.ArgumentParser(
        prog="gmail-farm",
        description=(
            "Create Google (Gmail) accounts over HTTP, in a loop. "
            "Stops cleanly at phone verification / CAPTCHA / blocks — "
            "it never enters a phone number and never pays for anything."
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
    p.add_argument("--version", action="version", version="gmail-farm 1.0.0")

    args = p.parse_args(argv)
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
    )
    cfg.validate()
    return cfg
