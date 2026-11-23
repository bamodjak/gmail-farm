#!/usr/bin/env python3
"""long_warmup.py — age a persistent Chrome profile over days.

Runs a real Chrome session against a PERSISTENT profile directory
and repeats light, organic-looking Google activity in a loop:
Google searches (with a scroll), News, YouTube homepage, Maps
homepage. It never signs up for anything and never enters a
credential — it only builds cookies/history so the profile looks
lived-in when a signup is later attempted from it (ytgm "profile
aging"). The profile is the point: a profile warmed over DAYS of
real use + a residential IP is the one untested combination that
could change Google's verification page (see IDEAS.md).

Usage:
    GMAIL_FARM_CHROME_BIN=/path/to/chrome \
      python3 long_warmup.py [--profile-dir DIR] [--cycle-minutes N]

Defaults: profile ~/workspace/storebot/farm/gmail-farm-profiles/longwarm,
cycle 15 minutes of activity + idle between cycles. Runs forever
until killed (SIGINT/SIGTERM); resume by running it again — the
profile dir persists, so aging continues where it left off.
Progress is logged to long-warmup.log in the working directory.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gmail_farm.browser import (  # noqa: E402
    create_driver,
    close_driver,
    warm_up_session,
    _load_page,
)

log = logging.getLogger("long_warmup")

_stop = False


def _on_signal(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True
    log.info("signal %s — finishing the current cycle, then exiting", signum)


EXTRA_SITES = [
    # Light homepages only. Video playback and Maps interaction
    # were dropped: they time out (60s+) through a slow proxy and
    # eat the cycle. The homepage visits still set cookies/history.
    ("https://news.google.com", 20),
    ("https://www.youtube.com", 20),
    ("https://maps.google.com", 15),
]


def _cycle(driver, cycle_no: int) -> None:
    """One aging cycle: deep warm-up + a few light site visits."""
    log.info("cycle %d: deep warm-up pass", cycle_no)
    warm_up_session(driver, deep=True)
    for url, dwell in EXTRA_SITES:
        if _stop:
            return
        try:
            if _load_page(driver, url, tries=2):
                time.sleep(random.uniform(dwell * 0.7, dwell * 1.3))
                try:
                    driver.execute_script(
                        "window.scrollTo(0, document.body.scrollHeight * 0.4);")
                    time.sleep(random.uniform(2, 4))
                except Exception:  # noqa: BLE001 - best-effort
                    pass
        except Exception as exc:  # noqa: BLE001 - best-effort
            log.warning("cycle %d: %s failed (%s)", cycle_no, url, exc)
    log.info("cycle %d done", cycle_no)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile-dir",
                   default=os.path.expanduser(
                       "~/workspace/storebot/farm/gmail-farm-profiles/longwarm"),
                   help="persistent Chrome profile dir to age")
    p.add_argument("--cycle-minutes", type=float, default=15.0,
                   help="idle minutes between cycles (default 15)")
    p.add_argument("--log-file", default="long-warmup.log",
                   help="log file (default long-warmup.log)")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.FileHandler(args.log_file),
                  logging.StreamHandler(sys.stdout)])
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    log.info("long warm-up starting: profile=%s cycle=%.0fmin",
             args.profile_dir, args.cycle_minutes)
    cycle_no = 0
    while not _stop:
        cycle_no += 1
        driver = None
        try:
            driver = create_driver(headless=True, device="desktop",
                                   profile_dir=args.profile_dir)
            _cycle(driver, cycle_no)
        except Exception as exc:  # noqa: BLE001 - restart the driver
            log.warning("cycle %d failed: %s — restarting driver",
                        cycle_no, exc)
        finally:
            if driver is not None:
                try:
                    close_driver(driver)
                except Exception:  # noqa: BLE001 - best-effort
                    pass
        if _stop:
            break
        # Idle between cycles so the profile ages in real time
        # instead of hammering Google (which would look automated).
        idle = args.cycle_minutes * 60
        log.info("cycle %d done; idling %.0f min", cycle_no, idle / 60)
        slept = 0.0
        while slept < idle and not _stop:
            time.sleep(min(10.0, idle - slept))
            slept += 10.0
    log.info("long warm-up stopped after %d cycles", cycle_no)
    return 0


if __name__ == "__main__":
    sys.exit(main())
