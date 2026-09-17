# VPS rebuild: the old IP is not our box any more, and the migration script would have died on it

**Agent:** claude-computer (interactive session, owner's machine)
**Date:** 2026-09-16
**Branch:** `fix/migrate-stale-host-key` → PR
**Lane:** deploy / infrastructure

## Context

Continues 2026-09-15 `2cf3a88` ("deploy: fix VPS bootstrap for rebuild on a new box"),
which landed without a worklog entry — this file covers both. That commit rewrote
`deploy/setup_vps.sh` as a thin wrapper over `vps_update.sh`, installed the seven cron
files that actually exist plus the weekly-report timer, and added
`deploy/migrate_to_new_vps.ps1` to carry the two things a `git clone` cannot restore:
`.env` and the gitignored `data/` state.

## Measured today

**1. `178.105.41.226` answers, but it is not ours.** It pings (167ms) and serves SSH,
so "the box is gone" is too simple. But the host key has changed and our deploy key is
rejected:

```
Host key for 178.105.41.226 has changed and you have requested strict checking.
Offending ECDSA key in known_hosts:3
root@178.105.41.226: Permission denied (publickey,password,keyboard-interactive).
```

Different host key + our `authorized_keys` absent = a different machine on a recycled
IP. **Nothing may be uploaded there** — `.env` carries live API keys. The existing guard
in `migrate_to_new_vps.ps1` that refuses this address was already right and is now
independently justified.

**2. The bootstrap path re-checked, no defects found.** `setup_vps.sh` and
`vps_update.sh` parse clean; the seven `install_cron` calls match the seven
`deploy/*_cron.txt` files on disk one-for-one; the committed blobs are LF (the working
tree is CRLF via autocrlf, which never reaches the VPS — the Linux checkout reads the
blob). `pytest tests/ -q --collect-only` → 3584 collected.

## Fixed

`migrate_to_new_vps.ps1` would have failed at step 1 on exactly the condition above.
`StrictHostKeyChecking=accept-new` covers a host that is entirely *unknown*; a host in
`known_hosts` under a *different* key fails hard regardless, and the raw ssh error reads
as an attack rather than as a stale record. Since rebuilding onto a recycled or reused IP
is the normal case for this script, it now detects that specific failure, prints the
served fingerprint and the offending `known_hosts` line, and **refuses to continue** —
uploading `.env` past an unverified host key is the one thing this script must not do.
`-ClearHostKey` runs `ssh-keygen -R` and retries, after the owner has matched the
fingerprint against the provider console.

Detection uses `ssh` itself rather than `ssh-keyscan`: the bundled Windows keyscan cannot
negotiate with the remote's OpenSSH 10 (`unsupported KEX method
sntrup761x25519-sha512@openssh.com`) and reports a reachable host as having no keys at
all — which would have made a keyscan-based check fail *open*, the wrong direction for a
guard standing in front of a secrets upload.

## Verification

- Script parses (`[Parser]::ParseFile`), `-WhatIfOnly` preflight runs, old-IP guard fires.
- The new detection regex was run against the **real** captured ssh failure output and
  matched, surfacing both the `SHA256:` fingerprint and the offending known_hosts line.
- The `-ClearHostKey` path itself is **not** end-to-end tested — that needs a live host
  with a changed key, which by definition we do not have yet.

## Open — needs the owner

The bot is running **nowhere**. Every forward test is stalled, not accumulating evidence
(see memory `paper_arms_stale_local`: all local arm state froze mid-June with `closed: []`).
Unblocking is one step the agent cannot take: **provision a new VPS and hand over the IP**,
then `.\deploy\migrate_to_new_vps.ps1 -NewHost <ip>`. Nothing else in the deploy lane can
move until then.

Related: the GEX cron (memory `gex_dealer_exposure_walls`) lives in unmerged PR #112 and
is therefore absent from `setup_vps.sh`'s seven. When #112 merges, an eighth `install_cron`
line is needed or the pipeline stays dark on the rebuilt box.
