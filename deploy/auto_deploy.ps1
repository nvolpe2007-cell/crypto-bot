<#
.SYNOPSIS
  One-shot push-and-deploy for the crypto-bot VPS.

.DESCRIPTION
  Designed to be called from Claude Code OR by the user.

  Steps:
    1. Verifies local repo is at D:\crypto-bot and on master
    2. Optionally commits all staged + tracked changes (-Commit switch + -Message)
    3. Pushes to origin/master (no-op if nothing to push)
    4. SSHes into the VPS (Host alias `crypto-bot-vps`) and runs vps_update.sh
    5. Tails the last 20 journal lines so we can see the bot booted cleanly

  Requires:
    - Passwordless SSH set up to crypto-bot-vps (one-time install via gist)
    - gh CLI authenticated for git push (already configured)

.PARAMETER Commit
  Stage and commit all tracked changes before pushing.

.PARAMETER Message
  Commit message; required when -Commit is used.

.PARAMETER Wait
  Seconds to wait after restart before grabbing journal tail. Default 5.

.EXAMPLE
  .\deploy\auto_deploy.ps1
  # Push whatever's already committed, restart bot, show logs.

.EXAMPLE
  .\deploy\auto_deploy.ps1 -Commit -Message "tune RSI threshold to 75"
  # Stage + commit + push + restart.
#>
param(
    [switch]$Commit,
    [string]$Message,
    [int]$Wait = 5
)

$ErrorActionPreference = 'Stop'
$RepoRoot = 'D:\crypto-bot'
$VPSHost  = 'crypto-bot-vps'

function Step($msg) { Write-Host "`n[*] $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }

Set-Location $RepoRoot

# ── 1. Sanity ─────────────────────────────────────────────────────────────────
Step "Verifying local repo state"
$branch = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'master') { Fail "Not on master (current: $branch)" }
Write-Host "  branch: $branch"
Write-Host "  HEAD:   $((& git log -1 --oneline).Trim())"

# ── 2. Optional commit ────────────────────────────────────────────────────────
# SAFETY: never `git add -A`. Caller must stage with `git add <files>` before
# invoking with -Commit. We only commit what's already in the index.
# This prevents accidentally shipping .env, .env.vps, embedded worktrees,
# editor crud, or untracked exploration scripts.
if ($Commit) {
    if (-not $Message) { Fail "-Commit requires -Message" }
    $staged = & git diff --cached --name-only
    if (-not $staged) {
        Fail "-Commit was set but the index is empty. Stage files first with 'git add <files>'."
    }
    Step "Committing staged files"
    $staged | ForEach-Object { Write-Host "  + $_" }
    $body = @"
$Message

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
"@
    & git commit -m $body | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "git commit failed" }
}

# ── 3. Push ───────────────────────────────────────────────────────────────────
# Note: `git push` writes its progress to stderr by default. Under
# `$ErrorActionPreference='Stop'` PowerShell treats stderr from native cmds
# as a terminating exception even on exit 0, so we capture both streams as
# strings and only fail on a non-zero exit code.
Step "Pushing to origin/master"
$prevPref = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$pushOutput = & git push origin master 2>&1 | Out-String
$pushExit  = $LASTEXITCODE
$ErrorActionPreference = $prevPref
$pushOutput.TrimEnd().Split("`n") | ForEach-Object { Write-Host "  $_" }
if ($pushExit -ne 0) { Fail "git push failed (exit $pushExit)" }

# ── 4. Trigger VPS update ─────────────────────────────────────────────────────
# Capture the currently-deployed commit BEFORE updating, so a failed deploy can
# be rolled back to a known-good SHA (more reliable than reflog HEAD@{1}).
$prevSha = (& ssh $VPSHost "cd /opt/crypto-bot && git rev-parse HEAD").Trim()

Step "Running vps_update.sh on $VPSHost"
# vps_update.sh handles git fetch, .env preservation, requirements, systemd restart.
# We tee its output but only show key lines locally to keep noise low.
& ssh $VPSHost "bash /opt/crypto-bot/deploy/vps_update.sh 2>&1 | tail -25"
if ($LASTEXITCODE -ne 0) { Fail "vps_update.sh failed (exit $LASTEXITCODE)" }

# ── 5. Verify the bot is alive — AUTO-ROLLBACK if the new commit won't run ─────
Step "Waiting ${Wait}s then checking service health"
Start-Sleep -Seconds $Wait
$alive = (& ssh $VPSHost "systemctl is-active crypto-bot").Trim()
if ($alive -ne "active") {
    Step "Service is '$alive' after deploy — ROLLING BACK to $prevSha"
    & ssh $VPSHost "cd /opt/crypto-bot && git reset --hard $prevSha && sudo systemctl restart crypto-bot && sleep $Wait"
    $recovered = (& ssh $VPSHost "systemctl is-active crypto-bot").Trim()
    & ssh $VPSHost "journalctl -u crypto-bot --no-pager -n 25"
    if ($recovered -eq "active") {
        Fail "Deploy failed health check; ROLLED BACK to $prevSha and the service recovered. The pushed commit is bad — investigate before redeploying."
    } else {
        Fail "Deploy FAILED health check and rollback did NOT recover (service=$recovered). Manual intervention required on $VPSHost."
    }
}
& ssh $VPSHost "journalctl -u crypto-bot --no-pager -n 15"
Step "Deploy complete — service healthy"
