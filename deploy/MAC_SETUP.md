# Deploying crypto-bot from a MacBook

The deploy never copies your disk to the VPS. **The link is git/GitHub:**

```
your machine  --git push-->  GitHub (origin/master)  --VPS pulls (vps_update.sh)-->  restart
```

Windows, macOS, and the VPS are three clones of the same repo. Tracked code stays
identical across all three through `origin/master`. Secrets and live data are
per-machine and you place them once (see below).

---

## One-time MacBook setup

### 1. Git + GitHub auth (so `git push` works)

```bash
brew install git gh            # install Homebrew first if needed: https://brew.sh
gh auth login                  # GitHub.com -> HTTPS -> login in browser
git clone https://github.com/nvolpe2007-cell/crypto-bot.git
cd crypto-bot
```

### 2. VPS SSH key (NOT in the repo)

The VPS trusts the private key at `~/.ssh/crypto_bot_vps`. Copy that file from the
Windows box (AirDrop / USB / password manager — **never commit it**), then:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# place the copied key at ~/.ssh/crypto_bot_vps
chmod 600 ~/.ssh/crypto_bot_vps

cat >> ~/.ssh/config <<'EOF'

Host crypto-bot-vps
    HostName 178.105.41.226
    User root
    IdentityFile ~/.ssh/crypto_bot_vps
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
EOF
chmod 600 ~/.ssh/config
```

Test: `ssh crypto-bot-vps "echo ok"` should print `ok` with no password.

> Alternative (don't want to copy the key): generate a fresh one on the Mac with
> `ssh-keygen -t ed25519 -f ~/.ssh/crypto_bot_vps` and append the new `.pub` to the
> VPS's `~/.ssh/authorized_keys`.

### 3. Local Python env (only if you want to run/test on the Mac)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

---

## What links automatically vs. what you place by hand

| Thing | Tracked? | On the Mac |
| --- | --- | --- |
| `src/`, `arbitrage/`, `deploy/`, `config.yaml`, tests | ✅ git | syncs via `git pull` / `git push` |
| `.env`, `.env.vps` (Kraken/Telegram/Anthropic secrets) | ❌ ignored | copy once by hand; **never commit** |
| `data/` (trade journals, `state.json`, `attribution.db`) | ❌ ignored | VPS is the source of truth — see caution below |
| `venv/`, `logs/` | ❌ ignored | rebuild / ignore locally |
| Claude memory (`~/.claude/projects/.../memory/`) | ❌ outside repo | copy the folder if you want the same Claude context |

**Caution on `data/`:** only the VPS runs the live bot. Don't let the Mac and the VPS
both write live trading state. For local analysis, pull a **read-only snapshot**:

```bash
scp -r crypto-bot-vps:/opt/crypto-bot/data ./data
```

For local forward-testing, use a separate state file instead (the funding/swing arms
support `*_STATE_FILE` overrides) so you never corrupt the live record.

---

## Deploying from the Mac

Same workflow as Windows. **Branch -> PR -> merge to master, then deploy** (multiple
agents race `master`; never push to it directly — see `CLAUDE.md`).

One-command deploy (bash port of `auto_deploy.ps1`):

```bash
git add <files>
./deploy/auto_deploy.sh -c -m "what changed"   # stage-commit + push + deploy + health-check
./deploy/auto_deploy.sh                          # deploy already-committed work
```

Or the manual two-liner:

```bash
git push origin master
ssh crypto-bot-vps "bash /opt/crypto-bot/deploy/vps_update.sh"
```

Watch it live:

```bash
ssh crypto-bot-vps "journalctl -u crypto-bot -f"
```
