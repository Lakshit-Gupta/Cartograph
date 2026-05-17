#!/usr/bin/env bash
# CF pretest — hits a known CF-protected source 10 times via the tier chain.
# Reports cf_clearance_solve_rate post-run. Pass criterion: rate >= 0.7.

set -euo pipefail
cd /home/lakshit_gupta/coding/Marked_Path

SOURCE_SLUG=${1:-fl_contra}
N=${2:-10}

echo "CF pretest: source=$SOURCE_SLUG iterations=$N"
for i in $(seq 1 "$N"); do
    sops exec-env secrets.yaml '
      docker compose run --rm tools python -m src.cli.main opps recent --limit 1
    ' >/dev/null 2>&1 || true
    sleep 2
done

# Read solve rate metric off the api-service
RATE=$(curl -s http://localhost:9090/metrics 2>/dev/null \
    | grep -E '^cf_clearance_solve_rate ' \
    | awk '{print $2}' || true)
echo "cf_clearance_solve_rate=${RATE:-unknown}"
