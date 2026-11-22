# gmail-farm

Create Google (Gmail) accounts over HTTP, in a loop — no browser.

General-purpose and reusable: no hardcoded personal details. Every
setting is a CLI flag or an environment variable. Random names,
usernames, passwords, birthdays and genders are generated per attempt.

## What it does

```
python3 gmail_farm.py --count 5            # attempt 5 accounts
python3 gmail_farm.py --count 0 --delay 120 # loop forever, 2 min apart
python3 gmail_farm.py --count 1 --dry-run   # bootstrap only, no signup step
python3 gmail_farm.py --count 3 --proxy http://127.0.0.1:8080
```

Each attempt is recorded in `accounts.jsonl` (one JSON object per
line: email, password, stage reached, timestamp). The file is
created with mode 600 because it contains passwords — keep it private.
Logs go to `gmail-farm.log` and the console.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 gmail_farm.py --count 1
```

Requires Python 3.9+ and `curl_cffi` (browser TLS impersonation).

## Options

| Flag | Env | Default | Meaning |
| --- | --- | --- | --- |
| `--count` | `GMAIL_FARM_COUNT` | 5 | accounts to attempt (0 = forever) |
| `--delay` | `GMAIL_FARM_DELAY` | 60 | seconds between attempts |
| `--max-retries` | `GMAIL_FARM_MAX_RETRIES` | 3 | retries per HTTP request |
| `--out` | `GMAIL_FARM_OUT` | accounts.jsonl | JSONL accounts file |
| `--log-file` | `GMAIL_FARM_LOG` | gmail-farm.log | log file |
| `--proxy` | `GMAIL_FARM_PROXY` | (none) | http(s) proxy URL |
| `--password-length` | `GMAIL_FARM_PASSWORD_LENGTH` | 14 | generated password length |
| `--max-consecutive-failures` | `GMAIL_FARM_MAX_CONSEC_FAILURES` | 5 | stop after N failures (0 = never) |
| `--dry-run` | — | off | bootstrap only, never submit a step |
| `--verbose` | — | off | verbose logging |

## Layout (one job per module)

```
gmail_farm.py              CLI entrypoint (thin wrapper)
gmail_farm/
  config.py                CLI/env config + validation
  identities.py            random identity generation
  http_client.py           curl_cffi session, retries, cookies, proxy
  signup.py                Google signup flow driver + stage detection
  storage.py               resume-safe JSONL account store
  logging_setup.py         console + file logging
```

Resume-safe: restarting never retries or duplicates a username that
is already in `accounts.jsonl`. Each record is appended with a flush
+ fsync, so a crash cannot lose it.

## Honest status (verified 2026-11-22)

There is **no public API to create consumer @gmail.com accounts**.
The Gmail API and Identity Platform `signUp` do not register
`@gmail.com` addresses. The only path is the web signup flow, which
is Google's proprietary "lifecycle" JS app
(`AccountLifecyclePlatformSignupUi`). Its step RPCs are undocumented
and obfuscated in a minified JS bundle, the page embeds a reCAPTCHA
site key, and the flow ends at **phone verification** (SMS / QR /
same-device) — and can also require a selfie check.

Live test (1 attempt, 2026-11-22):
- Session bootstrap **works**: the signup page loads, cookies and
  flow tokens (RPC endpoint, reCAPTCHA key, TL token) are extracted.
- The first step RPC was **rejected with HTTP 404** — the method
  path does not exist, because the exact method names are not public.
- No account was created. No phone number was entered, nothing paid.

So today this tool gets as far as HTTP allows (bootstrap + precise
stage detection) and stops cleanly instead of pretending to succeed.
To finish an account, the remaining work is: reverse-engineer the
exact lifecycle RPC method names/payloads from Google's JS bundle,
solve reCAPTCHA, and provide a phone number for verification — the
last two cannot be done by an HTTP-only tool.

## Safety

- Never enters a phone number, never solves a CAPTCHA, never pays.
- Use it only for accounts you are entitled to create, and respect
  Google's Terms of Service and rate limits. The default 60s pacing
  exists for that reason.
