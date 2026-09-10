# Chessnut bridge

Play your Chess.com correspondence games on a real board.

A Raspberry Pi sits in the corner of the room holding a Bluetooth link to a
[Chessnut Go](https://www.chessnutech.com/) board. When you move a piece, it
works out what move you made and submits it to the right game on Chess.com. When
your opponent replies, it lights the two squares of their move so you can play it
on the board. Nothing to launch, no app to open — the Pi is powered on and it is
already doing it.

It is deliberately small: no cloud service, no database, no JavaScript build, six
Python dependencies. Everything lives on one SD card.

```
INFO bridge.chesscom.login: signed in to chess.com as <you> (9 cookies)
INFO bridge.chesscom.write: submitting d2d3 to game 1026053674
INFO bridge.chesscom.write: move accepted by game 1026053674 (HTTP 200)
```

## What you need

| | |
|---|---|
| **Raspberry Pi Zero 2 W** | Any 64-bit Pi with built-in Bluetooth LE works — Zero 2 W, 3, 4, 5. The Zero 2 W is the one this is proven on, and it is the cheapest of them, fanless, and idles at about a watt. (The original Zero W is 32-bit only; see the note in step 1.) |
| **A good power supply** | Not a phone charger you had lying around. Undervoltage on a Pi shows up as Bluetooth that mysteriously stops working. |
| **microSD card, 8 GB or more** | The whole appliance, settings included, lives here. |
| **Chessnut Go board** | Other Chessnut models use the same protocol and will probably work, but have not been tried. |
| **A Chess.com account without two-factor authentication** | The Pi signs in on your behalf, and it cannot answer a 2FA prompt. It says so plainly rather than blaming your password. |

The board reports which squares are occupied, not which pieces are on them, so
the bridge works out your move by asking "which legal move from the position
Chess.com reports would produce what I can see?" That is why it needs to be
following a specific game, and why it can tell a castle from a king move.

## 1. Flash the card

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

- **Operating system** → Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**.
  Lite, because there is no screen to show a desktop to. 64-bit, because that is
  what the pinned dependencies have ready-built wheels for — on 32-bit, installing
  `cryptography` means compiling it, which takes the better part of an hour on a
  Zero.
- Click the **gear / "Edit settings"** button before writing, and fill in:
  - **Hostname**: `chessnut`. This is how you reach it: `http://chessnut.local/`.
  - **Enable SSH**, ideally with your public key rather than a password.
  - **Username and password** — remember these, they are how you get in.
  - **WiFi network and password**, plus your **country**. There is no ethernet
    port on a Zero and no keyboard to fix this later, so getting it wrong here
    means pulling the card back out.

Write the card. This is the only step where a mistake costs you a re-flash, so it
is worth checking the WiFi password twice.

## 2. Put your settings on the card

Leave the card in your computer after Imager finishes and open the small
partition that appears — **`bootfs`**. Copy
[`deploy/chessnut-bridge.conf.example`](deploy/chessnut-bridge.conf.example) onto
it, **rename it to `chessnut-bridge.conf`** (dropping `.example`), and edit two
lines:

```
chesscom_username=your-chess-com-username
web_password=something-at-least-8-characters
```

`web_password` protects the Pi's own web page. **It is not your Chess.com
password** — you will enter that later, on the page itself. If `web_password` is
missing or shorter than 8 characters the page serves nothing but an explanation
of what to fix. That is on purpose: this page can submit moves in your real
games, and quietly falling back to no password would be the worst possible
outcome of a typo.

The rename is not busywork. This repository ships the file under a name the Pi
deliberately ignores, because the real one holds a password and a tracked file
with the real name is the file people edit in a checkout — one `git add` away from
being in a public history forever. `.gitignore` ignores `chessnut-bridge.conf`
for the same reason.

Both of these sit in plain text on a FAT32 partition, which cannot be avoided —
it is the only way to tell a machine with no screen anything. See
[`deploy/README.md`](deploy/README.md#what-goes-on-the-card) for what that does
and does not protect.

Eject the card, put it in the Pi, power it on, and give it two or three minutes
to expand its filesystem and join your network.

## 3. Get the code onto the Pi

```
ssh <your-username>@chessnut.local
```

If that fails, the Pi is not on your WiFi — check the credentials you gave Imager
before anything else.

This repository is private, so the simplest route is to copy it from the machine
that has a clone. **From your laptop**, in the checkout:

```
rsync -az --delete --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
    ./ <your-username>@chessnut.local:~/chessnut-bridge/
```

<details>
<summary>Or clone it on the Pi directly</summary>

Needs GitHub credentials on the Pi, which is why it is not the default. With a
[personal access token](https://github.com/settings/tokens) that has `repo`
scope, git will prompt for it as the password:

```
sudo apt install -y git
git clone https://github.com/NicholasBM/chessnut-bridge.git ~/chessnut-bridge
```

If you make the repository public, this becomes the easier of the two.
</details>

## 4. Install

**On the Pi**:

```
cd ~/chessnut-bridge
sudo deploy/install.sh
```

Expect several minutes — most of it is pip building a virtualenv on a 1 GHz core.
It creates a `chessnut` system account, copies the code to `/opt`, turns on
BlueZ's `AutoEnable` so the Bluetooth adapter powers up after a reboot, installs
a systemd unit, and starts it. If the service fails to come up it prints the
journal and exits non-zero rather than claiming success.

It finishes by telling you where the page is:

```
== Result
  running. The page is at http://chessnut.local/
```

This is the update path too. Every step checks before it acts, so running it
again after an `rsync` is how you deploy a change.

## 5. Set it up from the page

Open **http://chessnut.local/** on your phone or laptop.

1. **Sign in to the page** with the `web_password` from step 2. Once per browser —
   the cookie survives reboots, so a power cut does not send you looking for the
   SD card.
2. **Sign in to Chess.com.** The page asks for your Chess.com username and
   password, and the Pi performs the login itself. Tick **"Remember it"** and the
   password is stored encrypted on the Pi, so it can sign itself back in when the
   session lapses. Without that, a lapsed session means your moves silently stop
   being sent until you retype it. Your call:
   [`deploy/README.md`](deploy/README.md#where-things-live-and-why-they-are-apart)
   sets out the trade-off and where the encryption key lives.
3. **Turn the board on** and make sure nothing else is holding it. A Bluetooth
   board can only talk to one thing at a time, so if the Chessnut app on your
   phone or a Mac you paired it with once still has the link, the Pi cannot see it
   at all. Close the app; the page shows **"Connected to Chessnut GO"** within a
   few seconds.
4. **Choose a game.** Your correspondence games are listed with whose turn it is
   and how long is left. Press **Follow** on one. Nothing is chosen for you,
   because two of your games in the same opening look identical to a board with no
   memory, and following the wrong one would send a legal move into a game you
   were not playing.
5. **Set the pieces up to match.** The page tells you which squares are wrong
   while you do it, and lights them on the board. When it says **"The board
   matches the game"**, you are playing.

## Playing

Move a piece. After a couple of seconds of it standing still, the move goes to
Chess.com, and the two squares light up for **seven seconds** as confirmation
that it went in.

When your opponent replies, the from and to squares of their move light up. Play
it on the board and the lights go out. If you set up a position the bridge cannot
account for, it says so on the page rather than guessing — and it never invents a
move.

The board **sleeps** when the link drops, which is normal Chessnut Go behaviour
and not a fault. The page says "not connected" in amber rather than red. Tapping a
piece wakes it.

A move is **never resent on a guess.** If a submission times out, the bridge has
no way to know whether it applied, so it stops and tells you rather than risking
playing the same move twice. The one exception is a session Chess.com rejected
outright, which is proof the move did not apply: it signs in again and sends it
once more.

## When something is wrong

The page is the intended diagnostic, and it is written to render before anything
has succeeded — no board, no network, no login. If the bridge loop dies the web
server keeps running and keeps reporting what it can see, because a process that
exited on the way down would take the only explanation with it.

```
systemctl status chessnut-bridge
journalctl -u chessnut-bridge -f
```

| Symptom | Usually |
|---|---|
| `chessnut.local` does not resolve | Wrong WiFi details given to Imager, or your network blocks mDNS — try the Pi's IP address. |
| Page says only what is misconfigured | `web_password` missing or under 8 characters in `chessnut-bridge.conf`. |
| Board never connects | Something else has it: the Chessnut app, or a computer it was paired with. One link at a time. |
| Connected, but no position ever arrives | It rebuilds the link itself after 30 seconds. If that does not fix it, power-cycle the board — the firmware occasionally needs it. |
| "Extra verification" when signing in to Chess.com | The account has two-factor authentication. It cannot be used this way. |

[`deploy/README.md`](deploy/README.md) covers the file layout, the encryption
key, and the rest of the operational detail. [`STATUS.md`](STATUS.md) records what
has actually been proven against the real board and the real site, including the
two bugs that only running it for real could find.

## Notes on the code

The interesting decisions are in the module docstrings rather than here. In short:

- **Level-driven, not edge-driven.** The board is read as "here is the current
  position", never as a stream of changes, so a missed frame or a reconnect cannot
  put the bridge permanently out of step.
- **Reconciliation by legal-move enumeration.** Diffing squares would be fooled by
  castling, en passant, promotion and a piece knocked over. Asking python-chess
  which legal move produces the observed occupancy is not.
- **`urllib`, not `requests`.** On 512 MB of RAM a second HTTP client has to earn
  its place.
- **Credentials never reach a log, a repr, or a traceback**, and there are tests
  that assert it.

```
python -m pytest -q      # 659 passed
```
