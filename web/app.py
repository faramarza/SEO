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

    from src.ledger.action_ledger import ActionFingerprint

    # Create fingerprint from opportunity data
    fingerprint = ActionFingerprint(
        page_type=data.get("page_type", "unknown"),
        intent_cluster=data.get("intent_cluster", "unknown"),
        action_surface=data.get("action_surface", "other"),
        action_type=data.get("action", "OBSERVE_ONLY"),
    )

    # Create proposed action
    action_id = ledger.create_action(
        url=data.get("url", ""),
        action_type=data.get("action", "OBSERVE_ONLY"),
        score_type=data.get("score_type", "EV"),
        score_value=data.get("expected_value", 0),
        confidence=data.get("confidence", 0.5),
        fingerprint=fingerprint,
        metadata={
            "mode": data.get("mode"),
            "risk_level": data.get("risk_level"),
            "implementation_steps": data.get("implementation_steps", []),
            "source": data.get("source", "manual"),
        },
    )

    return jsonify({
        "success": True,
        "action_id": action_id,
        "message": f"Action {action_id} created and added to Task Board",
    })


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
            if CONFIG_PATH.exists():
                config = WorkflowConfig.from_json(CONFIG_PATH)
            else:
                config = WorkflowConfig(
                    gsc_property="sc-domain:example.com",
                    ga4_property_id="123456789",
                    credentials_path="credentials.json",
                )

            config.block_on_tier_a = False

            workflow = FullEvaluationWorkflow(config)

            # Step 1: Load data
            job_state["message"] = "Loading data from GSC/GA4..."
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
            job_state["error"] = str(e)
            job_state["message"] = f"Error: {e}"
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
