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
    python3 gmail_farm.py --check                     # verify the setup, no signup
    python3 gmail_farm.py --count 1 --browser --stop-at-consent
        # stop at the DevicePhoneVerification consent page and guide
        # you to finish the SMS step on your own phone
    python3 gmail_farm.py --list                      # show every account in accounts.jsonl
    python3 gmail_farm.py --list --stage created      # only the created accounts
    python3 gmail_farm.py --export                    # write accounts.csv, no signup

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

import contextlib
import csv
import io
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

from gmail_farm.config import build_config
from gmail_farm.identities import generate_identity
from gmail_farm.logging_setup import setup_logging
from gmail_farm.storage import AccountStore, read_records

# HttpClient / SignupFlow are imported lazily inside main() (HTTP path
# only) so the --browser route and --help work without curl_cffi.

log = logging.getLogger("gmail_farm")

_stop = False


def _on_signal(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    log.info("signal %s received — finishing the current attempt, then exiting", signum)


def _check_import(name: str) -> tuple[bool, str]:
    """Try to import a module. Returns (ok, version-or-error)."""
    try:
        module = __import__(name)
    except Exception as exc:  # noqa: BLE001 - any import failure
        message = str(exc).strip().splitlines()
        return False, message[0] if message else "import failed"
    return True, getattr(module, "__version__", "") or "installed"


def _find_chrome() -> tuple[str | None, str]:
    """Find a Chrome binary the way browser.create_driver() uses it.

    Returns (path, how): how is 'env' (GMAIL_FARM_CHROME_BIN /
    CHROME_BIN — used directly), 'standard' (on PATH or a standard
    install location — ChromeDriver finds it on its own), or 'cache'
    (a chrome-for-testing cache — ChromeDriver does NOT look there,
    so the user must set GMAIL_FARM_CHROME_BIN to it). (None, '')
    when nothing is found.
    """
    for env_name in ("GMAIL_FARM_CHROME_BIN", "CHROME_BIN"):
        path = os.environ.get(env_name, "").strip()
        if path and os.path.isfile(path):
            return path, "env"
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path, "standard"
    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/opt/google/chrome/chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser("~/.cache/chrome-for-testing/chrome-linux64/chrome"),
        os.path.expanduser("~/.cache/chrome-for-testing/google-chrome-linux64/chrome"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            how = "cache" if "chrome-for-testing" in path else "standard"
            return path, how
    return None, ""


def _chrome_version(path: str) -> str:
    """Run `chrome --version`; return the version string or ''."""
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=15)
    except Exception:  # noqa: BLE001 - binary missing / not runnable
        return ""
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def run_setup_check() -> int:
    """Verify the setup for --browser and print plain-language results.

    One line per check; exit 0 when everything needed for --browser
    is present, 1 otherwise. Never attempts a signup.
    """
    problems: list[str] = []

    # Python version (requirements: 3.9+).
    py = sys.version_info
    if py >= (3, 9):
        print(f"Python {py.major}.{py.minor}.{py.micro} — OK (needs 3.9+)")
    else:
        print(f"Python {py.major}.{py.minor}.{py.micro} — TOO OLD (needs 3.9+)")
        problems.append("Python older than 3.9")

    # Required packages (requirements.txt). cv2 is optional: only the
    # QR-decode fallback needs it, so it never fails the check.
    for module, why in (("curl_cffi", "the HTTP route"),
                        ("selenium", "the browser route"),
                        ("webdriver_manager", "ChromeDriver downloads")):
        ok, detail = _check_import(module)
        if ok:
            print(f"{module} — OK"
                  + (f" ({detail})" if detail != "installed" else ""))
        else:
            print(f"{module} — MISSING (needed for {why}; "
                  "pip install -r requirements.txt)")
            problems.append(f"{module} is missing")
    ok, detail = _check_import("cv2")
    if ok:
        print(f"OpenCV (cv2) — OK ({detail}); QR-decode fallback available")
    else:
        print("OpenCV (cv2) — missing (optional: only the QR-decode "
              "fallback needs it; pip install opencv-python-headless "
              "to enable it)")

    # Google Chrome.
    chrome_path, how = _find_chrome()
    if chrome_path is None:
        print("Google Chrome — NOT FOUND (install Google Chrome to "
              "use --browser)")
        problems.append("Google Chrome not found")
    else:
        version = _chrome_version(chrome_path)
        shown = f"Google Chrome — {chrome_path}" + (f" ({version})"
                                                    if version else "")
        if how == "cache":
            print(shown + " — found, but ChromeDriver does not look "
                  "in the chrome-for-testing cache; set "
                  "GMAIL_FARM_CHROME_BIN to this path to use it")
            problems.append("Chrome only in the chrome-for-testing "
                            "cache (set GMAIL_FARM_CHROME_BIN)")
        elif how == "env":
            print(shown + " — OK (from GMAIL_FARM_CHROME_BIN / "
                  "CHROME_BIN)")
        else:
            print(shown + " — OK")

    # ChromeDriver, via webdriver-manager — the same call the browser
    # route uses. Its own download chatter is silenced so the check
    # stays one line per check; a cached driver returns instantly.
    ok_wdm, _ = _check_import("webdriver_manager")
    if not ok_wdm:
        print("ChromeDriver — skipped (webdriver-manager is missing)")
    else:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            with contextlib.redirect_stdout(io.StringIO()):
                driver_path = ChromeDriverManager().install()
            if os.path.isfile(driver_path):
                print(f"ChromeDriver — OK: {driver_path}")
            else:
                print("ChromeDriver — FAILED (webdriver-manager "
                      "returned a path that does not exist: "
                      f"{driver_path})")
                problems.append("ChromeDriver could not be obtained")
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            print(f"ChromeDriver — FAILED ({exc}); check your network "
                  "and try again")
            problems.append("ChromeDriver could not be obtained")

    if problems:
        print(f"Setup NOT ready: {'; '.join(problems)}")
        return 1
    print("Setup OK — ready for --browser")
    return 0


def _format_when(ts: str) -> str:
    """Format a record's ISO timestamp as 'YYYY-MM-DD HH:MM UTC'.

    Falls back to the raw value when it does not parse.
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(ts)


def _consent_url(detail: str) -> str:
    """Pull the DevicePhoneVerification consent URL out of a record.

    Only the consent-stage records carry this link (the URL the
    user opens on their phone to finish the SMS step). Other
    records' detail text may contain unrelated URLs (a Google
    sign-in page, a Selenium docs link) — those are NOT consent
    links, so they are not returned here. "" when there is none.
    """
    if not detail:
        return ""
    match = re.search(
        r"https://accounts\.google\.com/devicephoneverification/consent\?\S+",
        detail)
    return match.group(0).rstrip(".,)") if match else ""


def _one_line(text: str, limit: int = 0) -> str:
    """Collapse a detail string to one line (newlines -> spaces).

    limit > 0 truncates with an ellipsis; 0 = keep it whole.
    """
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit and len(flat) > limit:
        return flat[: limit - 1].rstrip() + "…"
    return flat


def run_list(cfg) -> int:
    """--list: print every account in accounts.jsonl, read-only.

    One block per account (reads well on a phone or a narrow
    terminal): email, password, stage, date, consent link when
    present, and a short note. --stage filters to one stage.
    Missing or empty file prints a clear message, never a
    traceback. Never starts Chrome, never touches the network.
    """
    records = read_records(cfg.out_file)
    if not records:
        print(f"No accounts yet — {cfg.out_file} is missing or "
              "empty. Run the tool first to create some.")
        return 0
    counts = Counter(str(r.get("stage", "")) for r in records)
    breakdown = ", ".join(f"{c} {s}" for s, c in sorted(counts.items())
                          if s)
    print(f"{len(records)} account(s) in {cfg.out_file}: {breakdown}")
    stage = (cfg.stage_filter or "").strip().lower()
    shown = records
    if stage:
        shown = [r for r in records
                 if str(r.get("stage", "")).lower() == stage]
        if not shown:
            print(f"No accounts at stage '{cfg.stage_filter}'.")
            return 0
        print(f"Showing {len(shown)} at stage "
              f"'{cfg.stage_filter}':")
    print()
    for i, rec in enumerate(shown, 1):
        print(f"[{i}] {rec.get('email', '')}")
        print(f"    Password: {rec.get('password', '')}")
        print(f"    Stage: {rec.get('stage', '')}")
        when = _format_when(str(rec.get("ts", "")))
        if when:
            print(f"    When: {when}")
        url = _consent_url(str(rec.get("detail", "")))
        if url:
            print(f"    Consent link: {url}")
        note = _one_line(str(rec.get("detail", "")), 160)
        if note:
            print(f"    Note: {note}")
        print()
    return 0


def run_export(cfg) -> int:
    """--export: write accounts.jsonl to a CSV file, read-only.

    Columns: email, password, stage, created_at, consent_url,
    notes. The notes column is the record's detail text with
    newlines collapsed so each account stays on one CSV row.
    Overwriting an existing export file is fine — it is a
    generated view of accounts.jsonl, not the source of truth.
    Never starts Chrome, never touches the network.
    """
    records = read_records(cfg.out_file)
    path = cfg.export_file or "accounts.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["email", "password", "stage", "created_at",
                        "consent_url", "notes"],
            lineterminator="\n")
        writer.writeheader()
        for rec in records:
            detail = str(rec.get("detail", ""))
            writer.writerow({
                "email": rec.get("email", ""),
                "password": rec.get("password", ""),
                "stage": rec.get("stage", ""),
                "created_at": rec.get("ts", ""),
                "consent_url": _consent_url(detail),
                "notes": _one_line(detail),
            })
    print(f"Wrote {len(records)} account(s) to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = build_config(argv)
    except (ValueError, SystemExit) as exc:
        # argparse exits with code 2 on bad usage; ValueError is ours.
        if isinstance(exc, ValueError):
            print(f"config error: {exc}", file=sys.stderr)
            return 1
        raise

    # --check: verify the setup and exit without attempting a signup.
    if cfg.check:
        return run_setup_check()

    # --list / --export: read-only views over accounts.jsonl. They
    # never start Chrome and never touch the network.
    if cfg.list_accounts or cfg.export_file:
        if cfg.list_accounts:
            run_list(cfg)
        if cfg.export_file:
            run_export(cfg)
        return 0

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
                    # --stop-at-consent / GMAIL_FARM_STOP_AT_CONSENT=1:
                    # end the attempt at the DevicePhoneVerification
                    # consent page (no "Send SMS" click on desktop)
                    # and guide the user to finish it on their phone.
                    stop_at_consent = cfg.stop_at_consent
                    if stop_at_consent:
                        logger.info("stop-at-consent mode ON")
                    result = browser_create(
                        identity, phone=cfg.phone or None,
                        proxy=cfg.proxy or None,
                        headless=not cfg.headed,
                        qr_pause=cfg.qr_pause,
                        qr_downgrade=cfg.qr_downgrade,
                        device=cfg.device,
                        entry_point=cfg.entry_point,
                        profile_dir=cfg.profile_dir or None,
                        warm=cfg.warm,
                        stop_at_consent=stop_at_consent)
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
