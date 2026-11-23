"""Browser route for Gmail signup (Selenium + Chrome).

Plain HTTP is blocked at Google's BotGuard attestation (see
RPC-REVERSE-2026-11-22.md), so this module drives the real signup
page in a Chrome browser with anti-detection measures. The approach
follows the one proven by the gmail-account-creator project
(github.com/vichhka-git/gmail-account-creator); this module is an
independent implementation — no code from it is imported, copied,
or run.

What it does, per attempt:
  1. Launch Chrome with anti-detection flags + a fresh random identity
     (random Chrome user-agent with a MATCHED window size, so the
     viewport never contradicts the UA's OS).
  2. Warm up the session on neutral sites (Google, Wikipedia, BBC)
     before touching any signup URL.
  3. Fill the signup form with human-like typing (random per-keystroke
     delays) and human-like clicks (ActionChains with pauses).
  4. Phone verification: first try to SKIP it (Skip / Not now).
     If Google requires a number and the caller supplied one
     (``phone``, the user's OWN number), fill it, submit, and ask
     the user for the SMS code they received (``code_prompt``) —
     the tool never pays for a number and never uses a third-party
     SMS service. If no number was supplied and the skip fails,
     the attempt is ABANDONED as stage "phone_verification".
     Google sometimes demands a QR-code scan on a real phone
     instead (crossflow verification). When that QR wall appears
     and ``qr_pause`` is on (the default for browser mode), the
     attempt PAUSES: the QR code is saved to a file the user can
     open, the console tells them to scan it with their phone and
     press Enter when done, and the flow resumes and continues to
     completion. With ``qr_pause`` off, the QR wall is abandoned
     as stage "phone_verification" like before.
  5. Verify creation by checking the page after the phone step
     (account home / welcome), not by assuming the skip worked.

Requirements (only for this module; the HTTP path does not need them):
  - Google Chrome installed.
  - selenium + webdriver-manager (pip install -r requirements.txt).

Selenium is imported LAZILY inside the functions, so importing this
module (and the rest of gmail-farm) works fine without Selenium
installed — only create_account() raises a clear error when it is
missing.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from .identities import Identity

_log = logging.getLogger(__name__)

# ------------------------------------------------------------------ flags
# Chrome flags that hide automation. The two that matter most are
# --disable-blink-features=AutomationControlled (removes the
# AutomationControlled blink flag) and the navigator.webdriver override
# applied after launch (see create_driver). The rest remove automation
# tells (infobars, extensions, first-run) and headless giveaways.
CHROME_FLAGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-infobars",
    "--disable-notifications",
    "--disable-software-rasterizer",
    "--disable-webgl",
    "--disable-webgl2",
    "--disable-logging",
    "--log-level=3",
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--no-experiments",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-blink-features=AutomationControlled",
    "--disable-extensions",
    "--disable-popup-blocking",
]

# (user-agent, (width, height)) — UA and window size are always set
# TOGETHER so the viewport matches the UA's OS (a Windows UA with a
# phone-sized viewport is an instant fingerprint mismatch).
USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", (1920, 1080)),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", (1366, 768)),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", (1440, 900)),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", (1920, 1080)),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36", (1920, 1080)),
]

# Neutral sites visited before any Google signup URL, so the session
# has history and cookies like a real browsing session. Kept short
# (two light pages) so a slow proxy cannot eat the whole attempt.
WARM_UP_SITES = [
    "https://www.google.com",
    "https://www.wikipedia.org",
]

SIGNUP_URL = (
    "https://accounts.google.com/signup/v2/webcreateaccount"
    "?flowName=GlifWebSignIn&flowEntry=SignUp"
)

# Deep warm-up (ytgm "profile aging", see IDEAS.md): organic-looking
# activity that builds cookies/history in a PERSISTENT profile before
# a signup, so Google sees a lived-in browser instead of a fresh one.
# Only meaningful with --profile-dir — a fresh profile throws the
# history away when Chrome quits. Kept short (~40s); real aging
# takes days of use, this is a best-effort warm-up, not a guarantee.
WARM_UP_SEARCHES = [
    "weather today",
    "how to learn python",
    "best budget laptop 2026",
    "best free photo editor",
    "how to start a garden",
]
WARM_UP_DEEP_SITES = [
    # Light pages only. YouTube and Maps were tried and dropped:
    # they are heavy and time out (60s each) through a slow proxy,
    # eating the attempt before the signup starts. Google service
    # homepages (mail/drive/calendar) were tried the same way
    # 2026-11-23 and dropped for the same reason: the warm-up ate
    # ~280s of the 300s attempt timeout before the form even
    # loaded (Nathan Walker, drive entry test).
    ("https://news.google.com", 5),
]

# Alternative signup entry points (ytgm). Each is a Google service
# whose "Create account" link lands on the SAME signup form as the
# direct URL — there is no technical bypass, only the hypothesis
# (unverified) that starting from a service Google trusts more
# lowers the risk score. 'direct' is the plain signup URL.
# 'cloud-console' is the entry point the google-qr-sms-helper post
# names (start sign-in from Google Cloud Console / Cloud Shell):
# never tested for SIGNUP before 2026-11-23. 'play' (Play Store
# web) and 'aistudio' (AI Studio) are other untested services.
ENTRY_POINTS = {
    "direct": None,
    "youtube": "https://www.youtube.com",
    "drive": "https://drive.google.com",
    "photos": "https://photos.google.com",
    "cloud-console": "https://console.cloud.google.com",
    "play": "https://play.google.com",
    "aistudio": "https://aistudio.google.com",
}


def _click_text(driver, text, timeout=5):
    """Click a button/link whose visible text contains ``text``.
    Returns True when something was clicked."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    for xpath in (f"//button[contains(., '{text}')]",
                  f"//a[contains(., '{text}')]"):
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].click();", el)
            time.sleep(2)
            return True
        except Exception:  # noqa: BLE001 - try the next selector
            pass
    return False


def _enter_via(driver, entry_point):
    """Start the signup from a Google service instead of the direct
    signup URL. Navigates to the service, clicks Sign in -> Create
    account -> For my personal use, and returns True when the
    browser lands on the signup form (the step loop takes over from
    there). Returns False when any step fails — the caller falls
    back to the direct signup URL."""
    from urllib.parse import quote_plus
    start = ENTRY_POINTS.get(entry_point)
    if not start:
        return False
    if not _load_page(driver, start, tries=3):
        _log.warning("entry point %s: %s did not load", entry_point, start)
        return False
    time.sleep(2)
    # YouTube has a "Sign in" button; Drive/Photos have a
    # "Go to ..." button or go straight to sign-in. Either way,
    # the goal is the "Create account" link.
    _click_text(driver, "Sign in", timeout=5)
    if "Go to" in driver.page_source:
        _click_text(driver, "Go to", timeout=5)
    if not _click_text(driver, "Create account", timeout=8):
        _log.warning("entry point %s: 'Create account' not found", entry_point)
        return False
    # Cloud Console's "Create account" goes to
    # accounts.cloud.google.com/signup/email (enter an existing
    # email) — there is NO "For my personal use" choice there.
    # "Get a new email address instead" leads to the lifecycle
    # username step (service=cloudconsole, continue to the Cloud
    # free-trial ToS). Verified by probe 2026-11-23.
    if entry_point == "cloud-console":
        if not _click_text(driver, "Get a new email address instead",
                           timeout=8):
            _log.warning("entry point %s: 'Get a new email address "
                         "instead' not found", entry_point)
            return False
        time.sleep(2)
        if ("username" in (driver.current_url or "")
                or _smart_find(driver, [("id", "username"),
                                        ("xpath",
                                         "//input[@name='Username']")],
                               timeout=5) is not None):
            _log.info("entry point %s: reached the signup form "
                      "(username step)", entry_point)
            return True
        _log.warning("entry point %s: did not reach the signup form (%s)",
                     entry_point, driver.current_url)
        return False
    if not _click_text(driver, "For my personal use", timeout=8):
        _log.warning("entry point %s: 'For my personal use' not found",
                     entry_point)
        return False
    # The signup form is the same page the direct URL loads.
    if "webcreateaccount" in (driver.current_url or "") \
            or _smart_find(driver, [("id", "firstName")], timeout=5) is not None:
        _log.info("entry point %s: reached the signup form", entry_point)
        return True
    _log.warning("entry point %s: did not reach the signup form (%s)",
                 entry_point, driver.current_url)
    return False
NEXT_BUTTON_SELECTORS = [
    "//button[.//span[contains(text(), 'Next')]]",
    "//button[@jsname='LgbsSe']",
    "//div[@jsname='QkNstf']//button",
    "//button[contains(@class, 'VfPpkd-LgbsSe')]",
]

# Phone-verification skip selectors (tried in order). ONLY explicit
# Skip / Not now controls — a generic "any non-Next button" selector
# used to be here and it clicked the wrong thing (e.g. "Use your
# existing email" or "Show password") and navigated away.
SKIP_PHONE_SELECTORS = [
    "//button[.//span[contains(text(),'Skip')]]",
    "//button[contains(text(),'Skip')]",
    "//a[contains(text(),'Skip')]",
    "//span[contains(text(),'Not now')]",
    "//button[.//span[contains(text(),'Not now')]]",
]

# Phone-number field selectors (used only to DETECT that the phone
# step is present — never to fill it).
PHONE_FIELD_SELECTORS = [
    ("id", "phoneNumberId"),
    ("xpath", "//input[@type='tel']"),
]

# SMS-code field selectors (used only to detect the code step).
CODE_FIELD_SELECTORS = [
    ("id", "code"),
    ("xpath", "//input[@type='tel' and @maxlength='6']"),
    ("xpath", "//input[@name='code']"),
]

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]


