#!/usr/bin/env bash
# ── crypto-bot repo sync check (macOS / Linux) ───────────────────────────────
# Keeps THIS machine on the same page as origin/master before you start work.
# Touches ONLY git state — never the bot, strategy, config, data/, or secrets.
# Fast-forward only: refuses to auto-merge if the branch has diverged or has
# uncommitted changes; it just warns so you decide. Safe to run anytime.

cd "$(dirname "$0")/.." || exit 0   # script lives in deploy/ → repo root
[ -d .git ] || exit 0

echo "  Checking repo sync (origin/master)..."
git fetch origin --quiet 2>/dev/null

counts=$(git rev-list --left-right --count origin/master...HEAD 2>/dev/null)
[ -z "$counts" ] && exit 0
behind=$(echo "$counts" | awk '{print $1}')
ahead=$(echo "$counts" | awk '{print $2}')
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
dirty=$(git status --porcelain)

if [ "$behind" = "0" ] && [ "$ahead" = "0" ] && [ -z "$dirty" ]; then
    echo "  OK - up to date with origin/master"
    exit 0
fi
[ "$ahead" != "0" ] && echo "  ! $ahead local commit(s) not pushed. Push via branch+PR (see CLAUDE.md)."
[ -n "$dirty" ] && echo "  ! Uncommitted local changes - not auto-pulling."
if [ "$behind" != "0" ]; then
    if [ "$branch" = "master" ] && [ "$ahead" = "0" ] && [ -z "$dirty" ]; then
        echo "  Down $behind commit(s) behind origin/master - fast-forwarding..."
        git pull --ff-only origin master
    else
        echo "  Down $behind commit(s) behind origin/master - run: git pull --ff-only"
    fi
fi
