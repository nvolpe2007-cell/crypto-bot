#!/usr/bin/env bash
#
# One-shot push-and-deploy for the crypto-bot VPS (macOS / Linux port of
# auto_deploy.ps1). Use this from a MacBook; Windows keeps using the .ps1.
#
# Steps:
#   1. Verifies we're inside the crypto-bot repo and on master
#   2. Optionally commits the STAGED index (-c with -m "msg")
#   3. Pushes to origin/master (no-op if nothing to push)
#   4. SSHes into the VPS (Host alias 'crypto-bot-vps') and runs vps_update.sh
#   5. Verifies the service is active; AUTO-ROLLS-BACK to the prior SHA if not
#   6. Tails the last journal lines so we can see the bot booted cleanly
#
# Requires:
#   - Passwordless SSH to 'crypto-bot-vps' (~/.ssh/config + ~/.ssh/crypto_bot_vps)
#   - git auth for push (gh auth login, or a PAT/SSH remote)
#
# SAFETY: never `git add -A`. Stage with `git add <files>` BEFORE calling -c,
# so we never ship .env, .env.vps, data/, venv, or editor crud.
#
# Usage:
#   ./deploy/auto_deploy.sh                       # push committed work, deploy, show logs
#   git add src/foo.py
#   ./deploy/auto_deploy.sh -c -m "tune foo"      # stage-commit + push + deploy
#   ./deploy/auto_deploy.sh -w 8                  # wait 8s before the health check

set -euo pipefail

VPS_HOST="crypto-bot-vps"
DO_COMMIT=0
MESSAGE=""
WAIT=5

# -- ANSI helpers (cyan step / red fail) ----------------------------------------
if [ -t 1 ]; then C='\033[36m'; R='\033[31m'; Z='\033[0m'; else C=''; R=''; Z=''; fi
step() { printf "\n${C}[*] %s${Z}\n" "$1"; }
fail() { printf "${R}[FAIL] %s${Z}\n" "$1" >&2; exit 1; }

# -- Parse args -----------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        -c|--commit)  DO_COMMIT=1; shift ;;
        -m|--message) MESSAGE="${2:-}"; shift 2 ;;
        -w|--wait)    WAIT="${2:-5}"; shift 2 ;;
        -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            fail "Unknown argument: $1" ;;
    esac
done

# -- 1. Sanity ------------------------------------------------------------------
step "Verifying local repo state"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "Not inside a git repo"
cd "$REPO_ROOT"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "master" ] || fail "Not on master (current: $BRANCH). Branch + PR + merge first, then deploy."
echo "  repo:   $REPO_ROOT"
echo "  branch: $BRANCH"
echo "  HEAD:   $(git log -1 --oneline)"

# -- 2. Optional commit (STAGED index only) -------------------------------------
if [ "$DO_COMMIT" -eq 1 ]; then
    [ -n "$MESSAGE" ] || fail "-c/--commit requires -m/--message"
    STAGED="$(git diff --cached --name-only)"
    [ -n "$STAGED" ] || fail "-c was set but the index is empty. Stage files first with 'git add <files>'."
    step "Committing staged files"
    echo "$STAGED" | sed 's/^/  + /'
    git commit -m "$(printf '%s\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>' "$MESSAGE")" \
        || fail "git commit failed"
fi

# -- 3. Push --------------------------------------------------------------------
step "Pushing to origin/master"
git push origin master || fail "git push failed"

# -- 4. Trigger VPS update ------------------------------------------------------
# Record the deployed SHA BEFORE updating, so a failed deploy can roll back to a
# known-good commit (more reliable than reflog HEAD@{1}).
PREV_SHA="$(ssh "$VPS_HOST" "cd /opt/crypto-bot && git rev-parse HEAD")"

step "Running vps_update.sh on $VPS_HOST"
# pipefail makes the pipeline reflect vps_update.sh's exit code, not tail's 0 -
# otherwise a failed update is silently masked (the gap that hid the unit drift).
ssh "$VPS_HOST" "set -o pipefail; bash /opt/crypto-bot/deploy/vps_update.sh 2>&1 | tail -30" \
    || fail "vps_update.sh failed"

# -- 5. Verify alive - AUTO-ROLLBACK if the new commit won't run ----------------
step "Waiting ${WAIT}s then checking service health"
sleep "$WAIT"
ALIVE="$(ssh "$VPS_HOST" "systemctl is-active crypto-bot" || true)"
if [ "$ALIVE" != "active" ]; then
    step "Service is '$ALIVE' after deploy - ROLLING BACK to $PREV_SHA"
    ssh "$VPS_HOST" "cd /opt/crypto-bot && git reset --hard $PREV_SHA && systemctl restart crypto-bot && sleep $WAIT"
    RECOVERED="$(ssh "$VPS_HOST" "systemctl is-active crypto-bot" || true)"
    ssh "$VPS_HOST" "journalctl -u crypto-bot --no-pager -n 25"
    if [ "$RECOVERED" = "active" ]; then
        fail "Deploy failed health check; ROLLED BACK to $PREV_SHA and the service recovered. The pushed commit is bad - investigate before redeploying."
    else
        fail "Deploy FAILED health check and rollback did NOT recover (service=$RECOVERED). Manual intervention required on $VPS_HOST."
    fi
fi

ssh "$VPS_HOST" "journalctl -u crypto-bot --no-pager -n 15"
step "Deploy complete - service healthy"