# ------------------------------------------------------------------ iPad Mini
# Device spoof that makes Google treat the browser as an iPad Mini,
# replicating the google-qr-sms-helper extension
# (github.com/havenicec/google-qr-sms-helper, MIT). The extension's
# background.js rewrites every accounts.google.com request: User-Agent
# set to the iPad Mini UA and ALL Sec-CH-UA* headers removed (a real
# iPad Safari sends none); its ua_spoof.js (document_start, MAIN
# world) aligns navigator.userAgent / appVersion / platform /
# vendor / maxTouchPoints / userAgentData. The extension also injects
# DevicePhoneVerificationUi RPCs to downgrade a SIGN-IN QR page to
# SMS — that part targets sign-in, not signup, and is not replicated
# here. What is replicated is the device spoof: if Google offers
# SMS/voice verification on the signup flow for an iPad, the
# existing --phone + SMS-code path completes the account with no QR
# scan. Chrome cannot remove Sec-CH-UA headers via a flag, so the
# profile does the closest equivalent: --disable-features=
# UserAgentClientHint (stops Chrome sending them) plus a CDP
# userAgentMetadata override that makes any hints that do get sent
# iPad-consistent instead of desktop-Chrome-consistent.
IPAD_MINI_UA = (
    "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")
IPAD_MINI_APP_VERSION = (
    "5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")
IPAD_MINI_WINDOW = (768, 1024)

# Navigator overrides, adapted from the extension's ua_spoof.js.
IPAD_MINI_NAVIGATOR_SPOOF = """
(() => {
  const UA = "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1";
  const AV = "5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1";
  Object.defineProperty(navigator, 'userAgent', { get: () => UA, configurable: true });
  Object.defineProperty(navigator, 'appVersion', { get: () => AV, configurable: true });
  Object.defineProperty(navigator, 'platform', { get: () => 'iPad', configurable: true });
  Object.defineProperty(navigator, 'vendor', { get: () => 'Apple Computer, Inc.', configurable: true });
  Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5, configurable: true });
  Object.defineProperty(navigator, 'userAgentData', { get: () => undefined, configurable: true });
})();
"""


def _apply_ipad_mini_profile(driver):
    """Apply the iPad Mini device profile via CDP, replicating the
    google-qr-sms-helper extension's UA spoof. Called right after the
    driver is created, before any navigation, so every request and
    every page sees the iPad. Best-effort per step: a CDP call that
    fails is logged and skipped, so a Chrome version without one
    command does not kill the attempt."""
    # 1. User-Agent + client-hints metadata (iPad-consistent Sec-CH-UA).
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent": IPAD_MINI_UA,
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "iPad",
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Safari", "version": "16"},
                    {"brand": "Not/A)Brand", "version": "99"},
                ],
                "fullVersionList": [
                    {"brand": "Safari", "version": "16.6"},
                    {"brand": "Not/A)Brand", "version": "99.0.0.0"},
                ],
                "platform": "iPad",
                "platformVersion": "16.6",
                "architecture": "",
                "model": "iPad",
                "mobile": True,
            },
        })
    except Exception as exc:  # noqa: BLE001 - UA override is best-effort
        _log.warning("iPad Mini: setUserAgentOverride failed: %s", exc)
    # 2. navigator overrides on every new document (ua_spoof.js).
    try:
        driver.execute_cdp_cmd("Page.enable", {})
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": IPAD_MINI_NAVIGATOR_SPOOF})
    except Exception as exc:  # noqa: BLE001 - navigator spoof is best-effort
        _log.warning("iPad Mini: navigator spoof failed: %s", exc)
    # 3. Touch + mobile viewport (an iPad is a touch device).
    try:
        driver.execute_cdp_cmd(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True, "maxTouchPoints": 5})
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": IPAD_MINI_WINDOW[0], "height": IPAD_MINI_WINDOW[1],
             "deviceScaleFactor": 2, "mobile": True})
    except Exception as exc:  # noqa: BLE001 - emulation is best-effort
        _log.warning("iPad Mini: touch/metrics override failed: %s", exc)


# Android-phone device profile (IDEAS.md #2): a real Android phone
# running Chrome. Unlike the iPad spoof, a real Android Chrome DOES
# send Sec-CH-UA headers, so they are not stripped — the CDP
# userAgentMetadata override makes them Android-consistent instead.
# The momoproxy guide and community reports claim Google asks for
# less (sometimes only a recovery email) on mobile; the iPad spoof
# did not change the signup page, so this is the next variant to
# test live.
ANDROID_PHONE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
ANDROID_PHONE_APP_VERSION = (
    "5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36")
ANDROID_PHONE_WINDOW = (412, 915)

ANDROID_PHONE_NAVIGATOR_SPOOF = """
(() => {
  const UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36";
  const AV = "5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36";
  Object.defineProperty(navigator, 'userAgent', { get: () => UA, configurable: true });
  Object.defineProperty(navigator, 'appVersion', { get: () => AV, configurable: true });
  Object.defineProperty(navigator, 'platform', { get: () => 'Linux armv8l', configurable: true });
  Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.', configurable: true });
  Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5, configurable: true });
})();
"""


def _apply_android_phone_profile(driver):
    """Apply the Android-phone device profile via CDP, mirroring
    _apply_ipad_mini_profile. Called right after the driver is
    created, before any navigation. Best-effort per step."""
    # 1. User-Agent + client-hints metadata (Android-consistent).
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent": ANDROID_PHONE_UA,
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "Android",
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Google Chrome", "version": "131"},
                    {"brand": "Chromium", "version": "131"},
                    {"brand": "Not/A)Brand", "version": "99"},
                ],
                "fullVersionList": [
                    {"brand": "Google Chrome", "version": "131.0.6778.87"},
                    {"brand": "Chromium", "version": "131.0.6778.87"},
                    {"brand": "Not/A)Brand", "version": "99.0.0.0"},
                ],
                "platform": "Android",
                "platformVersion": "14",
                "architecture": "arm",
                "model": "Pixel 8",
                "mobile": True,
            },
        })
    except Exception as exc:  # noqa: BLE001 - UA override is best-effort
        _log.warning("Android phone: setUserAgentOverride failed: %s", exc)
    # 2. navigator overrides on every new document.
    try:
        driver.execute_cdp_cmd("Page.enable", {})
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": ANDROID_PHONE_NAVIGATOR_SPOOF})
    except Exception as exc:  # noqa: BLE001 - navigator spoof is best-effort
        _log.warning("Android phone: navigator spoof failed: %s", exc)
    # 3. Touch + mobile viewport (a phone is a touch device).
    try:
        driver.execute_cdp_cmd(
            "Emulation.setTouchEmulationEnabled",
            {"enabled": True, "maxTouchPoints": 5})
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": ANDROID_PHONE_WINDOW[0],
             "height": ANDROID_PHONE_WINDOW[1],
             "deviceScaleFactor": 2.625, "mobile": True})
    except Exception as exc:  # noqa: BLE001 - emulation is best-effort
        _log.warning("Android phone: touch/metrics override failed: %s", exc)


@dataclass
class BrowserResult:
    """Outcome of one browser signup attempt."""

    stage: str          # created | phone_verification | blocked | error
    email: str = ""
    detail: str = ""


def _require_selenium():
    try:
        import selenium  # noqa: F401
        from selenium import webdriver  # noqa: F401
        from webdriver_manager.chrome import ChromeDriverManager  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "browser fallback needs selenium + webdriver-manager "
            "(pip install -r requirements.txt); Chrome must also be "
            "installed"
        ) from e


