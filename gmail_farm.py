#!/usr/bin/env python3
"""gmail-farm — create Google (Gmail) accounts, in a loop.

Usage:
    python3 gmail_farm.py --count 5
    python3 gmail_farm.py --count 0 --delay 120      # loop forever
    python3 gmail_farm.py --count 1 --dry-run         # bootstrap only
    python3 gmail_farm.py --count 3 --proxy http://127.0.0.1:8080
    python3 gmail_farm.py --count 1 --browser         # real Chrome
    python3 gmail_farm.py --count 1 --browser --phone +15551234567
    python3 gmail_farm.py --count 1 --browser --no-qr-pause  # no waiting

Plain HTTP is blocked by Google's BotGuard attestation (only a real
browser can produce it), so --browser (Selenium + Chrome) is the
working route. Phone verification: the tool first tries to skip it;
with --phone it uses YOUR own number — the SMS goes to your phone
and you type the code at the console prompt. It never pays for a
number, never uses a third-party SMS service, and never solves a
CAPTCHA. Without --phone, an attempt that reaches phone verification
is recorded as stage=phone_verification and the loop moves on.
Google's QR-verification wall pauses the attempt by default
(--qr-pause): the QR code is saved to a file, you scan it with your
own phone, press Enter, and the signup continues. Use --no-qr-pause
for non-interactive runs (the QR wall then abandons the attempt).

Exit codes: 0 = run finished (at least the requested attempts were
made); 1 = bad config; 2 = stopped early (consecutive-failure limit
or interrupted). Accounts are recorded in accounts.jsonl (JSONL,
mode 600 — it contains passwords, keep it private).
"""

from __future__ import annotations

import logging
import random
import signal
import sys
import time

from gmail_farm.config import build_config
from gmail_farm.identities import generate_identity
from gmail_farm.logging_setup import setup_logging
from gmail_farm.storage import AccountStore

# HttpClient / SignupFlow are imported lazily inside main() (HTTP path
# only) so the --browser route and --help work without curl_cffi.

log = logging.getLogger("gmail_farm")

_stop = False


def _on_signal(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    log.info("signal %s received — finishing the current attempt, then exiting", signum)


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = build_config(argv)
    except (ValueError, SystemExit) as exc:
        # argparse exits with code 2 on bad usage; ValueError is ours.
        if isinstance(exc, ValueError):
            print(f"config error: {exc}", file=sys.stderr)
            return 1
        raise

    logger = setup_logging(cfg.log_file, verbose=cfg.verbose)
    logger.info("gmail-farm starting: count=%s delay=%.0fs out=%s proxy=%s dry_run=%s browser=%s",
                cfg.count, cfg.delay_seconds, cfg.out_file,
                bool(cfg.proxy), cfg.dry_run, cfg.browser)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    rng = random.Random()
    store = AccountStore(cfg.out_file)

    # Browser route: import lazily so the HTTP path never needs
    # Selenium. Fail fast with a clear message when it is missing.
    browser_create = None
    if cfg.browser:
        try:
            from gmail_farm.browser import create_account as browser_create
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"browser route unavailable: {exc}", file=sys.stderr)
            return 1
        logger.info("browser route: Selenium + Chrome (phone=%s)",
                    "provided" if cfg.phone else "skip only")
    else:
        from gmail_farm.http_client import HttpClient
        from gmail_farm.signup import SignupFlow

        client = HttpClient(impersonate=cfg.impersonate, timeout=cfg.timeout,
                            max_retries=cfg.max_retries,
                            backoff_base=cfg.backoff_base, proxy=cfg.proxy)
        flow = SignupFlow(client)

    attempted = 0
    created = 0
    consecutive_failures = 0
    try:
        while not _stop:
            if cfg.count and attempted >= cfg.count:
                break

            # Generate a fresh identity, skipping usernames already tried
            # (resume safety: never duplicate an attempt after restart).
            for _ in range(20):
                identity = generate_identity(
                    rng, password_length=cfg.password_length,
                    birth_year_min=cfg.birth_year_min,
                    birth_year_max=cfg.birth_year_max)
                if not store.already_attempted(identity.username):
                    break
            else:
                logger.error("could not generate an untried username after 20 tries")
                return 2

            attempted += 1
            logger.info("[%d] attempt: %s", attempted, identity.public_summary())
            try:
                if browser_create is not None:
                    result = browser_create(
                        identity, phone=cfg.phone or None,
                        proxy=cfg.proxy or None,
                        headless=not cfg.headed,
                        qr_pause=cfg.qr_pause,
                        qr_downgrade=cfg.qr_downgrade,
                        device=cfg.device,
                        entry_point=cfg.entry_point,
                        profile_dir=cfg.profile_dir or None,
                        warm=cfg.warm)
                    # BrowserResult has no http_status / created flag;
                    # normalize to the HTTP result shape.
                    stage = result.stage
                    detail = result.detail
                    http_status = 0
                    was_created = stage == "created"
                else:
                    result = flow.attempt(identity, dry_run=cfg.dry_run)
                    stage = result.stage
                    detail = result.detail
                    http_status = result.http_status
                    was_created = result.created
            except Exception as exc:
                logger.exception("[%d] unexpected error: %s", attempted, exc)
                result = None
                stage, detail = "error", f"unexpected: {exc}"
                http_status = 0
                was_created = False
            if result is not None:
                if was_created:
                    created += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                store.record(identity, stage, detail, http_status)
                logger.info("[%d] stage=%s detail=%s", attempted, stage, detail)
            else:
                consecutive_failures += 1
                store.record(identity, stage, detail, 0)

            if (cfg.max_consecutive_failures
                    and consecutive_failures >= cfg.max_consecutive_failures):
                logger.warning("stopping: %d consecutive failures",
                               consecutive_failures)
                return 2

            if cfg.count and attempted >= cfg.count:
                break
            if _stop:
                break
            if cfg.delay_seconds > 0:
                logger.info("waiting %.0fs before next attempt", cfg.delay_seconds)
                # Sleep in small slices so Ctrl+C / SIGTERM is responsive.
                waited = 0.0
                while waited < cfg.delay_seconds and not _stop:
                    time.sleep(min(1.0, cfg.delay_seconds - waited))
                    waited += 1.0
    finally:
        if not cfg.browser:
            client.close()

    logger.info("done: attempted=%d created=%d", attempted, created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
