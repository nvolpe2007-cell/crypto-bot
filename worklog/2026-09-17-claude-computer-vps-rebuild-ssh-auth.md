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

## Verification

- `[Parser]::ParseFile` clean; `-WhatIfOnly` preflight passes against the real host.
- Key auth to `root@162.35.186.42` works; `ssh crypto-bot-vps` alias repointed.
- Remote confirmed: no `ubuntu-desktop`/`gnome-shell` packages, Python 3.12.3, git 2.43.0.
- **The migration itself has NOT been run** — the harness refused it as a production
  deploy. Nothing is on the box yet; `/opt/crypto-bot` does not exist.

## Open

1. **Run the migration** (owner):
   `.\deploy\migrate_to_new_vps.ps1 -NewHost 162.35.186.42 -SkipState`
   `-SkipState` is deliberate: every arm's state froze mid-June with `closed: []`, and
   resuming it puts a three-month hole *inside* each sample, which corrupts any t-stat
   computed across it. Pre-June history remains in git and the vault.
2. **GEX cron still missing.** PR #112 remains unmerged and CONFLICTING, so
   `setup_vps.sh` still installs seven crons, not eight. The pipeline stays dark on the
   rebuilt box until that lands (memory `gex_dealer_exposure_walls`).
3. The box boots to `graphical.target` — a template leftover, harmless with no DE
   installed, but `systemctl set-default multi-user.target` would be tidier.