def _env_proxy():
    """System proxy from the environment, for Chrome.

    Headless Chrome does not reliably pick up http_proxy /
    https_proxy on its own — without an explicit --proxy-server the
    CONNECT tunnel fails (net::ERR_TUNNEL_CONNECTION_FAILED) even
    though the same proxy works for curl. So when the caller did not
    pass a proxy, take it from the environment. Returns the proxy
    URL or None. Callers must never log the returned value: it can
    contain credentials.
    """
    import os

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def create_driver(proxy: str | None = None, headless: bool = True,
                  device: str = "desktop", profile_dir: str | None = None):
    """Build an anti-detection Chrome WebDriver instance (3 tries).

    ``proxy`` is an optional http(s) proxy URL (the CLI --proxy
    flag). ``headless`` defaults to True: a farm runs on servers with
    no display, and modern headless Chrome passes BotGuard; pass
    False to watch the browser (needs a display). ``device`` is
    'desktop' (default: a random desktop Chrome UA with a matched
    window size), 'ipad-mini' (the iPad Mini profile from
    google-qr-sms-helper: iPad UA, 768x1024 touch viewport, CDP
    UA/navigator/touch overrides, and an attempt to stop Chrome
    sending Sec-CH-UA headers at all) or 'android-phone' (Pixel 8 /
    Android 14 Chrome UA, 412x915 touch viewport, Android-consistent
    Sec-CH-UA metadata — a real Android Chrome sends those headers).

    Proxy handling: when the proxy URL carries credentials
    (``http://user:pass@host:port``), Chrome is NOT given the
    credentialed URL — Chrome silently ignores
    ``--proxy-server`` with embedded credentials (proven by its own
    net log: ``proxy_chain: [direct://]``) and the ``https_proxy``
    env var is only picked up intermittently. Instead a tiny local
    forwarding proxy (``gmail_farm.local_proxy``) is started on
    127.0.0.1 and Chrome is pointed at it with no credentials; the
    local proxy adds ``Proxy-Authorization`` upstream. The local
    proxy is attached to the driver as ``driver._local_proxy`` and
    must be stopped with :func:`close_driver`.

    ``profile_dir`` is a persistent Chrome profile directory
    (``--user-data-dir``). When set, cookies/history survive across
    attempts, so the profile warms up over time (the ytgm aging
    idea) instead of starting fresh every time. When unset, Chrome
    uses a fresh temporary profile per attempt.
    """
    _require_selenium()
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager

    # No proxy given: take the system proxy from the environment.
    # Chrome needs --proxy-server explicitly (see _env_proxy).
    proxy = proxy or _env_proxy()
    local_proxy = None
    chrome_proxy = proxy
    if proxy:
        from urllib.parse import urlparse
        parsed = urlparse(proxy)
        if parsed.username is not None:
            # Authenticated upstream: route through a local proxy
            # that adds Proxy-Authorization, so Chrome never sees
            # the credentials (it ignores them in --proxy-server).
            from gmail_farm.local_proxy import LocalAuthProxy
            local_proxy = LocalAuthProxy(proxy).start()
            chrome_proxy = local_proxy.proxy_url
            _log.info("local auth proxy on %s for upstream %s",
                      local_proxy.proxy_url,
                      f"{parsed.scheme}://{parsed.hostname}:{parsed.port}")
    if device == "ipad-mini":
        ua_string, (width, height) = IPAD_MINI_UA, IPAD_MINI_WINDOW
    elif device == "android-phone":
        ua_string, (width, height) = ANDROID_PHONE_UA, ANDROID_PHONE_WINDOW
    else:
        ua_string, (width, height) = random.choice(USER_AGENTS)
    options = ChromeOptions()
    # Chrome in a nonstandard location (e.g. /opt/meta-chromium/chrome
    # on this VM, or a portable install): let the user point at it
    # with GMAIL_FARM_CHROME_BIN / CHROME_BIN. Without this,
    # ChromeDriver only looks in the standard install locations and
    # the session fails with "Chrome instance exited".
    import os as _os
    chrome_bin = (_os.environ.get("GMAIL_FARM_CHROME_BIN")
                  or _os.environ.get("CHROME_BIN") or "").strip()
    if chrome_bin:
        options.binary_location = chrome_bin
        _log.info("using Chrome binary from env: %s", chrome_bin)
    for flag in CHROME_FLAGS:
        options.add_argument(flag)
    if profile_dir:
        options.add_argument(f"--user-data-dir={profile_dir}")
    if device == "ipad-mini":
        # A real iPad Safari sends no Sec-CH-UA headers; stop Chrome
        # sending its desktop ones (best-effort: the flag may not
        # exist in every Chrome version, and the CDP metadata
        # override below covers the rest).
        options.add_argument("--disable-features=UserAgentClientHint")
    if headless:
        options.add_argument("--headless=new")
    if chrome_proxy:
        options.add_argument(f"--proxy-server={chrome_proxy}")
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument(f"--user-agent={ua_string}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    last_err = None
    for _ in range(3):
        try:
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(60)
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
            if device == "ipad-mini":
                _apply_ipad_mini_profile(driver)
            elif device == "android-phone":
                _apply_android_phone_profile(driver)
            driver._local_proxy = local_proxy
            return driver
        except Exception as e:  # noqa: BLE001 - retry, then raise
            last_err = e
            time.sleep(2)
    if local_proxy is not None:
        local_proxy.stop()
    raise RuntimeError(
        f"could not start Chrome WebDriver after 3 attempts: {last_err}")


def close_driver(driver):
    """Quit a driver from :func:`create_driver` and stop its local
    auth proxy (if any). Always use this instead of ``driver.quit()``
    so no localhost proxy is left listening after the attempt."""
    try:
        driver.quit()
    finally:
        local_proxy = getattr(driver, "_local_proxy", None)
        if local_proxy is not None:
            local_proxy.stop()


def human_typing(element, text, delay_range=(0.05, 0.18)):
    """Type with human-like random per-keystroke delays.

    Robustness (learned live 2026-11-22): the username field
    sometimes is not interactable when first found (page still
    transitioning) — two attempts died with
    ElementNotInteractableException. And a JS value-set fallback
    silently breaks Google's form state (the field shows the text
    but the form never accepts it, looping on the username page
    for all 8 steps — Sophia Wilson, 2026-11-22). So: retry real
    keystrokes first (wait + retry, then ActionChains click +
    type); JS value-setting is the last resort, and the value is
    verified afterwards either way.
    """
    from selenium.webdriver.common.action_chains import ActionChains

    last_exc = None
    for _attempt in range(3):
        try:
            if _attempt > 0:
                try:
                    element.clear()
                except Exception:  # noqa: BLE001 - typing overwrites below
                    pass
            for char in text:
                element.send_keys(char)
                time.sleep(random.uniform(*delay_range))
            if (element.get_attribute("value") or "") == text:
                return
        except Exception as exc:  # noqa: BLE001 - retry below
            last_exc = exc
        time.sleep(1.0)
        try:
            ActionChains(element.parent).move_to_element(
                element).click().perform()
        except Exception:  # noqa: BLE001 - retry the keystrokes
            pass
    # Last resort: JS value + input/change events.
    try:
        element.parent.execute_script(
            "arguments[0].value = arguments[1]; "
            "arguments[0].dispatchEvent(new Event('input', "
            "{bubbles: true})); "
            "arguments[0].dispatchEvent(new Event('change', "
            "{bubbles: true}));",
            element, text)
        if (element.get_attribute("value") or "") == text:
            return
    except Exception:  # noqa: BLE001 - report the original failure
        pass
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"could not type into field (value mismatch)")


def human_click(driver, element):
    """Click with human-like mouse motion + pauses."""
    from selenium.webdriver.common.action_chains import ActionChains

    ActionChains(driver).move_to_element(element).pause(
        random.uniform(0.2, 0.5)).click().perform()
    time.sleep(random.uniform(0.5, 1.2))


def _is_error_page(driver):
    """True when Chrome is showing its network-error interstitial
    instead of the requested page (a transient proxy/egress blip —
    the page body is Chrome's "This site can't be reached" text
    with an ERR_ code). The signup form is never an error page, so
    this is safe to use as a retry signal."""
    try:
        title = (driver.title or "").lower()
        body = driver.find_element("tag name", "body").text.lower()
    except Exception:  # noqa: BLE001 - no readable page at all
        return False
    # Chrome's error page uses a curly apostrophe in "can't"
    # (U+2019); normalise it before matching, or the check below
    # silently never fires.
    title = title.replace("\u2019", "'").replace("\u2018", "'")
    body = body.replace("\u2019", "'").replace("\u2018", "'")
    if "can't be reached" in title or "can't be reached" in body:
        return True
    # Any ERR_ code (ERR_TUNNEL_CONNECTION_FAILED,
    # ERR_NO_SUPPORTED_PROXIES, ...) is Chrome's error page — a
    # real Google page never contains one.
    if "err_" in body or "err_" in title:
        return True
    return False


def _load_page(driver, url, tries=3):
    """Load a URL, retrying when Chrome shows a network-error page
    (transient proxy/egress blip). Returns True when the page loaded
    (not an error page)."""
    for attempt in range(tries):
        try:
            driver.get(url)
        except Exception:  # noqa: BLE001 - retry below
            pass
        time.sleep(2)
        if not _is_error_page(driver):
            return True
        _log.warning("page load %d/%d showed a network-error page; "
                     "retrying %s", attempt + 1, tries, url)
        time.sleep(3)
    return False


def warm_up_session(driver, deep: bool = False):
    """Visit neutral sites before any Google signup URL, so the
    session has history and cookies like a real browsing session.

    ``deep`` runs the ytgm-style warm-up: a few Google searches
    (with a scroll), YouTube, Maps and News. Only useful with a
    persistent profile (``--profile-dir``) — without one the
    history is discarded when Chrome quits, so deep warm-up is
    skipped then. Best-effort: a failed site never aborts the
    attempt."""
    from urllib.parse import quote_plus
    for site in WARM_UP_SITES:
        try:
            driver.get(site)
            time.sleep(random.uniform(1.5, 3.0))
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            pass
    if not deep:
        return
    for query in WARM_UP_SEARCHES:
        try:
            driver.get("https://www.google.com/search?q="
                       + quote_plus(query))
            time.sleep(random.uniform(3, 5))
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight * 0.5);")
            time.sleep(random.uniform(1, 2))
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            pass
    for site, dwell in WARM_UP_DEEP_SITES:
        try:
            driver.get(site)
            time.sleep(dwell + random.uniform(0, 2))
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            pass


def _smart_find(driver, selectors, timeout=5):
    """Try multiple (by, value) selectors; return the first element
    found, or None. ``by`` is 'id', 'xpath', 'name' or 'css'."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    by_map = {"id": By.ID, "xpath": By.XPATH,
              "name": By.NAME, "css": By.CSS_SELECTOR}
    for by, value in selectors:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by_map[by], value)))
        except Exception:  # noqa: BLE001 - try the next selector
            pass
    return None


def _smart_find_visible(driver, selectors, timeout=5):
    """Like _smart_find, but only returns a VISIBLE element. Typing
    into a merely-present element fails when Google's page has a
    hidden duplicate input (seen live 2026-11-22: the username step
    found a present-but-hidden input twice in a row and died with
    ElementNotInteractableException). Detection-only lookups keep
    using _smart_find; anything that gets typed into uses this."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    by_map = {"id": By.ID, "xpath": By.XPATH,
              "name": By.NAME, "css": By.CSS_SELECTOR}
    for by, value in selectors:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.visibility_of_element_located((by_map[by], value)))
        except Exception:  # noqa: BLE001 - try the next selector
            pass
    return None


def _fill_field_smart(driver, element, value):
    """Fill a field with fallbacks: send_keys, then JS value + input/
    change events, then ActionChains click + type."""
    try:
        element.clear()
        element.send_keys(value)
        return True
    except Exception:  # noqa: BLE001 - try the next strategy
        pass
    try:
        driver.execute_script(
            "arguments[0].value = arguments[1]; "
            "arguments[0].dispatchEvent(new Event('input', "
            "{bubbles: true})); "
            "arguments[0].dispatchEvent(new Event('change', "
            "{bubbles: true}));",
            element, value)
        return True
    except Exception:  # noqa: BLE001 - try the next strategy
        pass
    try:
        from selenium.webdriver.common.action_chains import ActionChains

        ActionChains(driver).move_to_element(element).click().send_keys(
            value).perform()
        return True
    except Exception:  # noqa: BLE001 - all strategies failed
        return False


def _click_next(driver):
    """Click the Next / Continue button (several selector fallbacks)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    for xpath in NEXT_BUTTON_SELECTORS:
        try:
            btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(1.5)
            return True
        except Exception:  # noqa: BLE001 - try the next selector
            pass
    return False


def _try_skip_phone(driver):
    """Try to skip phone verification (Skip / Not now). True ONLY when
    a skip button was clicked AND the page actually advanced (URL or
    body changed). A click that changes nothing is NOT a skip — the
    QR-verification page has elements that match a skip selector but
    do nothing when clicked. The absence of a phone field does NOT
    mean we are through (it may be a QR wall instead); the caller
    must verify creation separately."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    for xpath in SKIP_PHONE_SELECTORS:
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, xpath)))
        except Exception:  # noqa: BLE001 - try the next selector
            continue
        before_url = driver.current_url or ""
        try:
            before_body = driver.find_element(By.TAG_NAME, "body").text[:200]
        except Exception:  # noqa: BLE001 - body text is best-effort
            before_body = ""
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception:  # noqa: BLE001 - try the next selector
            continue
        time.sleep(2)
        after_url = driver.current_url or ""
        try:
            after_body = driver.find_element(By.TAG_NAME, "body").text[:200]
        except Exception:  # noqa: BLE001 - body text is best-effort
            after_body = ""
        if after_url != before_url or after_body != before_body:
            return True
    return False


