# Copyright (c) 2026 Lakshit Gupta. All rights reserved.
"""Local Internshala discovery ingest — curl_cffi + code-side INR floor.

Standalone, logged-out crawler that populates the ``opportunities`` table with
backend/ML/Python internships at or above a code-enforced INR floor, and writes
a bootstrap ``opportunity_scores`` row so ``/auto-apply preview`` returns them
immediately. Built after live recon (2026-05-30) proved three bugs:

  1. Internshala honours the category filter ONLY in the SINGULAR URL form
     (``/internships/backend-development-internship/``). The plural
     ``-internships`` form ``internshala_filters.yaml`` used is silently ignored
     -> the page falls back to "6443 Total Internships" (everything), which is
     why civil / mech / sales junk leaked in.
  2. The ``/stipend-N/`` path segment (and ``?stipend=``) is a logged-in-only
     filter; logged-out it does not restrict the stipend (Rs 2,000 listings
     still appear). So the floor MUST be enforced in code.
  3. The shared ``src.common.stipend_parser.parse_stipend`` mis-scales some
     Internshala strings (1000x via ``_apply_scale``; period mis-detected as
     'year'), producing absurd >Rs 2L/month figures. Internshala is INR +
     per-month, so this script uses its own small auditable
     ``_parse_monthly_inr`` -- no scale guessing.

``/auto-apply preview`` (``auto_apply_engine.find_eligible``) JOINs
``opportunity_scores`` and filters ``comp_min_inr >= min_comp_inr_month`` (strict)
with ``apply_method`` in the whitelist and ``state IN ('queued', ...)``. This
script satisfies every clause: monthly INR -> comp_min / comp_max / comp_min_inr
(when the V023 column exists), apply_method=in_platform, state=queued, and a
bootstrap opportunity_scores row per opp (the ranker-worker UPSERTs the real fit
score over it later).

Run::

    PGHOST=<host> PGPASSWORD=<pw> uv run python scripts/internshala_local_ingest.py
    ... --dry-run        # parse + filter + print, no DB writes
    ... --max-pages 2    # cap pages per category

Env: PGHOST/PGPORT/PGUSER/PGDATABASE (or the POSTGRES_* equivalents used by the
sidecar's .env.sidecar) set the connection; defaults 127.0.0.1:5432 /
Cartograph / Cartogrph (note the seed's misspelled db name). Local dev: Postgres
is Docker-network-only, so PGHOST must be the container IP (``docker inspect -f
'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
marked_path-postgres-1``), NOT 127.0.0.1. On the Pi / sidecar, 127.0.0.1 reaches
the published port / autossh tunnel. INTERNSHALA_COMP_FLOOR_INR overrides the
Rs 30,000 floor; APPLY_USER_ID overrides the scored tenant (default 1).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import random
import re
import sys
import time

import asyncpg
from curl_cffi import requests as cr
from selectolax.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BASE = "https://internshala.com"
_SOURCE_SLUG = "in_internshala"
_FLOOR_INR = float(os.environ.get("INTERNSHALA_COMP_FLOOR_INR", "30000"))
_USER_ID = int(os.environ.get("APPLY_USER_ID", "1"))

# Honoured SINGULAR category slugs (verified live 2026-05-30 via the heading
# oracle -- the <h1> reads "125 Backend Development Internships" when honoured,
# "6443 Total Internships" when ignored). Live totals in comments.
# Categories matched to the user's resume (parsed 2026-05-30 from
# config/profile/my_resume/): Backend (FastAPI/Python/Node), AI/ML agents
# (LangChain/LangGraph/RAG/LLM, TFT/ONNX, Qdrant embeddings), Cloud/DevOps
# (AWS EC2/S3/Lambda/EKS/ECR, GCP Cloud Run, Docker, Kubernetes), and
# React/Next/React-Native full-stack. Counts are the live <h1> totals at recon.
_CATEGORIES = [
    "backend-development-internship",          # 125
    "python-django-internship",                # 61
    "programming-internship",                  # 43
    "full-stack-development-internship",       # 118
    "data-engineering-internship",             # 41
    "machine-learning-internship",             # 162
    "data-science-internship",                 # 347
    "artificial-intelligence-internship",      # 149
    "deep-learning-internship",                # 39
    "natural-language-processing-internship",  # 40
    "computer-vision-internship",              # 12
    "sql-internship",                          # 75
    "devops-internship",                       # resume: AWS/GCP/Docker/K8s
    "cloud-computing-internship",              # resume: EC2/S3/Lambda/Cloud Run
    "react-native-internship",                 # resume: React Native mobile app
    "node-js-development-internship",          # resume: Node/Express backend
]
_WFH_CATEGORIES = [
    "work-from-home-backend-development-internship",
    "work-from-home-python-django-internship",
    "work-from-home-programming-internship",
    "work-from-home-machine-learning-internship",
    "work-from-home-data-science-internship",
    "work-from-home-artificial-intelligence-internship",
    "work-from-home-devops-internship",
    "work-from-home-full-stack-development-internship",
]

# Resume-derived positive role/tech keywords. A card title MUST contain one.
# Mirrors the user's actual stack: backend, AI/ML-agents, cloud/devops,
# React/Next full-stack. (config/profile/my_resume parsed 2026-05-30.)
_POSITIVE = [
    # Backend
    "backend", "back end", "back-end", "python", "django", "fastapi", "flask",
    "api develop", "rest api", "golang", "go developer", "rust",
    "node", "nodejs", "node.js", "express",
    # Full-stack + frontend (resume: React, Next.js, React Native, TS)
    "full stack", "fullstack", "full-stack", "react", "next.js", "nextjs",
    "react native", "typescript", "web develop", "mern", "mobile app develop",
    # Generic SDE
    "software develop", "software engineer", "software development engineer", "sde",
    "programming", "developer", "engineer", "automation", "web scraping", "scraping",
    # AI / ML / agents (resume's core: LangChain, LangGraph, RAG, LLM, MCP)
    "machine learning", "ml engineer", "ml intern", "deep learning",
    "data science", "data scientist", "data engineer", "data engineering",
    "data analyst", "analytics engineer",
    "nlp", "natural language", "llm", "generative ai", "genai", "gen ai",
    "computer vision", "pytorch", "tensorflow", "ai engineer", "ai/ml", "ai intern",
    "artificial intelligence", "langchain", "langgraph", "rag", "agentic",
    "ai agent", "agent develop", "prompt", "mlops", "ml ops",
    # Cloud / DevOps (resume: AWS, GCP, Docker, Kubernetes)
    "devops", "dev ops", "cloud", "aws", "gcp", "azure", "docker", "kubernetes",
    "k8s", "sre", "site reliability", "platform engineer", "infrastructure",
    # DB
    "sql develop", "database",
]

# Negative tokens -- reject the title outright.
_NEGATIVE = [
    "sales", "marketing", "content", "social media", "smm", "seo", "sem",
    "human resource", "hr intern", "recruit", "talent acquisition",
    "graphic design", "ui designer", "ux designer", "interior", "video edit",
    "animation", "photograph", "copywrit", "pre sales", "pre-sales",
    "business development", "telecall", "telesales", "bpo", "kpo",
    "customer support", "customer success", "customer service", "operations intern",
    "finance", "accounting", "accountant", "articleship", "chartered accountant",
    "company secretary", "counsellor", "counselor", "teaching", "tutor",
    "lead generation", "lead gen", "field sales", "data entry", "ms excel",
    "ms office", "branding", "influencer", "market research", "survey",
    "architect", "civil", "mechanical", "electrical",
    # Guard the broadened positive list: "cloud kitchen" is a food-delivery
    # role (matches bare "cloud"); "salesforce" admin/sales roles match
    # nothing technical here; "ui/ux" pulls design.
    "cloud kitchen", "salesforce", "ui/ux", "ux/ui", "wordpress", "shopify",
]

# One impersonation profile is picked ONCE per run and reused for every request
# on the same Session -- rotating the JA3/UA mid-session is itself a bot tell
# (a real browser keeps one fingerprint for the whole visit). Each tuple is
# (curl_cffi impersonate target, matching real UA string, Sec-CH-UA header)
# so the TLS fingerprint, User-Agent, and client-hints all agree.
_PROFILES = [
    (
        "chrome124",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    ),
    (
        "chrome120",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    ),
    (
        "chrome131",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    ),
]

_NON_NUMERIC = ("unpaid", "negotiable", "performance", "competitive", "not disclosed", "as per")
_NUM_RE = re.compile(r"[\d,]+(?:\.\d+)?")


def _parse_monthly_inr(raw: str) -> tuple[float, float] | None:
    """Internshala stipend string -> (min, max) monthly INR, or None.

    Internshala is INR-native and ~100% per-month, so the only scaling needed is
    the rare /year, /week, /day suffix. Indian comma grouping ("4,000",
    "1,00,000") is stripped before float() -- NO k/lakh multiplier guessing (the
    source of the 1000x bug). Returns None for non-numeric ("Unpaid") and
    one-off "lump sum" stipends.
    """
    if not raw:
        return None
    low = raw.lower()
    if any(m in low for m in _NON_NUMERIC) or "lump" in low:
        return None
    nums: list[float] = []
    for m in _NUM_RE.findall(raw):
        v = m.replace(",", "")
        if v and v.count(".") <= 1 and v.replace(".", "").isdigit():
            nums.append(float(v))
    nums = [n for n in nums if n >= 500]  # drop stray small ints ("10th", page nums)
    if not nums:
        return None
    lo, hi = min(nums), max(nums)
    if any(t in low for t in ("/year", "lpa", "p.a", "per year", "/annum", "per annum")):
        lo, hi = lo / 12.0, hi / 12.0
    elif "/week" in low or "per week" in low:
        lo, hi = lo * 4.0, hi * 4.0
    elif "/day" in low or "per day" in low:
        lo, hi = lo * 22.0, hi * 22.0
    return lo, hi


def _bootstrap_score(cmin: float) -> float:
    """Comp-ordered placeholder score in [0.60, 0.99] so opps surface in
    /auto-apply preview (min_score 0.30) ordered by stipend until the ranker
    computes the real fit score and UPSERTs over this row."""
    return round(min(0.99, 0.60 + cmin / 300000.0), 4)


def _platform_token(ua: str) -> str:
    if "Windows" in ua:
        return '"Windows"'
    if "Mac OS" in ua:
        return '"macOS"'
    return '"Linux"'


def _base_headers(ua: str, sec_ch_ua: str) -> dict[str, str]:
    """Realistic navigation headers consistent with the chosen UA. Sec-Fetch-*
    and the client-hints match what Chrome actually sends on a top-level
    document navigation; the platform hint is derived from the UA so they never
    disagree (a mismatch is a classic headless tell)."""
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Sec-Ch-Ua": sec_ch_ua,
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": _platform_token(ua),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def _sleep(seconds: float) -> None:
    """Blocking sleep -- used inside the synchronous _Fetcher (HTTP I/O is sync)."""
    time.sleep(seconds)


class _Fetcher:
    """Stateful HTTP fetcher with browser-like evasion.

    Holds a single ``curl_cffi.Session`` so cookies persist across the whole run
    (Internshala sets ``ci_session`` etc. on the first hit -- carrying them
    forward is exactly what a real browser does). One impersonation profile is
    chosen per run and never rotated. A warm-up GET of the listing root primes
    cookies and gives every later request a same-origin ``Referer``; ``Sec-Fetch-
    Site`` flips from ``none`` (first navigation) to ``same-origin`` thereafter.
    403/429 trigger exponential backoff with one re-warm before giving up.
    """

    def __init__(self) -> None:
        self.imp, self.ua, self.sec_ch_ua = random.choice(_PROFILES)
        self.session = cr.Session(impersonate=self.imp)
        self._last_url = f"{_BASE}/"
        self._warmed = False

    def _headers(self, *, first: bool) -> dict[str, str]:
        h = _base_headers(self.ua, self.sec_ch_ua)
        h["Referer"] = self._last_url
        h["Sec-Fetch-Site"] = "none" if first else "same-origin"
        return h

    def warmup(self) -> bool:
        """Hit the listing root once to acquire cookies + a real referer."""
        url = f"{_BASE}/internships/"
        try:
            r = self.session.get(url, headers=self._headers(first=True), timeout=25)
        except Exception as exc:
            print(f"  ! warmup error: {exc}")
            return False
        ok = r.status_code == 200
        if ok:
            self._last_url = url
            self._warmed = True
            print(f"  [warmup] {self.imp} cookies={len(self.session.cookies)} ok")
        else:
            print(f"  ! warmup HTTP {r.status_code}")
        return ok

    def get(self, url: str) -> str | None:
        """GET with up to 3 attempts; exponential backoff + one re-warm on 403/429."""
        for attempt in range(3):
            try:
                r = self.session.get(url, headers=self._headers(first=not self._warmed), timeout=25)
            except Exception as exc:
                print(f"  ! fetch error {url}: {exc}")
                _sleep(2.0 * (attempt + 1))
                continue
            if r.status_code == 200:
                self._last_url = url
                return r.text
            if r.status_code in (403, 429, 503):
                back = 5.0 * (2**attempt) + random.uniform(0, 3)
                print(f"  ! HTTP {r.status_code} {url} -> backoff {back:.1f}s (attempt {attempt + 1}/3)")
                _sleep(back)
                if attempt == 0:  # one re-warm after the first block
                    self._warmed = False
                    self.warmup()
                continue
            print(f"  ! HTTP {r.status_code} {url}")
            return None
        return None


def _title_ok(title: str) -> bool:
    low = title.lower()
    if any(neg in low for neg in _NEGATIVE):
        return False
    return any(pos in low for pos in _POSITIVE)


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(p.lower() for p in parts).encode()).hexdigest()  # noqa: S324


def _parse_cards(html: str) -> list[dict]:
    """Each div.individual_internship -> raw field dict (link from data-href)."""
    t = HTMLParser(html)
    rows = []
    for c in t.css("div.individual_internship"):
        tn = c.css_first(".job-internship-name")
        cn = c.css_first(".company-name") or c.css_first("p.company a")
        sn = c.css_first(".stipend")
        ln = c.css_first(".locations span a") or c.css_first(".location_link")
        href = (c.attributes.get("data-href") or "").strip()
        if not href:
            a = c.css_first("a.job-title-href") or c.css_first("a.view_detail_button")
            href = (a.attributes.get("href") or "") if a else ""
        rows.append({
            "title": tn.text(strip=True) if tn else None,
            "company": cn.text(strip=True) if cn else None,
            "stipend": sn.text(strip=True) if sn else "",
            "location": ln.text(strip=True) if ln else None,
            "url": (href if href.startswith("http") else f"{_BASE}{href}") if href else None,
            "is_wfh": "work from home" in (c.text() or "").lower(),
        })
    return rows


# Internshala is INR + monthly, so the monthly-INR floor value goes into
# comp_min / comp_max AND comp_min_inr (when that V023 column exists) -- the
# preview query filters on comp_min_inr. RETURNING id feeds the score upsert.
_UPSERT_WITH_INR = """
INSERT INTO opportunities (
    source_id, canonical_url, title, company, description,
    comp_min, comp_max, comp_min_inr, comp_currency, comp_period,
    location, remote_type, category, posted_at, apply_url, apply_method,
    fingerprint_hash, extraction_tier, extraction_confidence, state
) VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::remote_type_enum,$13,NOW(),$14,
    $15::apply_method_enum,$16,$17,$18,'queued'
)
ON CONFLICT (canonical_url) DO UPDATE SET
    last_seen = NOW(), comp_min = EXCLUDED.comp_min,
    comp_max = EXCLUDED.comp_max, comp_min_inr = EXCLUDED.comp_min_inr,
    state = 'queued'
