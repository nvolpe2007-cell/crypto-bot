<#
.SYNOPSIS
  Rebuild the crypto-bot on a NEW VPS from the GitHub repo + local state.

.DESCRIPTION
  The old VPS (178.105.41.226) died with its .env and data/ on board. Both are
  gitignored, so GitHub alone cannot restore a working bot. This script does the
  three things a clone cannot:

    1. wires SSH access and rewrites the ~/.ssh/config 'crypto-bot-vps' alias
    2. uploads .env   (secrets; never committed)
    3. uploads data/  state, ledgers and the trade journal (gitignored)

  then runs deploy/setup_vps.sh on the box, which clones the repo, builds the
  venv, installs systemd + crons + the weekly timer, and asserts the unit is active.

.EXAMPLE
  .\migrate_to_new_vps.ps1 -NewHost 203.0.113.10
  .\migrate_to_new_vps.ps1 -NewHost 203.0.113.10 -SkipState   # code only, fresh books
  .\migrate_to_new_vps.ps1 -NewHost 203.0.113.10 -ClearHostKey # IP recycled: drop the stale host key
#>
param(
    [Parameter(Mandatory = $true)][string]$NewHost,
    [string]$User    = "root",
    [string]$KeyFile = "$HOME\.ssh\crypto_bot_vps",
    [string]$EnvFile,
    [switch]$SkipState,
    [switch]$ClearHostKey,
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"
# $PSScriptRoot is not populated during param binding in PS 5.1, so resolve here.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir "..")).Path
if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot ".env" }
$Target   = "$User@$NewHost"

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)       { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn($msg)     { Write-Host "  !!  $msg" -ForegroundColor Yellow }

Write-Host "=== crypto-bot migration -> $Target ===" -ForegroundColor White

# ── 0. Preflight ─────────────────────────────────────────────────────────────
Step 0 "Preflight"
if (-not (Test-Path $KeyFile))  { throw "SSH key not found: $KeyFile" }
if (-not (Test-Path $EnvFile))  { throw ".env not found: $EnvFile  (the bot cannot run without it)" }
Ok "key  $KeyFile"
Ok ".env $EnvFile"

# The old VPS is gone; refuse to clobber a host that is still serving the bot.
if ($NewHost -eq "178.105.41.226") { throw "That is the OLD (dead) VPS address. Pass the new one." }

if ($WhatIfOnly) { Warn "WhatIfOnly set - stopping before any remote change."; return }

# ── 1. SSH reachability + key install ────────────────────────────────────────
Step 1 "Checking SSH to $Target"
# IdentitiesOnly=yes: without it ssh offers every default key in ~/.ssh (id_rsa,
# id_ecdsa, id_ed25519, ...) BEFORE the one named by -i, and each offer burns one
# of the server's MaxAuthTries (6 by default on Ubuntu). On a box where our key
# is not installed yet that budget is exhausted before the password prompt is
# even answered, and sshd drops the connection - which surfaces as
# "Connection closed by <host> port 22" and reads exactly like a rejected
# password. Measured on the 2026-09-17 rebuild: 7 keys offered, connection
# closed, password never actually evaluated.
$sshOpts = @("-i", $KeyFile, "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15")
$probe = & ssh @sshOpts -o BatchMode=yes $Target "echo READY" 2>&1

# A conflicting known_hosts entry fails HARD and no StrictHostKeyChecking value
# rescues it - accept-new only covers a host that is entirely unknown. Rebuilding
# onto a recycled or reused IP hits this routinely, and the raw ssh error reads as
# an attack rather than as a stale record, so handle it explicitly. (Detected via
# ssh itself, not ssh-keyscan: the bundled Windows keyscan cannot negotiate with
# a modern OpenSSH server here - "unsupported KEX method" - and would report a
# reachable host as having no keys at all.)
$probeText = ($probe | Out-String)
if ($probeText -match "REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed") {
    Warn "$NewHost is already in known_hosts with a DIFFERENT host key."
    Warn "Normal for a rebuilt box or a recycled IP - but it is also what a"
    Warn "man-in-the-middle looks like, and this script uploads .env (live API keys)."
    ($probe | Where-Object { $_ -match "SHA256:|Offending" }) | ForEach-Object { Write-Host "      $_" }
    if (-not $ClearHostKey) {
        throw "Stale host key for $NewHost. Confirm that fingerprint in your provider console, then re-run with -ClearHostKey."
    }
    & ssh-keygen -R $NewHost 2>&1 | Out-Null
    Ok "stale host key removed (-ClearHostKey); the new one is accepted on first connect"
    $probe = & ssh @sshOpts -o BatchMode=yes $Target "echo READY" 2>&1
}

if ($probe -notmatch "READY") {
    Warn "Key auth not working yet. Installing public key (you'll be asked for the root password once)."
    $pub = Get-Content "$KeyFile.pub" -Raw
    $installCmd = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qF '$($pub.Trim())' ~/.ssh/authorized_keys 2>/dev/null || echo '$($pub.Trim())' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; echo KEY_INSTALLED"
    # PubkeyAuthentication=no for the SAME MaxAuthTries reason as $sshOpts, and
    # it matters more here: this is the one call that is *expected* to fall
    # through to a password, so it must not spend the server's auth budget on
    # keys we already know are not installed. Without these two options this
    # line fails with "Connection closed" on a fresh box every time.
    & ssh -o StrictHostKeyChecking=accept-new `
          -o PubkeyAuthentication=no -o PreferredAuthentications=password `
          $Target $installCmd
    $probe = & ssh @sshOpts -o BatchMode=yes $Target "echo READY" 2>&1
    if ($probe -notmatch "READY") { throw "Key auth still failing after install. Check the VPS console." }
}
Ok "passwordless SSH works"

# ── 2. Repoint the ~/.ssh/config alias ───────────────────────────────────────
Step 2 "Updating ~/.ssh/config alias 'crypto-bot-vps'"
$cfgPath = "$HOME\.ssh\config"
$block = @"
Host crypto-bot-vps
    HostName $NewHost
    User $User
    IdentityFile ~/.ssh/crypto_bot_vps
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
"@
if (Test-Path $cfgPath) {
    Copy-Item $cfgPath "$cfgPath.bak-$(Get-Date -Format yyyyMMddHHmmss)"
    $cfg = Get-Content $cfgPath -Raw
    # drop any existing crypto-bot-vps block, keep everything else
    $cfg = [regex]::Replace($cfg, '(?ms)^Host\s+crypto-bot-vps\b.*?(?=^Host\s|\z)', '')
    $cfg = $cfg.TrimEnd() + "`n`n" + $block
} else { $cfg = $block }
Set-Content -Path $cfgPath -Value $cfg -Encoding utf8
Ok "alias now points at $NewHost (old config backed up)"

# ── 3. Upload .env ───────────────────────────────────────────────────────────
Step 3 "Uploading .env"
& ssh @sshOpts $Target "mkdir -p /opt/crypto-bot"
& scp @sshOpts $EnvFile "${Target}:/opt/crypto-bot/.env"
& ssh @sshOpts $Target "chmod 600 /opt/crypto-bot/.env"
Ok ".env in place (setup_vps.sh will strip any BOM)"

# ── 4. Upload gitignored state ───────────────────────────────────────────────
if ($SkipState) {
    Warn "SkipState set - the new box starts with empty books/journal."
} else {
    Step 4 "Uploading data/ state (gitignored, so GitHub cannot restore it)"
    $patterns = @('*_state.json','*ledger*','trade_journal.*','attribution.db','daily_circuit.json')
    $files = foreach ($p in $patterns) {
        Get-ChildItem "$RepoRoot\data" -Filter $p -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Length -lt 5MB }
    }
    $files = $files | Sort-Object FullName -Unique
    if (-not $files) { Warn "no state files matched - nothing to restore" }
    else {
        & ssh @sshOpts $Target "mkdir -p /opt/crypto-bot/data"
        foreach ($f in $files) { & scp @sshOpts $f.FullName "${Target}:/opt/crypto-bot/data/" | Out-Null }
        $kb = [math]::Round(($files | Measure-Object Length -Sum).Sum / 1KB, 1)
        Ok "$($files.Count) state file(s), $kb KB"
    }
}