# ------------------------------------------------------------------ QR wall
# Google's crossflow verification page shows a QR code to scan with a
# real phone and has no Skip button and no phone-number field. The
# QR-pause flow saves that code to a file, waits for the user to scan
# it on their own phone, and resumes when they confirm. The QR element
# selectors are tried in order; a plain <img> with a data: URI, a
# <canvas> (toDataURL), or an <img> served from a URL are all handled.
# If no QR element is found, the fallback is a full-page screenshot —
# the QR is still readable from that file.
QR_ELEMENT_SELECTORS = [
    ("xpath", "//canvas"),
    ("xpath", "//img[contains(translate(@alt, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
              "'abcdefghijklmnopqrstuvwxyz'), 'qr')]"),
    ("xpath", "//img[contains(@src, 'data:image')]"),
    ("xpath", "//img[contains(@src, 'chart.googleapis') or "
              "contains(@src, '/qr') or contains(@src, 'qr-code') "
              "or contains(@src, 'qr_code')]"),
    ("xpath", "//*[@aria-label and contains(translate(@aria-label, "
              "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
              "'qr')]"),
]

QR_DIR_NAME = "qr_codes"


def _find_qr_element(driver):
    """Return the first visible QR element of a usable size, or None."""
    from selenium.webdriver.common.by import By

    for by, value in QR_ELEMENT_SELECTORS:
        try:
            if by == "xpath":
                el = driver.find_element(By.XPATH, value)
            else:  # pragma: no cover - only xpath selectors are listed
                continue
        except Exception:  # noqa: BLE001 - try the next selector
            continue
        try:
            if not el.is_displayed():
                continue
            size = el.size or {}
            if size.get("width", 0) >= 40 and size.get("height", 0) >= 40:
                return el
        except Exception:  # noqa: BLE001 - element is unusable, try next
            continue
    return None


def _capture_qr_image(driver):
    """Capture the QR code as PNG bytes. Returns (png_bytes, source)
    or (None, None). ``source`` is one of 'img-data-uri', 'img-url',
    'canvas', 'element'. A data: URI or a served image URL is decoded
    directly (sharpest result); otherwise the element is screenshotted.
    """
    import base64
    import urllib.request

    el = _find_qr_element(driver)
    if el is None:
        return None, None
    tag = (el.tag_name or "").lower()
    # <canvas>: read the pixels back as a PNG data URI.
    if tag == "canvas":
        try:
            data_uri = driver.execute_script(
                "return arguments[0].toDataURL('image/png');", el)
            if data_uri and data_uri.startswith("data:image/png;base64,"):
                return base64.b64decode(
                    data_uri.split(",", 1)[1]), "canvas"
        except Exception:  # noqa: BLE001 - fall through to screenshot
            pass
    # <img>: prefer the raw image (data: URI decoded, URL fetched).
    if tag == "img":
        try:
            src = el.get_attribute("src") or ""
        except Exception:  # noqa: BLE001 - fall through to screenshot
            src = ""
        if src.startswith("data:image/"):
            try:
                return base64.b64decode(src.split(",", 1)[1]), "img-data-uri"
            except Exception:  # noqa: BLE001 - fall through to screenshot
                pass
        elif src.startswith(("http://", "https://")):
            try:
                req = urllib.request.Request(
                    src, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    return data, "img-url"
            except Exception:  # noqa: BLE001 - fall through to screenshot
                pass
    # Any element: screenshot just that element.
    try:
        return el.screenshot_as_png, "element"
    except Exception:  # noqa: BLE001 - caller falls back to full page
        return None, None


def _save_qr(driver, identity, qr_dir=None):
    """Save the QR code to a file. Returns (path, source) where source
    is the capture method ('img-data-uri' | 'img-url' | 'canvas' |
    'element' | 'full-page'). The file is always a readable PNG in a
    ``qr_codes`` directory in the current working directory (or
    ``qr_dir`` when given). Falls back to a full-page screenshot when
    no QR element can be captured cleanly."""
    import os

    directory = qr_dir or QR_DIR_NAME
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, f"qr-{identity.username}-{stamp}.png")
    png, source = _capture_qr_image(driver)
    if png:
        with open(path, "wb") as f:
            f.write(png)
        return os.path.abspath(path), source
    # Fallback: full-page screenshot — the QR is still readable.
    try:
        driver.save_screenshot(path)
        return os.path.abspath(path), "full-page"
    except Exception as exc:  # noqa: BLE001 - nothing more to try
        return "", f"failed to save QR screenshot: {exc}"


