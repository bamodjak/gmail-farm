#!/usr/bin/env python3
"""gmail-farm — create Google (Gmail) accounts over HTTP, in a loop.

Usage:
    python3 gmail_farm.py --count 5
    python3 gmail_farm.py --count 0 --delay 120      # loop forever
    python3 gmail_farm.py --count 1 --dry-run         # bootstrap only
    python3 gmail_farm.py --count 3 --proxy http://127.0.0.1:8080

Exit codes: 0 = run finished (at least the requested attempts were
made); 1 = bad config; 2 = stopped early (consecutive-failure limit
or interrupted). Accounts are recorded in accounts.jsonl (JSONL,
mode 600 — it contains passwords, keep it private).

This tool never enters a phone number, never solves a CAPTCHA, and
never pays for anything. If the flow reaches phone verification (the
usual terminal step for Google signup), the attempt is recorded as
stage=phone_verification and the loop moves on.
"""

from __future__ import annotations

import logging
import random
import signal
import sys
import time

from gmail_farm.config import build_config
from gmail_farm.http_client import HttpClient
from gmail_farm.identities import generate_identity
from gmail_farm.logging_setup import setup_logging
from gmail_farm.signup import SignupFlow
from gmail_farm.storage import AccountStore

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
    logger.info("gmail-farm starting: count=%s delay=%.0fs out=%s proxy=%s dry_run=%s",
                cfg.count, cfg.delay_seconds, cfg.out_file,
                bool(cfg.proxy), cfg.dry_run)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    rng = random.Random()
    store = AccountStore(cfg.out_file)
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
                result = flow.attempt(identity, dry_run=cfg.dry_run)
            except Exception as exc:
                logger.exception("[%d] unexpected error: %s", attempted, exc)
                result = None
                stage, detail = "error", f"unexpected: {exc}"
            if result is not None:
                stage, detail = result.stage, result.detail
                if result.created:
                    created += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                store.record(identity, stage, detail, result.http_status)
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
        client.close()

    logger.info("done: attempted=%d created=%d", attempted, created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
