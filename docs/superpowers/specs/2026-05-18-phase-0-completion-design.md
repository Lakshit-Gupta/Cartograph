# Phase 0 Completion — Close-Out Plan (2026-05-18)

Captures the remaining steps to finish Phase 0 prerequisites before Phase 1
Pi deploy. Most fields already filled. Three discrete actions remain.

---

## Current State (as of 2026-05-18)

| Item | Status |
|------|--------|
| OpenRouter API key + daily cap | DONE |
| Discord bot "Hop" + 15 IDs | DONE |
| Cloudflare Email Routing aliases | DONE |
| Resend domain verified + API key + from/reply-to | DONE |
| R2 bucket + token + rclone config | DONE |
| Postgres + Redis strong passwords | DONE |
| Libsodium 32-byte hex master key | DONE |
| Reddit code path | Anonymous JSON (no app needed) — DONE |
| Reddit UA string in secrets | DONE |
| Telegram api_id + hash | DONE (or deferred) |
| Gmail OAuth (client_id, secret, refresh_token, gmail_user) | DONE |
| Obsidian vault dir on Pi | DONE |
| Local YAMLs (skills.yaml, comp_floors.yaml, prefs.yaml) | DONE |
| Identity warmup on 4 platforms (10 min/day x 4 days) | IN PROGRESS |
| Reddit Data API approval ticket | NOT YET FILED |
| secrets.yaml encryption | NOT YET (intentionally cleartext until everything filled) |

## Remaining Actions

Three discrete moves in order:

### Move A — File Reddit Data API Ticket (non-blocking, background)

Reddit ticket approval takes 1-7 days. Submit it now so the clock starts;
deploy continues in parallel on the anonymous JSON code path. When the ticket
resolves, swap blank `reddit_client_id`/`reddit_client_secret` in
`secrets.yaml` with the granted credentials. The `http.py` fetcher
auto-detects configured credentials and upgrades from anonymous to OAuth
(100 QPM instead of 10 QPM). No code change required.

**Form**: https://support.reddithelp.com/hc/en-us/requests/new

**Category**: Developer Platform -> Data API (Reddit reshuffles labels; pick
the closest match or fall back to "Other").

**Subject**: `Non-Commercial Data API Access Request — Cartograph (Personal Job Tracker)`

**Body template** (edit `<>` placeholders):

```
Use Case:
I am requesting Reddit Data API access for a strictly personal,
non-commercial project called Cartograph. The app crawls public
job/freelance posts on r/forhire and similar subreddits to surface
opportunities matching my personal skills and preferences. It runs on
a single Raspberry Pi 5 at home; only I use it.

Commercial Status:
This project is not monetized in any way. I will not sell, license,
redistribute, or share Reddit data. No ads, no paid users, no business
entity involved.

AI/ML Training:
I confirm I will NOT use Reddit content to train any machine learning
or AI models. External LLMs (OpenRouter) are used only to classify
whether a post is a job listing vs other content; no training occurs.

Technical Details:
- App type: Script (single-user)
- Estimated rate: ~30 requests/hour across all subreddits
  (~0.5/min, well under 100/min free-tier limit)
- Endpoints used: /r/forhire/new.json, /r/remotejs/new.json
- Scopes requested: read (minimum needed)
- Data storage: Local PostgreSQL on Pi only, not redistributed
- User-Agent: "cartograph/0.1 by /u/<your_reddit_username>"

Compliance:
I have read and agree to abide by the Reddit Developer Terms, Data API
Terms, and the Responsible Builder Policy. I will respect rate limits,
will not circumvent quotas, and will not redistribute data.

Contact:
Reddit username: /u/<your_reddit_username>
Email: contact@lakshit.dev
```

**After submission**: Reddit replies via email to the contact address.
Approval mentions client_id + secret OR points back to the app registration
page where captcha clears post-approval. Either way, update
`secrets.yaml` and re-encrypt. http.py picks up bearer auth on next request.

### Move B — Final secrets.yaml Audit

Before encryption, verify every field is populated:

