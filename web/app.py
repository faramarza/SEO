"""
Agentic Organic Growth Governor — Web Dashboard

Flask application providing operator interface per doctrine:
- Dashboard with organic KPIs and governor posture
- Ranked Opportunity Queue
- Task Board (Proposed → Approved → Implemented → Measured → Closed)
- Learning & Memory view
- Tracking Integrity view
- Admin settings
"""

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, jsonify, request

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ledger.action_ledger import ActionLedger, ActionStatus, ActionOutcome
from src.data_sources.gsc_client import GSCClient
from src.data_sources.ga4_client import GA4Client
from src.data_sources.google_ads_client import GoogleAdsClient
from src.diagnostics.tracking_sanity import TrackingSanityDiagnostics


app = Flask(__name__,
            template_folder='templates',
            static_folder='static')

# Load config
CONFIG_PATH = Path(__file__).parent.parent / "config" / "defaults.json"
DATA_PATH = Path(__file__).parent.parent / "data"

# Global state for background jobs
job_state = {
    "running": False,
    "type": None,
    "progress": 0,
    "total": 0,
    "message": "",
    "error": None,
}


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def _persist_opportunity_update(url: str, updates: dict):
    """Update a specific opportunity in latest_evaluation.json by URL.

    Used to persist fetched page metadata and AI recommendations so they
    survive page refresh.
    """
    eval_path = DATA_PATH / "latest_evaluation.json"
    if not eval_path.exists():
        return

    try:
        with open(eval_path) as f:
            eval_data = json.load(f)

        for result in eval_data.get("results", []):
            if result.get("url") == url:
                result.update(updates)
                break

        with open(eval_path, "w") as f:
            json.dump(eval_data, f, indent=2)
    except Exception:
        pass  # Don't fail requests over persistence


def load_cached_data(filename):
    """Load cached JSON data."""
    path = DATA_PATH / filename
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


# ============================================================
# ROUTES - PAGES
# ============================================================

@app.route("/")
def dashboard():
    """Main dashboard view."""
    return render_template("dashboard.html")


@app.route("/opportunities")
def opportunities():
    """Opportunity queue view."""
    return render_template("opportunities.html")


@app.route("/tasks")
def tasks():
    """Task board view."""
    return render_template("tasks.html")


@app.route("/learning")
def learning():
    """Learning & memory view."""
    return render_template("learning.html")


@app.route("/tracking")
def tracking():
    """Tracking integrity view."""
    return render_template("tracking.html")


@app.route("/admin")
def admin():
    """Admin settings view."""
    return render_template("admin.html")


# ============================================================
# API ROUTES
# ============================================================

@app.route("/api/dashboard")
def api_dashboard():
    """Get dashboard KPIs and posture."""
    config = load_config()
    ledger = ActionLedger()

    # Load latest evaluation results if available
    eval_path = DATA_PATH / "latest_evaluation.json"
    eval_data = {}
    if eval_path.exists():
        with open(eval_path) as f:
            eval_data = json.load(f)

    # Calculate KPIs
    ledger_summary = ledger.summary()

    # Determine governor posture
    pending_actions = len(ledger.get_actions_by_status(ActionStatus.PROPOSED))
    implemented = len(ledger.get_actions_by_status(ActionStatus.IMPLEMENTED))

    if pending_actions == 0 and implemented == 0:
        posture = "PRESERVATION"
        posture_color = "green"
    elif pending_actions > 5:
        posture = "ACTIVE"
        posture_color = "blue"
    else:
        posture = "MONITORING"
        posture_color = "yellow"

    return jsonify({
        "kpis": {
            "total_pages": eval_data.get("total_pages", 0),
            "pages_with_action": eval_data.get("pages_with_action", 0),
            "action_rate": eval_data.get("action_rate", 0),
            "total_expected_value": eval_data.get("total_expected_value", 0),
        },
        "posture": {
            "mode": posture,
            "color": posture_color,
        },
        "ledger_summary": ledger_summary,
        "config": {
            "aov": config.get("profit_model", {}).get("aov", 53.19),
            "margin": config.get("profit_model", {}).get("gross_margin_low", 0.27),
            "regret_budget": config.get("governance", {}).get("regret_budget_year", 2),
            "exploration_confidence_threshold": config.get("governance", {}).get("exploration_confidence_threshold", 0.55),
            "preservation_confidence_threshold": config.get("governance", {}).get("preservation_confidence_threshold", 0.75),
        },
        "last_run": eval_data.get("timestamp", "Never"),
    })


@app.route("/api/opportunities")
def api_opportunities():
    """Get ranked opportunity queue."""
    eval_path = DATA_PATH / "latest_evaluation.json"

    if not eval_path.exists():
        return jsonify({"opportunities": [], "message": "No evaluation data. Run workflow first."})

    with open(eval_path) as f:
        eval_data = json.load(f)

    # Get URLs with active tasks (not closed) — these are already on the task board
    ledger = ActionLedger()
    active_task_urls = set()
    for status in (ActionStatus.PROPOSED, ActionStatus.APPROVED,
                   ActionStatus.IMPLEMENTED, ActionStatus.MEASURED):
        for action in ledger.get_actions_by_status(status):
            active_task_urls.add(action.url)

    # Return all evaluated pages, but mark those with active tasks
    all_results = eval_data.get("results", [])
    for r in all_results:
        r["has_active_task"] = r.get("url", "") in active_task_urls

    # Sort: actionable items first (by priority), then observe, then no-action
    action_order = {"NO_ACTION": 2, "OBSERVE_ONLY": 1}
    all_results.sort(
        key=lambda x: (
            action_order.get(x.get("recommended_action", ""), 0),
            -(x.get("priority_score", 0)),
        )
    )

    return jsonify({
        "opportunities": all_results,
        "total": len(all_results),
        "actionable": sum(1 for r in all_results if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)),
        "active_tasks": len(active_task_urls),
    })


@app.route("/api/tasks")
def api_tasks():
    """Get task board data."""
    ledger = ActionLedger()

    tasks_by_status = {
        "proposed": [],
        "approved": [],
        "implemented": [],
        "measured": [],
        "closed": [],
    }

    for action in ledger.get_all_actions():
        status = action.status.value
        rec = action.recommendation_json or {}
        tasks_by_status[status].append({
            "action_id": action.action_id,
            "url": action.url,
            "action_type": action.action_type,
            "score_type": action.score_type,
            "score_value": round(action.score_value, 2),
            "confidence": round(action.confidence, 2),
            "created_at": action.created_at,
            "implemented_at": action.implemented_at,
            "outcome": action.outcome.value if action.outcome else None,
            "notes": action.notes,
            "risk_level": rec.get("risk_level", "low"),
            "mode": rec.get("mode"),
            "primary_constraint": rec.get("primary_constraint"),
            "asset_type": rec.get("asset_type"),
            "implementation_steps": rec.get("implementation_steps", []),
            "implementation_summary": rec.get("implementation_summary", ""),
            "demand_score": rec.get("demand_score"),
        })

    return jsonify(tasks_by_status)


@app.route("/api/opportunities/approve", methods=["POST"])
def api_approve_opportunity():
    """Approve an opportunity and create a proposed action."""
    data = request.json
    ledger = ActionLedger()

    from src.ledger.action_ledger import ActionFingerprint, ActionRecord
    import uuid

    # Create fingerprint from opportunity data
    fingerprint = ActionFingerprint(
        page_type=data.get("page_type", "unknown"),
        intent_cluster=data.get("intent_cluster", "unknown"),
        action_surface=data.get("action_surface", "other"),
        action_type=data.get("action", "OBSERVE_ONLY"),
    )

    # Generate action ID
    action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"

    # Create action record — store full context for task board display
    action = ActionRecord(
        action_id=action_id,
        url=data.get("url", ""),
        action_type=data.get("action", "OBSERVE_ONLY"),
        score_type=data.get("score_type", "EV"),
        score_value=data.get("expected_value", 0),
        confidence=data.get("confidence", 0.5),
        fingerprint=fingerprint,
        recommendation_json={
            "mode": data.get("mode"),
            "risk_level": data.get("risk_level"),
            "implementation_steps": data.get("implementation_steps", []),
            "source": data.get("source", "manual"),
            "primary_constraint": data.get("primary_constraint"),
            "asset_type": data.get("asset_type"),
            "demand_score": data.get("demand_score"),
            "implementation_summary": data.get("implementation_summary"),
        },
    )

    # Add to ledger
    ledger.add_action(action)

    return jsonify({
        "success": True,
        "action_id": action_id,
        "message": f"Action {action_id} created and added to Task Board",
    })