def _default_qr_confirm(qr_path: str) -> str:
    """Ask the user to scan the QR code and confirm. Returns 'done'
    on Enter (or any non-abort text), 'abort' on an abort word, EOF
    or Ctrl+C."""
    print()
    print("=" * 64)
    print("GOOGLE NEEDS A PHONE SCAN — QR VERIFICATION")
    print("=" * 64)
    print()
    print("  Google wants you to verify this signup on a real phone.")
    print("  There is no Skip button and no phone-number field here —")
    print("  the QR code is the only way forward.")
    print()
    print(f"  1. Open this file and scan the QR code with your phone:")
    print(f"     {qr_path}")
    print()
    print("  2. Complete the verification on your phone (Google app")
    print("     or camera). Wait until the phone says it is done.")
    print()
    print("  3. Come back here and press Enter to continue.")
    print()
    print("     (Type 'abort' and press Enter to give up this attempt.)")
    print("=" * 64)
    try:
        answer = input("Press Enter once you have scanned the code "
                       "and finished on your phone: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "abort"
    if answer in ("abort", "quit", "exit", "q", "no"):
        return "abort"
    return "done"


def _pause_for_qr_scan(driver, identity, qr_dir=None, confirm=None,
                       wait_after_confirm: float = 180.0):
    """Pause the attempt at the QR wall and resume after the user
    scans the code on their own phone.

    Saves the QR code to a file, prints instructions, waits for the
    user to confirm (``confirm`` callable, or the console prompt),
    then polls for the verification page to advance. Returns
    (status, detail, qr_path) where status is:
      'advanced'     — the QR page is gone (verification done);
                       the caller should continue the signup loop.
      'not_advanced' — the page is still the QR wall after the wait;
                       the account was not created.
      'aborted'      — the user gave up (or input is unavailable).
    """
    qr_path, source = _save_qr(driver, identity, qr_dir)
    _log.info("QR wall: saved QR (%s) to %s", source, qr_path)
    prompt = confirm or _default_qr_confirm
    try:
        answer = (prompt(qr_path) or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "abort"
    if answer in ("abort", "quit", "exit", "q", "no"):
        return ("aborted",
                "user aborted at the QR pause (QR saved at "
                f"{qr_path})", qr_path)
    # The user confirmed: poll until the page moves on (or not).
    deadline = time.monotonic() + wait_after_confirm
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            url = driver.current_url or ""
        except Exception:  # noqa: BLE001 - driver died mid-poll
            return ("not_advanced",
                    "browser died while waiting for the QR page to "
                    "advance", qr_path)
        if "crossflowverification" not in url:
            created, detail = _verify_created(driver)
            return ("advanced",
                    detail or "left the QR verification page", qr_path)
        try:
            body = driver.find_element("tag name", "body").text.lower()
        except Exception:  # noqa: BLE001 - body text is best-effort
            body = ""
        # The QR wall is gone even though the URL slug stayed: the
        # flow moved to a phone/code field or another signup step.
        if "qr code" not in body and (
                _smart_find(driver, PHONE_FIELD_SELECTORS, timeout=1)
                is not None
                or _smart_find(driver, CODE_FIELD_SELECTORS, timeout=1)
                is not None):
            return ("advanced",
                    "QR page replaced by a phone/code step", qr_path)
    return ("not_advanced",
            "QR page did not advance after confirmation "
            f"(QR saved at {qr_path}); the account was not created",
            qr_path)


# ------------------------------------------------------- QR downgrade
# The signup QR wall is Google's DevicePhoneVerification service:
# the QR decodes to accounts.google.com/devicephoneverification/
# begin?... and the samedevice page carries the same begin URL in
# its Continue link (as an Android intent URL). google-qr-sms-helper
# (havenicec/google-qr-sms-helper, MIT) downgrades the SIGN-IN QR
# page to SMS by calling the DevicePhoneVerificationUi batchexecute
# RPCs — MypxBd, then YDtwf after fetching the consent UUID — which
# return [3,null,4] (the non-Android fallback), and then redirecting
# to the uplevelingstep/selection page with idv_xdevice=1. The RPCs
# run in the browser tab itself (same origin, same cookies, same
# TLS as the extension's fetch), with the tab's UA temporarily
# switched to the iPhone UA the extension uses for
# /devicephoneverification/* (its DNR rule 2). On success the tab
# is switched to the iPad Mini UA (the selection endpoint rejects
# desktop UAs, per the extension's README Q2) and navigated to the
# selection page; the step loop then observes what Google shows.
# Honest limit: the downgrade changes a QR scan into an SMS-code
# step — it does NOT remove verification, so it still needs a phone
# number per account. It converts a dead-end wall into the
# phone-field flow the --phone + SMS-code path already handles.
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1")

# In-page JS (execute_async_script): fetch the begin page, extract
# the SNlM0e / Qzxixc / TL tokens, call MypxBd, fetch the consent
# page for its UUID, call YDtwf, and report whether the response
# contains the [3,null,4] fallback state. Same calls, same order
# as the extension's background.js handleDowngrade().
_DOWNGRADE_JS = """
const beginUrl = arguments[0];
const done = arguments[arguments.length - 1];
(async () => {
  try {
    const r1 = await fetch(beginUrl, { credentials: 'include' });
    const html = await r1.text();
    const at = (html.match(/"SNlM0e":"([^"]+)"/) || [])[1];
    const dsh = (html.match(/"Qzxixc":"([^"]+)"/) || [])[1];
    let tl = '';
    const ds0 = html.match(/key:\\s*['"]ds:0['"][^}]*data:\\["([^"]+)"\\]/);
    if (ds0) { tl = ds0[1]; }
    else { const t = html.match(/ADG-[A-Za-z0-9_-]+/); if (t) tl = t[0]; }
    if (!at || !dsh || !tl) {
      return done({ ok: false, tl: tl || '',
        detail: 'begin page missing tokens (at=' + !!at +
          ' dsh=' + !!dsh + ' tl=' + !!tl + ')' });
    }
    async function rpc(rpcid, payload, sourcePath) {
      const url = 'https://accounts.google.com/devicephoneverification/_/DevicePhoneVerificationUi/data/batchexecute'
        + '?rpcids=' + rpcid + '&source-path=' + sourcePath
        + '&hl=en&tl=' + encodeURIComponent(tl) + '&rt=c';
      const body = new URLSearchParams({
        'f.req': JSON.stringify([[[rpcid, JSON.stringify(payload), null, 'generic']]]),
        'at': at });
      const r = await fetch(url, { method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' },
        body: body.toString() });
      return await r.text();
    }
    await rpc('MypxBd', [tl, dsh, false, null, null, false, null, null, 'browser'],
      '%2Fdevicephoneverification%2Finitiate');
    const cr = await fetch('https://accounts.google.com/devicephoneverification/consent'
      + '?flow=browser&plt&dsh=' + encodeURIComponent(dsh)
      + '&hl=en&tl=' + encodeURIComponent(tl), { credentials: 'include' });
    const cHtml = await cr.text();
    const uuid = (cHtml.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/) || [])[0];
    if (!uuid) {
      return done({ ok: false, tl: tl, detail: 'consent page gave no UUID' });
    }
    const yd = await rpc('YDtwf', [null, uuid, dsh, 7, null, tl, false],
      '%2Fdevicephoneverification%2Fmodacintro');
    const flat = yd.replace(/\\s/g, '');
    return done({ ok: flat.indexOf('[3,null,4]') !== -1, tl: tl,
      detail: 'YDtwf: ' + yd.slice(0, 200) });
  } catch (e) {
    return done({ ok: false, tl: '', detail: 'downgrade error: ' + e });
  }
})();
"""


def _extract_begin_url(page_source: str) -> str:
    """Extract the DevicePhoneVerification begin URL from a
    crossflow page's HTML. The QR variant carries it as a plain
    https URL; the samedevice variant carries it inside the
    Continue link as an Android intent:// URL. Returns the full
    https URL, or '' when the page has none."""
    import re as _re

    m = _re.search(r"devicephoneverification/begin\?[^\"'#\s<>]+",
                   page_source or "")
    if not m:
        return ""
    path = (m.group(0)
            .replace("&amp;", "&")
            .replace("\\u003d", "=")
            .replace("\\u0026", "&"))
    return "https://accounts.google.com/" + path


def _try_qr_downgrade(driver, device: str):
    """Attempt the google-qr-sms-helper downgrade on the current
    crossflow page. Returns (status, detail) where status is
    'downgraded' — the [3,null,4] fallback was returned and the
    tab was navigated to the verification-method selection page
    (the caller should continue the step loop and observe what
    Google shows there) — or 'failed' (detail says why; the
    caller falls through to the QR pause / phone flow)."""
    html = driver.page_source or ""
    begin_url = _extract_begin_url(html)
    if not begin_url:
        return ("failed",
                "no devicephoneverification/begin URL on the page")
    _log.info("QR downgrade: begin URL found (%s...)",
              begin_url[:80])
    # The extension uses the iPhone UA for /devicephoneverification/*
    # requests (it simulates the phone that scanned the QR).
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {
            "userAgent": IPHONE_UA,
            "acceptLanguage": "en-US,en;q=0.9",
            "platform": "iPhone",
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Safari", "version": "17"},
                    {"brand": "Not/A)Brand", "version": "99"},
                ],
                "fullVersionList": [
                    {"brand": "Safari", "version": "17.5"},
                    {"brand": "Not/A)Brand", "version": "99.0.0.0"},
                ],
                "platform": "iPhone",
                "platformVersion": "17.5",
                "architecture": "",
                "model": "iPhone",
                "mobile": True,
            },
        })
    except Exception as exc:  # noqa: BLE001 - UA override is best-effort
        _log.warning("QR downgrade: iPhone UA override failed: %s", exc)
    try:
        driver.set_script_timeout(90)
        result = driver.execute_async_script(_DOWNGRADE_JS, begin_url)
    except Exception as exc:  # noqa: BLE001 - downgrade failed, fall through
        return ("failed", f"downgrade script error: {exc}")
    finally:
        try:
            driver.set_script_timeout(30)
        except Exception:  # noqa: BLE001 - best-effort
            pass
    if not isinstance(result, dict) or not result.get("ok"):
        return ("failed",
                (result or {}).get("detail", "downgrade RPCs failed"))
    tl = result.get("tl") or ""
    _log.info("QR downgrade: [3,null,4] returned (%s)",
              result.get("detail", ""))
    # The selection endpoint rejects desktop UAs (extension README
    # Q2) — switch the tab to the iPad Mini profile before
    # navigating there, whatever device started the attempt.
    try:
        _apply_ipad_mini_profile(driver)
    except Exception as exc:  # noqa: BLE001 - best-effort
        _log.warning("QR downgrade: iPad profile for selection page "
                     "failed: %s", exc)
    # Which URL shows the SMS/voice selection for a SIGNUP session
    # is not documented (the extension only covers sign-in, where
    # the QR page's `continue` param already points at the
    # selection page). Try the candidates in order and stop at the
    # first page that actually offers SMS/voice — a redirect to the
    # sign-in identifier page means the candidate was rejected.
    from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
    from urllib.parse import quote as _quote
    crossflow_url = driver.current_url or ""
    _cq = _parse_qs(_urlparse(crossflow_url).query)
    crossflow_tl = (_cq.get("TL") or [""])[0]
    crossflow_continue = (_cq.get("continue") or [""])[0]
    candidates = [
        ("begin-TL + sarp/scc",
         "https://accounts.google.com/uplevelingstep/selection"
         f"?TL={tl}&authuser=0&flowName=GlifWebSignIn&sarp=1&scc=1"
         f"&idv_xdevice=1"),
        ("crossflow-TL + continue",
         "https://accounts.google.com/uplevelingstep/selection"
         f"?TL={crossflow_tl}&continue={_quote(crossflow_continue)}"
         f"&flowName=GlifWebSignIn&idv_xdevice=1"),
        # The begin page itself, now that the [3,null,4] fallback
        # state is set: on sign-in it renders the QR; with the
        # fallback it may render the SMS/phone option instead.
        # (The samedevice page's Continue button opens this URL
        # via an Android intent — a desktop browser cannot fire
        # the intent, so navigate to the https URL directly.)
        ("begin page after downgrade",
         begin_url),
        ("reload crossflow page",
         crossflow_url),
    ]
    # The candidate navigations often "time out" at the renderer's
    # 60s page-load timeout even though the redirect already
    # happened (the load event is just slow through the proxy).
    # A short timeout during the loop cuts ~90s off the downgrade
    # without losing the result (current_url/body are read after
    # every navigation, timeout or not).
    try:
        driver.set_page_load_timeout(20)
    except Exception:  # noqa: BLE001 - best-effort
        pass
    for label, selection_url in candidates:
        if not selection_url:
            continue
        if label == "begin page after downgrade":
            # The extension uses the iPhone UA for
            # /devicephoneverification/* (it simulates the phone
            # that scanned the QR); the tab is currently on the
            # iPad profile for the selection candidates.
            try:
                driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": IPHONE_UA,
                     "acceptLanguage": "en-US,en;q=0.9",
                     "platform": "iPhone"})
            except Exception as exc:  # noqa: BLE001 - best-effort
                _log.warning("QR downgrade: iPhone UA for begin page "
                             "failed: %s", exc)
        try:
            driver.get(selection_url)
            time.sleep(3)
        except Exception as exc:  # noqa: BLE001 - read the URL anyway
            _log.warning("QR downgrade: selection navigation (%s) "
                         "timed out: %s", label, exc)
            time.sleep(2)
        try:
            now_url = driver.current_url or ""
            now_body = driver.find_element(
                "tag name", "body").text.lower()
        except Exception:  # noqa: BLE001 - treat as a miss
            now_url, now_body = "", ""
        offers_sms = (("text message" in now_body
                       or "voice call" in now_body
                       or "sms" in now_body)
                      or _smart_find(driver, PHONE_FIELD_SELECTORS,
                                     timeout=1) is not None)
        _log.info("QR downgrade: selection candidate %s -> %s "
                  "(offers SMS/voice: %s)", label, now_url, offers_sms)
        if offers_sms:
            try:
                driver.set_page_load_timeout(60)
            except Exception:  # noqa: BLE001 - best-effort
                pass
            return ("downgraded",
                    f"[3,null,4] fallback returned; selection page "
                    f"offers SMS/voice via {label} ({now_url})")
    try:
        driver.set_page_load_timeout(60)
    except Exception:  # noqa: BLE001 - best-effort
        pass
    return ("downgraded",
            "[3,null,4] fallback returned but no selection candidate "
            "showed SMS/voice options; last page: "
            f"{driver.current_url}")


