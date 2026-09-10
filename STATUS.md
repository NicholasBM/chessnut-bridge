# Where this stands

_Reconstructed 2026-09-08 after a session crash; updated 2026-09-09. Not a git
repo, so this file is the record._

## One-line summary

**It works end to end on real hardware.** On 2026-09-09 the appliance signed in to
Chess.com by itself and a move made on the physical board landed in a real game:

```
INFO bridge.chesscom.login: signed in to chess.com as nbaronmorgan (9 cookies)
INFO bridge.chesscom.write: submitting d2d3 to game 1026053674 as lt
INFO bridge.chesscom.write: move accepted by game 1026053674 (HTTP 200)
```

That closes both of the unknowns this file listed for weeks. `deploy/install.sh`
runs on the Pi, the service binds port 80, polling returns the real game list, the
sign-in flow works against the live site, and the write path works against a live
game. The test suite is green at 638 passing.

Two bugs were found by running it for real, both written up below: a false
"two-factor required" that discarded a *successful* sign-in, and LEDs that stayed
lit after a move.

## What is done

| Area | Where | State |
|---|---|---|
| BLE board link | `src/bridge/chessnut/{ble,protocol}.py` | done, tested |
| Public read API | `src/bridge/chesscom/public.py` | done, tested |
| Move write path | `src/bridge/chesscom/write.py`, `tcn.py` | done, tested, **proven live**: `d2d3` accepted by game 1026053674 |
| Chess.com sign-in | `src/bridge/chesscom/login.py` | done, tested, **proven live** |
| Encrypted password store | `src/bridge/state/secrets.py` (`CredentialStore`) | done, tested |
| Sync engine | `src/bridge/sync/{engine,reconcile,settle}.py` | done, tested |
| Encrypted session store + device key | `src/bridge/state/{secrets,store}.py` | done, tested |
| Web UI (auth, SSE status page, actions, sign-in form) | `src/bridge/web/*` | done, tested |
| Process wiring / entrypoint | `src/bridge/main.py`, `__main__.py` | done, tested |
| Deploy (systemd unit, install.sh, README, conf example) | `deploy/` | done; **run successfully on the Pi 2026-09-09** |

## DECIDED (2026-09-09): the Pi holds the password

Supersedes the earlier "browser-extension helper" decision recorded here, which is
gone. It was rejected as overkill: the appliance page is already behind the owner's
password, so there is no need for a pairing window, a public endpoint, or a second
piece of software to install on another machine.

Owner's call, made explicitly: _"it's only a chess password, I am not worried about
it."_ The trade-off was raised first — a stored password is a larger exposure than
a cookie, and the key sits on the same SD card as the data — and accepted.

### What was built

- **`src/bridge/chesscom/login.py`** — the login flow in `urllib`: `GET /login`
  for the hidden `_token`, then `POST /login_check` with `_username`, `_password`,
  `_token`, `_remember_me=on`. Cookies are collected with `http.cookiejar`.
  `_remember_me` is not optional: without it the session dies in hours and an
  appliance that needs its password retyped daily is not an appliance.
  Every failure is its own type, because each needs a different action from the
  owner: `BadCredentials` (retype), `VerificationRequired` (2FA/captcha — this
  account cannot be used this way), `ChallengePresented` (Cloudflare, a network
  problem), `LoginFormChanged` (*this code* is stale), `SessionIncomplete`,
  `LoginUnavailable`. Each declares `retryable`.
- **`CredentialStore`** in `src/bridge/state/secrets.py` — Fernet, its own file
  `credentials.enc`, mode 0600, same device key as the session store and the same
  key/data separation. Its own file so "forget my password but stay signed in" is
  expressible. Every read failure lands on "no password", never on a dead service.
- **`BridgeService.sign_in` / `_sign_in_from_store` / `sign_in_again` /
  `forget_credentials`** — the password is stored only *after* a sign-in succeeds,
  and cleared when a failure is non-retryable, so the appliance never loops against
  a password that cannot work. `MIN_RETRY_SECONDS` (900 s) is a politeness floor;
  the button on the page forces past it.
- **Web**: `GET/POST /chesscom/login`, `POST /chesscom/forget`,
  `POST /chesscom/retry-login`, all protected by omission from `PUBLIC_PATHS`. The
  password is never echoed back into the HTML. The status page now carries real
  wording that distinguishes "the appliance will fix itself" from "you must act".

### The write-path invariant, preserved

