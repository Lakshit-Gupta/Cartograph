# LaTeX apply smoke — Stage 4 production flip

> Smoke ran 2026-05-19. `MP_RESUME_LATEX_ENABLED` flipped from `false` to
> `true` in SOPS-encrypted `secrets.yaml`. Two consecutive XADD smoke runs
> against real digested opportunities. Both paths (tailored + fallback)
> produce a PDF for Resend; CLAUDE.md hard rule #5 (no PDF on Discord)
> upheld.

## Pre-flight

```bash
# 1. Verify flag flipped
sops exec-env secrets.yaml 'env | grep mp_resume_latex'
# expected: mp_resume_latex_enabled=true

# 2. Rebuild base + applier images so the new prompt + sender.py reach the running container
sops exec-env secrets.yaml 'docker compose build jobs-scheduler'
sops exec-env secrets.yaml 'docker compose build applier-worker'

# 3. Force-recreate so MP_RESUME_LATEX_ENABLED is picked up
sops exec-env secrets.yaml 'docker compose up -d --force-recreate applier-worker'
sleep 6

# 4. Confirm env in container
sops exec-env secrets.yaml 'docker compose exec -T applier-worker env' | grep MP_RESUME_LATEX
# expected: MP_RESUME_LATEX_ENABLED=true

# 5. Confirm fallback warmed at boot
sops exec-env secrets.yaml 'docker compose logs --since 30s applier-worker' | grep fallback_warmup
# expected: fallback_warmup_ok path=/var/lib/agent/resume_artifacts/1/fallback.pdf
```

## Smoke run

```bash
# Pick a digested opp (state machine forbids ranked → applied direct transition;
# applier auto-transitions digested → applied internally)
sops exec-env secrets.yaml 'docker compose exec -T postgres psql -U "$postgres_user" -d "$postgres_db" -t -A -F"|" -c "
  SELECT id, title FROM opportunities
  WHERE state='"'"'digested'"'"' AND apply_method='"'"'ats_form'"'"'
  ORDER BY first_seen DESC LIMIT 3;"'

# Publish kind=apply (mirrors what the Discord apply button publishes)
sops exec-env secrets.yaml 'docker compose exec -T applier-worker /opt/venv/bin/python -c "
import asyncio
from datetime import UTC, datetime
from src.common.queue import RedisQ, Streams

async def go():
    q = await RedisQ.connect()
    msg_id = await q.publish(Streams.APPLY, {
        \"action\": \"apply\",
        \"opp_id\": \"<paste-uuid-here>\",
        \"user_id\": 1,
        \"ts\": datetime.now(UTC).isoformat(),
        \"source\": \"smoke_probe\",
    })
    print(\"xadd:\", msg_id)

asyncio.run(go())
"'

sleep 30
sops exec-env secrets.yaml 'docker compose logs --since 60s applier-worker' | tail -20
```

## Observed outcomes (real runs)

### Smoke 1 — opp `bd4f50bd-47d1-4f66-bdc4-836d5d43012e`

- `_send_with_latex` entered (flag picked up correctly)
- LLM tailor call crashed inside cost-ledger with `invalid input value for
  enum usage_kind_enum: "resume_tailor"` (see `sender.py:171`,
  cost-ledger has 8 fixed kinds: `llm_extract`, `llm_rerank`,
  `llm_writer`, `llm_classifier`, `embedding`, `proxy`, `captcha`,
  `other`). The crash is caught inside `chat_json`; tailoring returns
  no edits and the render path proceeds with the untailored tree.
- Compile succeeded (no splices happened, so no malformed LaTeX).
- `applications` row: `id=8, resume_compile_status=tailored,
  resume_artifact_sha256=80d03d5f86aba63f16900ce964a88b02f49ab0ab3b58ff9b9003f4ddea423caf`.
- `resume_compile_log`: `tailored, 5490ms, Tectonic 0.16.9, no stderr`.
- PDF written to
  `/var/lib/agent/resume_artifacts/1/bd4f50bd-.../complete/mmayer.pdf`
  (38,751 bytes).
- State machine: `ranked → digested → applied` (via two auto transitions).
- `stream:notify` `kind=manual_apply_ready` payload contained zero
  `pdf`/`attachment`/`resume_path` keys. Hard rule #5 upheld.

### Smoke 2 — opp `c2c780f1-34fc-4ea7-8af9-561f60743727`

After fixing the enum (`kind="resume_tailor"` → `kind="llm_writer"`,
commit `<see git log>`) and adding the fallback warm-up at applier-worker
boot, the LLM tailor returned real edits and the render attempted to
splice them.