def _select_combobox(driver, field_id: str, option_text: str,
                     tries: int = 3) -> bool:
    """Select an option in Google's Material combobox (a div with
    role=combobox, NOT a <select>). Click the combobox with a real
    mouse click to open its listbox, then click the displayed option
    whose text matches. Verifies the selection took (the combobox
    shows the chosen value) and retries. Returns True on success."""
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By

    for _ in range(tries):
        try:
            combo = driver.find_element(
                By.XPATH, f"//div[@id='{field_id}']//div[@role='combobox']")
        except Exception:  # noqa: BLE001 - no combobox with this id
            return False
        try:
            ActionChains(driver).move_to_element(combo).click().perform()
        except Exception:  # noqa: BLE001 - fall through to JS click
            try:
                driver.execute_script("arguments[0].click();", combo)
            except Exception:  # noqa: BLE001 - retry
                continue
        time.sleep(1.0)
        clicked = False
        for option in driver.find_elements(By.XPATH, "//*[@role='option']"):
            try:
                if (option.text or "").strip() == option_text and option.is_displayed():
                    ActionChains(driver).move_to_element(option).click().perform()
                    clicked = True
                    break
            except Exception:  # noqa: BLE001 - try the next option
                continue
        time.sleep(0.8)
        # Verify: the combobox now shows the chosen value.
        try:
            combo = driver.find_element(
                By.XPATH, f"//div[@id='{field_id}']//div[@role='combobox']")
            if option_text in (combo.text or ""):
                return True
        except Exception:  # noqa: BLE001 - retry
            pass
        if not clicked:
            # Menu never opened or option never found — retry.
            continue
    return False


def _select_month_gender(driver, identity) -> None:
    """Fill the month and gender comboboxes on the birthday step.
    Google A/B-tests the form: the fields are either native <select>
    elements or Material combobox divs. Both are handled."""
    from selenium.webdriver.support.ui import Select

    month = _smart_find(driver, [("id", "month"),
                                 ("xpath", "//select[@id='month']")])
    if month is not None:
        month_name = MONTH_NAMES[identity.birth_month - 1]
        done = False
        if month.tag_name == "select":
            try:
                Select(month).select_by_visible_text(month_name)
                done = True
            except Exception:  # noqa: BLE001 - try the combobox path
                pass
        if not done:
            done = _select_combobox(driver, "month", month_name)
        if not done:
            _fill_field_smart(driver, month, str(identity.birth_month))
    day = _smart_find(driver, [("id", "day"),
                               ("xpath", "//input[@name='day']")])
    if day is not None:
        _fill_field_smart(driver, day, str(identity.birth_day))
    year = _smart_find(driver, [("id", "year"),
                                ("xpath", "//input[@name='year']")])
    if year is not None:
        _fill_field_smart(driver, year, str(identity.birth_year))
    gender = _smart_find(driver, [("id", "gender"),
                                  ("xpath", "//select[@id='gender']")])
    if gender is not None:
        gender_label = {"male": "Male", "female": "Female",
                        "rather_not_say": "Rather not say"}.get(
                            identity.gender, "Rather not say")
        done = False
        if gender.tag_name == "select":
            try:
                Select(gender).select_by_value(
                    "1" if identity.gender == "male" else "2")
                done = True
            except Exception:  # noqa: BLE001 - try the combobox path
                pass
        if not done:
            _select_combobox(driver, "gender", gender_label)


# Buttons/links that switch from the "use your existing email" page
# back to creating a Gmail address (Google A/B-tests which page the
# flow lands on after the birthday step).
GMAIL_SWITCH_SELECTORS = [
    "//button[.//span[contains(text(),'Create a Gmail address')]]",
    "//button[contains(text(),'Create a Gmail address')]",
    "//a[contains(text(),'Create a Gmail address')]",
    "//button[.//span[contains(text(),'Get a Gmail address')]]",
    "//button[contains(text(),'Get a Gmail address')]",
    "//span[contains(text(),'Create a Gmail address')]",
]