`MoveWriter` still never retries: a timed-out POST may have applied. The one
automatic resend is on `write.SessionExpired` (401), which is *proof* the move was
not applied — so `_submit` signs in again and calls a separate `_submit_once` that
cannot sign in and cannot recurse. Tests assert that ambiguous and rejected
failures trigger neither a sign-in nor a resend.

### Tests

- `tests/test_chesscom_login.py` (26) — every failure named separately, the token
  found in either attribute order, four password-leak tests (reprs, logs,
  tracebacks, error strings).
- `tests/test_credentials.py` (25) — encryption checked by searching the bytes on
  disk, key/data separability, tampering, mode, every corruption path, refusal of
  half a credential.
- `tests/test_service.py` (53) and `tests/test_web_app.py` (66) extended.

## The first live sign-in found a real bug (2026-09-09)

Worth recording in full, because the lesson generalises: **a page-text heuristic
was allowed to overrule direct evidence.**

The owner's first attempt on hardware failed with `VerificationRequired` — "chess.com
wants extra verification" — on an account with no 2FA, which they could sign into in
a browser in one shot. Two compounding causes, both proven by fetching the real page
(63908 bytes) and measuring where the markers hit:

1. **The markers were bare substrings.** `"verification"` matched
   `<meta name="google-site-verification">` at offset 665 — present in the `<head>`
   of *every* chess.com page. `"captcha"` matched the JavaScript feature-flag names
   `'recovery_turnstile_captcha'` and `'signup_issue_challenge_captcha'` at offsets
   24587 and 24941. So the check fired on every page chess.com serves.
2. **Order was wrong.** The marker check in `_post_credentials` ran *before* the
   `SESSION_COOKIE` check, so a sign-in that had genuinely succeeded was thrown away.

Because `VerificationRequired.retryable is False`, `_sign_in_from_store` would then
have **deleted the stored password**. The feature could not have worked for anybody
on any account.

The fix: markers split into `_VERIFICATION_PATHS` (URL path fragments),
`_VERIFICATION_PHRASES` (whole human phrases, matched only against visible text with
`<head>`, `<script>` and comments stripped) and a separate `_CHALLENGE_PHRASES` for
Cloudflare; and `_post_credentials` reordered so that **a granted session cookie
outranks any inference from the page**. `_classify` checks the challenge markers
first, because that is the retryable one. Both phrase lists were validated against
the real page for zero false positives before being committed.

Regression tests use a fixture carrying the actual SEO meta tags and the actual JS
flag array, so the specific false positives cannot come back. `find_csrf_token` was
also confirmed against the real live `_token` for the first time.

## The first live move found a second bug (2026-09-09)

The move was accepted, but the two squares of it stayed lit on the board afterwards.
`_drive_leds` in `service.py` was written as:

```python
if squares:
    await self.board.set_leds(squares)
```

The empty set is a real instruction, not the absence of one — the LED command lights
*exactly* the squares given and has no "leave as you were". Once a move is submitted
there is nothing left to highlight, so `squares` goes empty, the `if` skipped the
write, and the last command stood until the board slept. `clear_leds` existed in
`ble.py` and nothing called it.

The same pass fixed two adjacent faults the rewrite exposed: LEDs are now written
only when the set *changes* (frames arrive ~10/s and the Zero 2 W shares one radio
between BLE and WiFi), and a write that returns `False` — which is what `set_leds`
does when the board is away, rather than raising — is no longer recorded as lit, so
it retries on the next frame. `_lit` is reset on a connect edge because a board that
slept comes back dark.

## Still open

- **`lastDate` mismatch response is uncaptured** (`write.py:334` `_classify`): when
  observed, add it as its own recoverable `WriteError` type.
- `ChallengePresented` (Cloudflare) is modelled and tested against a fake, never
  against the real thing.
- **`GAME_FINISHED` is recorded but never shown.** `sync/engine.py` sets it on
  `Snapshot.last_discontinuity`, and nothing outside that module reads it, so when a
  followed game ends its row just vanishes from the page. Carrying it into `View`
  would let the page say "game finished — choose another".

## The appliance

| | |
|---|---|
| Host | `chessnut.local` (192.168.4.36), passwordless SSH as `nbm` |
| Installed at | `/opt/chessnut-bridge`, repo staged at `~/chessnut-bridge` |
| Settings | `/boot/firmware/chessnut-bridge.conf` — holds the page password |
| Update | rsync the repo to the Pi, then `sudo deploy/install.sh` |

The board sleeps when the link drops and the BLE reconnect backoff caps at 30 s;
tapping a piece wakes it. That is normal Chessnut Go behaviour, not a fault.

## Sanity check

```
source .venv/bin/activate && python -m pytest -q      # 634 passed
```
