# Identity ban runbook

## When this fires

- `#🔔-alerts` posts: `identity X banned — sibling Y auto-quarantined`.
- The V004 `cascade_identity_ban` trigger fired.

## Investigate

```bash
docker compose run --rm tools python -m src.cli.main identity status
docker compose exec postgres psql -U marked -d marked -c \
  "SELECT * FROM identity_audit WHERE identity_id = <id> ORDER BY occurred_at DESC LIMIT 20;"
```

Look for:
- Sudden burst of failed logins → IP-level rate limit, not personal ban.
- Email from the platform → real account suspension.
- Same fingerprint_id shared across siblings → expected cascade.

## Recover

1. Rotate fingerprint: assign a fresh `fingerprints` row to the surviving siblings.
   ```sql
   UPDATE identities SET fingerprint_id = <new_fp_id> WHERE id IN (<sibling_ids>);
   ```
2. Re-warm each unquarantined identity manually (10 min on platform, no scraping).
3. After warmup window (24-72h), flip ban_status back:
   ```sql
   UPDATE identities SET ban_status = 'healthy' WHERE id = <id>;
   ```
4. If the original identity is unrecoverable, mark it permanently:
   ```sql
   UPDATE identities SET ban_status = 'banned' WHERE id = <id>;
   ```
   and create a replacement via `mp identity add`.

## Prevent

- Don't share fingerprint_id across more than 3 identities.
- Keep warmup_score >= 0.5 before using an identity for real fetches.
- Watch `identity_ban_status_count` Grafana panel daily.