def _switch_to_gmail_address(driver) -> bool:
    """On the 'use your existing email' page, click through to the
    Gmail-address (username) form. True when the switch was clicked."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    for xpath in GMAIL_SWITCH_SELECTORS:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2)
            return True
        except Exception:  # noqa: BLE001 - try the next selector
            pass
    return False


def _verify_created(driver):
    """Check the page after the phone step. Returns (created, detail).

    Creation is only claimed when the browser actually lands on the
    account home / welcome — never assumed from a successful skip.
    Cheap to call: no sleep, short lookups.
    """
    import re
    url = driver.current_url or ""
    title = (driver.title or "").lower()
    body = ""
    try:
        body = driver.find_element("tag name", "body").text.lower()
    except Exception:  # noqa: BLE001 - body text is best-effort
        pass
    # CAPTCHA / bot challenge — the attempt is blocked, not created.
    if "recaptcha" in body or "are you a robot" in body or "unusual traffic" in body:
        return False, f"blocked by a challenge page ({url})"
    # Account home / welcome — the account exists.
    if ("manageaccount" in url or "myaccount.google.com" in url
            or "mail.google.com" in url or "welcome" in title
            or ("welcome" in body and "gmail" in body)):
        return True, f"account home reached ({url})"
    # Still on a phone or code field — phone verification is pending.
    if _smart_find(driver, PHONE_FIELD_SELECTORS, timeout=2) is not None:
        return False, "still on the phone-number step"
    if _smart_find(driver, CODE_FIELD_SELECTORS, timeout=2) is not None:
        return False, "still on the SMS-code step"
    # Unknown page — report it instead of claiming success.
    snippet = re.sub(r"\s+", " ", body)[:160]
    return False, f"unknown page after phone step: {url} | {snippet}"


def _default_code_prompt(phone: str) -> str:
    """Ask the user for the SMS code they received on their phone."""
    try:
        return input(
            f"SMS code sent to {phone} — type the 6-digit code "
            "(or press Enter to abandon this attempt): ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _submit_phone(driver, phone: str, code_prompt) -> BrowserResult | None:
    """Fill the user's own phone number, submit, and enter the SMS
    code the user types at the prompt. Returns a BrowserResult when
    the phone step is resolved, or None when the phone field is not
    present (caller should handle the skip path instead)."""
    phone_el = _smart_find(driver, PHONE_FIELD_SELECTORS, timeout=5)
    if phone_el is None:
        return None
    human_typing(phone_el, phone)
    if not _click_next(driver):
        return BrowserResult("blocked", "", "Next not found after phone number")
    time.sleep(2)
    code_el = _smart_find(driver, CODE_FIELD_SELECTORS, timeout=10)
    if code_el is None:
        # No code field — maybe the number was accepted outright.
        created, detail = _verify_created(driver)
        return BrowserResult("created" if created else "phone_verification",
                             "", detail)
    prompt = code_prompt or _default_code_prompt
    code = (prompt(phone) or "").strip()
    if not code:
        return BrowserResult("phone_verification", "",
                             "no SMS code entered; attempt abandoned")
    human_typing(code_el, code)
    if not _click_next(driver):
        return BrowserResult("blocked", "", "Next not found after SMS code")
    created, detail = _verify_created(driver)
    return BrowserResult("created" if created else "phone_verification",
                         "", detail)


def create_account(identity: Identity, phone: str | None = None,
                   code_prompt=None, proxy: str | None = None,
                   headless: bool = True,
                   timeout: float = 600.0,
                   qr_pause: bool = True,
                   qr_dir: str | None = None,
                   qr_confirm=None,
                   qr_downgrade: bool = True,
                   device: str = "desktop",
                   entry_point: str = "direct",
                   profile_dir: str | None = None,
                   warm: bool = False) -> BrowserResult:
    """Run ONE Gmail signup in a real Chrome browser. Returns a
    BrowserResult. ``phone`` is the user's OWN number (E.164); when
    Google requires a number and none was supplied, the attempt ends
    as stage='phone_verification' — never a third-party number, never
    a paid SMS service. ``proxy`` is passed to Chrome as
    --proxy-server; ``headless`` defaults to True (no display).

    ``timeout`` (default 600s) is the attempt deadline. It was 300s
    until 2026-11-23: on a slow proxy the warm-up alone can take
    200s and the QR downgrade's candidate navigations another
    ~150s, so attempts kept timing out after reaching the wall
    (Benjamin Miller, Lily Scott). 600s fits the full flow.

    ``qr_pause`` (default True) pauses at Google's QR-verification
    wall instead of abandoning the attempt: the QR code is saved to
    a file (``qr_dir``, default ``qr_codes/`` in the working
    directory), the console tells the user to scan it with their own
    phone, and the flow resumes when they confirm (``qr_confirm``
    callable, or the console prompt). With ``qr_pause`` off, the QR
    wall ends the attempt as stage='phone_verification'.

    ``device`` is 'desktop' (default), 'ipad-mini' or
    'android-phone': the device profiles (google-qr-sms-helper
    iPad Mini spoof; Pixel 8 / Android 14 Chrome) that make Google
    treat the browser as a mobile device, so the verification step
    may offer SMS/voice instead of the QR wall. When it does, the
    existing --phone + SMS-code path completes the account. NOTE
    (verified live 2026-11-22, after the proxy fix): the iPad Mini
    spoof does NOT change the signup verification page — Google
    still shows the QR wall. The Android variant is untested.

    ``entry_point`` ('direct' default, or 'youtube' / 'drive' /
    'photos') starts the signup from a Google service instead of
    the direct signup URL (ytgm). ``profile_dir`` is a persistent
    Chrome profile reused across attempts (cookies/history warm up
    over time). ``warm`` runs a deeper warm-up pass (searches,
    YouTube, Maps, News) before the signup; it only makes sense
    with a persistent profile.

    The flow is a step loop, not a fixed sequence: Google A/B-tests
    the signup (username and password are sometimes one page,
    sometimes two; the "use your existing email" page sometimes
    appears instead of the username form). Each iteration detects
    the current step from the page and handles it, so a variant
    order cannot strand the attempt.
    """
    _require_selenium()
    from selenium.webdriver.common.by import By

    driver = create_driver(proxy=proxy, headless=headless, device=device,
                           profile_dir=profile_dir)
    started = time.monotonic()
    try:
        warm_up_session(driver, deep=warm and bool(profile_dir))
        if warm and not profile_dir:
            _log.warning("--warm without --profile-dir: deep warm-up "
                         "skipped (a fresh profile discards the history)")
        if entry_point != "direct":
            if not _enter_via(driver, entry_point):
                _log.warning("entry point %s failed; falling back to "
                             "direct signup", entry_point)
                if not _load_page(driver, SIGNUP_URL, tries=5):
                    return BrowserResult(
                        "error", identity.email,
                        "signup page did not load after 5 tries "
                        "(network-error page — proxy/egress blip?)")
        elif not _load_page(driver, SIGNUP_URL, tries=5):
            return BrowserResult(
                "error", identity.email,
                "signup page did not load after 5 tries "
                "(network-error page — proxy/egress blip?)")
        time.sleep(2)

        error_page_retries = 0
        downgrade_tried = False
        last_step = ""
        same_step_count = 0
        for _step in range(8):
            # Attempt deadline: abort instead of hanging forever when
            # the proxy stalls or the renderer stops responding.
            if time.monotonic() - started > timeout:
                return BrowserResult(
                    "error", identity.email,
                    f"attempt timed out after {timeout:.0f}s")
            # Driver health: if Chrome/ChromeDriver died, stop now
            # instead of looping on dead lookups until the timeout.
            try:
                url = driver.current_url or ""
            except Exception as e:  # noqa: BLE001 - driver is dead
                return BrowserResult("error", identity.email,
                                     f"browser died: {type(e).__name__}")

            # --- transient network-error page (flaky proxy/egress) ---
            # Chrome sometimes shows "This site can't be reached"
            # (ERR_NO_SUPPORTED_PROXIES / ERR_TUNNEL_...) instead of
            # Google; reloading the same URL usually fixes it. Retry
            # a few times before giving up — a network blip, not a
            # Google decision.
            if _is_error_page(driver):
                if error_page_retries >= 3:
                    return BrowserResult(
                        "error", identity.email,
                        "signup page kept showing a network-error page "
                        "(proxy/egress blip) after 3 reloads")
                error_page_retries += 1
                _log.warning("network-error page; reloading (%d/3)",
                             error_page_retries)
                try:
                    driver.get(url or SIGNUP_URL)
                except Exception:  # noqa: BLE001 - retry loop handles it
                    pass
                time.sleep(3)
                continue

            # --- done? (cheap URL check; full verify runs at the end) ---
            if ("manageaccount" in url or "myaccount.google.com" in url
                    or "mail.google.com" in url):
                created, detail = _verify_created(driver)
                return BrowserResult("created" if created else "blocked",
                                     identity.email, detail)

            # --- challenge / CAPTCHA page ---
            body = ""
            try:
                body = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:  # noqa: BLE001 - body text is best-effort
                pass
            if ("recaptcha" in body or "are you a robot" in body
                    or "unusual traffic" in body):
                return BrowserResult("blocked", identity.email,
                                     f"blocked by a challenge page ({url})")

            # Step dispatch is URL-first (the step slug is in the URL);
            # element detection is the fallback for pages whose URL
            # does not name the step. Detection lookups use a short
            # timeout so a miss is cheap.
            step = ""
            for slug in ("birthdaygender", "username", "password",
                         "emailsignup", "crossflowverification"):
                if slug in url:
                    step = slug
                    break
            # The QR downgrade can land the tab on the
            # DevicePhoneVerification consent page — that IS the
            # phone-verification step (a phone-number prompt), not
            # an unknown page.
            if not step and "devicephoneverification" in url:
                step = "phone"
            _log.info("browser step %d: %s (%s, %.0fs elapsed)",
                      _step + 1, step or "unknown",
                      url.split("?")[0].split("/")[-1],
                      time.monotonic() - started)
            if not step:
                if _smart_find(driver, [("id", "firstName")],
                               timeout=2) is not None:
                    step = "name"
                elif _smart_find(driver, [("id", "day")],
                                 timeout=2) is not None:
                    step = "birthdaygender"
                elif _smart_find(driver, [("id", "username"),
                                          ("xpath",
                                           "//input[@name='Username']")],
                                 timeout=2) is not None:
                    step = "username"
                elif _smart_find(driver, [("xpath",
                                           "//input[@name='Passwd']")],
                                 timeout=2) is not None:
                    step = "password"
                elif (_smart_find(driver, PHONE_FIELD_SELECTORS,
                                  timeout=2) is not None
                      or _smart_find(driver, CODE_FIELD_SELECTORS,
                                     timeout=2) is not None):
                    step = "phone"

            # Same-step guard: a page that never advances (e.g. the
            # username form silently rejecting the typed value —
            # Sophia Wilson looped on it for all 8 steps) ends fast
            # with its body text instead of burning the whole
            # attempt. One repeat is legitimate (a taken username
            # retries on the same step); three is stuck.
            # Crossflow is exempt: the downgrade may legitimately
            # land back on it once.
            if step == last_step:
                same_step_count += 1
            else:
                last_step = step
                same_step_count = 0
            if (same_step_count >= 3
                    and step not in ("crossflowverification", "")):
                import re as _re2
                _snippet = _re2.sub(r"\s+", " ", body)[:300]
                return BrowserResult(
                    "blocked", identity.email,
                    f"stuck on the {step or 'unknown'} step "
                    f"(4 visits, no advance): {url} | {_snippet}")

            # --- name step ---
            if step == "name":
                first = _smart_find_visible(driver, [("id", "firstName"),
                                             ("xpath",
                                              "//input[@name='firstName']")])
                last = _smart_find_visible(driver, [("id", "lastName"),
                                            ("xpath",
                                             "//input[@name='lastName']")])
                if not first or not last:
                    return BrowserResult("blocked", identity.email,
                                         "name fields not found (form changed?)")
                for _el in (first, last):
                    try:
                        _el.clear()
                    except Exception:  # noqa: BLE001 - typing overwrites
                        pass
                human_typing(first, identity.first_name)
                human_typing(last, identity.last_name)
                if not _click_next(driver):
                    return BrowserResult("blocked", identity.email,
                                         "Next button not found after name")
                time.sleep(2)
                continue

            # --- birthday + gender step ---
            if step == "birthdaygender":
                _select_month_gender(driver, identity)
                if not _click_next(driver):
                    return BrowserResult("blocked", identity.email,
                                         "Next button not found after birthday")
                time.sleep(2)
                if "birthdaygender" in driver.current_url:
                    errors = [ln for ln in body.split("\n")
                              if "Please enter" in ln or "Please select" in ln]
                    # Diagnostics: the birthday page varies by flow
                    # (the Cloud Console variant failed here
                    # 2026-11-23 with no visible error text) — save
                    # the page source + the actual inputs so the
                    # field IDs can be fixed instead of guessed.
                    try:
                        import os as _os3
                        _dump_dir = _os3.path.join(_os3.getcwd(), "runs")
                        _os3.makedirs(_dump_dir, exist_ok=True)
                        _stamp = time.strftime("%Y%m%d-%H%M%S")
                        _dump = _os3.path.join(
                            _dump_dir,
                            f"birthday-{identity.username}-{_stamp}.html")
                        with open(_dump, "w", encoding="utf-8") as _f:
                            _f.write(driver.page_source or "")
                        _log.info("birthday page source saved to %s", _dump)
                        _inputs = driver.find_elements(
                            "xpath", "//input | //select | //div[@role='combobox']")
                        _desc = []
                        for _el in _inputs[:20]:
                            try:
                                _desc.append(
                                    f"{_el.tag_name}[id="
                                    f"{_el.get_attribute('id') or ''} "
                                    f"name={_el.get_attribute('name') or ''} "
                                    f"type={_el.get_attribute('type') or ''} "
                                    f"text={(_el.text or '')[:30]!r}]")
                            except Exception:  # noqa: BLE001 - best-effort
                                pass
                        _log.info("birthday page controls: %s",
                                  " | ".join(_desc))
                    except Exception as _exc:  # noqa: BLE001 - best-effort
                        _log.warning("birthday diagnostics failed: %s", _exc)
                    return BrowserResult(
                        "blocked", identity.email,
                        "birthday step did not advance: "
                        + ("; ".join(errors) if errors
                           else "validation error"))
                continue

            # --- "use your existing email" page: switch to Gmail ---
            if step == "emailsignup":
                if _switch_to_gmail_address(driver):
                    time.sleep(2)
                    continue
                return BrowserResult(
                    "blocked", identity.email,
                    "stuck on the 'use your existing email' page "
                    f"({url})")

            # --- username step ---
            if step == "username":
                # The username field sometimes renders a few seconds
                # after the URL changes (slow proxy); retry the find
                # instead of failing the attempt on first miss
                # (Luke Green 2026-11-23: field not found, attempt
                # dead 11s into the step).
                username = None
                for _try in range(3):
                    username = _smart_find_visible(
                        driver, [("id", "username"),
                                 ("xpath",
                                  "//input[@name='Username']")])
                    if username is not None:
                        break
                    _log.warning("username field not found, retrying "
                                 "find (%d/3)", _try + 1)
                    time.sleep(5)
                if username is None:
                    return BrowserResult(
                        "blocked", identity.email,
                        f"username field not found (page: {url})")
                # A rejected username stays on this page with a "taken"
                # message. Refilling the SAME name would loop forever,
                # so mutate it (fresh random suffix) and retry.
                # Google also suggests a guaranteed-available name
                # ("Available: nk1892602") — prefer it over guessing.
                # The field is NOT always cleared by the page after a
                # rejection (Nathan King, 2026-11-22: the old name
                # stayed, so each retry APPENDED to it and the form
                # never advanced) — always clear before typing.
                if "that username is taken" in body:
                    import re as _re
                    _sugg = _re.search(
                        r"available:\s*([a-z0-9][a-z0-9._]*)", body)
                    if _sugg:
                        identity.username = _sugg.group(1)
                        _log.info("username taken; using Google's "
                                  "suggested name %s", identity.username)
                    else:
                        base = _re.sub(r"\d+$", "", identity.username)
                        identity.username = (
                            f"{base}{random.randint(100, 999)}")
                        _log.info("username taken; retrying as %s",
                                  identity.username)
                try:
                    username.clear()
                except Exception:  # noqa: BLE001 - typing overwrites
                    pass
                human_typing(username, identity.username)
                # Settle: Google checks availability ASYNC (debounced)
                # after typing. Clicking Next immediately re-submits
                # while the client still shows the PREVIOUS name's
                # "taken" error, so every retry looks taken forever
                # (Mason Scott, Nathan King: 2–3 "taken" in a row for
                # random suffixes). Wait for the error to clear (up
                # to 6s) before submitting; if it never clears the
                # name really is taken and the loop mutates again.
                for _ in range(12):
                    time.sleep(0.5)
                    try:
                        _live_body = driver.find_element(
                            "tag name", "body").text.lower()
                    except Exception:  # noqa: BLE001 - proceed anyway
                        break
                    if "that username is taken" not in _live_body:
                        break
                _log.info("username field holds: %r",
                          username.get_attribute("value"))
                # Password is sometimes on the same page.
                passwd = _smart_find_visible(driver, [("xpath",
                                               "//input[@name='Passwd']"),
                                              ("xpath",
                                               "//input[@type='password']")],
                                     timeout=3)
                confirm = _smart_find_visible(driver, [("xpath",
                                                "//input[@name='PasswdAgain']"),
                                               ("xpath",
                                                "//input[@name='ConfirmPasswd']")],
                                      timeout=3)
                if passwd is not None:
                    try:
                        passwd.clear()
                    except Exception:  # noqa: BLE001 - typing overwrites
                        pass
                    human_typing(passwd, identity.password)
                if confirm is not None:
                    try:
                        confirm.clear()
                    except Exception:  # noqa: BLE001 - typing overwrites
                        pass
                    human_typing(confirm, identity.password)
                if not _click_next(driver):
                    return BrowserResult("blocked", identity.email,
                                         "Next button not found after username")
                time.sleep(2)
                continue

            # --- password step (separate page) ---
            if step == "password":
                passwd = _smart_find_visible(driver, [("xpath",
                                               "//input[@name='Passwd']"),
                                              ("xpath",
                                               "//input[@type='password']")])
                confirm = _smart_find_visible(driver, [("xpath",
                                                "//input[@name='PasswdAgain']"),
                                               ("xpath",
                                                "//input[@name='ConfirmPasswd']")])
                if passwd is None and confirm is None:
                    return BrowserResult(
                        "blocked", identity.email,
                        f"password fields not found (page: {url})")
                for _el in (passwd, confirm):
                    if _el is not None:
                        try:
                            _el.clear()
                        except Exception:  # noqa: BLE001 - typing overwrites
                            pass
                if passwd is not None:
                    human_typing(passwd, identity.password)
                if confirm is not None:
                    human_typing(confirm, identity.password)
                if not _click_next(driver):
                    return BrowserResult("blocked", identity.email,
                                         "Next button not found after password")
                time.sleep(2)
                continue

            # --- crossflow verification (QR-code phone check) ---
            if step == "crossflowverification":
                # Diagnostics: the exact page state at the wall is
                # the evidence for which downgrade (if any) applies
                # — log the URL, a body snippet, and which
                # verification controls are present, and save the
                # full page source for later inspection.
                import re as _re
                _snippet = _re.sub(r"\s+", " ", body)[:500]
                _log.info("crossflow page: url=%s | body: %s",
                          url, _snippet)
                _log.info("crossflow controls: qr=%s phone_field=%s "
                          "code_field=%s skip=%s sms=%s call=%s",
                          "qr code" in body,
                          _smart_find(driver, PHONE_FIELD_SELECTORS,
                                      timeout=1) is not None,
                          _smart_find(driver, CODE_FIELD_SELECTORS,
                                      timeout=1) is not None,
                          "skip" in body or "not now" in body,
                          "text message" in body or "sms" in body,
                          "call" in body)
                try:
                    import os as _os
                    _dump_dir = _os.path.join(
                        _os.getcwd(), "runs")
                    _os.makedirs(_dump_dir, exist_ok=True)
                    _stamp = time.strftime("%Y%m%d-%H%M%S")
                    _dump = _os.path.join(
                        _dump_dir,
                        f"crossflow-{identity.username}-{_stamp}.html")
                    with open(_dump, "w", encoding="utf-8") as _f:
                        _f.write(driver.page_source or "")
                    _log.info("crossflow page source saved to %s", _dump)
                except Exception as _exc:  # noqa: BLE001 - best-effort
                    _log.warning("could not save crossflow page: %s", _exc)
                # Try the QR downgrade FIRST (before the QR pause):
                # it converts the QR/samedevice wall into the SMS
                # selection page via the DevicePhoneVerification RPCs
                # (google-qr-sms-helper). It needs no phone number
                # itself — it only changes which verification Google
                # offers. When it works, the step loop continues on
                # the selection page; when it fails, fall through to
                # the QR pause / phone flow below. At most once per
                # attempt: the [3,null,4] state does not change the
                # signup crossflow page, so re-running it on the
                # same wall only burns steps.
                if qr_downgrade and not downgrade_tried:
                    downgrade_tried = True
                    status, detail = _try_qr_downgrade(driver, device)
                    _log.info("QR downgrade: %s — %s", status, detail)
                    if status == "downgraded":
                        time.sleep(2)
                        continue
                # Google's device/phone check. The QR wall shows a QR
                # code to scan with a real phone and has NO Skip
                # button, NO phone-number field, and NO other way
                # forward — except the user's own phone. With qr_pause
                # on (default), pause here: save the QR, let the user
                # scan it, then resume the loop on the page the phone
                # verification leaves behind.
                if "qr code" in body:
                    if not qr_pause:
                        return BrowserResult(
                            "phone_verification", identity.email,
                            "Google requires a QR-code scan on a real "
                            "phone (crossflow verification); qr_pause "
                            "is off, so the attempt is abandoned — "
                            "re-run with --qr-pause to scan the code "
                            "yourself")
                    status, detail, qr_path = _pause_for_qr_scan(
                        driver, identity, qr_dir=qr_dir,
                        confirm=qr_confirm)
                    _log.info("QR pause: %s — %s", status, detail)
                    if status == "advanced":
                        # The user scanned the code and the page moved
                        # on. The pause time is the user's, not the
                        # attempt's — restart the attempt clock so a
                        # slow scan cannot trip the timeout.
                        started = time.monotonic()
                        time.sleep(2)
                        continue
                    return BrowserResult(
                        "phone_verification", identity.email, detail)
                # A non-QR crossflow variant: fall through to the
                # normal phone flow.
                step = "phone"

            # --- phone verification step ---
            if step == "phone":
                # Diagnostics for the DevicePhoneVerification consent
                # page (reached via the QR downgrade): what controls
                # does it actually offer — a phone-number input, a
                # Send-SMS button, another way? Save the page source
                # and log every input/button on it.
                if "devicephoneverification" in url:
                    try:
                        import os as _os2
                        _dump_dir = _os2.path.join(_os2.getcwd(), "runs")
                        _os2.makedirs(_dump_dir, exist_ok=True)
                        _stamp = time.strftime("%Y%m%d-%H%M%S")
                        _dump = _os2.path.join(
                            _dump_dir,
                            f"consent-{identity.username}-{_stamp}.html")
                        with open(_dump, "w", encoding="utf-8") as _f:
                            _f.write(driver.page_source or "")
                        _log.info("consent page source saved to %s", _dump)
                        _inputs = driver.find_elements(
                            "xpath", "//input | //button | //a")
                        _desc = []
                        for _el in _inputs[:20]:
                            try:
                                _desc.append(
                                    f"{_el.tag_name}[type="
                                    f"{_el.get_attribute('type') or ''} "
                                    f"id={_el.get_attribute('id') or ''} "
                                    f"name={_el.get_attribute('name') or ''} "
                                    f"text={(_el.text or '')[:40]!r}]")
                            except Exception:  # noqa: BLE001 - best-effort
                                pass
                        _log.info("consent page controls: %s",
                                  " | ".join(_desc))
                    except Exception as _exc:  # noqa: BLE001 - best-effort
                        _log.warning("consent diagnostics failed: %s", _exc)
                # PROBE (2026-11-23): the consent page has no phone
                # field at all — its only control is "Send SMS".
                # Click it once and record what Google does next:
                # an error, a page asking for a number (new finding),
                # or a code-entry page. No number is entered, no
                # payment, no CAPTCHA — this is just reading the
                # next page of the flow.
                if phone is None and "devicephoneverification" in url:
                    try:
                        _sms_btn = driver.find_element(
                            "xpath",
                            "//button[contains(., 'Send SMS')]")
                        _sms_btn.click()
                        _log.info("consent probe: clicked 'Send SMS'")
                        time.sleep(8)
                        _probe_url = driver.current_url
                        try:
                            _probe_body = driver.find_element(
                                "tag name", "body").text[:300]
                        except Exception:  # noqa: BLE001
                            _probe_body = ""
                        _log.info("consent probe result: url=%s | "
                                  "body: %s", _probe_url, _probe_body)
                        _result = BrowserResult(
                            "phone_verification", identity.email,
                            "Consent page has no phone field; clicked "
                            "'Send SMS' once. Next page: "
                            f"{_probe_url} | {_probe_body}")
                        return _result
                    except Exception as _exc:  # noqa: BLE001
                        _log.warning("consent probe failed: %s", _exc)
                if phone:
                    result = _submit_phone(driver, phone, code_prompt)
                    if result is not None:
                        result.email = identity.email
                        return result
                if _try_skip_phone(driver):
                    created, detail = _verify_created(driver)
                    return BrowserResult("created" if created else "blocked",
                                         identity.email, detail)
                import re as _re3
                _snippet = _re3.sub(r"\s+", " ", body)[:300]
                return BrowserResult(
                    "phone_verification", identity.email,
                    "Google requires a phone number and the skip failed; "
                    "no number was supplied, attempt abandoned — re-run "
                    "with --phone +<your number> to use your own number "
                    f"(page: {url} | {_snippet})")

            # --- unknown step: try an explicit skip, else report ---
            if _try_skip_phone(driver):
                time.sleep(2)
                continue
            created, detail = _verify_created(driver)
            return BrowserResult("created" if created else "blocked",
                                 identity.email, detail)
        return BrowserResult("blocked", identity.email,
                             f"too many steps without finishing ({url})")
    except Exception as e:  # noqa: BLE001 - stage it, don't crash
        return BrowserResult("error", identity.email, f"{type(e).__name__}: {e}"[:300])
    finally:
        try:
            close_driver(driver)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