# ── 5. Bootstrap the box ─────────────────────────────────────────────────────
Step 5 "Running deploy/setup_vps.sh on the VPS (clone, venv, systemd, crons, timer)"
$bootstrap = @'
set -e
apt-get update -qq
apt-get install -y git >/dev/null 2>&1
cd /opt/crypto-bot
if [ ! -d .git ]; then
  cp .env /tmp/.env.keep 2>/dev/null || true
  [ -d data ] && cp -r data /tmp/data.keep || true
  cd / && rm -rf /opt/crypto-bot
  git clone https://github.com/nvolpe2007-cell/crypto-bot.git /opt/crypto-bot
  cp /tmp/.env.keep /opt/crypto-bot/.env 2>/dev/null || true
  [ -d /tmp/data.keep ] && mkdir -p /opt/crypto-bot/data && cp -r /tmp/data.keep/. /opt/crypto-bot/data/ || true
fi
bash /opt/crypto-bot/deploy/setup_vps.sh
'@ -replace "`r`n", "`n"

# Base64 rather than `$bootstrap | ssh ... "bash -s"`.
#
# The -replace above is correct and still not enough: piping a multi-line string
# to a NATIVE command makes PowerShell re-split it and write each line with the
# system newline, which on Windows puts the CRLFs straight back. bash then reads
# `setup_vps.sh\r` and reports "No such file or directory" - and the bare CR
# returns the cursor mid-line, so the error prints as the garbled
# ": No such file or directoryy/setup_vps.sh". Measured on the 2026-09-17 run.
#
# Encoding sidesteps the pipeline's text handling entirely: one argv-safe token
# goes over, and the bytes bash receives are the bytes built here.
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bootstrap))
& ssh @sshOpts $Target "echo $b64 | base64 -d | bash"
if ($LASTEXITCODE -ne 0) { throw "Remote bootstrap failed (exit $LASTEXITCODE). Nothing below this point has run." }

# ── 6. Verify ────────────────────────────────────────────────────────────────
Step 6 "Verifying"
# SINGLE-quoted in PowerShell, double-quoted inside for the remote shell.
# The previous form used \$(...) on the assumption that backslash escapes the
# expansion. PowerShell has no backslash escape - the \ is literal and $(...)
# interpolated LOCALLY, so `systemctl` ran on the Windows box and the whole
# verify step died with CommandNotFoundException.
$verify = 'echo "unit:  $(systemctl is-active crypto-bot)"; ' +
          'echo "timer: $(systemctl is-active weekly_report.timer)"; ' +
          'echo "--- crons ---"; crontab -l 2>/dev/null | grep -v "^#"; ' +
          'echo "--- last 15 log lines ---"; ' +
          'journalctl -u crypto-bot --no-pager -n 15'
& ssh @sshOpts $Target $verify

Write-Host "`n=== Migration complete ===" -ForegroundColor White
Write-Host "Watch live:  ssh crypto-bot-vps `"journalctl -u crypto-bot -f`""