RETURNING id, (xmax = 0) AS inserted
"""

_UPSERT_NO_INR = """
INSERT INTO opportunities (
    source_id, canonical_url, title, company, description,
    comp_min, comp_max, comp_currency, comp_period,
    location, remote_type, category, posted_at, apply_url, apply_method,
    fingerprint_hash, extraction_tier, extraction_confidence, state
) VALUES (
    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::remote_type_enum,$12,NOW(),$13,
    $14::apply_method_enum,$15,$16,$17,'queued'
)
ON CONFLICT (canonical_url) DO UPDATE SET
    last_seen = NOW(), comp_min = EXCLUDED.comp_min,
    comp_max = EXCLUDED.comp_max, state = 'queued'
RETURNING id, (xmax = 0) AS inserted
"""

_SCORE_UPSERT = """
INSERT INTO opportunity_scores (user_id, opportunity_id, score, score_components, ranker_version)
VALUES ($1, $2, $3, '{"bootstrap": true}'::jsonb, 'ingest_bootstrap')
ON CONFLICT (user_id, opportunity_id) DO UPDATE SET score = EXCLUDED.score
"""


async def _connect() -> asyncpg.Connection:
    # Accept both libpq PG* names (local dev) and the project's POSTGRES_* names
    # (so the ThinkPad sidecar's .env.sidecar works unchanged -- network_mode
    # host means POSTGRES_HOST=127.0.0.1 reaches the autossh tunnel to the Pi).
    pw = os.environ.get("PGPASSWORD") or os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        raise SystemExit("set PGPASSWORD or POSTGRES_PASSWORD")
    return await asyncpg.connect(
        host=os.environ.get("PGHOST") or os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT") or os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ.get("PGUSER") or os.environ.get("POSTGRES_USER", "Cartograph"),
        password=pw,
        database=os.environ.get("PGDATABASE") or os.environ.get("POSTGRES_DB", "Cartogrph"),
    )


async def _has_comp_min_inr(conn: asyncpg.Connection) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'opportunities' AND column_name = 'comp_min_inr'"
    ))


async def _jitter(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def run(dry_run: bool, max_pages: int) -> None:
    cats = _CATEGORIES + _WFH_CATEGORIES
    stats = {"fetched": 0, "cards": 0, "neg": 0, "no_pos": 0, "subfloor": 0,
             "unparseable": 0, "inserted": 0, "updated": 0, "scored": 0, "kept": 0}
    survivors: list[dict] = []
    seen: set[str] = set()

    fetcher = _Fetcher()
    fetcher.warmup()
    await _jitter(1.0, 2.5)

    for cat in cats:
        for page in range(1, max_pages + 1):
            url = f"{_BASE}/internships/{cat}/page-{page}/" if page > 1 else f"{_BASE}/internships/{cat}/"
            html = fetcher.get(url)
            stats["fetched"] += 1
            if not html:
                break
            cards = _parse_cards(html)
            stats["cards"] += len(cards)
            kept = 0
            for card in cards:
                title = card["title"]
                if not title or not card["url"]:
                    continue
                if not _title_ok(title):
                    low = title.lower()
                    stats["neg" if any(n in low for n in _NEGATIVE) else "no_pos"] += 1
                    continue
                parsed = _parse_monthly_inr(card["stipend"])
                if parsed is None:
                    stats["unparseable"] += 1
                    continue
                cmin, cmax = parsed
                # Floor on the GUARANTEED minimum (range lower bound).
                if cmin < _FLOOR_INR:
                    stats["subfloor"] += 1
                    continue
                if card["url"] in seen:
                    continue
                seen.add(card["url"])
                stats["kept"] += 1
                kept += 1
                survivors.append({**card, "cmin": cmin, "cmax": cmax, "category": cat})
            print(f"  {cat} p{page}: {len(cards)} cards, kept {kept}")
            if len(cards) < 20:  # full pages ~40-51; short page = last
                break
            await _jitter(1.5, 3.5)
        await _jitter(2.0, 4.0)

    print(f"\n=== funnel === {stats}")
    print(f"\n=== {len(survivors)} survivors (guaranteed min >= Rs {int(_FLOOR_INR)}) ===")
    for s in sorted(survivors, key=lambda x: -x["cmin"])[:45]:
        rng = f"{int(s['cmin'])}-{int(s['cmax'])}"
        print(f"  Rs {rng:>14s}/mo | {(s['title'] or '?')[:38]:38s} | {(s['company'] or '?')[:22]:22s} | {s['category']}")

    if dry_run:
        print("\n[dry-run] no DB writes.")
        return

    conn = await _connect()
    try:
        rec = await conn.fetchrow("SELECT id FROM sources WHERE slug = $1", _SOURCE_SLUG)
        if rec is None:
            raise SystemExit(f"source {_SOURCE_SLUG!r} missing -- run migrations / seed first")
        source_id = int(rec["id"])
        has_inr = await _has_comp_min_inr(conn)
        print(f"[schema] comp_min_inr column present: {has_inr}  scoring user_id={_USER_ID}")
        for s in survivors:
            remote = "remote" if s["is_wfh"] or "work-from-home" in s["category"] else "onsite"
            desc = f"{s['title']} @ {s['company']} :: {s['stipend']} :: {s['location']}"
            fph = _fp(s["company"] or "", s["title"], s["location"] or "")
            if has_inr:
                row = await conn.fetchrow(
                    _UPSERT_WITH_INR, source_id, s["url"], s["title"], s["company"], desc,
                    s["cmin"], s["cmax"], s["cmin"], "INR", "month",
                    s["location"], remote, "internship", s["url"], "in_platform",
                    fph, 1, 0.8,
                )
            else:
                row = await conn.fetchrow(
                    _UPSERT_NO_INR, source_id, s["url"], s["title"], s["company"], desc,
                    s["cmin"], s["cmax"], "INR", "month",
                    s["location"], remote, "internship", s["url"], "in_platform",
                    fph, 1, 0.8,
                )
            if row is None:
                continue
            stats["inserted" if row["inserted"] else "updated"] += 1
            await conn.execute(_SCORE_UPSERT, _USER_ID, row["id"], _bootstrap_score(s["cmin"]))
            stats["scored"] += 1
    finally:
        await conn.close()

    print(f"\n=== DB write === inserted={stats['inserted']} updated={stats['updated']} scored={stats['scored']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-pages", type=int, default=3)
    a = ap.parse_args()
    asyncio.run(run(a.dry_run, a.max_pages))


if __name__ == "__main__":
    main()
