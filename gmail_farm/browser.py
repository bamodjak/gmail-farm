"""Browser fallback for Gmail signup (Selenium + Chrome).

The HTTP route (signup.py) is blocked at Google's obfuscated RPC
layer, so this module is the working fallback path: it drives the
real signup page in a Chrome browser with anti-detection measures,
following the approach proven by the gmail-account-creator project
(github.com/vichhka-git/gmail-account-creator). That repo was read
(cloned read-only to ~/workspace/storebot/farm/vendor/) to learn the
technique; this module is MY OWN implementation — no code from it is
imported, copied, or run.

What it does, per attempt:
  1. Launch Chrome with anti-detection flags + a fresh random identity
     (random Chrome user-agent with a MATCHED window size, so the
     viewport never contradicts the UA's OS).
  2. Warm up the session on neutral sites (Google, Wikipedia, BBC)
     before touching any signup URL.
  3. Fill the signup form with human-like typing (random per-keystroke
     delays) and human-like clicks (ActionChains with pauses).
  4. Attempt to SKIP phone verification (Skip / Not now buttons).
     If Google requires a number anyway, the attempt is ABANDONED —
     this module never enters a phone number and never pays for one
     (no 5sim, no SMS service). The stage is recorded as
     "phone_verification" and the loop moves on.

Requirements (only for this module; the HTTP path does not need them):
  - Google Chrome installed.
  - selenium + webdriver-manager (pip install -r requirements.txt).

Selenium is imported LAZILY inside the functions, so importing this
module (and the rest of gmail-farm) works fine without Selenium
installed — only create_account() raises a clear error when it is
missing.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from .identities import Identity

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
# has history and cookies like a real browsing session.
WARM_UP_SITES = [
    "https://www.google.com",
    "https://www.wikipedia.org",
    "https://www.bbc.com",
]

SIGNUP_URL = (
    "https://accounts.google.com/signup/v2/webcreateaccount"
    "?flowName=GlifWebSignIn&flowEntry=SignUp"
)

# "Next / Continue" button selectors on Google's signup forms
# (Google A/B-tests the form, so several fallbacks are tried in order).
NEXT_BUTTON_SELECTORS = [
    "//button[.//span[contains(text(), 'Next')]]",
    "//button[@jsname='LgbsSe']",
    "//div[@jsname='QkNstf']//button",
    "//button[contains(@class, 'VfPpkd-LgbsSe')]",
]

# Phone-verification skip selectors (tried in order). The last one
# clicks any non-Next button — used only after the explicit Skip /
# Not now texts are tried, because a generic button can be the wrong
# one on some form variants.
SKIP_PHONE_SELECTORS = [
    "//button[.//span[contains(text(),'Skip')]]",
    "//button[contains(text(),'Skip')]",
    "//a[contains(text(),'Skip')]",
    "//span[contains(text(),'Not now')]",
    "//button[.//span[contains(text(),'Not now')]]",
    "//button[contains(@jsname,'LgbsSe') and not(contains(.,'Next'))]",
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


def create_driver():
    """Build an anti-detection Chrome WebDriver instance (3 tries)."""
    _require_selenium()
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager

    ua_string, (width, height) = random.choice(USER_AGENTS)
    options = ChromeOptions()
    for flag in CHROME_FLAGS:
        options.add_argument(flag)
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument(f"--user-agent={ua_string}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    last_err = None
    for _ in range(3):
        try:
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            driver.maximize_window()
            driver.set_page_load_timeout(30)
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
            return driver
        except Exception as e:  # noqa: BLE001 - retry, then raise
            last_err = e
            time.sleep(2)
    raise RuntimeError(
        f"could not start Chrome WebDriver after 3 attempts: {last_err}")


def human_typing(element, text, delay_range=(0.05, 0.18)):
    """Type with human-like random per-keystroke delays."""
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(*delay_range))


def human_click(driver, element):
    """Click with human-like mouse motion + pauses."""
    from selenium.webdriver.common.action_chains import ActionChains

    ActionChains(driver).move_to_element(element).pause(
        random.uniform(0.2, 0.5)).click().perform()
    time.sleep(random.uniform(0.5, 1.2))


def warm_up_session(driver):
    """Visit neutral sites before any Google signup URL."""
    for site in WARM_UP_SITES:
        try:
            driver.get(site)
            time.sleep(random.uniform(1.5, 3.0))
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
    """Try to skip phone verification (Skip / Not now). True when a
    skip button was clicked, or when no phone step is present."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    for xpath in SKIP_PHONE_SELECTORS:
        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, xpath)))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(1.5)
            return True
        except Exception:  # noqa: BLE001 - try the next selector
            pass
    # No skip button — check whether a phone field is even present.
    # If not, there is no phone step and we are through.
    return _smart_find(driver, PHONE_FIELD_SELECTORS, timeout=5) is None


