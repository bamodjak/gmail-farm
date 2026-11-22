# Browser approach (fallback path for Gmail signup)

The HTTP route (`signup.py`) is blocked at Google's obfuscated RPC
layer — the step-submission method names live only in Google's
minified JS, and the flow ends at phone verification + reCAPTCHA
anyway. This document describes the working fallback: drive the real
signup page in a Chrome browser, following the approach proven by
the gmail-account-creator project
(github.com/vichhka-git/gmail-account-creator). That repo was read
(cloned read-only to `~/workspace/storebot/farm/vendor/`) to learn
the technique; `gmail_farm/browser.py` is our own implementation —
no code from it is imported, copied, or run.

## Flow (one attempt)

1. **Launch Chrome** with anti-detection flags + a fresh random
   identity (random Chrome user-agent with a MATCHED window size).
2. **Warm up** the session on neutral sites (google.com,
   wikipedia.org, bbc.com) before touching any signup URL.
3. **Fill the form** with human-like typing (50–180 ms per
   keystroke) and human-like clicks (ActionChains with pauses):
   name → birthday + gender → username + password.
4. **Skip phone verification** (Skip / Not now buttons). If Google
   requires a number anyway, the attempt is ABANDONED — this tool
   never enters a phone number and never pays for one.

## Anti-detection

- `--disable-blink-features=AutomationControlled` +
  `navigator.webdriver` override (`Object.defineProperty(navigator,
  'webdriver', {get: () => undefined})`)
- `excludeSwitches: ["enable-automation"]`,
  `useAutomationExtension: False`
- Random Chrome user-agent (Chrome 131, Windows / macOS / Linux)
  with a window size matched to the UA's OS — a viewport that
  contradicts the UA is an instant fingerprint mismatch
- Human-like typing (random 50–180 ms per keystroke) and
  human-like clicks (ActionChains move + pause + click)
- Session warm-up on neutral sites before any Google URL

## Phone verification

Selectors tried in order to skip (see `browser.py`):

```
 //button[.//span[contains(text(),'Skip')]]
 //button[contains(text(),'Skip')]
 //a[contains(text(),'Skip')]
 //span[contains(text(),'Not now')]
 //button[.//span[contains(text(),'Not now')]]
 //button[contains(@jsname,'LgbsSe') and not(contains(.,'Next'))]
```

Detection (never filled):

- Phone field: `#phoneNumberId`, `//input[@type='tel']`
- SMS code field: `#code`, `//input[@type='tel' and @maxlength='6']`,
  `//input[@name='code']`

The upstream project falls back to 5sim (paid virtual numbers) when
the skip fails. **This project does NOT do that** — no API key is
configured, no number is ever bought, no payment is ever made. A
failed skip ends the attempt as `phone_verification`.

## "Next" button selectors (Google A/B-tests the form)

```
 //button[.//span[contains(text(), 'Next')]]
 //button[@jsname='LgbsSe']
 //div[@jsname='QkNstf']//button
 //button[contains(@class, 'VfPpkd-LgbsSe')]
```

## Resilient element handling

- `smart_find`: try multiple selectors, return the first match.
- `fill_field_smart`: send_keys → JS value + input/change events →
  ActionChains click + type.
- `safe_click` (upstream name): JS click → ActionChains click →
  element.click(), 3 tries.

## Requirements (browser path only)

- Google Chrome installed.
- `selenium` + `webdriver-manager` (`pip install -r requirements.txt`).

Selenium is imported lazily, so the rest of gmail-farm works without
it. `create_account()` raises a clear `RuntimeError` when it is
missing instead of crashing.

## Honest status

This path is implemented and import-verified, but a live Gmail
account has NOT been created with it in this environment (that would
need Chrome + a signup attempt, and any account created would hit
the same phone-verification wall Google puts on the HTTP path).
The module is the correct fallback to run where Chrome is available;
expect most attempts to end at `phone_verification` unless the skip
succeeds, which is IP- and account-history-dependent.
