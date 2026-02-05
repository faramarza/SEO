"""
Agentic Organic Growth Governor — Web Dashboard

Flask application providing operator interface per doctrine:
- Dashboard with organic KPIs and governor posture
- Ranked Opportunity Queue
- Task Board (Proposed → Approved → Implemented → Measured → Closed)
- Learning & Memory view
- Tracking Integrity view
"""

import json
import threading
from datetime import datetime
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

    # Filter to actionable items and sort by priority
    opportunities = [
        r for r in eval_data.get("results", [])
        if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
    ]

    opportunities.sort(key=lambda x: x.get("priority_score", 0), reverse=True)

    return jsonify({
        "opportunities": opportunities[:50],  # Top 50
        "total": len(opportunities),
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
        tasks_by_status[status].append({
            "action_id": action.action_id,
            "url": action.url,
            "action_type": action.action_type,
            "score_type": action.score_type,
            "score_value": round(action.score_value, 2),
            "confidence": round(action.confidence, 2),
            "created_at": action.created_at,
            "outcome": action.outcome.value if action.outcome else None,
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

    # Create action record
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
    """Get AI-powered SEO recommendations for a page."""
    import os
    import httpx

    data = request.json
    url = data.get("url")
    opportunity = data.get("opportunity", {})

    if not url:
        return jsonify({"error": "URL required"}), 400

    config = load_config()
    ai_config = config.get("ai", {})

    # Get API key from environment or config
    api_key = os.environ.get(ai_config.get("api_key_env", "OPENAI_API_KEY"))
    if not api_key:
        return jsonify({"error": "OpenAI API key not configured. Set OPENAI_API_KEY environment variable."}), 400

    model = ai_config.get("model", "gpt-4o")

    # Fetch page content
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.get(url)
            html = response.text
    except Exception as e:
        return jsonify({"error": f"Failed to fetch page: {e}"}), 400

    # Parse HTML for SEO elements with full context extraction
    from html.parser import HTMLParser
    import re as re_module

    class SEOParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ""
            self.meta_description = ""
            self.canonical_url = ""
            self.h1s = []
            self.h2s = []
            self.in_title = False
            self.in_h1 = False
            self.in_h2 = False
            self.in_p = False
            self.in_a = False
            self.above_fold = ""
            self.char_count = 0
            self.paragraphs = []  # Intro paragraphs
            self.current_paragraph = ""
            self.internal_links = []  # Above-fold internal links
            self.current_link_href = ""
            self.current_link_text = ""
            self.cta_texts = []  # CTA-like elements above the fold
            self.first_heading = ""  # First visible heading

        def handle_starttag(self, tag, attrs):
            attrs_dict = dict(attrs)
            if tag == "title":
                self.in_title = True
            elif tag == "h1":
                self.in_h1 = True
            elif tag == "h2":
                self.in_h2 = True
            elif tag == "p" and self.char_count < 3000:
                self.in_p = True
                self.current_paragraph = ""
            elif tag == "meta" and attrs_dict.get("name", "").lower() == "description":
                self.meta_description = attrs_dict.get("content", "")
            elif tag == "link" and attrs_dict.get("rel", "").lower() == "canonical":
                self.canonical_url = attrs_dict.get("href", "")
            elif tag == "a" and self.char_count < 3000:
                href = attrs_dict.get("href", "")
                self.in_a = True
                self.current_link_href = href
                self.current_link_text = ""
                # Detect CTA-like buttons/links
                css_class = attrs_dict.get("class", "").lower()
                if any(w in css_class for w in ["btn", "cta", "button", "action"]):
                    self.cta_texts.append({"href": href, "text": "", "class": css_class})

        def handle_endtag(self, tag):
            if tag == "title":
                self.in_title = False
            elif tag == "h1":
                self.in_h1 = False
            elif tag == "h2":
                self.in_h2 = False
            elif tag == "p":
                if self.in_p and self.current_paragraph.strip():
                    self.paragraphs.append(self.current_paragraph.strip())
                self.in_p = False
            elif tag == "a":
                if self.in_a and self.current_link_href and self.char_count < 3000:
                    # Only capture internal links (relative or same domain)
                    href = self.current_link_href
                    if href.startswith("/") or not href.startswith("http"):
                        self.internal_links.append({
                            "href": href,
                            "text": self.current_link_text.strip(),
                        })
                    # Fill CTA text if this was a CTA
                    if self.cta_texts and self.cta_texts[-1]["href"] == href:
                        self.cta_texts[-1]["text"] = self.current_link_text.strip()
                self.in_a = False

        def handle_data(self, data):
            if self.in_title:
                self.title += data
            elif self.in_h1:
                self.h1s.append(data.strip())
                if not self.first_heading:
                    self.first_heading = data.strip()
            elif self.in_h2:
                self.h2s.append(data.strip())
                if not self.first_heading:
                    self.first_heading = data.strip()

            if self.in_p:
                self.current_paragraph += data
            if self.in_a:
                self.current_link_text += data

            # Capture above-the-fold text content
            if self.char_count < 3000:
                self.above_fold += data
                self.char_count += len(data)

    parser = SEOParser()
    try:
        parser.feed(html)
    except:
        pass

    # ── Required context validation ──────────────────────────────
    # The AI must never propose page-level changes without sufficient context.
    page_context = {
        "has_url": bool(url),
        "has_asset_type": bool(opportunity.get("asset_type")),
        "has_title": bool(parser.title.strip()),
        "has_h1": len(parser.h1s) > 0,
        "has_above_fold": len(parser.above_fold.strip()) > 100,
        "has_performance": bool(
            opportunity.get("top_queries")
            or opportunity.get("demand_score") is not None
        ),
    }

    missing_context = [k for k, v in page_context.items() if not v]

    # If critical context is missing, return NO ACTION
    if len(missing_context) >= 3:
        return jsonify({
            "success": True,
            "url": url,
            "page_analysis": {
                "title": parser.title.strip(),
                "meta_description": parser.meta_description,
                "h1s": parser.h1s[:3],
                "h2s": parser.h2s[:5],
            },
            "recommendations": {
                "no_action": True,
                "reason": "Insufficient page context for safe recommendation.",
                "missing_context": missing_context,
                "title_recommendation": None,
                "meta_description_recommendation": None,
                "heading_recommendations": [],
                "content_suggestions": [],
                "critical_issues": [
                    f"Cannot generate recommendations: missing {', '.join(c.replace('has_', '') for c in missing_context)}"
                ],
                "priority_actions": ["Re-run evaluation or verify page is accessible"],
            },
        })

    # Build prompt for OpenAI with full contextual constraints
    asset_type = opportunity.get("asset_type", "other")

    # ── 1) Performance context ───────────────────────────────────
    constraint_info = ""
    if opportunity.get("primary_constraint"):
        constraint_info = f"\nPrimary Constraint: {opportunity.get('primary_constraint')}"
        if opportunity.get("constraints"):
            for c in opportunity.get("constraints", []):
                constraint_info += f"\n- {c.get('constraint_type')}: {c.get('description')}"

    query_info = ""
    if opportunity.get("top_queries"):
        query_info = "\nTop Search Queries (from GSC):"
        for q in opportunity.get("top_queries", [])[:5]:
            query_info += (
                f"\n- \"{q.get('query')}\" "
                f"(pos: {q.get('position')}, impr: {q.get('impressions')}, "
                f"clicks: {q.get('clicks', 'N/A')}, ctr: {q.get('ctr', 'N/A')}%)"
            )

    performance_info = ""
    if opportunity.get("demand_score") is not None:
        performance_info = f"""
Performance Context:
- Avg Position: {opportunity.get('visibility_score', 'N/A')}
- Demand Score: {opportunity.get('demand_score', 'N/A')}
- Intent Score: {opportunity.get('intent_score', 'N/A')}
- Confidence: {opportunity.get('confidence', 'N/A')}"""

    routing_info = ""
    if opportunity.get("routing_data"):
        rd = opportunity["routing_data"]
        routing_info = "\nRouting Data:"
        if rd.get("current_routing_paths"):
            routing_info += f"\n- Current paths: {', '.join(rd['current_routing_paths'])}"
        if rd.get("recommended_destinations"):
            routing_info += f"\n- Recommended destinations: {', '.join(rd['recommended_destinations'])}"
        if rd.get("destination_rationale"):
            routing_info += f"\n- Rationale: {rd['destination_rationale']}"
        if rd.get("links_to_remove"):
            routing_info += f"\n- Links to remove: {', '.join(rd['links_to_remove'])}"
        routing_info += f"\n- Routing quality: {rd.get('routing_quality', 'unknown')}"

    # ── 2) Enhanced page context ─────────────────────────────────
    intro_paragraphs = parser.paragraphs[:3]
    intro_text = "\n".join(intro_paragraphs) if intro_paragraphs else "[No intro paragraphs captured]"

    above_fold_links = ""
    if parser.internal_links:
        above_fold_links = "\nAbove-the-fold Internal Links:"
        for link in parser.internal_links[:10]:
            above_fold_links += f"\n- [{link['text'] or '(no text)'}] → {link['href']}"

    cta_info = ""
    if parser.cta_texts:
        cta_info = "\nAbove-the-fold CTAs:"
        for cta in parser.cta_texts[:5]:
            cta_info += f"\n- \"{cta['text'] or '(empty)'}\" → {cta['href']}"

    # ── 3) Asset-type-specific suggestion rules ──────────────────
    if asset_type == "product":
        allowed_suggestions = """
ALLOWED suggestions for PRODUCT pages:
- Meta title/description improvements (must reference current H1 and above-fold content)
- Schema improvements
- Internal links INTO the product
- Content clarity and trust signals
- Canonical/indexability fixes

FORBIDDEN suggestions for PRODUCT pages (DO NOT suggest these):
- Broad informational expansion
- Blog-style content additions"""

    elif asset_type == "category":
        allowed_suggestions = """
ALLOWED suggestions for CATEGORY pages:
- Title and H1 alignment with commercial intent
- Intro content that aids selection and conversion
- Internal links from blogs into the category
- Indexability and filtering controls

FORBIDDEN suggestions for CATEGORY pages (DO NOT suggest these):
- Educational blog-style narratives
- Traffic-only keyword expansion"""

    elif asset_type == "blog":
        allowed_suggestions = """
ALLOWED suggestions for BLOG/GUIDE pages (ONLY these):
- Internal linking changes (primary focus)
- "Next step" blocks linking to products/categories
- Re-ordering sections to surface intent earlier
- Adding or removing product/category references
- Clarifying buying considerations (not sales copy)
- Removing irrelevant or misleading links

ABSOLUTELY FORBIDDEN suggestions for BLOG/GUIDE pages (NEVER suggest these):
- New keywords to target
- Traffic-driven meta title changes
- Conversion copy or sales language
- Expanding topical breadth
- "Create more content" recommendations
- ANY suggestion justified by traffic alone

If you cannot provide routing-focused suggestions, return NO ACTION."""
    else:
        allowed_suggestions = ""

    # ── 4) Build the prompt ──────────────────────────────────────
    prompt = f"""You are an expert SEO consultant analyzing a specific page. You must only recommend changes grounded in the actual page context provided below. Generic advice is forbidden.

=== PAGE IDENTITY ===
URL: {url}
Asset Type: {asset_type.upper()}
Recommended Action: {opportunity.get('recommended_action', 'Unknown')}
Expected Value: ${opportunity.get('expected_value', 0):.2f}
{constraint_info}
{performance_info}
{query_info}

=== CURRENT METADATA ===
- Title Tag: {parser.title.strip() or '[Missing]'}
- Meta Description: {parser.meta_description or '[Missing]'}
- Canonical URL: {parser.canonical_url or '[Not found]'}
- H1: {', '.join(parser.h1s[:3]) or '[None found]'}
- H2s: {', '.join(parser.h2s[:5]) or '[None found]'}
- First Visible Heading: {parser.first_heading or '[None]'}

=== CONTENT CONTEXT ===
Intro Paragraphs:
{intro_text[:1000]}
{above_fold_links}
{cta_info}
{routing_info}

Above-the-fold Content Preview:
{parser.above_fold[:1500]}

=== ASSET-TYPE RULES ===
{allowed_suggestions}

=== MANDATORY CONSTRAINTS ===

META & TITLE CONSTRAINTS:
- You MUST explain how any title/description change improves alignment with the CURRENT H1 and above-fold content.
- You MUST NOT introduce new intent unless explicitly justified with evidence from the query data.
- You MUST NOT use generic CTAs ("Learn now", "Discover", "Explore") unless they already appear on the page.
- Every title/description suggestion MUST include a rationale grounded in:
  (a) intent clarity relative to the current H1
  (b) CTR vs position data from the query context
  (c) differentiation from competing SERP snippets
- If these conditions cannot be met, set the recommendation to null.

CONTENT SUGGESTION CONSTRAINTS:
- You MUST NOT suggest adding sections, expanding comparisons, answering FAQs, or targeting new queries
  unless you can explicitly state: (a) what user confusion exists, (b) where it appears in the current page,
  and (c) how the change improves funnel routing or intent clarity.
- If you cannot ground a content suggestion in the page context above, do not include it.

OUTPUT CONSTRAINTS:
- Every recommendation MUST reference the current H1.
- Every recommendation MUST reference above-the-fold content.
- Every recommendation MUST explain alignment or misalignment with current page state.
- Every recommendation MUST state why the current version is insufficient.
- Generic advice is forbidden. If you cannot provide specific, grounded recommendations, return null for that field.

Format your response as a structured JSON with these fields:
{{
  "title_recommendation": {{
    "current": "the exact current title",
    "suggested": "the suggested title or null if no change needed",
    "h1_alignment": "how the suggestion aligns with the current H1",
    "reasoning": "grounded rationale referencing CTR, position, and above-fold content"
  }},
  "meta_description_recommendation": {{
    "current": "the exact current meta description",
    "suggested": "the suggested description or null if no change needed",
    "h1_alignment": "how the suggestion aligns with the current H1",
    "reasoning": "grounded rationale referencing intent and above-fold content"
  }},
  "heading_recommendations": ["each must reference current heading structure"],
  "content_suggestions": ["each must identify specific user confusion and where it occurs on the page"],
  "critical_issues": ["issues with evidence from the page context"],
  "priority_actions": ["actions with explicit grounding in the data above"]
}}"""

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
                    {"role": "system", "content": f"You are an expert SEO consultant. This page is classified as {asset_type.upper()}. You must ONLY recommend changes grounded in the actual page content and data provided. Generic advice is forbidden. Every suggestion must reference the current H1 and above-the-fold content. Strictly follow the allowed/forbidden suggestion rules for this asset type. For blog pages, focus exclusively on routing quality. Always respond with valid JSON. If you cannot provide grounded recommendations for a field, set it to null."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7,
                "max_tokens": 2000,
            },
            timeout=30.0,
        )

        if openai_response.status_code != 200:
            return jsonify({"error": f"OpenAI API error: {openai_response.text}"}), 500

        result = openai_response.json()
        ai_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        # Try to parse as JSON
        try:
            # Remove markdown code blocks if present
            if "```json" in ai_content:
                ai_content = ai_content.split("```json")[1].split("```")[0]
            elif "```" in ai_content:
                ai_content = ai_content.split("```")[1].split("```")[0]

            recommendations = json_module.loads(ai_content)
        except:
            recommendations = {"raw_response": ai_content}

        return jsonify({
            "success": True,
            "url": url,
            "page_analysis": {
                "title": parser.title.strip(),
                "meta_description": parser.meta_description,
                "canonical_url": parser.canonical_url,
                "h1s": parser.h1s[:3],
                "h2s": parser.h2s[:5],
                "first_heading": parser.first_heading,
                "intro_paragraphs": parser.paragraphs[:3],
                "internal_links_count": len(parser.internal_links),
                "cta_count": len(parser.cta_texts),
            },
            "recommendations": recommendations,
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
