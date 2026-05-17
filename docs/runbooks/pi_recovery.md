# Pi recovery runbook (power-fail, SD failure)

## Symptoms

- Pi rebooted unexpectedly.
- `#🔔-alerts` shows "READY event absent >5min".
- Cloudflared tunnel still up but pipeline silent.

## Recovery flow

1. **fsck runs on boot** (already enabled via `tune2fs -c 1`). If fsck reports unfixable errors, jump to "SD failure" below.

2. **Docker Compose restart** is automatic (`restart: unless-stopped`). Verify on first SSH after recovery:
   ```bash
   cd /home/lakshit_gupta/coding/cartograph
   docker compose ps
   ```
   All services should be `Up`. If `postgres` is `Restarting`, jump to step 4.

3. **Postgres WAL replay**: max 15-min checkpoint window. Recovery is automatic on container start. Confirm:
   ```bash
   docker compose logs --tail=200 postgres | grep -i 'redo done\|database system is ready'
   ```

4. **Postgres consistency check**:
   ```bash
   docker compose exec postgres pg_amcheck -U marked marked --verbose
   ```
   If anything reports `corruption`, jump to "Restore from R2" below.

5. **Redis AOF replay**: max 1s loss. Auto on container start. Confirm:
   ```bash
   docker compose logs --tail=50 redis | grep -i 'aof loaded\|ready to accept'
   ```

6. **Redis Streams reclaim**: crawler workers run `XAUTOCLAIM` after 5 min idle. No action needed; watch:
   ```bash
   docker compose logs --tail=200 crawler-worker | grep -E 'reclaim|XAUTOCLAIM'
   ```

7. **Discord gateway**: reconnect handled by `discord.py`. If READY missing >5 min, restart:
   ```bash
   docker compose restart notifier-discord
   ```

## SD failure

1. Pull SD card. Confirm bad sectors with another Linux box:
   ```bash
   sudo badblocks -v /dev/sdX
   ```
2. Reflash DietPi onto a new card.
3. Restore Docker volumes from R2:
   ```bash
   bash scripts/restore_drill.sh
   # then promote drill DB into pg_data volume by editing pg_data mount target
   ```
4. Re-run bootstrap: `CARTOGRAPH_PI_CONFIRM=1 sudo bash scripts/bootstrap.sh`.
5. Pull SOPS-encrypted secrets.yaml from git, decrypt with age key (copy from secure offline backup).
6. `make up && make migrate && make seed`.

## Restore from R2 (non-corruption case)

1. Identify dump:
   ```bash
   rclone ls r2:agent-jobs-backups/postgres/ | sort | tail
   ```
2. `bash scripts/restore_drill.sh` to verify dump integrity (into tmpfs).
3. Stop primary postgres:
   ```bash
   docker compose stop postgres
   ```
4. Wipe + restore:
   ```bash
   docker volume rm cartograph_pg_data
   docker compose up -d postgres
   sleep 10
   docker compose exec -T postgres pg_restore -U marked -d marked --clean --if-exists < /path/to/decrypted.dump
   ```
5. Replay any missed WAL files from `/mnt/storage/wal_archive` if available.
6. Restart stack: `docker compose up -d`.

## Cron entries

Cron entries managed by `scripts/install_cron.sh`. After SD reflash, re-run `CARTOGRAPH_PI_CONFIRM=1 sudo bash scripts/bootstrap.sh` and confirm `crontab -l | grep cartograph_cron` shows 3 entries (nightly backup at 03:30, weekly restore drill Sunday 04:00, nightly `pg_amcheck` at 05:00).

## Acceptance

- 12 verification checks in CLAUDE.md pass.
- Daily digest delivers in next cron window.