def create_account(identity: Identity, timeout: float = 120.0) -> BrowserResult:
    """Run ONE Gmail signup in a real Chrome browser. Returns a
    BrowserResult. Never enters a phone number and never pays for
    one: if Google requires a number and the skip fails, the attempt
    ends as stage='phone_verification'."""
    _require_selenium()
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select

    driver = create_driver()
    try:
        warm_up_session(driver)
        driver.get(SIGNUP_URL)
        time.sleep(2)

        # --- name ---
        first = _smart_find(driver, [("id", "firstName"),
                                     ("xpath", "//input[@name='firstName']")])
        last = _smart_find(driver, [("id", "lastName"),
                                    ("xpath", "//input[@name='lastName']")])
        if not first or not last:
            return BrowserResult("blocked", identity.email,
                                 "name fields not found (form changed?)")
        human_typing(first, identity.first_name)
        human_typing(last, identity.last_name)
        if not _click_next(driver):
            return BrowserResult("blocked", identity.email,
                                 "Next button not found after name")

        # --- birthday + gender ---
        month = _smart_find(driver, [("id", "month"),
                                     ("xpath", "//select[@id='month']")])
        day = _smart_find(driver, [("id", "day"),
                                   ("xpath", "//input[@name='day']")])
        year = _smart_find(driver, [("id", "year"),
                                    ("xpath", "//input[@name='year']")])
        if month is not None:
            try:
                Select(month).select_by_visible_text(
                    MONTH_NAMES[identity.birth_month - 1])
            except Exception:  # noqa: BLE001 - try typing the number
                _fill_field_smart(driver, month,
                                  str(identity.birth_month))
        if day is not None:
            _fill_field_smart(driver, day, str(identity.birth_day))
        if year is not None:
            _fill_field_smart(driver, year, str(identity.birth_year))
        gender = _smart_find(driver, [("id", "gender"),
                                      ("xpath", "//select[@id='gender']")])
        if gender is not None:
            try:
                Select(gender).select_by_value(
                    "1" if identity.gender == "male" else "2")
            except Exception:  # noqa: BLE001 - gender is optional
                pass
        if not _click_next(driver):
            return BrowserResult("blocked", identity.email,
                                 "Next button not found after birthday")

        # --- username + password ---
        username = _smart_find(driver, [("id", "username"),
                                        ("xpath",
                                         "//input[@name='Username']")])
        if username is None:
            return BrowserResult("blocked", identity.email,
                                 "username field not found")
        human_typing(username, identity.username)
        passwd = _smart_find(driver, [("xpath",
                                       "//input[@name='Passwd']"),
                                      ("xpath",
                                       "//input[@type='password']")])
        confirm = _smart_find(driver, [("xpath",
                                        "//input[@name='ConfirmPasswd']")])
        if passwd is not None:
            human_typing(passwd, identity.password)
        if confirm is not None:
            human_typing(confirm, identity.password)
        if not _click_next(driver):
            return BrowserResult("blocked", identity.email,
                                 "Next button not found after username")

        # --- phone verification: skip or abandon ---
        time.sleep(2)
        if _try_skip_phone(driver):
            return BrowserResult("created", identity.email,
                                 "created (phone step skipped or absent)")
        phone = _smart_find(driver, PHONE_FIELD_SELECTORS, timeout=5)
        code = _smart_find(driver, CODE_FIELD_SELECTORS, timeout=5)
        if phone is not None or code is not None:
            return BrowserResult(
                "phone_verification", identity.email,
                "Google requires a phone number and the skip failed; "
                "no number entered, attempt abandoned")
        return BrowserResult("created", identity.email,
                             "created (no phone step detected)")
    except Exception as e:  # noqa: BLE001 - stage it, don't crash
        return BrowserResult("error", identity.email, f"{type(e).__name__}: {e}"[:300])
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
