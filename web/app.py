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
    max_tokens = ai_config.get("max_tokens", 1500)
    timeout_sec = ai_config.get("timeout", 30)
    max_queries = ai_config.get("max_queries_in_prompt", 0)
    max_issues = ai_config.get("max_issues_in_prompt", 0)
    min_context = ai_config.get("min_context_fields", 3)

    # ── Use cached page metadata from evaluation pipeline ────────
    # No live page fetch — metadata was collected during crawl step
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
                "recommendation": "NO_ACTION",
                "problem_statement": f"System confidence below the {exploration_threshold} minimum threshold for any action lane.",
                "rationale": (
                    f"The system-calculated confidence is {confidence:.2f}, "
                    f"which is below the {exploration_threshold} minimum required for EXPLORATION actions "
                    f"(the lowest confidence lane). PRESERVATION actions require ≥ {preservation_threshold}. "
                    f"At this confidence level, the risk of a bad recommendation outweighs "
                    f"the expected value of ${opportunity.get('expected_value', 0):.2f}."
                ),
                "action": {"type": None, "surface": None, "instruction": None, "guardrails": {"must_not_change": "N/A", "must_preserve": "N/A"}},
                "expected_impact": "None — no action proposed.",
                "measurement": {"primary_metric": "N/A", "evaluation_window": "N/A", "abort_conditions": "N/A"},
                "risk_notes": "Inaction is the safest path when confidence is insufficient.",
            },
        })

    # ── Required context validation ──────────────────────────────
    # Only block if truly critical context is missing (URL, asset type,
    # performance data). Crawl metadata (title/H1) is nice-to-have
    # but the AI can still make constraint-based recommendations without it.
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

    # Block if too many critical items are missing
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

    # ── Build structured prompt per AI Recommendation Subsystem ──
    asset_type = opportunity.get("asset_type", "other").upper()

    # Determine operating mode from the opportunity data
    mode = opportunity.get("mode", "")
    if not mode:
        # Infer from recommended action and asset type
        if asset_type == "BLOG":
            mode = "FUNNEL_ALIGNMENT"
        elif opportunity.get("recommended_action") in ("PAGE_REINVESTMENT",):
            mode = "PRESERVATION"
        else:
            mode = "OPPORTUNITY_DISCOVERY"

    # ── 1) Constraint evidence ──────────────────────────────────
    constraint_evidence_lines = []
    if opportunity.get("constraints"):
        for c in opportunity.get("constraints", []):
            evidence_str = ""
            if c.get("evidence"):
                evidence_str = f" | Evidence: {json.dumps(c['evidence'])}"
            constraint_evidence_lines.append(
                f"- [{c.get('severity', 'medium')}] {c.get('constraint_type', 'unknown')}: "
                f"{c.get('description', '')}{evidence_str}"
            )
    constraint_evidence = "\n".join(constraint_evidence_lines) if constraint_evidence_lines else "No constraint evidence available."

    # ── 2) GSC summary ──────────────────────────────────────────
    gsc_lines = []
    if opportunity.get("top_queries"):
        queries = opportunity.get("top_queries", [])
        for q in (queries[:max_queries] if max_queries else queries):
            gsc_lines.append(
                f"  \"{q.get('query')}\" → pos: {q.get('position')}, "
                f"impr: {q.get('impressions')}, clicks: {q.get('clicks', 'N/A')}, "
                f"ctr: {q.get('ctr', 'N/A')}%"
            )
    gsc_summary = "\n".join(gsc_lines) if gsc_lines else "No GSC query data available."

    # ── 3) GA4 summary ──────────────────────────────────────────
    ga4_parts = []
    if opportunity.get("demand_score") is not None:
        ga4_parts.append(f"Demand Score: {opportunity.get('demand_score')}")
    if opportunity.get("intent_score") is not None:
        ga4_parts.append(f"Intent Score: {opportunity.get('intent_score')}")
    if opportunity.get("visibility_score") is not None:
        ga4_parts.append(f"Visibility Score: {opportunity.get('visibility_score')}")
    ga4_summary = ", ".join(ga4_parts) if ga4_parts else "No GA4 performance data available."

    # ── 4) Internal link summary ────────────────────────────────
    link_parts = []
    if opportunity.get("routing_data"):
        rd = opportunity["routing_data"]
        link_parts.append(f"Routing quality: {rd.get('routing_quality', 'unknown')}")
        if rd.get("current_routing_paths"):
            link_parts.append(f"Current routing paths: {', '.join(rd['current_routing_paths'])}")
        if rd.get("recommended_destinations"):
            link_parts.append(f"Recommended destinations: {', '.join(rd['recommended_destinations'])}")
        if rd.get("destination_rationale"):
            link_parts.append(f"Destination rationale: {rd['destination_rationale']}")
        if rd.get("links_to_remove"):
            link_parts.append(f"Links to remove: {', '.join(rd['links_to_remove'])}")
    internal_link_summary = "\n".join(link_parts) if link_parts else "No internal link data available."

    # ── 5) Technical summary ────────────────────────────────────
    tech_parts = []
    tech_parts.append(f"Title: {cached_title or '[Missing]'}")
    tech_parts.append(f"Meta Description: {cached_meta or '[Missing]'}")
    tech_parts.append(f"Canonical: {cached_canonical or '[Not found]'}")
    tech_parts.append(f"H1: {cached_h1 or '[None found]'}")
    tech_parts.append(f"Word Count: {cached_word_count}")
    if cached_content_preview:
        tech_parts.append(f"Above-the-fold content (first ~200 words): {cached_content_preview}")
    if opportunity.get("issues"):
        issues_list = opportunity["issues"]
        for issue in (issues_list[:max_issues] if max_issues else issues_list):
            tech_parts.append(f"Issue [{issue.get('severity')}]: {issue.get('type')} — {issue.get('description')}")
    technical_summary = "\n".join(tech_parts)

    # ── 6) Evaluator findings ───────────────────────────────────
    evaluator_lines = []
    evaluator_lines.append(f"Recommended Action: {opportunity.get('recommended_action', 'Unknown')}")
    if opportunity.get("capture_class"):
        evaluator_lines.append(f"Capture Class: {opportunity['capture_class']}")
    if opportunity.get("implementation_steps"):
        evaluator_lines.append("Implementation Steps:")
        for step in opportunity["implementation_steps"]:
            evaluator_lines.append(f"  - {step}")
    if opportunity.get("rollback_plan"):
        evaluator_lines.append(f"Rollback Plan: {opportunity['rollback_plan']}")
    evaluator_findings = "\n".join(evaluator_lines)

    # ── 7) Build the full structured prompt ─────────────────────
    prompt = f"""You are the Alphabet Trains Agentic Growth Governor.

Your job is to evaluate pages and propose actions that increase long-term organic revenue
while preserving measurement integrity and avoiding irreversible harm.

You are NOT an SEO assistant.
You are NOT a traffic maximizer.
You are an economic decision system with controlled exploration.

────────────────────────
CORE DOCTRINE (NON-NEGOTIABLE)
────────────────────────

1) Measurement integrity precedes action.
If data is unreliable or context is insufficient, return NO_ACTION with an explicit reason.

2) Inaction is a valid and common outcome.
Most pages should result in NO_ACTION unless there is clear, justified upside.

3) Reversibility governs risk tolerance.
Additive and reversible actions may be taken at lower confidence than destructive actions.

4) Growth and preservation are separate lanes.
Do not block growth by applying preservation thresholds universally.

────────────────────────
ASSET CLASSIFICATION (MANDATORY)
────────────────────────

Each page must be classified as exactly one:
- PRODUCT (transactional)
- CATEGORY (commercial hub)
- BLOG / GUIDE (informational bridge)
- OTHER (utility, policy, etc.)

All valuation and allowed actions depend on asset type.

────────────────────────
REQUIRED CONTEXT (HARD GATE)
────────────────────────

Before proposing ANY page-level action, you must have:

- URL
- Asset type
- H1
- Meta title (current)
- Meta description (current)
- Canonical URL
- Above-the-fold content (or text equivalent)
- Primary internal links above the fold
- GSC impressions, CTR, avg position (28 days)

If any required field is missing:
→ Return NO_ACTION
→ Reason: Insufficient context for safe evaluation

────────────────────────
INPUT CONTEXT
────────────────────────
URL: {url}
Asset Type: {asset_type}  (PRODUCT | CATEGORY | BLOG | OTHER)
Operating Mode: {mode}  (PRESERVATION | OPPORTUNITY_DISCOVERY | FUNNEL_ALIGNMENT)

Primary Constraint: {opportunity.get('primary_constraint', 'none')}
Constraint Evidence:
{constraint_evidence}

Expected Value: ${opportunity.get('expected_value', 0):.2f}
Confidence Score: {opportunity.get('confidence', 0)}
Risk Level: {opportunity.get('risk_level', 'unknown')}

Key Signals:
- GSC Summary:
{gsc_summary}
- GA4 Summary: {ga4_summary}
- Internal Link Summary:
{internal_link_summary}
- Technical Summary:
{technical_summary}

Evaluator Findings:
{evaluator_findings}

────────────────────────
VALUE MODELS
────────────────────────

PRODUCT PAGES — Value source: Direct revenue (RAIP-based).
Traffic increases confidence only — never overrides negative RAIP.

CATEGORY PAGES — Value source: Aggregated revenue + demand capture.
Traffic relevant only when aligned with commercial intent.

BLOG / GUIDE PAGES — Blogs are routing infrastructure, not revenue assets.
Value source: Assist Value (AV) ONLY.
Rules:
- High traffic without routing = LOW value
- Blogs must not be valued on sessions alone
- Weak routing indicates opportunity, not automatic rejection

────────────────────────
ACTION LANES
────────────────────────

1) PRESERVATION (Exploit)
- Irreversible or high-risk
- Requires high confidence (≥ 0.75)
- Examples: Canonical changes, indexing/noindex, removing content, URL changes

2) EXPLORATION (Growth)
- Additive and reversible
- Allowed at lower confidence (≥ 0.55)
- Does NOT consume regret budget
- Examples: New internal links, blog ideas, blog outlines, new content blocks,
  title/meta tests (non-destructive)

────────────────────────
ALLOWED ACTIONS BY ASSET TYPE
────────────────────────

PRODUCT PAGES — MAY: Propose meta title/description changes, improve schema,
suggest internal links INTO the product, improve clarity or trust signals.
MUST NOT: Suggest informational expansion or blog-style content.

CATEGORY PAGES — MAY: Propose title/H1 alignment, improve intro content,
suggest internal links from blogs, fix visibility/indexing issues.

BLOG / GUIDE PAGES — MAY:
- Propose internal link routing changes
- Propose title/meta tests IF impressions ≥ threshold, CTR suppressed for position,
  and intent remains informational
- Propose visibility/indexing fixes
- Propose new blog ideas or outlines that target adjacent high-intent demand
  and explicitly funnel to a category or product
MUST NOT: Optimize for traffic alone, suggest conversion copy,
suggest unrelated products, expand topical breadth without funnel logic.

────────────────────────
INTERNAL LINKING GOVERNANCE
────────────────────────
All internal linking suggestions must answer:
"Does this narrow the funnel toward the correct revenue asset?"

Rules:
- Prefer CATEGORY links for broad intent
- Prefer PRODUCT links for specific use cases
- Limit primary destinations (max 1–2)
- Avoid linking to low-converting or irrelevant assets
- A blog with traffic but no clear downstream destination should trigger
  routing improvement suggestions, NOT automatic NO_ACTION

────────────────────────
META / TITLE SUGGESTIONS (CONSTRAINED)
────────────────────────
When proposing meta/title changes:
- Reference the existing H1 and above-the-fold content
- Explain why current version misaligns with intent or CTR
- Avoid generic CTAs unless already present
- Provide exact proposed text AND rationale
If this cannot be done precisely → Return NO_ACTION

────────────────────────
OUTPUT REQUIREMENTS
────────────────────────

For every recommendation, output:
- Action type (Exploration / Preservation)
- Confidence score
- Expected upside (revenue or assist-based)
- Why this page, why now
- Exact implementation details
- Rollback / safety note

Generic SEO advice is forbidden.

────────────────────────
DEFAULT POSTURE
────────────────────────
If upside is unclear, confidence is low, or constraints conflict → NO_ACTION.
Explain why.

────────────────────────
OUTPUT FORMAT
────────────────────────
Respond ONLY with valid JSON matching this exact schema (no markdown, no commentary):
{{{{
  "recommendation": "<NO_ACTION | PAGE_REINVESTMENT | INTERNAL_LINK_REALLOCATION | TITLE_META_TEST | NEW_PAGE_CREATION | OBSERVE_ONLY>",
  "problem_statement": "<≤25 words describing the constraint in plain language>",
  "rationale": "<2–4 sentences explaining why this action is better than inaction, referencing expected value, confidence, and risk>",
  "action": {{{{
    "type": "<EXPLORATION | PRESERVATION>",
    "surface": "<title | meta | internal_links | content | structure | technical | canonical | navigation | null>",
    "instruction": "<precise, implementation-ready directive or null if NO_ACTION>",
    "guardrails": {{{{
      "must_not_change": "<what must NOT be changed>",
      "must_preserve": "<what must be preserved>"
    }}}}
  }}}},
  "expected_impact": "<primary metric change and downstream business effect>",
  "measurement": {{{{
    "primary_metric": "<the metric to watch>",
    "evaluation_window": "<time period>",
    "abort_conditions": "<when to rollback>"
  }}}},
  "risk_notes": "<explicit downside risks and why they are acceptable>"
}}}}"""

    # ── Reproducibility: hash the prompt ──────────────────────
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    system_message = (
        "You are the Alphabet Trains Agentic Growth Governor — "
        "an economic decision system with controlled exploration. "
        f"This page is classified as {asset_type}. Operating mode: {mode}. "
        "You evaluate pages and propose actions that increase long-term organic revenue "
        "while preserving measurement integrity. "
        "Inaction is valid and common — NO_ACTION unless there is clear, justified upside. "
        "EXPLORATION actions (additive, reversible) allowed at confidence ≥ 0.55. "
        "PRESERVATION actions (irreversible) require confidence ≥ 0.75. "
        "BLOG pages may receive title/meta tests if CTR is suppressed, "
        "internal link routing changes, visibility fixes, and new blog ideas with funnel logic. "
        "Generic SEO advice is forbidden. "
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

        return jsonify({
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
        })

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
                                    parser = HTMLMetaParser()
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