```bash
cd /path/to/cartograph
grep -nE 'REPLACE|REPLACE_ME|REPLACE_64_HEX_CHARS|^[a-z_]+: ""$|^[a-z_]+: 0$' secrets.yaml
```

Expected leftover blanks (these stay blank intentionally):

- `reddit_client_id: ""`
- `reddit_client_secret: ""`
- `reddit_username: ""`
- `reddit_password: ""`
- `gmail_worker_user: ""`
- `gmail_worker_app_password: ""`
- `telegram_api_id: 0` (if Telegram deferred)
- `telegram_api_hash: REPLACE_LATER` (if Telegram deferred)

Anything else with REPLACE / 0 / empty is a real gap. Fix before encrypting.

### Move C — Encrypt + Commit + Push

Once Move B passes:

```bash
# 1. Read age public key (Pi has both keys; pubkey safe to display)
AGE_PUBKEY=$(grep -oP 'public key: \Kage1\S+' ~/.config/sops/age/keys.txt)
echo "Using pubkey: $AGE_PUBKEY"

# 2. Backup cleartext OUTSIDE git tree
cp secrets.yaml /tmp/secrets.cleartext.bak

# 3. Encrypt in-place
sops --encrypt --age "$AGE_PUBKEY" --in-place secrets.yaml

# 4. Verify encrypted
head -3 secrets.yaml
# Expected: starts with "sops:" or shows ciphertext blobs.
# If real values still visible, STOP and re-run encrypt.

# 5. Shred cleartext backup
shred -u /tmp/secrets.cleartext.bak

# 6. Commit + push
git add secrets.yaml
git commit -m "secrets: encrypt Phase 0 secrets.yaml"
git push origin main
```

After this, `secrets.yaml` lives encrypted in git. Pi clones the repo, has
the age private key locally, can decrypt via `sops --decrypt` or `make up`
(which runs `sops exec-env` automatically).

## Pre-commit Gate

The repo's `.pre-commit-config.yaml` blocks committing an unencrypted
`secrets.yaml`. If git commit fails with a SOPS-related error, that means
the encryption step did not complete. Re-run Move C step 3.

## Identity Warmup (background, not gating)

Continue 10 min/day on Wellfound, Cuvette, Unstop, Contra through Day 0.
Hop reads stored cookies on Day 1 to inherit human-pattern trust scores.
This runs alongside everything else and does not block any other step.

## What Comes After (Phase 1 deploy, separate plan)

Once Moves A-C complete and identity warmup hits Day 0:

1. SSH to Pi
2. `git clone <repo>` (or `git pull` if already cloned)
3. `CARTOGRAPH_PI_CONFIRM=1 sudo bash scripts/bootstrap.sh`
4. `make up` -> SOPS decrypts secrets, all containers boot
5. `make migrate` -> V001 through V007
6. `make seed` -> 30-source seed
7. `/digest now` in Discord -> first opp card
8. Day-14 verification per `docs/PI_DEPLOY.md` Section 7

These steps are detailed in `docs/PI_DEPLOY.md` and not part of this spec.

## Open Risks

- **Reddit ticket may take longer than 7 days.** Mitigation: anonymous JSON
  path works indefinitely. No code path depends on OAuth-only behavior.
- **Age key loss.** Encrypted `secrets.yaml` becomes unrecoverable. User has
  backed up offline per earlier session direction; reconfirm before
  encrypting.
- **Identity warmup incomplete by Day 0.** Hop boots fine with empty
  identity vault but auth-gated sources (Internshala, Cuvette, Unstop,
  Contra) will fail until identities loaded later via
  `docker compose run --rm tools python -m src.cli.main identity add`.

## Spec Self-Review

- Placeholders: none.
- Internal consistency: Reddit anonymous path matches earlier spec
  (2026-05-18-llm-model-selection-design.md is unrelated; no contradiction).
- Scope: focused on Phase 0 close-out only. Phase 1 deploy is referenced
  but explicitly out of scope.
- Ambiguity: Move A submission method is the Reddit ticket form; no
  alternative paths needed because anonymous JSON unblocks code.
