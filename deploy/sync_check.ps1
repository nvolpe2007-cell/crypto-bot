# ── crypto-bot repo sync check (Windows) ─────────────────────────────────────
# Keeps THIS machine on the same page as origin/master before you start work.
# Touches ONLY git state — never the bot, strategy, config, data/, or secrets.
# Fast-forward only: refuses to auto-merge if the branch has diverged or has
# uncommitted changes; it just warns so you decide. Safe to run anytime.

$repo = Split-Path $PSScriptRoot -Parent   # script lives in deploy/ → repo root
Set-Location $repo
if (-not (Test-Path ".git")) { return }

Write-Host "  Checking repo sync (origin/master)..." -ForegroundColor DarkGray
git fetch origin --quiet 2>$null

$counts = git rev-list --left-right --count origin/master...HEAD 2>$null
if (-not $counts) { return }
$parts  = $counts -split '\s+'
$behind = [int]$parts[0]
$ahead  = [int]$parts[1]
$branch = (git rev-parse --abbrev-ref HEAD).Trim()
$dirty  = [bool](git status --porcelain)

if ($behind -eq 0 -and $ahead -eq 0 -and -not $dirty) {
    Write-Host "  OK - up to date with origin/master" -ForegroundColor Green
    return
}
if ($ahead -gt 0) {
    Write-Host "  ! $ahead local commit(s) not pushed. Push via branch+PR (see CLAUDE.md)." -ForegroundColor Yellow
}
if ($dirty) {
    Write-Host "  ! Uncommitted local changes - not auto-pulling." -ForegroundColor Yellow
}
if ($behind -gt 0) {
    if ($branch -eq 'master' -and $ahead -eq 0 -and -not $dirty) {
        Write-Host "  Down $behind commit(s) behind origin/master - fast-forwarding..." -ForegroundColor Cyan
        git pull --ff-only origin master
    } else {
        Write-Host "  Down $behind commit(s) behind origin/master - run: git pull --ff-only" -ForegroundColor Yellow
    }
}
