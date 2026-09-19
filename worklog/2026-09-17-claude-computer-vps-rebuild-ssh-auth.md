# VPS rebuilt and reachable: the blocker was Ubuntu Desktop, not the password

**Agent:** claude-computer (interactive session, owner's machine)
**Date:** 2026-09-17
**Branch:** `fix/migrate-identities-only` → PR
**Lane:** deploy / infrastructure

## Context

Continues `worklog/2026-09-16-claude-computer-vps-rebuild-host-key.md`, whose one open
item was "provision a new VPS and hand over the IP". That happened today. PR #132 merged
(`f429d33`); master collects 3584 tests.

## The new box

InterServer VPS `vps3629076`, **162.35.186.42**, Ubuntu 24.04.4 LTS, 2 cores, ~6 GB RAM,
119 GB disk. Identity confirmed two independent ways before anything was uploaded: the
owner's InterServer panel, and `PTR 162.35.186.42 -> vps3629076.trouble-free.net`. Worth
recording because the *old* address also still answers SSH while belonging to someone
else — "it responds" proves nothing here.

## Two failures, and only the second one was real

**1. Host key changed, three times.** Expected: each OS reinstall regenerates them. This
is exactly the case PR #132 built for, and the detection fired correctly on the real
error rather than a synthetic one. The `-ClearHostKey` path that entry flagged as
untested has now been exercised end to end.

**2. `Connection closed by 162.35.186.42 port 22` after the password prompt.** This
looked like a rejected password and was not. `ssh -v` showed the client offering **seven**
default keys (`id_rsa`, `id_ecdsa`, `id_ed25519`, …) before reaching the password;
Ubuntu's `MaxAuthTries` is 6, so the budget was exhausted and sshd dropped the connection
without ever evaluating the password. Forcing `-o PubkeyAuthentication=no -o
PreferredAuthentications=password` changed the symptom to a clean `Permission denied`,
which is what a genuinely wrong password looks like — and that distinction is the whole
diagnostic value.

## The actual root cause of the long outage

**The reinstalls were installing Ubuntu *Desktop*.** InterServer's reinstall form defaults
its Version dropdown to `24.04 Desktop 2GB+ recommended`, and Ubuntu Desktop ships no SSH
server. So the box answered ICMP with port 22 closed, across two rebuilds, while every
theory on the table was about passwords, bans or boot timing.

The signature to remember: **ping succeeds, port 22 refuses, and it never resolves.**
That is "no sshd installed", not "still booting" and not fail2ban — a ban produces
timeouts, not refusals. Reinstalling with the plain `24.04` template fixed it in one go.

## Fixed here

`deploy/migrate_to_new_vps.ps1`:

- `$sshOpts` gains **`IdentitiesOnly=yes`**, so `-i` means only that key and the
  MaxAuthTries budget is not spent on keys we know are absent.
- The key-install call — the one path deliberately expected to fall through to a password
  — gains `PubkeyAuthentication=no` + `PreferredAuthentications=password`. Without these
  it fails with "Connection closed" on *every* fresh box, which is the worst kind of bug:
  it misattributes its own failure to the operator's password.

## Two more bugs the real run exposed

The first migration attempt printed "Migration complete" while installing **zero** crons.
Both faults are in `migrate_to_new_vps.ps1` and neither can be caught without a live host:

**Step 5 sent CRLF to bash.** The here-string *is* normalised with a CR/LF replace — and
that is not enough, because piping a multi-line string to a NATIVE command makes
PowerShell re-split it and write each line with the system newline, restoring the CRLFs.
bash read `setup_vps.sh\r`, and the bare CR returned the cursor mid-line, so the error
surfaced as the garbled `: No such file or directoryy/setup_vps.sh`. Now base64-encoded
into one argv-safe token; verified by round-tripping a two-line script through the host.

**Step 6 escaped a subexpression with a backslash.** PowerShell has no backslash escape,
so `$(...)` interpolated LOCALLY and `systemctl` ran on the Windows box —
`CommandNotFoundException`. Rewritten single-quoted with double quotes inside.

The dangerous part was the combination: no exit-code check followed the bootstrap, so a
remote step that never ran still produced a Step 6 header and a "Migration complete"
banner. Silent failure *and* a success summary. An exit-code check now guards it.

## Verification — the bot is LIVE

- `systemctl is-active crypto-bot` → **active**, enabled; `weekly_report.timer` active.
- **13 crons installed** (swing 4h-majors, tsmom, trend_ensemble, lev_perp ×4,
  meme_cohort ×2, meme_radar ×2, callout_scorecard, trade_close_notifier).
- Kraken websockets up: `[PublicWS] connected`, `[TradeFeed] connected`,
  `[BookFeed] connected`.
- First live heartbeat since mid-June: `equity=$500.00 pnl=$+0.00 trades=0 open=none`,
  `[FUNNEL] seen=90 ... directional_shelved=90` (correct — the scalper is shelved),
  `[SUBSYSTEMS] all OK (15 tasks)`.
- `-SkipState` did what it was for: clean $500 book, no three-month hole in the sample.

Note: the box had already been bootstrapped once at ~23:37–23:50 by another agent, so
the first run's Step 5 failure was real but harmless (`[ ! -d .git ]` was already false).
The crons were the part that never got installed, and the second run fixed that.

## Open

1. **GEX cron still missing.** PR #112 remains unmerged and CONFLICTING, so
   `setup_vps.sh` installs the set above and not the GEX line. That pipeline stays dark
   on the rebuilt box until #112 lands (memory `gex_dealer_exposure_walls`).
2. The box boots to `graphical.target` — a template leftover, harmless with no DE
   installed, but `systemctl set-default multi-user.target` would be tidier.
3. `deploy/setup_outreach_vps.sh` and the Remy units in `D:\agent-swarm\deploy\` now have
   a host to install onto; neither has been deployed.
