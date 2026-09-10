# Putting this on a Raspberry Pi

The appliance is a Pi Zero 2 W that stays powered on, holds a Bluetooth link to a
Chessnut Go, and mirrors it into 3-day correspondence games on Chess.com. It has
no screen and no keyboard, so everything you tell it either goes on the SD card
before it boots or through the page it serves.

## What goes on the card

Two files on the small partition that appears when you put the card in a computer
(`bootfs` on macOS and Windows):

1. **WiFi and SSH** — Raspberry Pi Imager writes these for you if you fill in its
   advanced options. Do that; there is no other way in.
2. **`chessnut-bridge.conf`** — copy `chessnut-bridge.conf.example`, set
   `chesscom_username` and `web_password`.

`web_password` protects the appliance's own page, and is not your Chess.com
password. If it is missing or shorter than 8 characters, **the page serves nothing
but an explanation of what to fix**. That is deliberate: the page can submit moves
in real games, and a silent fallback to no password would be the worst possible
outcome of a typo.

Both files sit in plain text on a FAT32 partition. There is no way around that —
it is the only channel a machine with no screen has — and it is no worse than what
is already there, because the key that decrypts the stored Chess.com session lives
on the same card. None of this protects against somebody holding the card. What it
protects is everyone else on your network.

## Installing

From a checkout on the Pi:

```
sudo deploy/install.sh
```

It is the update path too — every step checks before it acts. It creates a
`chessnut` system account, copies the code to `/opt/chessnut-bridge`, builds a
virtualenv from the pinned `requirements.txt`, turns on BlueZ's `AutoEnable` so
the adapter powers up after a reboot, installs the unit, and starts it. If the
service fails to come up it prints the journal and exits non-zero rather than
reporting success.

Expect the virtualenv step to take several minutes on a Zero 2 W.

## Where things live, and why they are apart

| Path | What | Why there |
|---|---|---|
| `/opt/chessnut-bridge` | code and virtualenv | root-owned, read-only to the service |
| `/var/lib/chessnut-bridge` | encrypted Chess.com session, sync state | the data, rewritten as it runs |
| `/var/lib/chessnut-bridge/credentials.enc` | your Chess.com password, encrypted | its own file, so forgetting it need not sign you out |
| `/etc/chessnut-bridge/session.key` | the device key | **not** with the data it decrypts |
| `/boot/firmware/chessnut-bridge.conf` | your settings | the only thing you can edit without the Pi running |

The key is in a different directory from the data on purpose. The likely accident
is copying or sharing the state directory, and a copy of it is useless without a
second file nobody thought to take. Being on the same card, it is no defence
against physical possession — a deliberate trade for surviving a power cut with
nobody present to type a passphrase.

Replacing the key makes the appliance forget the stored Chess.com session and
password **and** signs every browser out, because the cookie is signed with a key
derived from it. That is the correct behaviour rather than a bug.

Deleting `credentials.enc` — or pressing "Forget the password" on the page — opts
out of unattended sign-in without signing you out. The session keeps working until
Chess.com expires it, and then the page asks you to sign in again.

## Checking on it

```
systemctl status chessnut-bridge
journalctl -u chessnut-bridge -f
curl -s http://localhost/healthz          # says only whether the process is up
```

The page itself is the intended diagnostic, and it is written to render before
anything has succeeded — no board, no poll, no login. If the bridge loop dies the
web server keeps running and the page keeps reporting what it can see, because a
process that exited on the way down would take the only explanation with it.

`/healthz` is the one path on the appliance reachable without the password, so it
deliberately carries nothing about the account.

## Things worth knowing

- **Port 80** comes from `CAP_NET_BIND_SERVICE` in the unit, not from running as
  root. If that capability is missing the service exits saying so, rather than
  quietly moving to a port you would never think to try.
- **The board sleeps** as soon as the link drops — that is the Chessnut Go's
  normal behaviour, not a fault. The page says "not connected" in amber rather
  than red, and tapping a piece wakes it.
- **Nothing is chosen for you.** Two of your games in the same opening look
  identical to a board with no memory, so following one is an explicit choice.
- **Signing in to Chess.com** happens on the appliance's own page: "Sign in to
  Chess.com" takes your Chess.com username and password, and the Pi performs the
  login itself. Tick "Remember it" and the password is stored encrypted, so the
  appliance can sign itself back in when the session lapses — without that, a
  lapsed session means moves stop being sent until you retype it. An account with
  two-factor authentication cannot be signed in this way; the page says so
  instead of blaming your password.
- **A move is never resent on a guess.** The one exception is a session that
  Chess.com rejected outright, which is proof the move was not applied: the
  appliance signs in again and sends it once more. Anything ambiguous — a
  timeout, a 500 — still waits for you.