@app.route("/api/ai/recommend", methods=["POST"])
def api_ai_recommend():
    """Get AI-powered SEO recommendations for a page.

    Uses cached crawl data from evaluation results (page_metadata)
    instead of re-fetching the page live.
    """
    import os
    import httpx

    data = request.json
    url = data.get("url")
    opportunity = data.get("opportunity", {})

    if not url:
        return jsonify({"error": "URL required"}), 400

    config = load_config()
    ai_config = config.get("ai", {})
    gov_config = config.get("governance", {})

    # Lane-aware confidence thresholds from admin config
    exploration_threshold = gov_config.get("exploration_confidence_threshold", 0.55)
    preservation_threshold = gov_config.get("preservation_confidence_threshold", 0.75)

    # Get API key from environment or config
    api_key = os.environ.get(ai_config.get("api_key_env", "OPENAI_API_KEY"))
    if not api_key:
        return jsonify({"error": "OpenAI API key not configured. Set OPENAI_API_KEY environment variable."}), 400

    model = ai_config.get("model", "gpt-4o-mini")
    temperature = ai_config.get("temperature", 0.4)
    max_tokens = ai_config.get("max_tokens", 4500)
    timeout_sec = ai_config.get("timeout", 30)
    max_queries = ai_config.get("max_queries_in_prompt", 0)
    max_issues = ai_config.get("max_issues_in_prompt", 0)
    min_context = ai_config.get("min_context_fields", 3)

    # ── Page metadata from frontend (populated by Fetch Page button) ──
    pm = opportunity.get("page_metadata", {})
    cached_title = pm.get("title", "")
    cached_h1 = pm.get("h1", "")
    cached_meta = pm.get("meta_description", "")
    cached_canonical = pm.get("canonical_url", "")
    cached_word_count = pm.get("word_count", 0)
    cached_content_preview = pm.get("content_preview", "")

    page_analysis = {
        "title": cached_title,
        "meta_description": cached_meta,
        "canonical_url": cached_canonical,
        "h1": cached_h1,
        "word_count": cached_word_count,
        "content_preview": cached_content_preview,
        "has_crawl_data": pm.get("has_crawl_data", False),
    }

    # ── Confidence short-circuit ─────────────────────────────────
    # If confidence is below the EXPLORATION threshold (lowest lane),
    # no action lane applies — return NO_ACTION without an API call.
    confidence = opportunity.get("confidence", 0)
    if confidence < exploration_threshold:
        return jsonify({
            "success": True,
            "url": url,
            "page_analysis": page_analysis,
            "recommendations": {
                "summary": (
                    f"Confidence {confidence:.2f} is below the {exploration_threshold} minimum for any action lane. "
                    f"No action recommended."
                ),
                "validity_audit": {},
                "funnel_analysis": {},
                "constraint_accountability": {},
                "recommendations": [{
                    "action_type": "NO_ACTION",
                    "diagnosed_constraint": "Confidence below threshold",
                    "exact_changes": "None — confidence too low for any action lane.",
                    "why_this_works": (
                        f"System confidence is {confidence:.2f}, below {exploration_threshold} (EXPLORATION) "
                        f"and {preservation_threshold} (PRESERVATION). EV: ${opportunity.get('expected_value', 0):.2f}."
                    ),
                    "risk_level": "low",
                    "rollback_plan": "N/A — no action taken.",
                    "measurement": {
                        "primary_metric": "N/A",
                        "expected_direction": "stable",
                        "evaluation_window": "N/A",
                        "stop_threshold": "N/A",
                        "continue_threshold": "N/A",
                    },
                }],
                "no_actions": [
                    "All elements: confidence too low for safe evaluation"
                ],
            },
        })

    # ── Required context validation ──────────────────────────────
    critical_context = {
        "has_url": bool(url),
        "has_asset_type": bool(opportunity.get("asset_type")),
        "has_performance": bool(
            opportunity.get("top_queries")
            or opportunity.get("demand_score") is not None
        ),
        "has_constraint": bool(opportunity.get("primary_constraint")),
    }

    missing_critical = [k for k, v in critical_context.items() if not v]

    if len(missing_critical) >= min_context:
        return jsonify({
            "success": True,
            "url": url,
            "page_analysis": page_analysis,
            "recommendations": {
                "no_action": True,
                "reason": "Insufficient context for safe recommendation.",
                "missing_context": missing_critical,
            },
        })

    # ── Build structured inputs per new AI Recommendation spec ──
    asset_type = opportunity.get("asset_type", "other").upper()
    page_type = asset_type.lower()
    if page_type not in ("product", "category", "blog"):
        page_type = "other"

    # ── 1) Constraint evidence ──────────────────────────────────
    constraint_list = []
    if opportunity.get("constraints"):
        for c in opportunity.get("constraints", []):
            constraint_list.append(
                f"- [{c.get('severity', 'medium')}] {c.get('constraint_type', 'unknown')}: "
                f"{c.get('description', '')}"
            )
    constraints_str = "\n".join(constraint_list) if constraint_list else "None detected."

    # ── 2) GSC query data ───────────────────────────────────────
    gsc_lines = []
    if opportunity.get("top_queries"):
        queries = opportunity.get("top_queries", [])
        for q in (queries[:max_queries] if max_queries else queries):
            gsc_lines.append(
                f'  {{query: "{q.get("query")}", impressions: {q.get("impressions", 0)}, '
                f'clicks: {q.get("clicks", 0)}, position: {q.get("position", 0)}, '
                f'ctr: {q.get("ctr", 0)}%}}'
            )
    gsc_str = "\n".join(gsc_lines) if gsc_lines else "null"

    # ── 2b) Server-side CTR suppression detection ────────────────
    # If CTR is heavily suppressed relative to position, inject a hard
    # instruction so the model cannot defer the title/meta test.
    ctr_suppression_flag = ""
    if opportunity.get("top_queries"):
        queries = opportunity.get("top_queries", [])
        total_impressions = sum(q.get("impressions", 0) for q in queries)
        total_clicks = sum(q.get("clicks", 0) for q in queries)
        if total_impressions > 500:
            actual_ctr = (total_clicks / total_impressions) * 100 if total_impressions else 0
            # Weighted average position
            weighted_pos = sum(
                q.get("position", 50) * q.get("impressions", 0) for q in queries
            ) / total_impressions if total_impressions else 50
            # Expected CTR by position (industry benchmarks)
            expected_ctr_map = {1: 28, 2: 15, 3: 10, 4: 7, 5: 5, 6: 4, 7: 3, 8: 2.5, 9: 2, 10: 1.5}
            expected_ctr = expected_ctr_map.get(round(weighted_pos), max(0.5, 30 / (weighted_pos + 1)))
            suppression_ratio = expected_ctr / actual_ctr if actual_ctr > 0 else 999

            if suppression_ratio > 5:  # CTR is 5x+ below expected
                ctr_suppression_flag = (
                    f"\n\n⚠️ SERVER-ENFORCED CTR ALERT ⚠️\n"
                    f"CTR is {suppression_ratio:.0f}x below position-expected rate "
                    f"(actual: {actual_ctr:.2f}%, expected: {expected_ctr:.1f}% at position {weighted_pos:.1f}).\n"
                    f"This is a {total_impressions:,} impression page.\n"
                    f"MANDATORY: You MUST propose a TITLE_META_TEST action for this page.\n"
                    f"You MUST NOT defer this. Title/meta fixes SERP acquisition; routing fixes monetization.\n"
                    f"These are independent. Propose BOTH in parallel.\n"
                )

    # ── 3) Internal outlinks from page metadata ─────────────────
    outlinks = pm.get("internal_outlinks", [])
    outlinks_lines = []
    for ol in outlinks[:30]:  # Limit to 30 links for prompt size
        outlinks_lines.append(
            f'  {{target_url: "{ol.get("target_url", "")}", '
            f'anchor_text: "{ol.get("anchor_text", "")}", '
            f'location: "{ol.get("location", "body")}"}}'
        )
    outlinks_str = "\n".join(outlinks_lines) if outlinks_lines else "null"

    # ── 4) Business context from config ─────────────────────────
    profit_cfg = config.get("profit_model", {})
    biz_ctx = config.get("business_context", {})
    aov = profit_cfg.get("aov", biz_ctx.get("aov", 53.19))
    margin_low = profit_cfg.get("gross_margin_low", 0.25)
    margin_high = profit_cfg.get("gross_margin_high", 0.30)
    break_even_roas = biz_ctx.get("break_even_roas", round(1.0 / margin_low, 1) if margin_low else 4.0)
    product_families = biz_ctx.get("product_families", [
        "name trains", "puzzles", "step stools", "books", "blocks", "rugs",
        "wooden toys", "play kitchens", "art supplies",
    ])

    business_context_str = (
        f"AOV: ${aov:.2f}\n"
        f"Gross margin range: {margin_low*100:.0f}%–{margin_high*100:.0f}%\n"
        f"Break-even ROAS: {break_even_roas}\n"
        f"Product families: {', '.join(product_families)}"
    )

    # ── 5) Available target pages — auto-populated from evaluation data ──
    # Read all crawled pages from evaluation so AI knows what actually exists
    eval_path = DATA_PATH / "latest_evaluation.json"
    site_pages = {"category": [], "product": [], "blog": [], "other": []}
    if eval_path.exists():
        try:
            with open(eval_path) as f:
                eval_data = json.load(f)
            for r in eval_data.get("results", []):
                r_url = r.get("url", "")
                if r_url == url:
                    continue  # Skip the current page
                r_type = r.get("asset_type", "other").lower()
                if r_type not in site_pages:
                    r_type = "other"
                pm_r = r.get("page_metadata", {})
                title_r = pm_r.get("title", "") or pm_r.get("h1", "")
                # Include basic performance signal for prioritization
                ev = r.get("expected_value", 0)
                conf = r.get("confidence", 0)
                site_pages[r_type].append({
                    "url": r_url,
                    "title": title_r[:80],
                    "ev": round(ev, 2) if ev else 0,
                })
        except Exception:
            pass

    # Format site map — compact but informative
    target_pages_str = ""
    for ptype in ("category", "product", "blog"):
        pages = site_pages.get(ptype, [])
        if pages:
            # Sort by expected value descending, show top 30
            pages.sort(key=lambda p: p["ev"], reverse=True)
            target_pages_str += f"\n{ptype.upper()} pages ({len(pages)} total):\n"
            for p in pages[:30]:
                target_pages_str += f'  - {p["url"]}  [{p["title"]}]  EV=${p["ev"]}\n'
            if len(pages) > 30:
                target_pages_str += f"  ... and {len(pages) - 30} more\n"

    if not target_pages_str:
        target_pages_str = "null (no evaluation data — run evaluation first)"

    # ── 6) Above-fold HTML and robots meta ──────────────────────
    above_fold_html = pm.get("above_fold_html", "")
    robots_meta = pm.get("robots_meta", "")

    # ── 7) Pipeline scores — pass to AI for anchoring ─────────
    pipeline_ev = opportunity.get("expected_value", 0)
    pipeline_confidence = opportunity.get("confidence", 0)
    pipeline_intent = opportunity.get("intent_score", 0)
    pipeline_mode = opportunity.get("mode", "unknown")

    # ── 8) Build the full prompt per governor spec ──────────────
    prompt = f"""You are the Alphabet Trains Agentic Growth Governor.

Your role is to evaluate pages and propose actions that improve organic revenue
while preserving everything that is already correct and valid.

You are NOT allowed to "optimize by default."
You must first prove something is broken, weak, or misaligned before proposing change.

────────────────────────────────
ABSOLUTE NON-DESTRUCTION RULE
────────────────────────────────
You must NEVER change, suggest changing, or re-test any element that is:
- Correct
- Valid
- Aligned with page intent
- Not causally linked to a diagnosed problem

If an element is valid, explicitly mark it as:
"VALID — NO CHANGE RECOMMENDED"

This includes (but is not limited to):
- Canonical tags that are self-referencing and correct
- Indexing directives that match intent
- Titles/meta that align with H1 and intent and are not causing measurable harm
- Content sections that already satisfy the user's informational need

Do NOT propose changes "just to test."
Testing is only allowed when a concrete constraint is proven.

────────────────────────────────
MANDATORY CONTEXT CHECK (HARD GATE)
────────────────────────────────
Before evaluation, confirm availability of:
- URL
- Asset type (PRODUCT / CATEGORY / BLOG)
- H1
- Meta title
- Meta description
- Canonical
- Above-the-fold content
- Internal links above the fold
- GSC impressions, CTR, position (28 days)

If any are missing:
→ Return NO_ACTION
→ Reason: Insufficient context for safe evaluation

────────────────────────────────
STEP 1 — VALIDITY AUDIT (MUST COME FIRST)
────────────────────────────────
For each of the following, explicitly classify as:
VALID / INVALID / INCONCLUSIVE

- Canonical
- Indexability
- Page intent alignment (title, H1, content)
- SERP alignment
- Internal link presence
- Funnel role suitability

If VALID:
→ Lock the element
→ Do NOT include it in recommendations

────────────────────────────────
STEP 2 — FUNNEL ANALYSIS (MANDATORY)
────────────────────────────────
You must perform funnel analysis BEFORE proposing any action.

For this page, explicitly output:

A) Entry Intent Analysis
- Dominant query clusters (informational / commercial / mixed)
- % impression share by cluster
- What users expect as a next step

B) Current Funnel Paths (Observed)
- Page → (where users actually go, if known)
- If unknown, state "No observable downstream path"

C) Ideal Funnel Paths (Proposed)
- Page → Category → Product
  OR
- Page → Product
Explain WHY this path is correct for the dominant intent.

D) Weak Routing Diagnosis
Classify the failure as ONE OR MORE of:
- Missing next step
- Misaligned destination
- Poor placement/visibility
- Competing exits
- SERP pogo-stick behavior

If no routing weakness exists:
→ Mark routing as VALID
→ Do NOT propose internal linking changes

E) Funnel Opportunity Estimate (REQUIRED — SHOW YOUR MATH)
Do NOT invent percentages. Use this framework with THREE sensitivity bands:

For INTERNAL LINKING / ROUTING actions:
  Low:  impressions × routing_low  × CVR × AOV × margin = $X
  Base: impressions × routing_base × CVR × AOV × margin = $X
  High: impressions × routing_high × CVR × AOV × margin = $X
  Where:
    routing_low / base / high:
      If you have actual click data between pages → use it ± 30%
      If not → use 3% / 5% / 8% for in-content links on relevant content
      These are realistic priors for well-placed contextual links.
      Only go below 3% if the page has very low engagement or misaligned intent.
    downstream_CVR: Use site average or state "assumed [X]% — no page-level data"

For BLOG / GUIDE pages (CRITICAL — blogs are OPTION CREATORS, not cash registers):
  Blogs create purchase options. They introduce products, educate on categories,
  and seed future purchase intent. They are the TOP of the funnel.

  Direct routing value:
    Low:  impressions × 0.03 × downstream_CVR × AOV × margin
    Base: impressions × 0.05 × downstream_CVR × AOV × margin
    High: impressions × 0.08 × downstream_CVR × AOV × margin

  Assisted conversion value (THIS IS THE PRIMARY VALUE OF BLOGS):
    Low:  impressions × 0.03 × assisted_CVR × AOV × margin
    Base: impressions × 0.05 × assisted_CVR × AOV × margin
    High: impressions × 0.08 × assisted_CVR × AOV × margin
    Where assisted_CVR: 1–3% of assisted sessions (users who read, leave, return to buy)

  TOTAL blog value = direct + assisted. Report both rows AND the total.
  The assisted value will often EXCEED direct value. This is expected and correct.
  If your total blog value < $100/mo on 20,000+ impressions, your priors are too low.

For TITLE / META TESTS:
  Low:  impressions × (current_CTR × 1.3 − current_CTR) × downstream_CVR × AOV × margin
  Base: impressions × (target_CTR − current_CTR) × downstream_CVR × AOV × margin
  High: impressions × (target_CTR × 1.2 − current_CTR) × downstream_CVR × AOV × margin
  target_CTR MUST reference position-appropriate benchmarks:
    Position 1: ~28%  Position 3: ~10%  Position 5: ~5%
    Position 7: ~3%   Position 10: ~1.5%

Show each number on its own line. State every assumption.
Use the HIGH scenario for prioritization — blogs are strategic assets.

────────────────────────────────
CANNIBALIZATION HANDLING (MANDATORY IF DETECTED)
────────────────────────────────
If ANY constraint mentions cannibalization, query overlap, or competing pages:

1. State which queries are affected and which page(s) compete
2. Classify the impact:
   BLOCKING — cannibalization likely explains the primary symptom (e.g., low CTR,
     position instability). In this case:
     → Defer CTR/title tests (they will be unreliable)
     → Recommend consolidation review as primary action
     → Explain why other actions should wait
   CAUTIONARY — cannibalization exists but is not the primary cause:
     → Proceed with other actions but add explicit warning
     → Note which metrics may be unreliable due to cannibalization
   INFORMATIONAL — minor overlap, not materially affecting performance:
     → Note in no_actions with reasoning

You MUST NOT detect cannibalization and then ignore it.
It MUST appear in constraint_accountability with a clear disposition.

────────────────────────────────
STEP 3 — CONSTRAINT-TO-ACTION MAPPING
────────────────────────────────
Only after Steps 1–2 may you propose actions.

Each proposed action MUST:
- Map directly to a diagnosed constraint
- Be the MINIMAL change needed
- Be reversible or explicitly labeled irreversible

If an action does not clearly fix a diagnosed issue:
→ Do NOT propose it

PARALLEL ACTION RULE (CRITICAL — DO NOT OVER-DEFER):
Actions that target DIFFERENT surfaces are non-conflicting and SHOULD be proposed together:
  - TITLE_META_TEST improves SERP click acquisition (CTR)
  - INTERNAL_LINKING improves post-click monetization (routing)
  - CONTENT_CLARIFY improves on-page engagement
These are ORTHOGONAL. One does not need to "finish" before the other starts.

Do NOT defer a CTR test just because you also proposed internal linking.
Internal linking fixes post-click flow, NOT SERP attractiveness.
Title/meta fixes SERP attractiveness, NOT post-click flow.
Both can and should run in parallel when both constraints exist.

The ONLY valid reason to defer a CTR test is:
  - BLOCKING cannibalization (competing pages make CTR data unreliable)
  - Insufficient data to construct a valid test (no query patterns, no CTR data)
"Low CTR is likely caused by poor routing" is NOT a valid deferral reason.

MULTI-CONSTRAINT ACCOUNTABILITY RULE (CRITICAL):
Every constraint listed in the inputs MUST be accounted for in the output.
For EACH constraint, you MUST do ONE of:
  a) Produce a recommendation that directly addresses it
  b) Explicitly defer it with a VALID reason in constraint_accountability
  c) Explain why it's superseded by another action

You may NOT detect a constraint and then silently drop it in Step 3.
Specifically:
  - CTR suppression detected → MUST propose a title/meta test (parallel with other actions)
    UNLESS cannibalization is BLOCKING
  - Cannibalization detected → MUST follow cannibalization handling rules above
  - Routing failure detected → MUST propose specific linking changes OR explain why not
  - Content gap detected → MUST propose content action OR explain why not

Multiple constraints → multiple actions. Default is PARALLEL, not sequential.
"I only proposed one action" is NOT acceptable if multiple constraints were diagnosed.

VALIDITY AUDIT → ACTION MAPPING (MANDATORY):
Every element you mark INVALID in Step 1 MUST result in EITHER:
  a) A recommendation in Step 3 that addresses it, OR
  b) An explicit entry in constraint_accountability explaining why it was deferred
You CANNOT mark an element INVALID and then produce no action and no deferral for it.
If serp_alignment is INVALID → you MUST address it (usually via TITLE_META_TEST).
If internal_links is INVALID → you MUST address it (usually via INTERNAL_LINKING).
If funnel_role is INVALID → you MUST address it.

INTERNAL LINK URL VERIFICATION (MANDATORY):
You MUST ONLY recommend internal links to URLs that appear in the
available_target_pages list below. If a URL is not in that list, it does not exist.
Do NOT invent, guess, or construct URLs. If no suitable target page exists in the
list, state this explicitly and recommend NO_ACTION for internal linking.

────────────────────────────────
ALLOWED ACTIONS BY ASSET TYPE
────────────────────────────────

PRODUCT
- Fix real CTR suppression
- Improve schema
- Add relevant internal links IN
- Do NOT expand informational content

CATEGORY
- Fix intent misalignment
- Improve intro clarity
- Add internal links from blogs
- Do NOT add blog-style content

BLOG / GUIDE
- Improve funnel routing
- Add or refine internal links
- Propose title/meta tests ONLY IF:
  – CTR is suppressed relative to position
  – Title/meta are shown to misalign with dominant queries
  – Change does not alter informational intent
- Do NOT optimize for traffic alone

────────────────────────────────
INTERNAL LINKING SPECIFICITY RULE
────────────────────────────────
Any INTERNAL_LINKING recommendation MUST include ALL of:
  1. EXACT target URL(s) — from available_target_pages or discovered outlinks
  2. WHY this target (not another) — tie to funnel analysis and query intent
  3. WHERE in the content — e.g., "after paragraph 2 where [topic] is discussed",
     "in a CTA block below the product comparison"
  4. HOW MANY links — specific count with reasoning (e.g., "2 links: 1 primary, 1 secondary")
  5. PRIMARY vs SECONDARY — which is the main funnel step, which are supporting

"Add internal links to relevant category pages" is NOT acceptable.
"Add 2 links to /collections/wooden-trains: 1 in paragraph 3 after the
comparison section (primary funnel step) and 1 in the summary CTA
(secondary reinforcement), because the dominant query cluster is
purchase-oriented and this category is the logical next step" IS acceptable.

────────────────────────────────
LINK TARGET INTENT GUARDRAILS
────────────────────────────────
NEVER recommend internal links to:
  - Search pages (/search, /catalogsearch/, /s?q=)
  - Account/login pages (/account, /login, /register)
  - Cart/checkout pages (/cart, /checkout)
  - Utility pages (/sitemap, /privacy, /terms, /contact, /about)
  - Advanced search or filter pages (/catalogsearch/advanced, /filter)
  - Any page whose purpose is navigational infrastructure, not content or commerce

Link targets MUST be one of:
  - Product pages (direct conversion surface)
  - Category/collection pages (product discovery surface)
  - Curated guide/comparison pages (consideration surface)
  - Related blog posts (only if they deepen the funnel, not widen it)

If a URL in the current outlinks looks like a utility/infrastructure page,
explicitly note it as a misplaced link in the funnel analysis.

────────────────────────────────
META / TITLE CHANGE SAFETY RULE
────────────────────────────────
Before proposing any title/meta change, you MUST:
- State why the current version is insufficient
- Reference specific query patterns
- Confirm the H1 and content already support the change
- Provide 2–3 variants (not one)
- State rollback conditions

If you cannot do all of the above:
→ NO_ACTION on title/meta

════════════════════════════════
CORRECTION LAYER (FINAL OVERRIDE)
════════════════════════════════
SYSTEM INSTRUCTION — GROWTH & VALUATION CORRECTION ONLY

This prompt applies as a correction layer on top of the existing evaluation system.

You are NOT permitted to:
• Re-evaluate validity audits
• Re-open canonical, indexability, or intent checks
• Alter funnel structure logic already marked valid
• Modify governance, reversibility, or NO_ACTION discipline

All previously validated reasoning MUST remain unchanged.

Your task is to correct ONLY the growth-blocking defects below.

────────────────────────────────
1) ACQUISITION VS MONETIZATION SEPARATION (CTR LOGIC)
────────────────────────────────

CTR (SERP acquisition) and routing (post-click monetization) are independent constraints.

Rules:
• If CTR is materially suppressed relative to average position
• AND the title/meta are generic or weakly aligned to dominant queries
→ You MAY propose a TITLE/META TEST even if routing is weak.

• Title/meta tests are reversible and may run in PARALLEL with routing fixes.

Forbidden:
• Do NOT defer CTR fixes because routing is weak.
• Do NOT claim internal linking materially improves SERP CTR.

────────────────────────────────
2) ASSIST / FUNNEL VALUE MODEL CORRECTION
────────────────────────────────

Blogs and non-terminal pages must be valued as ASSIST / OPTION-CREATION assets,
not as terminal converters.

Rules:
• Do NOT use single-point worst-case estimates.
• Do NOT collapse uncertainty into near-zero value.
• Weak current routing does NOT imply low opportunity.

You MUST model funnel value using SCENARIO RANGES.

────────────────────────────────
MANDATORY VALUE DERIVATION (NO EXCEPTIONS)
────────────────────────────────

Whenever you output ANY estimated value (direct, assisted, funnel, or total),
you MUST explicitly show how the value was derived.

For EACH scenario (LOW / BASE / HIGH), you MUST provide:

• Demand input used (impressions or sessions) — cite the exact number from INPUTS
• Routing probability assumed — cite evidence: observed CTR, link density, or industry benchmark
• Downstream conversion rate assumed — cite evidence: site avg, GA4 data, or benchmark
• Revenue proxy used (AOV or equivalent) — must match business_context AOV
• Margin applied (if applicable) — must match business_context margin range

You MUST express the calculation as explicit arithmetic, for example:
21,000 impressions × 3% routing × 2.1% CVR × $53.19 AOV × 27% margin = $X

ROUTING PROBABILITY EVIDENCE RULE:
Every routing % you assume MUST cite ONE of these sources:
  – OBSERVED: actual outlink CTR from internal_outlinks data
  – BENCHMARK: named industry benchmark (e.g. "avg blog→category CTR is 3-5%")
  – INFERRED: explicit reasoning from page layout/content (e.g. "no CTA above fold → low routing")
Unsourced routing assumptions are forbidden. If you cannot justify a routing %, use 0% and state NO_ACTION.

SELF-CONSISTENCY CHECK (mandatory before output):
After computing your scenario values, verify:
  – LOW < BASE < HIGH (monotonic)
  – Your total value summary matches the BASE scenario (not LOW, not HIGH)
  – Your total value range [LOW..HIGH] is stated, not just BASE
  – If pipeline_expected_value falls within [LOW..HIGH], state agreement
  – If pipeline_expected_value falls OUTSIDE [LOW..HIGH], explain the divergence

Interpretation rules:
• If assumptions are weak or speculative → LOWER CONFIDENCE, not VALUE
• If assumptions cannot be justified → NO_ACTION (value cannot be responsibly estimated)

Forbidden:
• Unexplained dollar figures
• Single-scenario estimates
• Inflated confidence to compensate for uncertainty
• Routing % without evidence citation

PIPELINE EV ANCHORING:
The pipeline has pre-calculated an Expected Value for this page (see pipeline_expected_value in INPUTS).
Your value estimate in opportunity_estimate MUST acknowledge and reconcile with this figure:
• If your math produces a similar range (within 2×), state agreement.
• If your math diverges significantly (>2× difference), you MUST explain WHY:
  – e.g. "Pipeline uses 5% CTR assumption; actual routing is lower because [reason]"
  – e.g. "Pipeline underestimates because it ignores assisted value through [path]"
• NEVER ignore the pipeline figure. The user sees BOTH numbers side by side.
  A 10× mismatch with no explanation is a model integrity failure.

────────────────────────────────
3) INTERNAL LINK TARGET INTENT CONTROL
────────────────────────────────

Internal links must narrow the funnel while supporting topical authority.

Rules:
• Primary funnel progress counts ONLY when linking to:
  – Category pages
  – Product pages

• Blog → blog links are allowed ONLY IF they:
  – Provide prerequisite understanding
  – OR advance intent closer to a commercial decision
  – AND do NOT replace category/product links

Constraints:
• A blog page must always contain a clear primary next step to a category or product.
• Blog → blog links are secondary and supportive only.

Forbidden:
• Utility, search, or navigational pages as funnel links
• Lateral blog loops with no commercial exit
• Using blog → blog links to justify funnel "improvement"

Every internal link recommendation MUST justify:
• Why this destination matches dominant intent
• Whether it serves understanding or funnel progression
• Why alternative revenue pages were rejected

────────────────────────────────
4) PARALLEL ACTION ALLOWANCE (LIMITED)
────────────────────────────────

You MAY propose multiple actions IF:
• Each addresses a different diagnosed constraint
• Each is reversible
• They do not interfere with one another

Example allowed:
• Title/meta test (acquisition)
• Internal linking (monetization)

Example forbidden:
• Multiple title rewrites
• Structural URL changes
• Broad content expansion

────────────────────────────────
OUTPUT CONSTRAINT
────────────────────────────────

Do NOT restate or re-validate existing correct reasoning.

Explicitly label which correction area each action addresses:
• CTR acquisition
• Assist / funnel valuation
• Funnel routing

If none apply:
→ Return NO_ACTION
→ State: "No growth correction required."

END CORRECTION LAYER

────────────────────────────────
INPUTS
────────────────────────────────
url: {url}
page_type: {page_type}
pipeline_expected_value: ${pipeline_ev:.2f}
pipeline_confidence: {pipeline_confidence:.0%}
pipeline_intent_score: {pipeline_intent:.0%}
pipeline_mode: {pipeline_mode}
title_tag_current: {cached_title or 'null'}
meta_desc_current: {cached_meta or 'null'}
h1_current: {cached_h1 or 'null'}
canonical_current: {cached_canonical or 'null'}
robots_meta: {robots_meta or 'null'}
word_count: {cached_word_count}
above_fold_html: {above_fold_html[:1500] if above_fold_html else 'null'}

internal_outlinks:
{outlinks_str}

top_gsc_queries:
{gsc_str}

constraints:
{constraints_str}

business_context:
{business_context_str}

site_pages (ALL known pages by type — ONLY link to URLs in this list):
{target_pages_str}
{ctr_suppression_flag}
────────────────────────────────
OUTPUT FORMAT (STRICT)
────────────────────────────────
Respond ONLY with valid JSON (no markdown fences, no commentary outside JSON):
{{{{
  "validity_audit": {{{{
    "canonical": "<VALID | INVALID | INCONCLUSIVE> — <brief reason>",
    "indexability": "<VALID | INVALID | INCONCLUSIVE> — <brief reason>",
    "intent_alignment": "<VALID | INVALID | INCONCLUSIVE> — <brief reason>",
    "serp_alignment": "<VALID | INVALID | INCONCLUSIVE> — <brief reason>",
    "internal_links": "<VALID | INVALID | INCONCLUSIVE> — <brief reason>",
    "funnel_role": "<VALID | INVALID | INCONCLUSIVE> — <brief reason>"
  }}}},
  "funnel_analysis": {{{{
    "entry_intent": "<dominant query clusters, impression share, what users expect next>",
    "current_paths": "<where users currently go from this page, or 'No observable downstream path'>",
    "ideal_paths": "<proposed funnel path and WHY this path matches dominant intent>",
    "routing_diagnosis": "<failure type(s) or VALID>",
    "opportunity_estimate": {{{{
      "low": {{{{ "math": "<impressions × routing% × CVR × AOV × margin = $X>", "routing_evidence": "<OBSERVED|BENCHMARK|INFERRED: source>", "total": <number> }}}},
      "base": {{{{ "math": "<impressions × routing% × CVR × AOV × margin = $X>", "routing_evidence": "<OBSERVED|BENCHMARK|INFERRED: source>", "total": <number> }}}},
      "high": {{{{ "math": "<impressions × routing% × CVR × AOV × margin = $X>", "routing_evidence": "<OBSERVED|BENCHMARK|INFERRED: source>", "total": <number> }}}},
      "summary": "<Total value range: $LOW–$HIGH/mo (base: $BASE). For blogs: direct $X + assisted $Y>"
    }}}},
    "pipeline_reconciliation": "<Pipeline EV is $X. My base estimate is $Y. [AGREES within 2× | DIVERGES because: reason]>"
  }}}},
  "constraint_accountability": {{{{
    "<constraint_type>": {{{{
      "disposition": "<ACTIONED | DEFERRED | SUPERSEDED>",
      "action_ref": "<which recommendation # addresses it, or null if deferred>",
      "reasoning": "<why this disposition — if DEFERRED, explain what blocks action>"
    }}}}
  }}}},
  "recommendations": [
    {{{{
      "action_type": "<TITLE_META_TEST | INTERNAL_LINKING | VISIBILITY_FIX | CANONICAL_FIX | CONTENT_CLARIFY | CONSOLIDATION_REVIEW | NO_ACTION>",
      "diagnosed_constraint": "<the specific constraint this fixes>",
      "exact_changes": "<implementation-ready details — for INTERNAL_LINKING: exact URLs, placement, count, primary/secondary>",
      "why_this_works": "<tie to diagnosed constraint + GSC data>",
      "risk_level": "<low | medium | high>",
      "rollback_plan": "<how to undo>",
      "measurement": {{{{
        "primary_metric": "<metric>",
        "expected_direction": "<increase | decrease | stable>",
        "evaluation_window": "<time period>",
        "stop_threshold": "<when to rollback>",
        "continue_threshold": "<when to keep going>"
      }}}}
    }}}}
  ],
  "no_actions": [
    "<element>: <why no change is needed>"
  ]
}}}}

If nothing is broken or improvable:
→ Return empty recommendations array with justification in no_actions."""

    # ── Reproducibility: hash the prompt ──────────────────────
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    system_message = (
        "You are the Alphabet Trains Agentic Growth Governor. "
        f"This page is classified as {page_type}. "
        "You evaluate pages and propose actions that improve organic revenue "
        "while preserving everything that is already correct and valid. "
        "You are NOT allowed to optimize by default — you must first prove something is "
        "broken, weak, or misaligned before proposing change. "
        "NEVER suggest changing valid elements. "
        "Follow the 3-step process: validity audit → funnel analysis → constraint-to-action mapping. "
        "CRITICAL RULES: "
        "1) Every input constraint MUST be accounted for — either actioned, deferred, or superseded. "
        "Never silently drop a constraint. "
        "2) Actions on different surfaces (CTR vs routing vs content) are ORTHOGONAL — propose them in PARALLEL, not sequentially. "
        "Do NOT defer CTR tests just because you also proposed internal linking. "
        "3) Cannibalization MUST be classified (BLOCKING/CAUTIONARY/INFORMATIONAL) and resolved, not just logged. "
        "4) Internal links MUST target conversion surfaces (product, category, curated guides) — NEVER utility/search/account pages. "
        "5) Funnel math MUST show low/base/high sensitivity bands with explicit arithmetic per scenario. "
        "Blogs are option creators — assisted value often exceeds direct. If total blog value < $100/mo on 20k+ impressions, priors are too low. "
        "6) CONFIDENCE COHERENCE: Your stated confidence must be consistent with your assumptions. "
        "If you cite 'no GA4 data' or 'unknown routing' as limitations, confidence MUST be ≤0.5. "
        "If your routing % is speculative (INFERRED), confidence MUST be ≤0.6. "
        "Only OBSERVED evidence supports confidence >0.7. "
        "7) VALUE COHERENCE: The total in your opportunity_estimate.summary MUST equal your base scenario total. "
        "The pipeline_reconciliation field MUST reference the pipeline_expected_value from INPUTS. "
        "Respond ONLY with valid JSON. No markdown fences, no commentary outside the JSON."
    )

    # Call OpenAI API
    try:
        import json as json_module
        openai_response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=float(timeout_sec),
        )

        if openai_response.status_code != 200:
            return jsonify({"error": f"OpenAI API error: {openai_response.text}"}), 500

        result = openai_response.json()
        ai_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = result.get("usage", {})

        # ── Reproducibility log entry ──────────────────────────
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "url": url,
            "prompt_hash": prompt_hash,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tokens_used": {
                "prompt": usage.get("prompt_tokens"),
                "completion": usage.get("completion_tokens"),
                "total": usage.get("total_tokens"),
            },
            "confidence": confidence,
            "asset_type": asset_type,
            "constraint": opportunity.get("primary_constraint"),
        }

        # Append to JSONL log
        log_path = DATA_PATH / "ai_call_log.jsonl"
        try:
            with open(log_path, "a") as lf:
                lf.write(json.dumps(log_entry) + "\n")
        except Exception:
            pass  # Don't fail the request over logging

        # Try to parse as JSON
        try:
            # Remove markdown code blocks if present
            clean = ai_content.strip()
            if clean.startswith("```json"):
                clean = clean[7:]
            elif clean.startswith("```"):
                clean = clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()

            recommendations = json_module.loads(clean)
        except Exception:
            recommendations = {"raw_response": ai_content}

        response_data = {
            "success": True,
            "url": url,
            "page_analysis": page_analysis,
            "recommendations": recommendations,
            "reproducibility": {
                "prompt_hash": prompt_hash,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tokens_used": log_entry["tokens_used"],
            },
        }

        # Persist AI recommendations so they survive page refresh
        _persist_opportunity_update(url, {
            "ai_recommendations": recommendations,
            "ai_reproducibility": response_data["reproducibility"],
            "ai_timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return jsonify(response_data)

    except Exception as e:
        return jsonify({"error": f"AI request failed: {e}"}), 500


@app.route("/api/gsc/inspect", methods=["POST"])
def api_gsc_inspect():
    """Inspect a URL in GSC to check indexing status."""
    data = request.json
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL required"}), 400

    config = load_config()
    gsc_config = config.get("data_sources", {}).get("gsc", {})

    from src.data_sources.gsc_client import GSCClient
    client = GSCClient(
        site_url=gsc_config.get("property_url", ""),
        credentials_path=gsc_config.get("credentials_path"),
    )

    result = client.inspect_url(url)
    return jsonify(result)


@app.route("/api/gsc/request-indexing", methods=["POST"])
def api_gsc_request_indexing():
    """Request (re)indexing of a URL."""
    data = request.json
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL required"}), 400

    config = load_config()
    gsc_config = config.get("data_sources", {}).get("gsc", {})

    from src.data_sources.gsc_client import GSCClient
    client = GSCClient(
        site_url=gsc_config.get("property_url", ""),
        credentials_path=gsc_config.get("credentials_path"),
    )

    result = client.request_indexing(url)
    return jsonify(result)


@app.route("/api/tasks/<action_id>/advance", methods=["POST"])
def api_advance_task(action_id):
    """Advance task to next status."""
    ledger = ActionLedger()
    action = ledger.get_action(action_id)

    if not action:
        return jsonify({"error": "Action not found"}), 404

    # Status progression
    progression = {
        ActionStatus.PROPOSED: ActionStatus.APPROVED,
        ActionStatus.APPROVED: ActionStatus.IMPLEMENTED,
        ActionStatus.IMPLEMENTED: ActionStatus.MEASURED,
        ActionStatus.MEASURED: ActionStatus.CLOSED,
    }

    current = action.status
    if current in progression:
        action.update_status(progression[current])
        ledger.update_action(action)
        return jsonify({"success": True, "new_status": action.status.value})

    return jsonify({"error": "Cannot advance from current status"}), 400


@app.route("/api/tasks/<action_id>/reject", methods=["POST"])
def api_reject_task(action_id):
    """Reject a task — removes it from the board."""
    ledger = ActionLedger()
    data = request.json or {}
    action = ledger.get_action(action_id)

    if not action:
        return jsonify({"error": "Action not found"}), 404

    # Record rejection as negative outcome with notes, then close
    action.outcome = ActionOutcome.NEGATIVE
    action.notes = f"REJECTED: {data.get('reason', 'No reason given')}"
    action.update_status(ActionStatus.CLOSED)
    ledger.update_action(action)

    return jsonify({"success": True, "message": f"Task {action_id} rejected and closed"})


@app.route("/api/tasks/<action_id>/send-back", methods=["POST"])
def api_send_back_task(action_id):
    """Send a task back for re-evaluation. Removes it from the task board
    so it will be re-scored on the next evaluation run."""
    ledger = ActionLedger()
    data = request.json or {}
    action = ledger.get_action(action_id)

    if not action:
        return jsonify({"error": "Action not found"}), 404

    if action.status in (ActionStatus.MEASURED, ActionStatus.CLOSED):
        return jsonify({"error": "Cannot send back a task that is already measured or closed"}), 400

    # Remove the task from the ledger so the URL rejoins the opportunity pool
    ledger.delete_action(action_id)

    return jsonify({
        "success": True,
        "message": f"Task {action_id} sent back. URL will be re-evaluated on next run.",
        "url": action.url,
    })


@app.route("/api/tasks/fix-sent-back", methods=["POST"])
def api_fix_sent_back_tasks():
    """Clean up tasks that were incorrectly closed by the old send-back bug.
    Deletes any closed task whose notes start with 'SENT BACK' so the URL
    rejoins the opportunity pool."""
    ledger = ActionLedger()
    fixed = []
    for action in ledger.get_actions_by_status(ActionStatus.CLOSED):
        if action.notes and action.notes.startswith("SENT BACK"):
            fixed.append({"action_id": action.action_id, "url": action.url})
            ledger.delete_action(action.action_id)

    return jsonify({
        "success": True,
        "fixed": len(fixed),
        "tasks": fixed,
        "message": f"Removed {len(fixed)} incorrectly closed task(s). Their URLs will rejoin the opportunity pool.",
    })


@app.route("/api/tasks/<action_id>/outcome", methods=["POST"])
def api_record_outcome(action_id):
    """Record outcome for a task."""
    ledger = ActionLedger()
    data = request.json

    outcome_str = data.get("outcome", "").upper()
    try:
        outcome = ActionOutcome(outcome_str.lower())
    except ValueError:
        return jsonify({"error": f"Invalid outcome: {outcome_str}"}), 400

    success = ledger.record_outcome(
        action_id=action_id,
        outcome=outcome,
        outcome_metrics=data.get("metrics"),
        notes=data.get("notes", ""),
    )

    if success:
        return jsonify({"success": True})
    return jsonify({"error": "Failed to record outcome"}), 400


@app.route("/api/learning")
def api_learning():
    """Get learning and memory data."""
    ledger = ActionLedger()

    # Group actions by fingerprint
    fingerprint_stats = {}

    for action in ledger.get_all_actions():
        fp_hash = action.fingerprint.hash()

        if fp_hash not in fingerprint_stats:
            fingerprint_stats[fp_hash] = {
                "fingerprint": action.fingerprint.to_dict(),
                "hash": fp_hash,
                "total": 0,
                "positive": 0,
                "negative": 0,
                "neutral": 0,
                "inconclusive": 0,
                "actions": [],
            }

        stats = fingerprint_stats[fp_hash]
        stats["total"] += 1

        if action.outcome:
            stats[action.outcome.value] += 1

        stats["actions"].append({
            "action_id": action.action_id,
            "url": action.url,
            "outcome": action.outcome.value if action.outcome else None,
            "created_at": action.created_at,
        })

    # Calculate success rate and status for each fingerprint
    for fp_hash, stats in fingerprint_stats.items():
        completed = stats["positive"] + stats["negative"] + stats["neutral"]
        if completed > 0:
            stats["success_rate"] = round(stats["positive"] / completed, 2)
        else:
            stats["success_rate"] = None

        # Determine status per doctrine
        if stats["negative"] >= 3:
            stats["status"] = "FORBIDDEN"
            stats["status_color"] = "red"
        elif stats["negative"] >= 2:
            stats["status"] = "CAUTION"
            stats["status_color"] = "orange"
        elif stats["positive"] >= 2:
            stats["status"] = "PROVEN"
            stats["status_color"] = "green"
        else:
            stats["status"] = "LEARNING"
            stats["status_color"] = "gray"

    return jsonify({
        "fingerprints": list(fingerprint_stats.values()),
        "summary": ledger.summary(),
    })


@app.route("/api/tracking")
def api_tracking():
    """Get tracking integrity data."""
    diag_path = DATA_PATH / "diagnostic_results.json"

    if not diag_path.exists():
        return jsonify({
            "status": "unknown",
            "message": "No diagnostic data. Run workflow first.",
            "tier_a": [],
            "tier_b": [],
        })

    with open(diag_path) as f:
        diag_data = json.load(f)

    tier_a = [d for d in diag_data.get("results", []) if d.get("tier") == "A"]
    tier_b = [d for d in diag_data.get("results", []) if d.get("tier") == "B"]

    # Determine overall status
    if len(tier_a) > 0:
        status = "BLOCKED"
        status_color = "red"
    elif len(tier_b) > 3:
        status = "WARNING"
        status_color = "orange"
    else:
        status = "HEALTHY"
        status_color = "green"

    return jsonify({
        "status": status,
        "status_color": status_color,
        "tier_a": tier_a,
        "tier_b": tier_b,
        "tier_a_count": len(tier_a),
        "tier_b_count": len(tier_b),
        "last_check": diag_data.get("timestamp", "Unknown"),
    })


@app.route("/api/ads")
def api_ads():
    """Get Google Ads campaign and monetization data."""
    config = load_config()
    ads_config = config.get("data_sources", {}).get("google_ads", {})

    if not ads_config.get("customer_id"):
        return jsonify({
            "connected": False,
            "message": "Google Ads not configured",
            "campaigns": [],
            "summary": {},
        })

    # Resolve credentials path relative to project root
    creds_path = ads_config.get("config_path", "google-ads.yaml")
    if not Path(creds_path).is_absolute():
        project_root = Path(__file__).parent.parent
        creds_path = str(project_root / creds_path)

    # Debug: Check if file exists
    if not Path(creds_path).exists():
        return jsonify({
            "connected": False,
            "message": f"Credentials file not found: {creds_path}",
            "campaigns": [],
            "summary": {},
        })

    try:
        client = GoogleAdsClient(
            credentials_path=creds_path,
            customer_id=ads_config.get("customer_id"),
            brand_terms=ads_config.get("brand_terms", []),
        )

        # Fetch campaign data
        campaigns = client.fetch_campaign_summary(days=28)

        # Debug: Log if no campaigns returned
        if not campaigns:
            error_msg = client._last_error or "No error captured"
            return jsonify({
                "connected": True,
                "message": f"No campaigns returned. Error: {error_msg}",
                "campaigns": [],
                "summary": {"debug_path": creds_path, "error": error_msg},
            })

        # Build response
        campaign_list = []
        total_impressions = 0
        total_clicks = 0
        total_cost = 0
        total_conversions = 0
        total_value = 0

        for c in campaigns.values():
            campaign_list.append({
                "name": c.campaign_name,
                "type": c.campaign_type.value,
                "status": c.status,
                "impressions": c.impressions,
                "clicks": c.clicks,
                "cost": round(c.cost, 2),
                "conversions": round(c.conversions, 1),
                "conversion_value": round(c.conversion_value, 2),
                "roas": round(c.conversion_value / c.cost, 2) if c.cost > 0 else 0,
                "impression_share": c.search_impression_share,
            })
            total_impressions += c.impressions
            total_clicks += c.clicks
            total_cost += c.cost
            total_conversions += c.conversions
            total_value += c.conversion_value

        # Sort by impressions
        campaign_list.sort(key=lambda x: x["impressions"], reverse=True)

        return jsonify({
            "connected": True,
            "campaigns": campaign_list,
            "summary": {
                "total_campaigns": len(campaigns),
                "active_campaigns": len([c for c in campaign_list if c["impressions"] > 0]),
                "total_impressions": total_impressions,
                "total_clicks": total_clicks,
                "total_cost": round(total_cost, 2),
                "total_conversions": round(total_conversions, 1),
                "total_value": round(total_value, 2),
                "roas": round(total_value / total_cost, 2) if total_cost > 0 else 0,
            },
        })

    except Exception as e:
        return jsonify({
            "connected": False,
            "message": f"Error: {str(e)}",
            "campaigns": [],
            "summary": {},
        })


@app.route("/api/admin/config")
def api_admin_config():
    """Get current configuration."""
    config = load_config()
    return jsonify(config)


@app.route("/api/admin/config", methods=["POST"])
def api_admin_save_config():
    """Save configuration changes."""
    updates = request.json
    config = load_config()

    # Deep merge updates into config
    for section, values in updates.items():
        if section not in config:
            config[section] = {}
        if isinstance(values, dict):
            config[section].update(values)
        else:
            config[section] = values

    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)
        f.write('\n')

    return jsonify({"success": True, "config": config})


@app.route("/api/admin/ai-log")
def api_admin_ai_log():
    """Get AI call reproducibility log."""
    log_path = DATA_PATH / "ai_call_log.jsonl"
    if not log_path.exists():
        return jsonify({"calls": []})

    calls = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                calls.append(json.loads(line))

    # Most recent first
    calls.reverse()
    return jsonify({"calls": calls})


@app.route("/api/page-metadata", methods=["POST"])
def api_page_metadata():
    """Live-fetch page metadata for a given URL."""
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400

    from src.crawlers.simple_crawler import HTMLMetaParser
    import httpx
    from urllib.parse import urljoin

    try:
        with httpx.Client(
            headers={"User-Agent": "AlphabetTrains-SEO-Crawler/1.0"},
            follow_redirects=True,
            timeout=8.0,
        ) as client:
            response = client.get(url)

        if response.status_code == 200:
            parser = HTMLMetaParser(base_url=url)
            try:
                parser.feed(response.text)
            except Exception:
                pass

            canonical = parser.canonical_url
            if canonical and not canonical.startswith(("http://", "https://")):
                canonical = urljoin(url, canonical)

            metadata = {
                "title": parser.title.strip(),
                "h1": parser.h1.strip(),
                "meta_description": parser.meta_description.strip(),
                "canonical_url": canonical,
                "word_count": parser.get_word_count(),
                "content_preview": parser.get_content_preview(200),
                "robots_meta": parser.meta_robots.strip(),
                "above_fold_html": parser.get_above_fold_html(),
                "internal_outlinks": parser.get_internal_outlinks(),
                "has_crawl_data": True,
            }

            # Persist to evaluation file so data survives page refresh
            _persist_opportunity_update(url, {"page_metadata": metadata})

            return jsonify(metadata)
        else:
            return jsonify({"error": f"HTTP {response.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)[:100]}), 502


@app.route("/api/run-evaluation", methods=["POST"])
def api_run_evaluation():
    """Trigger a new evaluation run with optional crawling."""
    global job_state

    if job_state["running"]:
        return jsonify({"error": "A job is already running", "status": "busy"}), 400

    data = request.json or {}
    run_crawl = data.get("crawl", False)

    def run_workflow():
        global job_state
        import traceback
        try:
            job_state["running"] = True
            job_state["type"] = "evaluation"
            job_state["progress"] = 0
            job_state["total"] = 0
            job_state["message"] = "Starting evaluation..."
            job_state["error"] = None

            from src.workflows.full_evaluation import FullEvaluationWorkflow, WorkflowConfig
            from src.output.decision_formatter import OutputFormat

            # Load config
            job_state["message"] = "Loading config..."
            if CONFIG_PATH.exists():
                config = WorkflowConfig.from_json(CONFIG_PATH)
                job_state["message"] = f"Config loaded: GSC={config.gsc_property}, GA4={config.ga4_property_id}"
            else:
                config = WorkflowConfig(
                    gsc_property="sc-domain:example.com",
                    ga4_property_id="123456789",
                    credentials_path="credentials.json",
                )

            config.block_on_tier_a = False

            job_state["message"] = "Initializing workflow..."
            workflow = FullEvaluationWorkflow(config)

            # Step 1: Load data
            job_state["message"] = f"Loading data from GSC ({config.gsc_property}) and GA4 ({config.ga4_property_id})..."
            workflow.load_data(days=28)
            job_state["total"] = len(workflow._assets)
            job_state["message"] = f"Loaded {len(workflow._assets)} pages"

            # Step 2: Crawl if requested
            if run_crawl and workflow._assets:
                job_state["message"] = "Crawling pages for canonical data..."
                job_state["progress"] = 0

                from src.crawlers.simple_crawler import SimpleCrawler, CrawlResult, HTMLMetaParser

                crawler = SimpleCrawler(timeout=5.0, max_concurrent=50)
                urls = [asset.url for asset in workflow._assets]

                # Fast crawl with progress tracking
                import asyncio
                import httpx
                from urllib.parse import urljoin

                async def crawl_with_progress():
                    global job_state
                    semaphore = asyncio.Semaphore(50)
                    completed = 0

                    async def fetch_one(client, url):
                        nonlocal completed
                        async with semaphore:
                            try:
                                response = await client.get(url, timeout=5.0)

                                if response.status_code == 200:
                                    parser = HTMLMetaParser(base_url=url)
                                    try:
                                        parser.feed(response.text)
                                    except:
                                        pass

                                    canonical = parser.canonical_url
                                    if canonical and not canonical.startswith(("http://", "https://")):
                                        canonical = urljoin(url, canonical)

                                    result = CrawlResult(
                                        url=url,
                                        status_code=response.status_code,
                                        canonical_url=canonical,
                                        indexable=parser.is_indexable,
                                        title=parser.title.strip(),
                                        h1=parser.h1.strip(),
                                        word_count=parser.get_word_count(),
                                        meta_description=parser.meta_description.strip(),
                                        content_preview=parser.get_content_preview(200),
                                        above_fold_html=parser.get_above_fold_html(),
                                    )
                                else:
                                    result = CrawlResult(
                                        url=url,
                                        status_code=response.status_code,
                                        canonical_url=None,
                                        indexable=True,
                                        title="",
                                        h1="",
                                        word_count=0,
                                        error=f"HTTP {response.status_code}",
                                    )
                            except Exception as e:
                                result = CrawlResult(
                                    url=url,
                                    status_code=0,
                                    canonical_url=None,
                                    indexable=True,
                                    title="",
                                    h1="",
                                    word_count=0,
                                    error=str(e)[:50],
                                )

                            completed += 1
                            job_state["progress"] = completed
                            if completed % 20 == 0 or completed == len(urls):
                                job_state["message"] = f"Crawling... {completed}/{len(urls)}"
                            return result

                    async with httpx.AsyncClient(
                        headers={"User-Agent": "AlphabetTrains-SEO-Crawler/1.0"},
                        follow_redirects=True,
                        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                    ) as client:
                        tasks = [fetch_one(client, url) for url in urls]
                        results = await asyncio.gather(*tasks)

                    # Store results
                    for result in results:
                        if result and result.error is None:
                            crawler._results[result.url.lower().rstrip('/')] = result

                    return results

                asyncio.run(crawl_with_progress())

                # Enrich assets
                total, enriched = crawler.enrich_assets(workflow._assets)
                job_state["message"] = f"Enriched {enriched}/{total} pages with crawl data"
                print(f"  Crawl: {len(crawler._results)} results cached, enriched {enriched}/{total} assets")
                if enriched == 0 and len(crawler._results) > 0:
                    # Debug: show first asset URL vs first result URL
                    sample_asset = workflow._assets[0].url.lower().rstrip('/') if workflow._assets else "(none)"
                    sample_result = list(crawler._results.keys())[0] if crawler._results else "(none)"
                    print(f"  URL mismatch? Asset: {sample_asset}")
                    print(f"                Result: {sample_result}")

            # Step 3: Run diagnostics
            job_state["message"] = "Running diagnostics..."
            if config.run_diagnostics:
                workflow.run_diagnostics()

            # Step 4: Build link graph
            job_state["message"] = "Building link graph..."
            workflow.build_link_graph()

            # Step 5: Run evaluations
            job_state["message"] = "Running evaluations..."
            workflow.evaluate_all()

            # Step 6: Save results
            job_state["message"] = "Saving results..."
            workflow.save_results_for_dashboard()

            job_state["message"] = "Evaluation complete!"
            job_state["progress"] = job_state["total"]

        except Exception as e:
            tb = traceback.format_exc()
            job_state["error"] = f"{e}\n\nTraceback:\n{tb}"
            job_state["message"] = f"Error: {e}"
            print(f"Evaluation error: {e}\n{tb}")  # Also log to Flask console
        finally:
            job_state["running"] = False

    # Run in background thread
    thread = threading.Thread(target=run_workflow)
    thread.start()

    return jsonify({
        "message": "Evaluation started",
        "status": "running",
        "crawl": run_crawl,
    })


@app.route("/api/job-status")
def api_job_status():
    """Get current job status."""
    return jsonify(job_state)


@app.route("/api/debug")
def api_debug():
    """Debug endpoint to verify paths and data."""
    eval_path = DATA_PATH / "latest_evaluation.json"
    diag_path = DATA_PATH / "diagnostic_results.json"

    eval_exists = eval_path.exists()
    diag_exists = diag_path.exists()

    eval_data = None
    if eval_exists:
        with open(eval_path) as f:
            eval_data = json.load(f)

    return jsonify({
        "data_path": str(DATA_PATH),
        "data_path_exists": DATA_PATH.exists(),
        "eval_file_exists": eval_exists,
        "eval_file_path": str(eval_path),
        "diag_file_exists": diag_exists,
        "eval_data_keys": list(eval_data.keys()) if eval_data else None,
        "eval_total_pages": eval_data.get("total_pages") if eval_data else None,
        "eval_timestamp": eval_data.get("timestamp") if eval_data else None,
    })


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":
    app.run(debug=True, port=8080, host="127.0.0.1")