- LLM tailor returned bullets cleanly.
- `render.write_partial` produced `mmayer.tex` that tectonic refused with
  `mmayer.tex:239: LaTeX Error: Lonely \item—perhaps a missing list
  environment.`. The render is appending an `\begin{itemize}…\end{itemize}`
  block to the end of a `\cvevent` region whose downstream content
  contains a commented-out `% \item ...` series — the comment marker
  prematurely closes a list environment that the render assumed open.
- Fallback path triggered: warmed `fallback.pdf` (compiled at boot
  from the untailored source tree) was used as the email attachment.
- `applications` row: `id=11, resume_compile_status=fallback`,
  `sha256` populated from the fallback PDF.
- `resume_compile_log`: `status=fallback`, `tectonic_stderr` carries the
  full Lonely-\item error for audit.
- `stream:notify` `kind=manual_apply_ready`: still no PDF leakage.

## Known defects after smoke

1. **Render — Lonely \item** — **FIXED in 2365b03**
   - File: `src/application/resume_latex/render.py:70 _splice_block_region`
   - Was: tailored splice corrupted mmayer.tex around lines containing
     commented-out itemize blocks; tectonic refused the compile with
     `mmayer.tex:239: LaTeX Error: Lonely \item—perhaps a missing list
     environment.`
   - Fix: `_strip_line_comments` builds a comment-masked view of the
     block region with byte offsets preserved (commented chars replaced
     with spaces, newlines kept). `_ITEMIZE_RE` now searches the masked
     view so a `% \begin{itemize}` no longer claims a match. The original
     region is what gets returned by the splice — comments are kept
     verbatim in output, only ignored for boundary matching.
   - Tests: 20 render tests green (13 pre-existing + 7 new — 4 mandated
     regression tests plus 3 escape-handling helpers for the masking
     primitive). See `tests/application/test_render.py`.
   - Smoke rerun (post-fix): opp `c4b158e2-9016-4adc-a4fd-053a0e6153b8`
     picked from `state='digested' AND apply_method='ats_form'` not yet
     applied; XADD `1779186374293-0` published; after 40s wait the
     verification SELECT returned
     `resume_compile_status=tailored | has_pdf=t | lonely_item_error=f`.
     `resume_compile_log` row: `status=tailored, stderr=<null>,
     compile_duration_ms=4607`. Warm fallback path no longer triggered
     for resumes whose source contains commented-out `% \item`
     scaffolding.

2. **Cost-ledger enum**
   - File: `migrations/V001__core_schema.sql` enum `usage_kind_enum`.
   - Symptom: the tailored LLM call used `kind="resume_tailor"` which
     wasn't in the V001 enum. Code now writes `kind="llm_writer"`
     against the existing model. A follow-up migration can add a
     dedicated `resume_tailor` value if per-lane cost attribution
     becomes needed.

## Rollback (if degradation observed in prod)

```bash
# 1. Flip flag back via the same sops editor pattern
sops secrets.yaml
# remove or set: mp_resume_latex_enabled: "false"

# 2. Force-recreate applier-worker — legacy JSON-template path resumes
sops exec-env secrets.yaml 'docker compose up -d --force-recreate applier-worker'

# 3. Confirm
sops exec-env secrets.yaml 'docker compose exec -T applier-worker env' | grep MP_RESUME
# expected: MP_RESUME_LATEX_ENABLED=false
```

The legacy JSON-template path lives in `src/application/sender.py` after
the `is_latex_enabled()` branch at line 599. It pre-dates Stage 4 and is
known-good against the same digested opps.

## Verification queries

```sql
-- Applications written by smoke
SELECT id, opportunity_id, method, resume_compile_status,
       resume_artifact_sha256 IS NOT NULL AS has_sha
FROM applications WHERE sent_at > now() - interval '1 hour'
ORDER BY sent_at DESC;

-- Compile log
SELECT status, compile_duration_ms, tectonic_version,
       tectonic_stderr LIKE '%Lonely%' AS lonely_item_error
FROM resume_compile_log
WHERE created_at > now() - interval '1 hour'
ORDER BY created_at DESC;

-- NOTIFY payload audit (no pdf/attachment fields allowed)
XRANGE stream:notify - + COUNT 10
| grep -iE "pdf|attachment|resume_path"
-- expected: zero matches in payload keys
```

## Status

Flag stays **ON**. The Stage-4 render defect (Lonely \item on commented
itemize scaffolding) is fixed in `2365b03`; tailored compiles now
succeed against `config/profile/my_resume/mmayer.tex` end-to-end. The
warm fallback path is still wired and exercised for unrelated compile
failure modes. The existing legacy JSON path remains available as a
one-flag rollback.
