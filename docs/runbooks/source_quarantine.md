# Source quarantine runbook

## When this fires

- `#🛠-source-health` posts: `source X auto-quarantined`.
- Metric: `source_health_24h.opps_24h = 0` for an `active` source whose
  `fetch_freq_minutes` window has elapsed multiple cycles.
- Or: rolling 24h fetch error rate > 50%.

## Diagnose

```bash
docker compose run --rm tools python -m src.cli.main sources list
# look for status=quarantined
docker compose logs --tail=300 crawler-worker | grep <slug>
```

Common root causes:

| Cause                          | Sign                                                    | Fix |
|--------------------------------|---------------------------------------------------------|-----|
| CF challenge upgrade           | `cf_challenge_appeared_rate` spike                      | escalate tier_chain to include T1+T2 |
| Auth cookie expired            | repeated 401/403 with no CF marker                       | re-warm identity, refresh cookies |
| Site schema changed            | tier-1 selector misses 100%                              | update `src/extractors/tier1_selectors/<slug>.py` |
| Rate-limited from this IP      | 429s                                                    | back off, lower fetch_freq, consider residential proxy (Phase 4) |
| Network drop                   | DNS errors                                              | check `cloudflared` + Tailscale |

## Recover

```bash
# After fixing root cause
docker compose run --rm tools python -m src.cli.main sources resume <slug>
```

## Escalate

If two consecutive cycles after resume still fail → pause permanently and
file a ticket in `docs/specs/` for v2 source replacement.
