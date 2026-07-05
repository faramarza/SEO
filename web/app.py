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
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ledger.action_ledger import ActionLedger, ActionStatus, ActionOutcome, evaluation_window_for
from src.data_sources.gsc_client import GSCClient
from src.data_sources.ga4_client import GA4Client
from src.data_sources.google_ads_client import GoogleAdsClient
from src.data_sources.moz_client import MozClient
from src.data_sources import serp_client
from src.data_sources.crux_client import CrUXClient
from src.data_sources import ai_visibility
from src.data_sources import content_manager
from src.diagnostics.tracking_sanity import TrackingSanityDiagnostics


app = Flask(__name__,
            template_folder='templates',
            static_folder='static')

# Load config
CONFIG_PATH = Path(__file__).parent.parent / "config" / "defaults.json"
DATA_PATH = Path(__file__).parent.parent / "data"

# Global state for background jobs — synced to a JSON file so all gunicorn
# workers can read the current state via /api/job-status.
JOB_STATE_PATH = DATA_PATH / "job_state.json"


class _SyncDict(dict):
    """Dict that auto-syncs to a JSON file on every write.

    Unlike the old proxy class, reads use standard dict (in-memory) so the
    worker running the job always sees its own updates. Only the status
    endpoint reads from the file for cross-worker visibility.
    """

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._sync()

    def update(self, __m=(), **kwargs):
        super().update(__m, **kwargs)
        self._sync()

    def _sync(self):
        try:
            tmp = JOB_STATE_PATH.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(dict(self), f)
            tmp.replace(JOB_STATE_PATH)
        except OSError:
            pass


job_state = _SyncDict(
    running=False,
    type=None,
    progress=0,
    total=0,
    message="",
    error=None,
)


NOTIFICATIONS_PATH = DATA_PATH / "notifications.json"
SCHEDULER_CHECK_INTERVAL = 3600  # 1 hour
SERP_COLLECT_INTERVAL = 3600  # 1 hour (checks quota, collects if available)


def _load_notifications():
    if NOTIFICATIONS_PATH.exists():
        with open(NOTIFICATIONS_PATH) as f:
            return json.load(f)
    return []


def _save_notification(notification):
    """Append a notification and keep the last 100."""
    notes = _load_notifications()
    notes.insert(0, notification)
    notes = notes[:100]
    NOTIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTIFICATIONS_PATH, "w") as f:
        json.dump(notes, f, indent=2)
        f.write("\n")


def _run_scheduled_measurement():
    """Background thread: periodically auto-measure tasks past their evaluation window."""
    while True:
        time.sleep(SCHEDULER_CHECK_INTERVAL)
        try:
            with app.app_context():
                config = load_config()
                ledger = ActionLedger()
                pending = ledger.get_pending_evaluations()
                if not pending:
                    continue

                for action in pending:
                    result = _auto_measure_action(action, config)
                    if result is None:
                        continue

                    outcome_str = result["outcome"]
                    outcome = ActionOutcome(outcome_str)
                    variants = _get_variants(action)
                    has_next_variant = variants and action.active_variant_index < len(variants) - 1

                    short_url = action.url.replace("https://", "").replace("http://", "")

                    if outcome_str in ("negative", "neutral") and has_next_variant:
                        action.variant_outcomes.append({
                            "variant_index": action.active_variant_index,
                            "outcome": outcome_str,
                            "metrics": result.get("metrics"),
                            "notes": result.get("notes", ""),
                        })
                        action.active_variant_index += 1
                        action.baseline_metrics = None
                        action.implemented_at = None
                        ledger.update_action(action)
                        next_v = variants[action.active_variant_index]
                        _save_notification({
                            "type": "variant_advance",
                            "severity": "warning",
                            "action_id": action.action_id,
                            "url": action.url,
                            "message": f"Variant {action.active_variant_index} of '{action.action_type}' on {short_url} didn't improve. Deploy Variant {action.active_variant_index + 1}: {next_v.get('title', '')}",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "read": False,
                        })
                    else:
                        notes = result.get("notes", "")
                        if outcome_str == "positive" and variants:
                            notes = f"Variant {action.active_variant_index + 1} succeeded. " + notes
                        elif variants and not has_next_variant and outcome_str != "positive":
                            notes = f"All {len(variants)} variants tested, none improved. " + notes
                        if variants:
                            action.variant_outcomes.append({
                                "variant_index": action.active_variant_index,
                                "outcome": outcome_str,
                                "metrics": result.get("metrics"),
                                "notes": result.get("notes", ""),
                            })
                            ledger.update_action(action)
                        ledger.record_outcome(
                            action_id=action.action_id,
                            outcome=outcome,
                            outcome_metrics=result.get("metrics"),
                            notes=notes,
                        )
                        severity = "success" if outcome_str == "positive" else "info" if outcome_str == "neutral" else "error"
                        _save_notification({
                            "type": "measurement_complete",
                            "severity": severity,
                            "action_id": action.action_id,
                            "url": action.url,
                            "outcome": outcome_str,
                            "message": f"'{action.action_type}' on {short_url} measured as {outcome_str.upper()}. {notes}",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "read": False,
                        })
        except Exception as e:
            print(f"[Scheduler] Error in auto-measure: {e}")


def _run_serp_collector():
    """Background thread: collect SERP data for top opportunities, respecting daily quota.
    Only runs when SERP_AUTO_COLLECT=true is set in environment.
    """
    if not os.environ.get("SERP_AUTO_COLLECT", "").lower() in ("true", "1", "yes"):
        print("[SERP Collector] Auto-collection disabled. Set SERP_AUTO_COLLECT=true in .env to enable.")
        return
    time.sleep(30)  # Wait for app to be fully ready
    while True:
        try:
            remaining = serp_client.get_remaining_quota()
            if remaining <= 0:
                time.sleep(SERP_COLLECT_INTERVAL)
                continue

            eval_path = DATA_PATH / "latest_evaluation.json"
            if not eval_path.exists():
                time.sleep(SERP_COLLECT_INTERVAL)
                continue

            with open(eval_path) as f:
                eval_data = json.load(f)

            results = eval_data.get("results", [])
            # Sort by expected_value descending — prioritize highest-value pages
            results.sort(key=lambda r: r.get("expected_value", 0), reverse=True)

            queries_fetched = 0
            for opp in results:
                if remaining <= 0:
                    break
                top_queries = opp.get("top_queries", [])
                for q in top_queries[:3]:  # Top 3 queries per opportunity
                    query_text = q.get("query", "")
                    if not query_text:
                        continue
                    # Skip if already cached (< 7 days)
                    cached = serp_client.get_cached_serp(query_text)
                    if cached:
                        continue
                    result = serp_client.fetch_serp(query_text)
                    if result:
                        queries_fetched += 1
                        remaining -= 1
                        time.sleep(0.3)
                    if remaining <= 0:
                        break

            if queries_fetched > 0:
                print(f"[SERP Collector] Fetched {queries_fetched} queries, {remaining} quota remaining today")

        except Exception as e:
            print(f"[SERP Collector] Error: {e}")

        time.sleep(SERP_COLLECT_INTERVAL)


def _start_scheduler():
    """Start the background measurement scheduler and SERP collector."""
    t = threading.Thread(target=_run_scheduled_measurement, daemon=True)
    t.start()
    t2 = threading.Thread(target=_run_serp_collector, daemon=True)
    t2.start()
    print("[Scheduler] Background task monitor + SERP collector started")


_scheduler_started = False


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

    # Also persist AI recommendations to a separate durable file
    # so they survive re-evaluation runs that overwrite latest_evaluation.json
    if "ai_recommendations" in updates:
        _persist_ai_recommendation(url, updates)


def _persist_ai_recommendation(url: str, ai_data: dict):
    """Save AI recommendation to data/ai_recommendations.json (keyed by URL).

    This file is never overwritten by evaluation runs, so recommendations
    persist across re-evaluations.
    """
    recs_path = DATA_PATH / "ai_recommendations.json"
    try:
        if recs_path.exists():
            with open(recs_path) as f:
                all_recs = json.load(f)
        else:
            all_recs = {}

        all_recs[url] = {
            "ai_recommendations": ai_data.get("ai_recommendations"),
            "ai_reproducibility": ai_data.get("ai_reproducibility"),
            "ai_revised_value": ai_data.get("ai_revised_value"),
            "ai_timestamp": ai_data.get("ai_timestamp", datetime.now(timezone.utc).isoformat()),
        }

        DATA_PATH.mkdir(parents=True, exist_ok=True)
        with open(recs_path, "w") as f:
            json.dump(all_recs, f, indent=2)
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


@app.route("/link-map")
def link_map():
    """Internal link map view."""
    return render_template("link_map.html")


@app.route("/growth")
def growth():
    """Growth & AI Visibility view."""
    return render_template("growth.html")


@app.route("/content")
def content_page():
    """Content Strategy view."""
    return render_template("content.html")


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

    # Compute AI-revised total from individual results
    ai_values = [
        r.get("ai_revised_value")
        for r in eval_data.get("results", [])
        if r.get("ai_revised_value") is not None
    ]
    ai_total = round(sum(ai_values), 2) if ai_values else None
    ai_analyzed_count = len(ai_values)
    ai_total_pages = len(eval_data.get("results", []))

    # Task alerts: tasks ready for measurement or approaching window end
    now = datetime.now()
    task_alerts = []
    for action in ledger.get_all_actions():
        if action.status not in (ActionStatus.IMPLEMENTED, ActionStatus.APPROVED):
            continue
        if not action.implemented_at:
            continue
        try:
            impl_dt = datetime.fromisoformat(action.implemented_at)
            window_end = impl_dt.timestamp() + (action.evaluation_window_days * 86400)
            days_remaining = max(0, int((window_end - now.timestamp()) / 86400))
            task_alerts.append({
                "action_id": action.action_id,
                "url": action.url,
                "action_type": action.action_type,
                "days_remaining": days_remaining,
                "evaluation_window_days": action.evaluation_window_days,
                "ready": days_remaining == 0,
            })
        except (ValueError, TypeError):
            continue

    return jsonify({
        "kpis": {
            "total_pages": eval_data.get("total_pages", 0),
            "pages_with_action": eval_data.get("pages_with_action", 0),
            "action_rate": eval_data.get("action_rate", 0),
            "total_expected_value": eval_data.get("total_expected_value", 0),
            "ai_revised_total": ai_total,
            "ai_analyzed_count": ai_analyzed_count,
            "ai_total_pages": ai_total_pages,
        },
        "posture": {
            "mode": posture,
            "color": posture_color,
        },
        "ledger_summary": ledger_summary,
        "task_alerts": task_alerts,
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

    # Deduplicate: normalize URLs to strip pagination/filter params,
    # keeping the entry with the highest priority_score for each canonical URL.
    raw_results = eval_data.get("results", [])
    seen = {}
    for r in raw_results:
        canonical = _normalize_url(r.get("url", ""))
        r["url"] = canonical  # Fix the URL in-place
        existing = seen.get(canonical)
        if existing is None:
            seen[canonical] = r
        else:
            # Keep the one with higher priority; merge impression data
            if r.get("priority_score", 0) > existing.get("priority_score", 0):
                seen[canonical] = r

    all_results = list(seen.values())

    # Re-classify asset types from sitemap_types.json (source of truth).
    # This corrects stale classifications left over from old heuristic runs.
    # Also fix non-page resources (images, media) that were misclassified.
    _MEDIA_EXTS = {
        ".jpeg", ".jpg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
        ".pdf", ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
        ".mp4", ".webm", ".mp3", ".ogg", ".zip", ".gz",
    }
    sitemap_types_path = DATA_PATH / "sitemap_types.json"
    reclassified = 0

    # Fix media files misclassified as pages
    from urllib.parse import urlparse as _urlparse
    for r in all_results:
        url = r.get("url", "")
        ext = Path(_urlparse(url).path).suffix.lower()
        if ext in _MEDIA_EXTS and r.get("asset_type") != "other":
            r["asset_type"] = "other"
            reclassified += 1

    if sitemap_types_path.exists():
        try:
            with open(sitemap_types_path) as f:
                sitemap_types = json.load(f)
            for r in all_results:
                stype = sitemap_types.get(r.get("url", ""))
                if stype:
                    # Safety net: don't let sitemap override /blog/ URLs
                    # to "other" — some sitemaps put blog posts in generic
                    # sub-sitemaps (e.g. sitemap_pages.xml).
                    if stype == "other":
                        from urllib.parse import urlparse as _up
                        _path = _up(r.get("url", "").lower()).path
                        if "/blog" in _path or "/article" in _path:
                            stype = "blog"
                    if r.get("asset_type") != stype:
                        r["asset_type"] = stype
                        reclassified += 1
        except (json.JSONDecodeError, IOError):
            pass

    # Re-parse any ai_recommendations that were stored as raw_response
    # due to a bug where the dedup exception handler destroyed valid JSON
    ai_reparsed = 0
    for r in all_results:
        ai_rec = r.get("ai_recommendations")
        if isinstance(ai_rec, dict) and "raw_response" in ai_rec and len(ai_rec) == 1:
            try:
                raw = ai_rec["raw_response"].strip()
                # Strip markdown fences
                raw_lower = raw.lower()
                if raw_lower.startswith("```json"):
                    raw = raw[7:]
                elif raw.startswith("```"):
                    raw = raw[3:]
                if raw.rstrip().endswith("```"):
                    raw = raw.rstrip()[:-3]
                raw = raw.strip()
                # Find first { if there's a preamble
                if raw and raw[0] != '{':
                    brace_idx = raw.find('{')
                    if brace_idx >= 0:
                        raw = raw[brace_idx:]
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and ("validity_audit" in parsed or "recommendations" in parsed):
                    r["ai_recommendations"] = parsed
                    # Also extract ai_revised_value if it wasn't set
                    if r.get("ai_revised_value") is None:
                        fa = parsed.get("funnel_analysis", {})
                        if isinstance(fa, dict):
                            oe = fa.get("opportunity_estimate", {})
                            if isinstance(oe, dict):
                                base = oe.get("base", {})
                                if isinstance(base, dict) and base.get("total") is not None:
                                    try:
                                        r["ai_revised_value"] = float(base["total"])
                                    except (ValueError, TypeError):
                                        pass
                    ai_reparsed += 1
            except (json.JSONDecodeError, Exception):
                pass  # Genuine parse failure — leave as raw_response

    # Persist cleanup if anything changed
    if len(all_results) < len(raw_results) or reclassified > 0 or ai_reparsed > 0:
        eval_data["results"] = all_results
        with open(eval_path, "w") as f:
            json.dump(eval_data, f, indent=2)
            f.write("\n")

    # Restore AI recommendations from durable storage if they were lost
    # during a re-evaluation run (which overwrites latest_evaluation.json)
    recs_path = DATA_PATH / "ai_recommendations.json"
    if recs_path.exists():
        try:
            with open(recs_path) as f:
                saved_recs = json.load(f)
            ai_restored = 0
            for r in all_results:
                url = r.get("url", "")
                saved = saved_recs.get(url)
                if saved and not r.get("ai_recommendations"):
                    r["ai_recommendations"] = saved.get("ai_recommendations")
                    r["ai_reproducibility"] = saved.get("ai_reproducibility")
                    r["ai_timestamp"] = saved.get("ai_timestamp")
                    if saved.get("ai_revised_value") is not None and r.get("ai_revised_value") is None:
                        r["ai_revised_value"] = saved["ai_revised_value"]
                    ai_restored += 1
            if ai_restored > 0:
                print(f"[AI-PERSIST] Restored {ai_restored} AI recommendations from durable storage")
        except (json.JSONDecodeError, IOError):
            pass

    # Return all evaluated pages, mark active tasks and SERP data availability
    for r in all_results:
        r["has_active_task"] = r.get("url", "") in active_task_urls
        # Check SERP data coverage for this opportunity
        top_q = r.get("top_queries", [])[:5]
        serp_count = sum(1 for q in top_q if serp_client.get_cached_serp(q.get("query", "")))
        r["serp_coverage"] = serp_count
        r["serp_total"] = len(top_q)

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
    """Get task board data.

    Three-column model:
      proposed   — Governor recommendations awaiting human decision
      implemented — Approved & in-flight, waiting for evaluation window
      closed     — Measured (auto or manual) with outcome recorded

    Legacy APPROVED tasks map to implemented, MEASURED to closed.
    """
    ledger = ActionLedger()
    now = datetime.now()

    tasks_by_status = {
        "proposed": [],
        "implemented": [],
        "closed": [],
    }

    # Map old granular statuses into the 3-column model
    column_map = {
        "proposed": "proposed",
        "approved": "implemented",
        "implemented": "implemented",
        "measured": "closed",
        "closed": "closed",
    }

    for action in ledger.get_all_actions():
        column = column_map.get(action.status.value, "proposed")
        rec = action.recommendation_json or {}

        # Calculate days remaining in evaluation window
        days_remaining = None
        if action.implemented_at and action.status in (ActionStatus.IMPLEMENTED, ActionStatus.APPROVED):
            try:
                impl_dt = datetime.fromisoformat(action.implemented_at)
                window_end = impl_dt.timestamp() + (action.evaluation_window_days * 86400)
                days_remaining = max(0, int((window_end - now.timestamp()) / 86400))
            except (ValueError, TypeError):
                pass

        tasks_by_status[column].append({
            "action_id": action.action_id,
            "url": action.url,
            "action_type": action.action_type,
            "score_type": action.score_type,
            "score_value": round(action.score_value, 2),
            "confidence": round(action.confidence, 2),
            "created_at": action.created_at,
            "implemented_at": action.implemented_at,
            "evaluation_window_days": action.evaluation_window_days,
            "days_remaining": days_remaining,
            "outcome": action.outcome.value if action.outcome else None,
            "outcome_metrics": action.outcome_metrics,
            "baseline_metrics": action.baseline_metrics,
            "notes": action.notes,
            "risk_level": rec.get("risk_level", "low"),
            "mode": rec.get("mode"),
            "primary_constraint": rec.get("primary_constraint"),
            "asset_type": rec.get("asset_type"),
            "implementation_steps": rec.get("implementation_steps", []),
            "implementation_summary": rec.get("implementation_summary", ""),
            "demand_score": rec.get("demand_score"),
            # Full opportunity context for modal parity
            "constraints": rec.get("constraints", []),
            "top_queries": rec.get("top_queries", []),
            "page_metadata": rec.get("page_metadata", {}),
            "ai_recommendations": rec.get("ai_recommendations"),
            "ai_reproducibility": rec.get("ai_reproducibility"),
            "issues": rec.get("issues", []),
            "rollback_plan": rec.get("rollback_plan", ""),
            "learning_reference": rec.get("learning_reference", ""),
            "capture_class": rec.get("capture_class", ""),
            "intent_score": rec.get("intent_score"),
            "visibility_score": rec.get("visibility_score"),
            # Variant tracking
            "active_variant_index": action.active_variant_index,
            "variant_outcomes": action.variant_outcomes,
            "variants": _get_variants(action),
            "baseline_captured": action.baseline_metrics is not None,
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
            # Full opportunity context for the task board modal
            "constraints": data.get("constraints", []),
            "top_queries": data.get("top_queries", []),
            "page_metadata": data.get("page_metadata", {}),
            "ai_recommendations": data.get("ai_recommendations"),
            "ai_reproducibility": data.get("ai_reproducibility"),
            "issues": data.get("issues", []),
            "rollback_plan": data.get("rollback_plan", ""),
            "learning_reference": data.get("learning_reference", ""),
            "capture_class": data.get("capture_class", ""),
            "intent_score": data.get("intent_score"),
            "visibility_score": data.get("visibility_score"),
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

    model = ai_config.get("model", "gpt-4o-mini")

    # Resolve the correct API key based on the selected model
    if model.startswith("claude-"):
        api_key_env = "ANTHROPIC_API_KEY"
    else:
        api_key_env = ai_config.get("api_key_env", "OPENAI_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        return jsonify({"error": f"AI API key not configured. Set {api_key_env} environment variable."}), 400
    temperature = ai_config.get("temperature", 0.4)
    max_tokens = ai_config.get("max_tokens", 4500)
    timeout_sec = ai_config.get("timeout", 120)
    max_queries = ai_config.get("max_queries_in_prompt", 0)
    max_issues = ai_config.get("max_issues_in_prompt", 0)
    min_context = ai_config.get("min_context_fields", 3)

    # ── Page metadata from frontend (populated by Fetch Page button) ──
    pm = opportunity.get("page_metadata", {})
    print(f"[AI-DEBUG] page_metadata keys received: {list(pm.keys())}")
    print(f"[AI-DEBUG] body_html in pm: {'body_html' in pm}, length: {len(pm.get('body_html', ''))}")

    # Fallback: if body_html missing from POST (browser cache), read from persisted data
    if not pm.get("body_html"):
        try:
            eval_path = DATA_PATH / "latest_evaluation.json"
            if eval_path.exists():
                import json as _json_fb
                with open(eval_path) as _f:
                    _eval = _json_fb.load(_f)
                for _r in _eval.get("results", []):
                    if _r.get("url") == url:
                        _persisted_pm = _r.get("page_metadata", {})
                        _body = _persisted_pm.get("body_html", "")
                        if _body:
                            pm["body_html"] = _body
                            print(f"[AI-DEBUG] body_html recovered from persisted data: {len(_body)} chars")
                        break
        except Exception:
            pass
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
    # Gate on ACTION confidence (already lane-gated by the evaluator),
    # not data_confidence (raw data quality score).  The evaluator
    # already verified that action_confidence ≥ threshold before
    # recommending an action, so re-gating on the lower data_confidence
    # here would silently block pages that the evaluator approved.
    #
    # When force_analysis=True (batch mode), skip this gate entirely
    # so every page gets an AI estimate regardless of confidence.
    force_analysis = data.get("force_analysis", False)
    confidence = opportunity.get("action_confidence", opportunity.get("confidence", 0))
    if not force_analysis and confidence < exploration_threshold:
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
        "has_page_metadata": bool(
            pm.get("has_crawl_data")
            and (pm.get("title") or pm.get("h1"))
        ),
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
    # Patch stale constraint descriptions with live data when available.
    # The batch evaluation may have run without --crawl, producing "0 outlinks"
    # even when the page has real outlinks (discovered via live Fetch Page).
    live_outlinks = pm.get("internal_outlinks", [])
    live_outlink_count = len(live_outlinks) if live_outlinks else 0

    constraint_list = []
    if opportunity.get("constraints"):
        for c in opportunity.get("constraints", []):
            ctype = c.get("constraint_type", "unknown")
            desc = c.get("description", "")
            severity = c.get("severity", "medium")

            # Override stale weak_funnel_routing when live outlinks exist
            if ctype == "weak_funnel_routing" and live_outlink_count >= 3:
                desc = (
                    f"[OVERRIDDEN BY LIVE DATA] Batch evaluation reported 0 outlinks, "
                    f"but live fetch found {live_outlink_count} content outlinks. "
                    f"Routing may still need improvement but is not absent."
                )
                severity = "low"

            # Enrich vague cannibalization with competing page URLs from evidence
            if ctype == "cannibalization":
                evidence = c.get("evidence", {})
                competing = evidence.get("competing_pages", [])
                query = evidence.get("query", "")
                if competing:
                    pages_str = ", ".join(competing[:3])
                    desc = f"Query '{query}' also targets: {pages_str}"

            constraint_list.append(
                f"- [{severity}] {ctype}: {desc}"
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
    outlinks_str = "\n".join(outlinks_lines) if outlinks_lines else "null (NO CRAWL DATA — run evaluation with crawl to get link data. Do NOT suggest internal links without this data.)"

    # Build explicit blocklist of existing outlink target URLs for dedup enforcement
    existing_outlink_urls = sorted(set(
        ol.get("target_url", "") for ol in outlinks
    )) if outlinks else []
    if existing_outlink_urls:
        outlink_blocklist_section = (
            "\nBLOCKLIST -- the following URLs are ALREADY linked from this page.\n"
            "Do NOT recommend any of these as target_urls. They are NOT new links:\n"
            + "\n".join(f"  - {u}" for u in existing_outlink_urls) + "\n"
            "If the weak_funnel_routing constraint says '0 outlinks', IGNORE that number -- the outlinks above are the real data.\n"
        )
    else:
        outlink_blocklist_section = ""

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
    # Stop words — only true grammatical filler with zero topical signal.
    # Do NOT include product attributes (personalized, custom, wooden, name)
    # or audience terms (kids, toddler) — those carry real SEO meaning.
    _QUERY_STOP_WORDS = {
        "for", "the", "and", "with", "how", "what", "why", "are",
        "can", "from", "that", "this", "your", "our", "all", "has",
        "its", "you", "was", "get", "not", "but", "will", "more",
        "buy", "shop", "free", "shipping", "sale", "price",
        "online", "store", "review", "reviews",
        "best", "top", "new", "usa", "2024", "2025", "2026",
    }

    # Read all crawled pages from evaluation so AI knows what actually exists
    eval_path = DATA_PATH / "latest_evaluation.json"
    site_pages = {"category": [], "product": [], "blog": [], "other": []}
    if eval_path.exists():
        try:
            with open(eval_path) as f:
                eval_data = json.load(f)

            # Collect the target page's query terms for overlap scoring
            target_queries_set = set()
            for q in opportunity.get("top_queries", []):
                for word in q.get("query", "").lower().split():
                    if len(word) > 2 and word not in _QUERY_STOP_WORDS:
                        target_queries_set.add(word)

            # Build reverse link index: which pages already link TO the target?
            # These should NOT be recommended as inbound link sources.
            # Normalize URLs to handle relative paths, www/non-www, http/https.
            from urllib.parse import urlparse, urljoin
            def _norm_for_compare(u, base_url=""):
                """Normalize URL for comparison: resolve relative, strip scheme/www/trailing slash."""
                if not u:
                    return ""
                # Resolve relative URLs against the base
                if not u.startswith(("http://", "https://")):
                    if base_url:
                        u = urljoin(base_url, u)
                    else:
                        u = urljoin("https://alphabet-trains.com/", u)
                parsed = urlparse(u.lower())
                netloc = parsed.netloc.replace("www.", "")
                path = parsed.path.rstrip("/") or "/"
                return f"{netloc}{path}"

            target_norm = _norm_for_compare(url)
            already_links_to_target = set()
            for r in eval_data.get("results", []):
                r_url = r.get("url", "")
                r_pm = r.get("page_metadata", {})
                r_outlinks = r_pm.get("internal_outlinks", [])
                for ol in r_outlinks:
                    ol_target = ol.get("target_url", "")
                    if _norm_for_compare(ol_target, r_url) == target_norm:
                        already_links_to_target.add(r_url)
                        break

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

                # GSC metrics for this page (flat fields in evaluation results)
                impressions = r.get("gsc_impressions", 0) or 0
                clicks = r.get("gsc_clicks", 0) or 0
                avg_pos = r.get("gsc_position", 0) or 0

                # Query overlap: count how many query words this page shares
                # with the target page (topical relevance signal)
                r_queries = r.get("top_queries", [])
                r_query_words = set()
                for rq in r_queries:
                    for word in rq.get("query", "").lower().split():
                        if len(word) > 2 and word not in _QUERY_STOP_WORDS:
                            r_query_words.add(word)
                overlap = len(target_queries_set & r_query_words)

                # Mark if this page already links to the target
                has_inbound = r_url in already_links_to_target

                site_pages[r_type].append({
                    "url": r_url,
                    "title": title_r[:80],
                    "ev": round(ev, 2) if ev else 0,
                    "impressions": impressions,
                    "clicks": clicks,
                    "position": round(avg_pos, 1) if avg_pos else 0,
                    "query_overlap": overlap,
                    "already_links_to_target": has_inbound,
                    "top_queries_summary": ", ".join(
                        rq.get("query", "") for rq in r_queries[:5]
                    ) if r_queries else "",
                })
        except Exception:
            pass

    # Filter out URLs that already appear as outlinks on this page.
    # This prevents the AI from recommending links that already exist,
    # even if it ignores the prompt rules.
    existing_outlink_url_set = set(ol.get("target_url", "") for ol in outlinks)

    # Format site map — compact but informative, with linking signals
    target_pages_str = ""
    inbound_blocklist_urls = []
    for ptype in ("category", "product", "blog"):
        pages = site_pages.get(ptype, [])
        if pages:
            # Remove pages already linked from this page (outbound dedup)
            pages = [p for p in pages if p["url"] not in existing_outlink_url_set]
            # Separate pages that already link to target (inbound dedup)
            already_linking = [p for p in pages if p.get("already_links_to_target")]
            available = [p for p in pages if not p.get("already_links_to_target")]
            inbound_blocklist_urls.extend(p["url"] for p in already_linking)
            # Compute linking_score — overlap is the dominant factor (squared),
            # impressions dampened by log to prevent raw traffic from dominating.
            # Formula: overlap² × (11 - position) × log2(1 + impressions)
            import math
            for p in available:
                pos_factor = max(1, 11 - p["position"]) if p["position"] > 0 else 1
                overlap_sq = p["query_overlap"] ** 2 if p["query_overlap"] > 0 else 0.1
                impr_log = math.log2(1 + p["impressions"]) if p["impressions"] > 0 else 0.1
                p["linking_score"] = overlap_sq * pos_factor * impr_log
            # Sort by linking score descending, show top 30
            available.sort(key=lambda p: p["linking_score"], reverse=True)
            target_pages_str += f"\n{ptype.upper()} pages ({len(available)} available, {len(already_linking)} already link to this page):\n"
            for p in available[:30]:
                queries_tag = f'  queries=[{p["top_queries_summary"]}]' if p.get("top_queries_summary") else ''
                target_pages_str += (
                    f'  - {p["url"]}  [{p["title"]}]  '
                    f'EV=${p["ev"]}  impr={p["impressions"]}  '
                    f'pos={p["position"]}  overlap={p["query_overlap"]}  '
                    f'link_score={p["linking_score"]:.0f}{queries_tag}\n'
                )
            if len(available) > 30:
                target_pages_str += f"  ... and {len(available) - 30} more\n"

    if not target_pages_str:
        target_pages_str = "null (no evaluation data — run evaluation first)"

    # ── 5b) Moz authority metrics ─────────────────────────────
    # Fetch DA, PA, referring domains for the target page.
    # Uses 30-day cache to stay within 50 calls/month budget.
    moz_str = "null (Moz API not configured)"
    try:
        moz = MozClient()
        if moz.api_token:
            moz_metrics = moz.get_url_metrics(url)
            if moz_metrics:
                moz_str = (
                    f"domain_authority: {moz_metrics.get('domain_authority', 0)}\n"
                    f"page_authority: {moz_metrics.get('page_authority', 0)}\n"
                    f"spam_score: {moz_metrics.get('spam_score', 0)}\n"
                    f"root_domains_to_page: {moz_metrics.get('root_domains_to_page', 0)}\n"
                    f"external_pages_to_page: {moz_metrics.get('external_pages_to_page', 0)}\n"
                    f"cached: {moz_metrics.get('cached', False)}"
                )
            else:
                moz_str = "null (API call failed or returned empty)"
    except Exception as exc:
        print(f"[MozClient] Error fetching metrics for {url}: {exc}")
        moz_str = "null (error fetching Moz data)"

    # ── 5c) Core Web Vitals (CrUX API) ──────────────────────
    cwv_str = "null (GOOGLE_API_KEY not configured)"
    try:
        crux = CrUXClient()
        if crux.api_key:
            cwv_data = crux.get_cwv(url)
            if cwv_data:
                cwv_str = (
                    f"level: {cwv_data.get('level', 'url')} ({'page-level' if cwv_data.get('level') == 'url' else 'origin-level fallback'})\n"
                    f"LCP: {cwv_data.get('lcp_ms', 'N/A')}ms ({cwv_data.get('lcp_rating', 'unknown')})\n"
                    f"INP: {cwv_data.get('inp_ms', 'N/A')}ms ({cwv_data.get('inp_rating', 'unknown')})\n"
                    f"CLS: {cwv_data.get('cls', 'N/A')} ({cwv_data.get('cls_rating', 'unknown')})\n"
                    f"FCP: {cwv_data.get('fcp_ms', 'N/A')}ms\n"
                    f"TTFB: {cwv_data.get('ttfb_ms', 'N/A')}ms\n"
                    f"overall: {cwv_data.get('overall_rating', 'unknown')}"
                )
            else:
                cwv_str = "null (no CrUX data available for this URL or origin)"
    except Exception as exc:
        print(f"[CrUX] Error: {exc}")
        cwv_str = "null (error fetching CrUX data)"

    # ── 5d) URL Inspection data ──────────────────────────────
    url_inspection = pm.get("url_inspection")
    if url_inspection and "error" not in url_inspection:
        url_insp_str = (
            f"verdict: {url_inspection.get('verdict', 'UNKNOWN')}\n"
            f"coverage_state: {url_inspection.get('coverage_state', 'UNKNOWN')}\n"
            f"indexing_state: {url_inspection.get('indexing_state', 'UNKNOWN')}\n"
            f"robotstxt_state: {url_inspection.get('robotstxt_state', 'UNKNOWN')}\n"
            f"page_fetch_state: {url_inspection.get('page_fetch_state', 'UNKNOWN')}\n"
            f"last_crawl_time: {url_inspection.get('last_crawl_time', 'UNKNOWN')}\n"
            f"crawled_as: {url_inspection.get('crawled_as', 'UNKNOWN')}"
        )
    else:
        url_insp_str = "null (URL inspection data not available — run evaluation with crawl to get inspection data)"

    # ── 6) Above-fold HTML and robots meta ──────────────────────
    above_fold_html_raw = pm.get("above_fold_html", "")
    robots_meta = pm.get("robots_meta", "")
    # Clean above-fold HTML: strip structural tags, keep semantic content
    # This gives the AI a readable view of what's above the fold
    if above_fold_html_raw:
        import re as _re
        # Remove structural tags (div, span, section, article, figure, etc.) but keep their content
        above_fold_html = _re.sub(
            r'</?(?:div|span|section|article|figure|figcaption|main|aside|header|footer|nav|form|input|button|label|textarea|select|option|table|thead|tbody|tr|td|th|dl|dt|dd|details|summary|fieldset|legend|picture|source|video|audio|canvas|map|area)(?:\s[^>]*)?>', '',
            above_fold_html_raw
        )
        # Collapse whitespace
        above_fold_html = _re.sub(r'\s+', ' ', above_fold_html).strip()
    else:
        above_fold_html = ""

    # ── 6b) HTML structural issues (pre-computed) ─────────────
    # Run HTMLIssueEvaluator on full body HTML (or above-fold fallback)
    # to detect structural defects (span CTAs, empty media links, etc.)
    # that the AI cannot see after tag stripping.
    body_html_raw = pm.get("body_html", "")
    html_issues_str = ""
    print(f"[AI-DEBUG] body_html length: {len(body_html_raw)}, above_fold_html length: {len(above_fold_html_raw)}")
    if body_html_raw or above_fold_html_raw:
        try:
            from src.evaluators.html_issue_evaluator import HTMLIssueEvaluator
            from src.models.page_asset import PageAsset as _PA, AssetType as _AT

            _asset_type_map = {
                "product": _AT.PRODUCT, "category": _AT.CATEGORY,
                "blog": _AT.BLOG,
            }
            _pa = _PA(
                url=url,
                asset_type=_asset_type_map.get(page_type, _AT.OTHER),
                above_fold_html=above_fold_html_raw,
                body_html=body_html_raw,
            )
            _html_result = HTMLIssueEvaluator().evaluate(_pa)
            print(f"[AI-DEBUG] HTMLIssueEvaluator found {len(_html_result.issues)} issues (has_issues={_html_result.has_issues})")
            for iss in _html_result.issues:
                print(f"[AI-DEBUG]   -> {iss.issue_type}: {iss.description[:80]}")
            if _html_result.has_issues:
                issue_lines = []
                for iss in _html_result.issues:
                    issue_lines.append(
                        f"- [{iss.severity}] {iss.issue_type}: {iss.description}"
                    )
                html_issues_str = (
                    "\n\nHTML STRUCTURAL ISSUES (detected by automated audit — "
                    "these are pre-verified facts, not suggestions):\n"
                    + "\n".join(issue_lines) + "\n"
                    "You MUST acknowledge these issues in your analysis. "
                    "If recommending a CONTENT_CLARIFY or routing fix, "
                    "include fixing these structural issues in the implementation steps."
                )
        except Exception as exc:
            print(f"[AI-DEBUG] HTMLIssueEvaluator FAILED: {exc}")
            import traceback
            traceback.print_exc()

    # ── 7) SERP competitor data ─────────────────────────────────
    serp_summary = serp_client.get_serp_summary_for_opportunity(opportunity)
    serp_str = ""
    if serp_summary and serp_summary.get("serp_results"):
        serp_lines = [f"SERP data available for {serp_summary['queries_with_serp_data']}/{serp_summary['queries_total']} top queries:"]
        for sr in serp_summary["serp_results"]:
            serp_lines.append(f"\n  Query: \"{sr['query']}\" (GSC impressions: {sr['impressions']}, GSC position: {sr.get('our_position_gsc', '?')})")
            if sr.get("serp_features"):
                serp_lines.append(f"  SERP features: {', '.join(sr['serp_features'])}")
            if sr.get("spelling_suggestion"):
                serp_lines.append(f"  Google suggests: \"{sr['spelling_suggestion']}\"")
            for comp in sr.get("competitors", []):
                serp_lines.append(f"    #{comp['position']}: [{comp['title']}] — {comp['snippet'][:120]}")
                serp_lines.append(f"        URL: {comp['url']}")
        serp_str = "\n".join(serp_lines)
    else:
        serp_str = "null (SERP competitor data not yet collected for this page's queries)"

    # ── 8) Pipeline scores — pass to AI for anchoring ─────────
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

NO HOMEWORK RULE (CRITICAL):
You have the GSC data. You have the competing URLs. You have the positions and CTR.
DO NOT tell the operator to "run a review" or "check GSC" — they are looking at the same
data you are. YOUR JOB is to analyze it and make the call.

For cannibalization specifically:
  – If a query has position 1 + 0% CTR across hundreds of impressions, that is NOT
    cannibalization — that is query-intent mismatch. The query is irrelevant to this page.
    Say so: "Query 'X' is a junk match — position 1 with 0% CTR over N impressions means
    the SERP intent does not match this page. Ignore this query for optimization purposes."
  – If two pages alternate in rankings (position instability) for the same query, that IS
    cannibalization. State which page should own the query and why, based on intent match.
  – If you can make the decision from the provided data, MAKE IT. Do not defer.
  – "Run a cannibalization check in GSC" is NOT an acceptable recommendation when the
    GSC data is already in the inputs. Analyze it NOW and state the conclusion.
  – Only recommend a manual review if genuinely missing data (e.g., "competing page's
    content/title is not provided — cannot determine intent match without it").

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

CRITICAL DISTINCTION:
  – "internal_outlinks" (in INPUTS) = links that ALREADY EXIST on this page. Use these to understand
    the CURRENT state of routing. Do NOT recommend adding a link that already appears in outlinks —
    it's already there. If routing is weak DESPITE existing links, recommend repositioning or
    adding NEW links to DIFFERENT pages.
  – "site_pages" (in INPUTS) = ALL known pages on the site, grouped by type. Your target URLs
    for NEW links must come from this list.

RULES:
  – Check outlinks FIRST. Count how many link to product/category pages.
    If the page already links to relevant product/category pages, mark internal_links
    as VALID (not INVALID), and note the existing links. The weak_funnel_routing constraint
    is based on batch data that may undercount — the outlinks list above is ground truth.
  – For NEW link recommendations: target URLs MUST come from site_pages AND must NOT
    already appear in internal_outlinks or the BLOCKLIST.
  – If a URL does not appear in site_pages, it DOES NOT EXIST. Do NOT:
    • Invent or guess URLs (e.g. /category/wooden-blocks)
    • Recommend utility pages: /catalogsearch/*, /customer/*, /wishlist, /contact, /enable-cookies
    • Example: /catalogsearch/advanced/ is a utility page — NEVER recommend linking to it
  – If no suitable NEW target page exists (all good targets are already linked), state this
    and either recommend strengthening existing link placement or NO_ACTION.
  – The target_urls array in your output MUST contain ONLY URLs from site_pages that are
    NOT already in internal_outlinks.

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
TITLE/META TEST SPECIFICITY RULE
────────────────────────────────
Any TITLE_META_TEST recommendation MUST include the EXACT proposed text:
  1. The complete new title tag text (not a description — the actual title)
  2. The complete new meta description text (not a description — the actual text)
  3. WHY this specific wording — tie to dominant GSC queries by name

CHARACTER LENGTH RULES (Google best practices):
  - Title tag: 50-60 characters MAX. Google truncates at ~60 characters (or ~580px).
    Titles over 60 chars get cut off with "..." in SERPs — losing your key message.
  - Meta description: 150-160 characters MAX. Google truncates at ~160 characters.
    Descriptions over 160 chars get cut off. Front-load the value proposition.
  - Include character count in parentheses after each proposed title and meta description.
    Example: "Best Wooden Trains for Kids" (28 chars)
  - If a proposed title exceeds 60 chars, REWRITE it shorter. No exceptions.

YEAR RULE: If any variant includes a year, it MUST use current_year from INPUTS (currently {datetime.now().year}).
NEVER use a past year. Stale years make the page look outdated in SERPs.

DELIMITER RULE: Title tags MUST read as natural phrases a human would say out loud.
Do NOT use delimiters (pipes |, em dashes —, en dashes –, colons :, slashes //) to
glue keyword segments together. Google frequently rewrites segmented titles.

WRONG: "Montessori Toys by Age | Wooden, USA-Made | Free Shipping"
WRONG: "Montessori Toys for Babies, Toddlers, and Preschoolers — Wooden, Expert-Curated"
WRONG: "Montessori Toys for Babies Through Preschoolers, Expert-Curated in Wood"
  ("Through Preschoolers" is unnatural; "Expert-Curated in Wood" is grammatically broken)
WRONG: "Montessori Toys Sorted by Age for Babies Through Preschoolers"
  ("Babies Through Preschoolers" is unnatural — NEVER use "Through" to span age groups.
   Use "Babies, Toddlers, and Preschoolers" or "Every Age" or "Ages 0-6" instead.)

RIGHT: "Best Wooden Montessori Toys for Babies, Toddlers, and Preschoolers"
  (Differentiator "Wooden" is a natural adjective before the noun)
RIGHT: "Expert-Curated Wooden Montessori Toys Sorted by Age"
  (Two adjectives + prepositional phrase — reads like a real sentence)
RIGHT: "Shop Wooden Montessori Toys for Every Age"
  (Action verb + adjective + natural object)

Technique: Place differentiators as ADJECTIVES before the product noun or as
PREPOSITIONAL PHRASES after it. Do not comma-append extra keywords at the end.
Read the title out loud — if it sounds like a list of SEO keywords glued together,
rewrite it until it sounds like something a person would actually say.

FACTUAL ACCURACY RULE (CRITICAL — LEGAL LIABILITY):
Every claim in a proposed title or meta description MUST be verifiable from the page content
provided in the INPUTS (content_preview, above_fold_html, internal_outlinks).
You MUST NOT:
  – Broaden qualified claims. If the page says "free shipping within the continental US",
    you CANNOT write "Free Shipping on all orders."
  – Universalize partial claims. If the page says "Many of our toys are made in the USA",
    you CANNOT write "USA-Made" without the qualifier. Write "Includes USA-Made" or
    "Many USA-Made Options" instead.
  – Invent claims not on the page. If no "price match guarantee" appears in the content,
    you CANNOT add it to the meta description.
False or overstated claims in meta descriptions create legal liability (FTC Act Section 5,
state consumer protection laws) and erode trust when users land on a page that contradicts
the SERP snippet. Verify every claim against the actual page content before including it.

SEQUENTIAL TEST RULE: Title tags cannot be A/B tested — Google shows one title at a time.
When proposing multiple variants, present them as PRIORITY-RANKED sequential options:
  - "Deploy Variant A first. Evaluate in GSC after 28 days."
  - "If CTR does not improve by [threshold], replace with Variant B and re-evaluate."
Do NOT frame variants as parallel A/B splits. Be honest about the execution reality:
one change at a time, ~28 days per test cycle.

"Propose new title and meta description to better align with queries" is NOT acceptable.
"PRIORITY 1 — Deploy first:
 Title: 'Best Wooden Blocks for Kids: Complete Buying Guide {datetime.now().year}'
 Meta: 'Compare top wooden block brands for toddlers. Materials, sizes, safety
 standards, and age-appropriate picks from Community Playthings to Guidecraft.'
 Why: Incorporates 'best wooden blocks' (133 impr) and 'building blocks for kids' (71 impr)
 into title. Meta targets 'brands' and 'toddlers' clusters.
 Evaluate after 28 days. If CTR does not improve by 50%, move to Priority 2." IS acceptable.

────────────────────────────────
MANDATORY INBOUND LINK ANALYSIS (REQUIRED FOR EVERY PAGE)
────────────────────────────────
Each site_page in INPUTS includes: impr (28-day impressions), pos (avg position),
overlap (shared query-word count with target page), and link_score (composite).

You MUST ALWAYS include a "Step 4: Inbound Link Opportunities" section in your output,
even if you believe other constraints are more important. This section is MANDATORY
whenever site_pages data is available (not null). Do NOT defer, skip, or fold this
into the "Explicitly Preserved" section.

In Step 4, you MUST:
  1. Identify the TOP 3 pages from site_pages that should link TO this page, ranked by
     link_score (= impressions × position_factor × topical_overlap). These are the pages
     where adding a link to the current page will have the most impact.
  2. For EACH of the top 3, provide ALL of:
     a. EXACT source URL — copy-pasted from site_pages (NOT from outlinks)
     b. EXACT target URL — the current page being analyzed
     c. DATA JUSTIFICATION — cite the page's impressions, position, and query overlap
        that make it a strong linking source (e.g., "1,240 impressions, position 4.2,
        3 overlapping query terms: 'wooden trains', 'toy trains', 'model trains'")
     d. WHERE on the source page — e.g., "in the product comparison section",
        "after the introductory paragraph about [topic]"
     e. SUGGESTED ANCHOR TEXT — following the anchor text accuracy rule below
     f. PRIMARY vs SECONDARY — which is the main funnel link, which are supporting

If site_pages is null, state "Inbound link analysis unavailable — run full evaluation first."

INTERNAL LINKING SPECIFICITY RULE (for outbound link recommendations)
────────────────────────────────
When recommending INTERNAL_LINKING as an action (links FROM this page to others), you MUST:
  1. Provide EXACT target URLs copy-pasted from site_pages (NOT invented)
  2. Explain WHY this target — tie to funnel analysis, query intent, and link_score
  3. Specify WHERE in the content to place the link
  4. Suggest ANCHOR TEXT following the anchor text accuracy rule below

"Add internal links to relevant category pages" is NOT acceptable.
"TOP INBOUND LINKING SOURCES for this page:
1. /collections/wooden-trains [Wooden Train Sets] — link_score=15,480
   (1,240 impr, pos 4.2, 3 query overlaps). Add link in the comparison
   section after 'types of wooden trains' paragraph. Anchor: 'See our
   Alphabet Train Set'. Primary funnel link.
2. /blog/montessori-toy-guide [Montessori Toy Guide] — link_score=8,200
   (890 impr, pos 6.1, 2 query overlaps). Add link in the 'educational
   benefits' section. Anchor: 'Alphabet Learning Train'. Secondary.
3. /wooden-blocks [Wooden Blocks Collection] — link_score=5,100
   (620 impr, pos 8.3, 1 query overlap). Add link in 'related products'
   CTA. Anchor: 'Pair with Wooden Blocks'. Secondary." IS acceptable.

────────────────────────────────
ANCHOR TEXT ACCURACY RULE
────────────────────────────────
Anchor text MUST accurately describe the destination page scope:
  – If the URL is a single product (PDP): use the specific product name.
    CORRECT: "See the 218-Piece Unit Block Set" → /unit-blocks-set-e-218-piece-set.html
    WRONG:   "See our Unit Block Sets" → /unit-blocks-set-e-218-piece-set.html
    (Plural "Sets" implies a collection; linking to one product is misleading.)
  – If the URL is a category (PLP): collection/plural language is appropriate.
    CORRECT: "Browse Wooden Blocks" → /wooden-blocks.html
  – If the URL is a brand page: name the brand.
    CORRECT: "Shop Guidecraft" → /shop-by-brands/guide-craft.html
    WRONG:   "Shop top brands" → /shop-by-brands/guide-craft.html

Misleading anchor text hurts conversion — users who expect a collection page but land on
a single product will bounce. Every anchor text must set accurate expectations for what
the user will find when they click.

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
Every routing % you assume MUST cite ONE of these sources AND justify the specific number:
  – OBSERVED: "outlink CTR to /category-page is X% based on Y clicks / Z pageviews from internal_outlinks data"
  – BENCHMARK: "industry avg blog→category CTR is 3-5% (source: [named benchmark]). Using [low/mid/high] end because [reason]."
  – INFERRED: "[specific page feature from above_fold_html or internal_outlinks] → [why this implies N% routing]."
    E.g. 'no CTA above fold, 1 text link in paragraph 8 → ~2% routing (low end of 1-5% range for buried links)'
    IMPORTANT: INFERRED evidence REQUIRES above_fold_html or internal_outlinks data.
    If above_fold_html is null AND internal_outlinks is null, you CANNOT use INFERRED — use BENCHMARK instead.
The routing_evidence field must contain BOTH the evidence type AND the number justification.
"INFERRED: based on typical blog to category routing" is NOT acceptable — it restates the assumption without justifying it.
Unsourced or unjustified routing assumptions are forbidden. If you cannot justify a routing %, use 0% and state NO_ACTION.

SELF-CONSISTENCY CHECK (mandatory before output):
After computing your scenario values, verify:
  – LOW < BASE < HIGH (monotonic)
  – LOW/BASE/HIGH MUST use DIFFERENT routing percentages (this is the primary sensitivity variable)
  – low.routing_pct < base.routing_pct < high.routing_pct (e.g. 2% / 5% / 8%)
  – If all three scenarios use the same routing%, your range is INVALID — fix it
  – Your total value summary matches the BASE scenario (not LOW, not HIGH)
  – pipeline_reconciliation MUST reconcile with pipeline_est_value using specific assumptions

Interpretation rules:
• If assumptions are weak or speculative → LOWER CONFIDENCE, not VALUE
• If assumptions cannot be justified → NO_ACTION (value cannot be responsibly estimated)

Forbidden:
• Unexplained dollar figures
• Single-scenario estimates
• Inflated confidence to compensate for uncertainty
• Routing % without evidence citation

PIPELINE VALUE RECONCILIATION:
The pipeline has pre-calculated a monthly value estimate for this page (see pipeline_est_value in INPUTS).
This is computed as: missed_clicks × AOV × margin × conversion_factor.
It represents what the page SHOULD generate monthly if it performed at position-expected CTR.
Your opportunity_estimate scenarios represent what you project based on routing analysis.
Your job is to explain the relationship:
• State: "Pipeline estimates $X/mo based on [formula]. My base estimate is $Y/mo. [Agreement or divergence reason]."
• If your base is lower, explain which assumption differs (routing %, CVR, margin).
• If your base is higher, explain what additional value you identified (assisted value, multi-path routing).

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
current_year: {datetime.now().year}
url: {url}
page_type: {page_type}
site_platform: {config.get("data_sources", {}).get("site_platform", "magento")}
pipeline_est_value: ${pipeline_ev:.2f} (pipeline's monthly value estimate: missed_clicks × AOV × margin × conversion_factor, based on position-expected CTR)
pipeline_confidence: {pipeline_confidence:.0%}
pipeline_intent_score: {pipeline_intent:.0%}
pipeline_mode: {pipeline_mode}
title_tag_current: {cached_title or 'null'}
meta_desc_current: {cached_meta or 'null'}
h1_current: {cached_h1 or 'null'}
canonical_current: {cached_canonical or 'null'}
robots_meta: {robots_meta or 'null'}
word_count: {cached_word_count}
schema_types: {', '.join(pm.get('schema_types', [])) or 'none detected'}
above_fold_html: {above_fold_html[:1500] if above_fold_html else 'null'}
{html_issues_str}
internal_outlinks (DIAGNOSTIC ONLY — these links ALREADY EXIST on this page, do NOT recommend these):
{outlinks_str}
{outlink_blocklist_section}
top_gsc_queries:
{gsc_str}

constraints:
{constraints_str}

business_context:
{business_context_str}

site_pages (YOUR ONLY SOURCE for recommending new internal links — copy-paste URLs from here):
{target_pages_str}
{"INBOUND BLOCKLIST — these pages ALREADY link to this page. Do NOT recommend them as inbound link sources:" + chr(10) + chr(10).join("  - " + u for u in inbound_blocklist_urls) + chr(10) if inbound_blocklist_urls else ""}
moz_authority (domain & page authority from Moz — use for backlink gap analysis):
{moz_str}

serp_competitors (ACTUAL search results for this page's queries — use to craft differentiated titles):
{serp_str}
IMPORTANT: If SERP competitor data is provided, you MUST reference it when proposing title/meta changes.
Your proposed title MUST be differentiated from competitors shown above — not generic SEO.
Study what competitors say and find an angle they DON'T cover (e.g., personalization, material, age range).

core_web_vitals (real-user performance data from Chrome UX Report):
{cwv_str}

url_inspection (Google URL Inspection API — indexing status from Googlebot's perspective):
{url_insp_str}
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
    "funnel_role": "<VALID | INVALID | INCONCLUSIVE> — <brief reason>",
    "schema_markup": "<VALID | INVALID | MISSING | INCONCLUSIVE> — <brief reason based on schema_types input>"
  }}}},
  "funnel_analysis": {{{{
    "entry_intent": "<dominant query clusters, impression share, what users expect next>",
    "current_paths": "<where users currently go from this page, or 'No observable downstream path'>",
    "ideal_paths": "<proposed funnel path and WHY this path matches dominant intent>",
    "routing_diagnosis": "<failure type(s) or VALID>",
    "opportunity_estimate": {{{{
      "low":  {{{{ "routing_pct": <number MUST be less than base e.g. 2>,  "math": "<21804 × 2% × 2.1% × $53.19 × 25% = $X>", "total": <number> }}}},
      "base": {{{{ "routing_pct": <number — your best estimate e.g. 5>,  "math": "<21804 × 5% × 2.1% × $53.19 × 25% = $X>", "total": <number> }}}},
      "high": {{{{ "routing_pct": <number MUST be greater than base e.g. 8>,  "math": "<21804 × 8% × 2.1% × $53.19 × 25% = $X>", "total": <number> }}}},
      "routing_evidence": "<OBSERVED|BENCHMARK|INFERRED: justify the BASE routing%>",
      "summary": "<Total value range: $[low.total]–$[high.total]/mo (base: $[base.total])>"
    }}}},
    "pipeline_reconciliation": "<Pipeline estimates $X/mo (missed_clicks × AOV × margin). My base estimate is $Y/mo. [AGREES | DIVERGES: specific assumption difference].>"
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
      "target_urls": ["<ONLY for INTERNAL_LINKING: list each target URL here — must be copy-pasted from site_pages>"],
      "exact_changes": "<implementation-ready details. For TITLE_META_TEST: you MUST write out the Priority 1 title and meta description in full here — the actual text, not 'see variants'. The variants array is for the system to track; exact_changes is what the operator reads. For INTERNAL_LINKING: exact URLs from target_urls, anchor text, placement location, primary/secondary>",
      "variants": [
        {{{{
          "priority": <1 | 2 | 3>,
          "title": "<for TITLE_META_TEST: the exact title tag text>",
          "meta_description": "<for TITLE_META_TEST: the exact meta description text>",
          "why": "<why this variant, which queries it targets>"
        }}}}
      ],
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
  "inbound_link_opportunities": {{{{
    "available": <true if site_pages data exists, false if null>,
    "top_3": [
      {{{{
        "source_url": "<exact URL from site_pages that should link TO this page>",
        "source_title": "<title of the source page>",
        "link_score": <number>,
        "impressions": <number>,
        "position": <number>,
        "query_overlap": <number>,
        "justification": "<why this page is a strong linking source — cite shared query terms>",
        "placement": "<where on the source page to add the link>",
        "anchor_text": "<suggested anchor text following anchor text accuracy rules>",
        "priority": "<PRIMARY | SECONDARY>"
      }}}}
    ]
  }}}},
  "authority_analysis": {{{{
    "available": <true if moz_authority data exists, false if null>,
    "domain_authority": <number>,
    "page_authority": <number>,
    "referring_domains": <number>,
    "authority_verdict": "<STRONG | ADEQUATE | WEAK | INSUFFICIENT> — brief assessment",
    "backlink_gap": {{{{
      "current_position": <weighted avg position from GSC>,
      "target_position": <1-3>,
      "pa_target": <number — PA needed for target position>,
      "pa_gap": <number — pa_target minus current page_authority>,
      "high_da_links": {{{{ "da_range": "50-80+", "links_needed": <number>, "sources": "<types of sites>", "timeline": "<months>" }}}},
      "medium_da_links": {{{{ "da_range": "25-50", "links_needed": <number>, "sources": "<types of sites>", "timeline": "<months>" }}}},
      "low_da_links": {{{{ "da_range": "10-25", "links_needed": <number>, "sources": "<types of sites>", "timeline": "<months>" }}}},
      "confidence": "<HIGH | MEDIUM | LOW>",
      "reasoning": "<cite DA, PA, referring domains, query competitiveness, position gap>"
    }}}},
    "quick_wins": "<specific pages or strategies, or 'N/A'>"
  }}}},
  "no_actions": [
    "<element>: <why no change is needed>"
  ]
}}}}

If site_pages is null, set inbound_link_opportunities.available = false and top_3 = [].
If site_pages has data, you MUST populate top_3 with exactly 3 entries sorted by link_score.

VARIANTS RULE: For TITLE_META_TEST recommendations, you MUST populate the "variants" array
with exactly 3 priority-ranked variants. Each variant has: priority (1/2/3), title (exact text),
meta_description (exact text), and why (justification). The system will track which variant is
deployed and auto-evaluate after the measurement window. For all other action types, set
variants to an empty array [].
IMPORTANT: exact_changes MUST ALSO contain the Priority 1 title and meta description text
in full. Do NOT write "Deploy Priority 1" or "see variants" — the operator reads exact_changes,
not the variants array. Write the actual title and meta text in both places.

AUTHORITY ANALYSIS RULE: If moz_authority data is available (not null), you MUST populate
authority_analysis with ALL fields including backlink_gap.link_building_scenarios (exactly 3 entries).
This is a REQUIRED array, not optional. Each entry has: scenario, links_needed, example_sources, timeline.
DA-WEIGHTED heuristics — NOT all links are equal:
- A single DA 80+ link can equal 20-50 DA 20 links in ranking impact
- Provide 3 scenarios at different DA tiers (high DA 50-80+, medium DA 25-50, low DA 10-25)
- For each scenario, estimate how many links AT THAT DA LEVEL would close the authority gap
- PA improvement heuristics per link:
  • DA 60+ link → ~1-2 PA points
  • DA 30-50 link → ~0.3-0.8 PA points
  • DA 10-25 link → ~0.05-0.2 PA points
- Factor in domain_authority as a baseline: higher site DA means less page-level authority needed
- For competitive queries (>5000 impressions/mo), target PA 40-55 for top-3
- For moderate queries (1000-5000 impressions/mo), target PA 30-40 for top-3
- For low-competition queries (<1000 impressions/mo), target PA 20-30 for top-3
- If moz_authority is null, set authority_analysis.available = false and leave other fields at 0/empty

If nothing is broken or improvable:
→ Return empty recommendations array with justification in no_actions."""

    # ── Reproducibility: hash the prompt ──────────────────────
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    print(f"[AI-DEBUG] prompt_hash={prompt_hash}, prompt_length={len(prompt)}")
    print(f"[AI-DEBUG] html_issues_str present: {bool(html_issues_str)} (length={len(html_issues_str)})")
    print(f"[AI-DEBUG] DELIMITER RULE in prompt: {'DELIMITER RULE' in prompt}")

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
        "5) Funnel math: low/base/high MUST each use a DIFFERENT routing_pct. "
        "Example: low=2%, base=5%, high=8%. If your three routing_pct values are identical, your output is WRONG. "
        "The math field MUST use the routing_pct for that scenario, not the base % for all three. "
        "Blogs are option creators — assisted value often exceeds direct. If total blog value < $100/mo on 20k+ impressions, priors are too low. "
        "CRITICAL DEDUP: Before recommending any internal link, check the BLOCKLIST in the inputs. "
        "If a URL appears in the BLOCKLIST or internal_outlinks, it ALREADY EXISTS on the page — "
        "recommending it again is an ERROR. Only recommend URLs from site_pages that are NOT in the BLOCKLIST. "
        "If ALL relevant targets are already linked, recommend repositioning existing links or NO_ACTION for routing. "
        "6) CONFIDENCE COHERENCE: Your stated confidence must be consistent with your assumptions. "
        "If you cite 'no GA4 data' or 'unknown routing' as limitations, confidence MUST be ≤0.5. "
        "If your routing % is speculative (INFERRED), confidence MUST be ≤0.6. "
        "Only OBSERVED evidence supports confidence >0.7. "
        "7) VALUE COHERENCE: The total in your opportunity_estimate.summary MUST equal your base scenario total. "
        "The pipeline_reconciliation MUST reconcile your estimate with pipeline_est_value using specific assumptions. "
        "Respond ONLY with valid JSON. No markdown fences, no commentary outside the JSON."
    )

    # Call AI API — route to Anthropic or OpenAI based on model prefix
    try:
        import json as json_module
        is_anthropic = model.startswith("claude-")
        # Short connect timeout, long read timeout for slow models (Claude, o-series)
        api_timeout = httpx.Timeout(connect=10.0, read=float(timeout_sec), write=10.0, pool=10.0)

        if is_anthropic:
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not anthropic_key:
                return jsonify({"error": "ANTHROPIC_API_KEY not set. Add it to your environment."}), 400

            anthropic_body = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_message,
                "messages": [
                    {"role": "user", "content": prompt},
                ],
            }
            # Fable 5 has thinking always-on; temperature is not supported alongside thinking
            if model == "claude-fable-5":
                anthropic_body["thinking"] = {"type": "adaptive"}
            else:
                anthropic_body["temperature"] = temperature

            api_response = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=anthropic_body,
                timeout=api_timeout,
            )
            if api_response.status_code != 200:
                return jsonify({"error": f"Anthropic API error: {api_response.text}"}), 500
            result = api_response.json()
            # Extract text from content blocks (skip thinking blocks for Fable 5)
            ai_content = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    ai_content = block.get("text", "")
                    break
            usage = result.get("usage", {})
            usage = {
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
                "total_tokens": (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0),
            }
        else:
            # Build OpenAI payload — handle parameter differences across model generations
            _m = model.lower()
            _needs_new_token_param = any(_m.startswith(p) for p in ("o1", "o3", "gpt-4.1", "gpt-4.5", "gpt-5"))
            _is_reasoning_model = _m.startswith(("o1", "o3"))

            openai_payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
            }
            # Newer models require max_completion_tokens; older ones use max_tokens
            if _needs_new_token_param:
                openai_payload["max_completion_tokens"] = max_tokens
            else:
                openai_payload["max_tokens"] = max_tokens
            # o-series reasoning models don't support temperature
            if not _is_reasoning_model:
                openai_payload["temperature"] = temperature

            api_response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=openai_payload,
                timeout=api_timeout,
            )
            if api_response.status_code != 200:
                return jsonify({"error": f"OpenAI API error: {api_response.text}"}), 500
            result = api_response.json()
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
            # Remove markdown code blocks if present (case-insensitive)
            clean = ai_content.strip()
            clean_lower = clean.lower()
            if clean_lower.startswith("```json"):
                clean = clean[7:]
            elif clean_lower.startswith("```"):
                clean = clean[3:]
            if clean.rstrip().endswith("```"):
                clean = clean.rstrip()[:-3]
            clean = clean.strip()

            # If stripping fences didn't reveal JSON, try to extract it
            if clean and clean[0] != '{':
                first_brace = clean.find('{')
                if first_brace >= 0:
                    clean = clean[first_brace:]

            try:
                recommendations = json_module.loads(clean)
            except json_module.JSONDecodeError:
                # Response may be truncated (hit max_tokens). Try to repair.
                repair = clean

                # Step 1: Close unclosed string literals.
                # Count unescaped quotes — if odd, we're inside a string.
                quote_count = 0
                i = 0
                while i < len(repair):
                    if repair[i] == '\\':
                        i += 2
                        continue
                    if repair[i] == '"':
                        quote_count += 1
                    i += 1
                if quote_count % 2 == 1:
                    repair += '"'

                # Step 2: Trim trailing partial key/value (after last comma or colon)
                for trim_char in [',', ':']:
                    last = repair.rfind(trim_char)
                    if last > repair.rfind('}') and last > repair.rfind(']'):
                        repair = repair[:last]
                        break

                # Step 3: Close open brackets and braces
                open_braces = repair.count('{') - repair.count('}')
                open_brackets = repair.count('[') - repair.count(']')
                repair += ']' * max(0, open_brackets) + '}' * max(0, open_braces)

                try:
                    recommendations = json_module.loads(repair)
                except json_module.JSONDecodeError:
                    # Step 4: More aggressive repair — trim back to last valid structure
                    # Find the last closing brace/bracket that could be a valid boundary
                    aggressive = clean
                    # Close unclosed string
                    if quote_count % 2 == 1:
                        aggressive += '"'
                    # Find last } or ] that's NOT inside a string
                    last_valid = -1
                    in_str = False
                    for j, ch in enumerate(aggressive):
                        if ch == '\\' and in_str:
                            continue
                        if ch == '"':
                            in_str = not in_str
                        if not in_str and ch in ('}', ']'):
                            last_valid = j
                    if last_valid > 0:
                        aggressive = aggressive[:last_valid + 1]
                        # Re-close remaining structures
                        ob = aggressive.count('{') - aggressive.count('}')
                        oq = aggressive.count('[') - aggressive.count(']')
                        aggressive += ']' * max(0, oq) + '}' * max(0, ob)
                        try:
                            recommendations = json_module.loads(aggressive)
                        except Exception:
                            recommendations = {"raw_response": ai_content}
                    else:
                        recommendations = {"raw_response": ai_content}
                except Exception:
                    recommendations = {"raw_response": ai_content}
        except Exception:
            recommendations = {"raw_response": ai_content}

        # ── Server-side dedup: strip existing outlink URLs from target_urls ──
        # AI sometimes copies URLs from the outlinks section despite rules.
        # This programmatic guardrail catches it post-hoc.
        # Separated from JSON parsing so dedup errors don't destroy valid results.
        try:
            if existing_outlink_url_set and isinstance(recommendations, dict) and "raw_response" not in recommendations:
                recs_list = recommendations.get("recommendations", [])
                if isinstance(recs_list, list):
                    for rec in recs_list:
                        if not isinstance(rec, dict):
                            continue
                        target_urls = rec.get("target_urls", [])
                        if not isinstance(target_urls, list):
                            continue
                        dupes = [u for u in target_urls if u in existing_outlink_url_set]
                        if dupes:
                            rec["target_urls"] = [u for u in target_urls if u not in existing_outlink_url_set]
                            existing_note = rec.get("exact_changes", "") or ""
                            rec["exact_changes"] = (
                                f"[SERVER NOTE: Removed {len(dupes)} URL(s) already on this page: "
                                f"{', '.join(dupes)}. These links already exist — "
                                f"consider repositioning them or linking to different pages.]\n\n"
                                + existing_note
                            )
        except Exception:
            pass  # Dedup failure should not destroy parsed recommendations

        # Extract AI's revised value estimate from funnel_analysis
        ai_revised_value = None
        if isinstance(recommendations, dict):
            fa = recommendations.get("funnel_analysis", {})
            if isinstance(fa, dict):
                oe = fa.get("opportunity_estimate", {})
                if isinstance(oe, dict):
                    base = oe.get("base", {})
                    if isinstance(base, dict) and base.get("total") is not None:
                        try:
                            ai_revised_value = float(base["total"])
                        except (ValueError, TypeError):
                            pass

        response_data = {
            "success": True,
            "url": url,
            "page_analysis": page_analysis,
            "recommendations": recommendations,
            "ai_revised_value": ai_revised_value,
            "reproducibility": {
                "prompt_hash": prompt_hash,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tokens_used": log_entry["tokens_used"],
            },
        }

        # Persist AI recommendations so they survive page refresh
        persist_updates = {
            "ai_recommendations": recommendations,
            "ai_reproducibility": response_data["reproducibility"],
            "ai_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if ai_revised_value is not None:
            persist_updates["ai_revised_value"] = ai_revised_value
        _persist_opportunity_update(url, persist_updates)

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


# ---------------------------------------------------------------------------
# Task workflow helpers
# ---------------------------------------------------------------------------

def _capture_baseline(url: str) -> dict | None:
    """Snapshot current GSC + GA4 metrics for a URL.

    Called when a task moves to IMPLEMENTED so we have a before picture.
    Returns None if APIs are unavailable (auto-measure will mark INCONCLUSIVE).
    """
    config = load_config()
    baseline = {}

    # GSC
    try:
        gsc_cfg = config.get("data_sources", {}).get("gsc", {})
        gsc = GSCClient(
            site_url=gsc_cfg.get("property_url", ""),
            credentials_path=gsc_cfg.get("credentials_path"),
        )
        m = gsc.get_page_metrics(url, days=28)
        baseline["gsc"] = {
            "impressions_28d": m.impressions_28d,
            "clicks_28d": m.clicks_28d,
            "ctr_28d": m.ctr_28d,
            "avg_position_28d": m.avg_position_28d,
        }
    except Exception:
        baseline["gsc"] = None

    # GA4
    try:
        ga4_cfg = config.get("data_sources", {}).get("ga4", {})
        ga4 = GA4Client(
            property_id=ga4_cfg.get("property_id", ""),
            credentials_path=ga4_cfg.get("credentials_path"),
        )
        page_path = url.replace("https://", "").replace("http://", "")
        page_path = "/" + page_path.split("/", 1)[-1] if "/" in page_path else "/"
        m = ga4.get_page_metrics(page_path, days=28)
        baseline["ga4"] = {
            "sessions_28d": m.sessions_28d,
            "revenue_28d": m.revenue_28d,
            "conversions_28d": m.conversions_28d,
        }
    except Exception:
        baseline["ga4"] = None

    return baseline if (baseline.get("gsc") or baseline.get("ga4")) else None


def _auto_measure_action(action, config: dict) -> dict | None:
    """Compare current metrics against baseline for one action.

    Returns a dict with outcome/metrics/notes, or None if not measurable.
    """
    if action.baseline_metrics is None:
        return {"outcome": "inconclusive", "notes": "No baseline metrics captured at implementation time."}

    baseline_gsc = action.baseline_metrics.get("gsc")
    if not baseline_gsc or baseline_gsc.get("clicks_28d", 0) == 0 and baseline_gsc.get("impressions_28d", 0) == 0:
        return {"outcome": "inconclusive", "notes": "Baseline had zero GSC traffic — cannot compare."}

    # Fetch current GSC metrics
    try:
        gsc_cfg = config.get("data_sources", {}).get("gsc", {})
        gsc = GSCClient(
            site_url=gsc_cfg.get("property_url", ""),
            credentials_path=gsc_cfg.get("credentials_path"),
        )
        current = gsc.get_page_metrics(action.url, days=28)
    except Exception:
        return {"outcome": "inconclusive", "notes": "Could not fetch current GSC metrics."}

    if current.impressions_28d == 0 and current.clicks_28d == 0:
        return {"outcome": "inconclusive", "notes": "Current GSC data returned zero — possible API issue."}

    # Compare clicks (primary signal)
    old_clicks = baseline_gsc["clicks_28d"]
    new_clicks = current.clicks_28d
    old_impressions = baseline_gsc["impressions_28d"]
    new_impressions = current.impressions_28d

    metrics = {
        "baseline_clicks": old_clicks,
        "current_clicks": new_clicks,
        "baseline_impressions": old_impressions,
        "current_impressions": new_impressions,
        "baseline_position": baseline_gsc.get("avg_position_28d"),
        "current_position": current.avg_position_28d,
    }

    # Determine outcome: >10% improvement = positive, >10% decline = negative
    if old_clicks > 0:
        click_change = (new_clicks - old_clicks) / old_clicks
    elif new_clicks > 0:
        click_change = 1.0  # went from 0 to something
    else:
        click_change = 0.0

    if old_impressions > 0:
        imp_change = (new_impressions - old_impressions) / old_impressions
    else:
        imp_change = 0.0

    if click_change > 0.10 or imp_change > 0.15:
        outcome = "positive"
        notes = f"Clicks {old_clicks}→{new_clicks} ({click_change:+.0%}), impressions {old_impressions}→{new_impressions} ({imp_change:+.0%})."
    elif click_change < -0.10 or imp_change < -0.15:
        outcome = "negative"
        notes = f"Clicks {old_clicks}→{new_clicks} ({click_change:+.0%}), impressions {old_impressions}→{new_impressions} ({imp_change:+.0%})."
    else:
        outcome = "neutral"
        notes = f"No significant change. Clicks {old_clicks}→{new_clicks} ({click_change:+.0%}), impressions {old_impressions}→{new_impressions} ({imp_change:+.0%})."

    return {"outcome": outcome, "metrics": metrics, "notes": notes}


@app.route("/api/tasks/<action_id>/advance", methods=["POST"])
def api_advance_task(action_id):
    """Advance task to next status.

    Simplified workflow:
      PROPOSED  →  IMPLEMENTED  →  CLOSED (via auto-measure)
    Approve = "I'm doing this now", starts the evaluation clock.
    Legacy APPROVED/MEASURED statuses still advance forward.
    """
    ledger = ActionLedger()
    action = ledger.get_action(action_id)

    if not action:
        return jsonify({"error": "Action not found"}), 404

    # Collapsed progression: skip APPROVED and MEASURED
    progression = {
        ActionStatus.PROPOSED: ActionStatus.IMPLEMENTED,
        ActionStatus.APPROVED: ActionStatus.IMPLEMENTED,   # legacy compat
        ActionStatus.IMPLEMENTED: ActionStatus.CLOSED,
        ActionStatus.MEASURED: ActionStatus.CLOSED,         # legacy compat
    }

    current = action.status
    if current not in progression:
        return jsonify({"error": "Cannot advance from current status"}), 400

    new_status = progression[current]
    action.update_status(new_status)

    # Don't capture baseline here — user will click "Mark as Done"
    # when they actually deploy the change in Magento.

    ledger.update_action(action)
    return jsonify({"success": True, "new_status": action.status.value})


@app.route("/api/tasks/<action_id>/mark-done", methods=["POST"])
def api_mark_done(action_id):
    """Mark a task as deployed — captures GSC/GA4 baseline and starts the evaluation clock.

    Called when the user has actually made the change in Magento (or whatever CMS).
    This is when the baseline snapshot is taken, not at approval time.
    """
    ledger = ActionLedger()
    action = ledger.get_action(action_id)

    if not action:
        return jsonify({"error": "Action not found"}), 404

    if action.status != ActionStatus.IMPLEMENTED:
        return jsonify({"error": "Task must be in In Progress status"}), 400

    # Capture baseline now — this is when the change was actually deployed
    action.baseline_metrics = _capture_baseline(action.url)
    action.implemented_at = datetime.now().isoformat()
    # Set per-action-type evaluation window
    action.evaluation_window_days = evaluation_window_for(action.action_type)

    ledger.update_action(action)

    variant_label = ""
    variants = _get_variants(action)
    if variants:
        v = variants[action.active_variant_index] if action.active_variant_index < len(variants) else None
        variant_label = f" (Variant {action.active_variant_index + 1})" if v else ""

    return jsonify({
        "success": True,
        "message": f"Baseline captured{variant_label}. Evaluation starts now ({action.evaluation_window_days} days).",
        "baseline_captured": action.baseline_metrics is not None,
        "evaluation_window_days": action.evaluation_window_days,
    })


def _get_variants(action) -> list[dict]:
    """Extract structured variants from an action's recommendation_json."""
    rec_json = action.recommendation_json or {}
    # Look in ai_recommendations (stored at approval) or directly in recommendations
    ai_rec = rec_json.get("ai_recommendations", rec_json)
    recs = ai_rec.get("recommendations", [])
    for r in recs:
        variants = r.get("variants", [])
        if variants:
            return sorted(variants, key=lambda v: v.get("priority", 99))
    return []


@app.route("/api/tasks/<action_id>/reject", methods=["POST"])
def api_reject_task(action_id):
    """Reject a task — sends it back to opportunities for re-evaluation."""
    ledger = ActionLedger()
    action = ledger.get_action(action_id)

    if not action:
        return jsonify({"error": "Action not found"}), 404

    url = action.url
    ledger.delete_action(action_id)

    return jsonify({
        "success": True,
        "message": f"Task {action_id} rejected. URL will be re-evaluated on next run.",
        "url": url,
    })


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


@app.route("/api/tasks/fix-rejected", methods=["POST"])
def api_fix_rejected_tasks():
    """Clean up tasks that were incorrectly closed by the old reject bug.
    Deletes any closed task whose notes start with 'REJECTED' so the URL
    rejoins the opportunity pool."""
    ledger = ActionLedger()
    fixed = []
    for action in ledger.get_actions_by_status(ActionStatus.CLOSED):
        if action.notes and action.notes.startswith("REJECTED"):
            fixed.append({"action_id": action.action_id, "url": action.url})
            ledger.delete_action(action.action_id)

    return jsonify({
        "success": True,
        "fixed": len(fixed),
        "tasks": fixed,
        "message": f"Removed {len(fixed)} incorrectly rejected task(s). Their URLs will rejoin the opportunity pool.",
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


@app.route("/api/tasks/auto-measure", methods=["POST"])
def api_auto_measure():
    """Auto-measure all IMPLEMENTED tasks that have passed their evaluation window.

    Pulls current GSC metrics, compares against baseline, records outcome,
    and closes the task.  Returns a summary of what was measured.
    """
    config = load_config()
    ledger = ActionLedger()
    pending = ledger.get_pending_evaluations()

    results = []
    for action in pending:
        result = _auto_measure_action(action, config)
        if result is None:
            continue

        outcome_str = result["outcome"]
        outcome = ActionOutcome(outcome_str)
        variants = _get_variants(action)
        has_next_variant = variants and action.active_variant_index < len(variants) - 1

        if outcome_str in ("negative", "neutral") and has_next_variant:
            # Variant failed but there are more to try — advance to next variant
            action.variant_outcomes.append({
                "variant_index": action.active_variant_index,
                "outcome": outcome_str,
                "metrics": result.get("metrics"),
                "notes": result.get("notes", ""),
            })
            action.active_variant_index += 1
            action.baseline_metrics = None  # Reset — user needs to "Mark as Done" again
            action.implemented_at = None    # Reset evaluation clock
            ledger.update_action(action)
            next_v = variants[action.active_variant_index]
            results.append({
                "action_id": action.action_id,
                "url": action.url,
                "outcome": outcome_str,
                "notes": result.get("notes", ""),
                "variant_advanced": True,
                "next_variant": action.active_variant_index + 1,
                "next_variant_title": next_v.get("title", ""),
                "message": f"Variant {action.active_variant_index} didn't improve. Deploy Variant {action.active_variant_index + 1} and mark as done.",
            })
        else:
            # Either positive, or all variants exhausted — close the task
            if variants:
                action.variant_outcomes.append({
                    "variant_index": action.active_variant_index,
                    "outcome": outcome_str,
                    "metrics": result.get("metrics"),
                    "notes": result.get("notes", ""),
                })
                ledger.update_action(action)
            notes = result.get("notes", "")
            if outcome_str == "positive" and variants:
                notes = f"Variant {action.active_variant_index + 1} succeeded. " + notes
            elif variants and not has_next_variant and outcome_str != "positive":
                notes = f"All {len(variants)} variants tested, none improved. " + notes
            ledger.record_outcome(
                action_id=action.action_id,
                outcome=outcome,
                outcome_metrics=result.get("metrics"),
                notes=notes,
            )
            results.append({
                "action_id": action.action_id,
                "url": action.url,
                "outcome": outcome_str,
                "notes": notes,
            })

    # Generate notifications for manual measurement results too
    for r in results:
        short_url = r["url"].replace("https://", "").replace("http://", "")
        if r.get("variant_advanced"):
            _save_notification({
                "type": "variant_advance",
                "severity": "warning",
                "action_id": r["action_id"],
                "url": r["url"],
                "message": r.get("message", f"Variant advanced on {short_url}"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "read": False,
            })
        else:
            outcome_str = r.get("outcome", "inconclusive")
            severity = "success" if outcome_str == "positive" else "info" if outcome_str == "neutral" else "error"
            _save_notification({
                "type": "measurement_complete",
                "severity": severity,
                "action_id": r["action_id"],
                "url": r["url"],
                "outcome": outcome_str,
                "message": f"'{r['action_id']}' on {short_url} measured as {outcome_str.upper()}. {r.get('notes', '')}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "read": False,
            })

    return jsonify({
        "success": True,
        "measured": len(results),
        "pending_remaining": len(pending) - len(results),
        "results": results,
    })


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


@app.route("/api/data-quality")
def api_data_quality():
    """
    Data quality comparison between GSC and GA4.

    Computes per-page metrics comparison, flags mismatches, and returns
    aggregate statistics to help diagnose tracking issues.
    """
    eval_path = DATA_PATH / "latest_evaluation.json"

    if not eval_path.exists():
        return jsonify({
            "status": "no_data",
            "message": "No evaluation data. Run workflow first.",
            "summary": {},
            "pages": [],
        })

    with open(eval_path) as f:
        eval_data = json.load(f)

    results = eval_data.get("results", [])

    # Normalize /blog/post/ URLs to canonical /blog/ and merge duplicates
    merged = {}
    for r in results:
        url = r.get("url", "")
        if "/blog/post/" in url:
            url = url.replace("/blog/post/", "/blog/")
            r["url"] = url
        if url in merged:
            existing = merged[url]
            for k in ("gsc_clicks", "gsc_impressions", "ga4_sessions", "ga4_users",
                       "ga4_engaged_sessions", "ga4_revenue", "ga4_purchases"):
                existing[k] = existing.get(k, 0) + r.get(k, 0)
            if r.get("gsc_position") and existing.get("gsc_position"):
                existing["gsc_position"] = min(existing["gsc_position"], r.get("gsc_position", 0))
            if existing.get("gsc_impressions", 0) > 0:
                existing["gsc_ctr"] = existing.get("gsc_clicks", 0) / existing["gsc_impressions"]
            if existing.get("ga4_sessions", 0) > 0:
                existing["ga4_engagement_rate"] = existing.get("ga4_engaged_sessions", 0) / existing["ga4_sessions"]
                existing["ga4_bounce_rate"] = r.get("ga4_bounce_rate", existing.get("ga4_bounce_rate", 0))
        else:
            merged[url] = r
    results = list(merged.values())

    # ── Per-page data quality analysis ──
    pages = []
    # Aggregate counters
    total = 0
    gsc_only = 0        # Has GSC data but no GA4
    ga4_only = 0        # Has GA4 data but no GSC
    both_sources = 0    # Has both
    neither_source = 0  # Has neither (sitemap-only imports)
    ratio_ok = 0
    ratio_low = 0       # GA4 sessions < GSC clicks (tracking loss)
    ratio_high = 0      # GA4 sessions > GSC clicks (misattribution)
    zero_sessions = 0   # GSC clicks > 0 but GA4 sessions = 0
    total_gsc_clicks = 0
    total_ga4_sessions = 0
    total_gsc_impressions = 0
    total_ga4_revenue = 0

    for r in results:
        gsc_clicks = r.get("gsc_clicks", 0)
        gsc_impressions = r.get("gsc_impressions", 0)
        gsc_ctr = r.get("gsc_ctr", 0)
        gsc_position = r.get("gsc_position", 0)
        ga4_sessions = r.get("ga4_sessions", 0)
        ga4_users = r.get("ga4_users", 0)
        ga4_engaged = r.get("ga4_engaged_sessions", 0)
        ga4_engagement_rate = r.get("ga4_engagement_rate", 0)
        ga4_revenue = r.get("ga4_revenue", 0)
        ga4_purchases = r.get("ga4_purchases", 0)
        ga4_bounce_rate = r.get("ga4_bounce_rate", 0)

        has_gsc = gsc_clicks > 0 or gsc_impressions > 0
        has_ga4 = ga4_sessions > 0

        total += 1
        total_gsc_clicks += gsc_clicks
        total_ga4_sessions += ga4_sessions
        total_gsc_impressions += gsc_impressions
        total_ga4_revenue += ga4_revenue

        # Source coverage
        if has_gsc and has_ga4:
            both_sources += 1
        elif has_gsc:
            gsc_only += 1
        elif has_ga4:
            ga4_only += 1
        else:
            neither_source += 1

        # Clicks-to-sessions ratio (only meaningful with ≥5 clicks)
        ratio = None
        ratio_status = "insufficient_data"
        if gsc_clicks >= 5:
            if ga4_sessions == 0:
                ratio = 0.0
                ratio_status = "zero_sessions"
                zero_sessions += 1
            else:
                ratio = round(ga4_sessions / gsc_clicks, 2)
                if 0.7 <= ratio <= 1.3:
                    ratio_status = "healthy"
                    ratio_ok += 1
                elif ratio < 0.7:
                    ratio_status = "low"
                    ratio_low += 1
                else:
                    ratio_status = "high"
                    ratio_high += 1

        page_entry = {
            "url": r.get("url", ""),
            "asset_type": r.get("asset_type", "other"),
            "gsc_impressions": gsc_impressions,
            "gsc_clicks": gsc_clicks,
            "gsc_ctr": round(gsc_ctr * 100, 2),
            "gsc_position": gsc_position,
            "ga4_sessions": ga4_sessions,
            "ga4_users": ga4_users,
            "ga4_engaged_sessions": ga4_engaged,
            "ga4_engagement_rate": round(ga4_engagement_rate * 100, 1),
            "ga4_revenue": ga4_revenue,
            "ga4_purchases": ga4_purchases,
            "ga4_bounce_rate": round(ga4_bounce_rate * 100, 1),
            "clicks_sessions_ratio": ratio,
            "ratio_status": ratio_status,
            "data_confidence": r.get("data_confidence", 0),
        }
        pages.append(page_entry)

    # Sort: problems first (zero_sessions, low ratio, high ratio), then by clicks desc
    status_priority = {"zero_sessions": 0, "low": 1, "high": 2, "healthy": 3, "insufficient_data": 4}
    pages.sort(key=lambda p: (status_priority.get(p["ratio_status"], 5), -p["gsc_clicks"]))

    # Aggregate ratio for site-wide check
    site_ratio = round(total_ga4_sessions / total_gsc_clicks, 2) if total_gsc_clicks > 0 else None

    # Determine overall status
    ratio_checked = ratio_ok + ratio_low + ratio_high + zero_sessions
    problem_pages = ratio_low + ratio_high + zero_sessions
    if ratio_checked == 0:
        overall_status = "no_data"
        status_color = "gray"
    elif zero_sessions > 5:
        overall_status = "critical"
        status_color = "red"
    elif zero_sessions > 0 and (ratio_checked > 0 and problem_pages / ratio_checked > 0.3):
        overall_status = "critical"
        status_color = "red"
    elif problem_pages > 3 or (ratio_checked > 0 and problem_pages / ratio_checked > 0.15):
        overall_status = "warning"
        status_color = "orange"
    else:
        overall_status = "healthy"
        status_color = "green"

    summary = {
        "total_pages": total,
        "both_sources": both_sources,
        "gsc_only": gsc_only,
        "ga4_only": ga4_only,
        "neither_source": neither_source,
        "ratio_healthy": ratio_ok,
        "ratio_low": ratio_low,
        "ratio_high": ratio_high,
        "zero_sessions": zero_sessions,
        "ratio_checked": ratio_checked,
        "total_gsc_clicks": total_gsc_clicks,
        "total_ga4_sessions": total_ga4_sessions,
        "total_gsc_impressions": total_gsc_impressions,
        "total_ga4_revenue": round(total_ga4_revenue, 2),
        "site_ratio": site_ratio,
        "status": overall_status,
        "status_color": status_color,
    }

    return jsonify({
        "summary": summary,
        "pages": pages,
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

    if not Path(creds_path).exists():
        return jsonify({
            "connected": False,
            "message": "Google Ads not configured — credentials file missing",
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


@app.route("/api/serp/status")
def api_serp_status():
    """Get SERP data collection status across all opportunities."""
    eval_path = DATA_PATH / "latest_evaluation.json"
    if not eval_path.exists():
        return jsonify({"total_opportunities": 0, "with_serp_data": 0, "coverage": 0})

    with open(eval_path) as f:
        eval_data = json.load(f)

    results = eval_data.get("results", [])
    actionable = [r for r in results if r.get("recommended_action") not in ("NO_ACTION", None)]

    with_serp = 0
    total_queries = 0
    queries_cached = 0
    for opp in actionable:
        top_q = opp.get("top_queries", [])[:5]
        has_any = False
        for q in top_q:
            total_queries += 1
            if serp_client.get_cached_serp(q.get("query", "")):
                queries_cached += 1
                has_any = True
        if has_any:
            with_serp += 1

    return jsonify({
        "total_opportunities": len(actionable),
        "with_serp_data": with_serp,
        "coverage": round(with_serp / max(len(actionable), 1) * 100),
        "total_queries": total_queries,
        "queries_cached": queries_cached,
        "query_coverage": round(queries_cached / max(total_queries, 1) * 100),
        "daily_quota_remaining": serp_client.get_remaining_quota(),
        "daily_quota_used": serp_client.get_daily_usage(),
    })


@app.route("/api/notifications")
def api_notifications():
    """Get recent notifications."""
    notifications = _load_notifications()
    unread = sum(1 for n in notifications if not n.get("read"))
    return jsonify({"notifications": notifications, "unread": unread})


@app.route("/api/notifications/mark-read", methods=["POST"])
def api_mark_notifications_read():
    """Mark all notifications as read."""
    notifications = _load_notifications()
    for n in notifications:
        n["read"] = True
    NOTIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(NOTIFICATIONS_PATH, "w") as f:
        json.dump(notifications, f, indent=2)
        f.write("\n")
    return jsonify({"success": True})


# ============================================================
# GROWTH & AI VISIBILITY
# ============================================================

EVAL_HISTORY_PATH = DATA_PATH / "eval_history"


@app.route("/api/growth/summary")
def api_growth_summary():
    """Growth movers + evaluation history for charts."""
    eval_path = DATA_PATH / "latest_evaluation.json"
    history_path = EVAL_HISTORY_PATH

    # Load evaluation history for charts
    history = []
    if history_path.exists():
        for f in sorted(history_path.glob("*.json")):
            try:
                with open(f) as fh:
                    snap = json.load(fh)
                results = snap.get("results", [])
                total_clicks = sum(r.get("gsc_clicks", 0) for r in results)
                total_impressions = sum(r.get("gsc_impressions", 0) for r in results)
                total_pages = len(results)
                history.append({
                    "date": snap.get("timestamp", f.stem)[:10],
                    "total_clicks": total_clicks,
                    "total_impressions": total_impressions,
                    "total_pages": total_pages,
                })
            except (json.JSONDecodeError, OSError):
                pass

    # Compute movers: compare latest vs previous eval
    gainers = []
    decliners = []
    net_clicks = 0
    current_date = ""
    previous_date = ""

    snapshots = sorted(history_path.glob("*.json")) if history_path.exists() else []
    if len(snapshots) >= 2:
        try:
            with open(snapshots[-1]) as f:
                current = json.load(f)
            with open(snapshots[-2]) as f:
                previous = json.load(f)

            current_date = current.get("timestamp", "")[:10]
            previous_date = previous.get("timestamp", "")[:10]

            prev_by_url = {r["url"]: r for r in previous.get("results", [])}
            ledger = ActionLedger()
            all_actions = ledger.get_all_actions()
            actions_by_url = {}
            for a in all_actions:
                actions_by_url.setdefault(a.url, []).append(a)

            for r in current.get("results", []):
                url = r["url"]
                prev = prev_by_url.get(url)
                if not prev:
                    continue

                clicks_cur = r.get("gsc_clicks", 0)
                clicks_prev = prev.get("gsc_clicks", 0)
                impr_cur = r.get("gsc_impressions", 0)
                impr_prev = prev.get("gsc_impressions", 0)
                pos_cur = r.get("gsc_position", 0)
                pos_prev = prev.get("gsc_position", 0)

                clicks_delta = clicks_cur - clicks_prev
                impr_delta = impr_cur - impr_prev
                pos_delta = pos_cur - pos_prev
                net_clicks += clicks_delta

                if clicks_delta == 0 and impr_delta == 0:
                    continue

                why = _explain_mover(url, actions_by_url.get(url, []), clicks_delta, pos_delta)

                mover = {
                    "url": url,
                    "page_type": r.get("asset_type", ""),
                    "clicks_cur": clicks_cur,
                    "clicks_prev": clicks_prev,
                    "clicks_delta": clicks_delta,
                    "impressions_cur": impr_cur,
                    "impressions_prev": impr_prev,
                    "impressions_delta": impr_delta,
                    "position_cur": pos_cur,
                    "position_prev": pos_prev,
                    "position_delta": round(pos_delta, 1),
                    "why": why,
                }

                if clicks_delta > 0:
                    gainers.append(mover)
                elif clicks_delta < 0:
                    decliners.append(mover)

            gainers.sort(key=lambda m: m["clicks_delta"], reverse=True)
            decliners.sort(key=lambda m: m["clicks_delta"])

        except (json.JSONDecodeError, OSError, KeyError):
            pass

    return jsonify({
        "history": history,
        "gainers": gainers[:20],
        "decliners": decliners[:20],
        "net_clicks": net_clicks,
        "current_date": current_date,
        "previous_date": previous_date,
    })


def _explain_mover(url: str, actions: list, clicks_delta: int, pos_delta: float) -> str:
    """Try to explain why a page moved."""
    reasons = []

    for action in actions:
        if action.status.value in ("implemented", "closed"):
            atype = action.action_type or "change"
            label = atype.replace("_", " ").title()
            if action.outcome and action.outcome.value == "positive":
                reasons.append(f"{label} (positive outcome)")
            elif action.status.value == "implemented":
                reasons.append(f"{label} in progress")
            else:
                reasons.append(f"{label} applied")

    if not reasons:
        if pos_delta < -2:
            reasons.append("Position improved significantly")
        elif pos_delta > 2:
            reasons.append("Position dropped")
        elif abs(clicks_delta) > 50:
            reasons.append("Traffic shift (no task found)")
        else:
            reasons.append("Organic fluctuation")

    return "; ".join(reasons[:2])


@app.route("/api/growth/ai-visibility")
def api_growth_ai_visibility():
    """Get AI visibility summary."""
    return jsonify(ai_visibility.get_visibility_summary())


@app.route("/api/growth/ai-visibility/check", methods=["POST"])
def api_growth_ai_visibility_check():
    """Run an AI visibility check as a background job."""
    global job_state

    if job_state["running"]:
        return jsonify({"error": "A job is already running", "status": "busy"}), 400

    ai_visibility.sync_queue_prompts()

    data = ai_visibility._load_data()
    if not data.get("prompts"):
        return jsonify({"error": "No prompts configured. Add prompts first."}), 400

    config = data.get("config", {})
    if not config.get("brand_keywords") and not config.get("site_domain"):
        return jsonify({"error": "Configure brand keywords or site domain first. Click Config to set them."}), 400

    prompts = data["prompts"]
    brand_keywords = config.get("brand_keywords", [])
    site_domain = config.get("site_domain", "")

    job_state.update({
        "running": True,
        "type": "ai_visibility",
        "error": None,
        "message": "Starting AI visibility check...",
        "progress": 0,
        "total": len(prompts),
    })

    def run_check():
        import traceback
        try:
            all_results = []
            for i, prompt in enumerate(prompts):
                engines = prompt.get("engines", ["chatgpt", "claude", "gemini", "perplexity"])
                short = prompt["text"][:50]
                job_state["message"] = f"[{i+1}/{len(prompts)}] Checking: {short}..."
                job_state["progress"] = i

                results = ai_visibility.check_prompt(
                    prompt["text"],
                    engines=engines,
                    brand_keywords=brand_keywords,
                    site_domain=site_domain,
                )
                all_results.extend(results)

            # Save all results
            vis_data = ai_visibility._load_data()
            vis_data["results"].extend(all_results)
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(days=90)).isoformat()
            vis_data["results"] = [r for r in vis_data["results"] if r.get("timestamp", "") >= cutoff]
            ai_visibility._save_data(vis_data)

            mentioned = sum(1 for r in all_results if r.get("mentioned"))
            non_error = [r for r in all_results if not r.get("error")]
            errors = sum(1 for r in all_results if r.get("error"))

            summary = f"AI visibility complete: mentioned in {mentioned}/{len(non_error)} responses"
            if errors:
                summary += f" ({errors} errors — check API keys)"

            batch_result = ai_visibility.process_batch_results()
            if batch_result.get("processed", 0) > 0:
                summary += f" | Queue: {batch_result['retained']} retained, {batch_result['archived']} archived"

            job_state["message"] = summary
            job_state["progress"] = len(prompts)

        except Exception as e:
            tb = traceback.format_exc()
            job_state["error"] = f"{e}\n\nTraceback:\n{tb}"
            job_state["message"] = f"Error: {e}"
        finally:
            from datetime import datetime
            job_state["finished_at"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
            job_state["running"] = False

    thread = threading.Thread(target=run_check)
    thread.start()

    return jsonify({"message": "AI visibility check started", "status": "running"})


@app.route("/api/growth/ai-visibility/generate-advice", methods=["POST"])
def api_growth_generate_advice():
    """Generate AI advice for all prompts that have results but no advice. Runs in background."""
    global job_state
    if job_state["running"]:
        return jsonify({"error": "A job is already running", "status": "busy"}), 400

    job_state.update({
        "running": True, "type": "generate_advice", "error": None,
        "message": "Generating AI advice...", "progress": 0, "total": 1,
    })

    def run_backfill():
        try:
            result = ai_visibility.backfill_ai_advice()
            job_state["message"] = f"Generated advice for {result['generated']} keywords"
            job_state["progress"] = 1
        except Exception as e:
            job_state["error"] = str(e)
            job_state["message"] = f"Error: {e}"
        finally:
            from datetime import datetime
            job_state["finished_at"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
            job_state["running"] = False

    thread = threading.Thread(target=run_backfill)
    thread.start()
    return jsonify({"message": "Generating AI advice in background", "status": "running"})


@app.route("/api/growth/ai-visibility/prompts", methods=["POST"])
def api_growth_add_prompt():
    """Add a new prompt to track."""
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "Prompt text required"}), 400
    engines = data.get("engines", ["chatgpt", "claude", "gemini", "perplexity"])
    prompt = ai_visibility.add_prompt(text, engines)
    return jsonify({"success": True, "prompt": prompt})


@app.route("/api/growth/ai-visibility/prompts/<prompt_id>", methods=["DELETE"])
def api_growth_remove_prompt(prompt_id):
    """Remove a tracked prompt."""
    ai_visibility.remove_prompt(prompt_id)
    return jsonify({"success": True})


@app.route("/api/growth/ai-visibility/config", methods=["POST"])
def api_growth_config():
    """Update AI visibility config."""
    data = request.json or {}
    ai_visibility.update_config(
        brand_keywords=data.get("brand_keywords"),
        site_domain=data.get("site_domain"),
    )
    return jsonify({"success": True})


# ── Keyword Queue Endpoints ──

@app.route("/api/growth/keyword-queue")
def api_keyword_queue():
    """Get keyword queue summary."""
    return jsonify(ai_visibility.get_queue_summary())


@app.route("/api/growth/keyword-queue/import", methods=["POST"])
def api_keyword_queue_import():
    """Import keywords from Ahrefs CSV."""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        if not file.filename or not file.filename.endswith(".csv"):
            return jsonify({"error": "File must be a CSV"}), 400

        raw = file.read()
        for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
            try:
                csv_text = raw.decode(encoding)
                if "keyword" in csv_text.lower()[:500]:
                    break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            csv_text = raw.decode("utf-8", errors="replace")
        if not csv_text.strip():
            return jsonify({"error": "CSV file is empty"}), 400

        filters = {}
        if request.form.get("min_volume"):
            filters["min_volume"] = int(request.form["min_volume"])
        if request.form.get("max_kd"):
            filters["max_kd"] = int(request.form["max_kd"])
        if request.form.get("must_contain"):
            filters["must_contain"] = [t.strip() for t in request.form["must_contain"].split(",") if t.strip()]
        if request.form.get("exclude_terms"):
            filters["exclude_terms"] = [t.strip() for t in request.form["exclude_terms"].split(",") if t.strip()]
        if request.form.get("skip_informational"):
            filters["skip_informational"] = True
        if request.form.get("require_commercial"):
            filters["require_commercial"] = True
        if request.form.get("min_cpc"):
            filters["min_cpc"] = float(request.form["min_cpc"])
        if request.form.get("require_shopping"):
            filters["require_shopping"] = True
        if request.form.get("only_ai_overview"):
            filters["only_ai_overview"] = True
        if request.form.get("min_competitors"):
            filters["min_competitors"] = int(request.form["min_competitors"])

        result = ai_visibility.import_keywords_csv(csv_text, filters)
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Import failed: {str(e)}"}), 500


@app.route("/api/growth/keyword-queue/activate", methods=["POST"])
def api_keyword_queue_activate():
    """Activate the next batch of keywords (add as prompts)."""
    result = ai_visibility.activate_next_batch()
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/growth/keyword-queue/process", methods=["POST"])
def api_keyword_queue_process():
    """Process batch results — retain mentioned, archive invisible."""
    result = ai_visibility.process_batch_results()
    return jsonify(result)


@app.route("/api/growth/keyword-queue/recheck", methods=["POST"])
def api_keyword_queue_recheck():
    """Reactivate archived keywords due for monthly re-check."""
    result = ai_visibility.reactivate_rechecks()
    return jsonify(result)


@app.route("/api/growth/keyword-queue/settings", methods=["POST"])
def api_keyword_queue_settings():
    """Update queue settings (batch size)."""
    data = request.json or {}
    ai_visibility.update_queue_settings(
        batch_size=data.get("batch_size"),
    )
    return jsonify({"success": True})


@app.route("/api/growth/keyword-queue/delete", methods=["POST"])
def api_keyword_queue_delete():
    """Delete specific keywords from the queue."""
    data = request.json or {}
    keywords = data.get("keywords", [])
    if not keywords:
        return jsonify({"error": "No keywords specified"}), 400
    result = ai_visibility.delete_keywords(keywords)
    return jsonify(result)


@app.route("/api/growth/keyword-queue/activate-selected", methods=["POST"])
def api_keyword_queue_activate_selected():
    """Activate specific selected keywords."""
    data = request.json or {}
    keywords = data.get("keywords", [])
    if not keywords:
        return jsonify({"error": "No keywords specified"}), 400
    result = ai_visibility.activate_selected(keywords)
    return jsonify(result)


@app.route("/api/growth/keyword-queue/clear", methods=["POST"])
def api_keyword_queue_clear():
    """Clear the entire keyword queue."""
    ai_visibility.clear_queue()
    return jsonify({"success": True})


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


def _normalize_url(url):
    """Strip tracking, pagination, and filter params from a URL."""
    # Canonical blog URL: /blog/slug, not /blog/post/slug
    if "/blog/post/" in url:
        url = url.replace("/blog/post/", "/blog/")
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    _STRIP = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "gclsrc", "gbraid", "wbraid", "dclid",
        "fbclid", "msclkid", "twclid", "mc_cid", "mc_eid", "ref", "source",
        "p", "page", "pg", "start", "offset",
        "product_list_limit", "limit", "product_list_order", "product_list_dir",
        "product_list_mode", "order", "dir", "sort", "sortby", "sort_by",
        "mode", "view",
    }
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in params.items() if k.lower() not in _STRIP}
    new_query = urlencode(cleaned, doseq=True) if cleaned else ""
    return urlunparse((
        parsed.scheme, parsed.netloc,
        parsed.path.rstrip("/") or "/",
        parsed.params, new_query, "",
    ))


@app.route("/api/admin/import-sitemap", methods=["POST"])
def api_admin_import_sitemap():
    """Fetch sitemap and merge URLs into the evaluation data.

    When the sitemap is a sitemap index with sub-sitemaps (e.g.
    sitemap_products_1.xml, sitemap_categories.xml, sitemap_blogs_1.xml),
    the sub-sitemap filename is used to classify each URL's asset type.
    This is far more reliable than URL-pattern heuristics.
    """
    import xml.etree.ElementTree as ET
    import httpx

    data = request.json or {}
    sitemap_url = data.get("sitemap_url", "").strip()
    if not sitemap_url:
        return jsonify({"error": "sitemap_url required"}), 400

    def _type_from_sitemap_name(sitemap_url_str):
        """Infer asset type from the sub-sitemap filename.

        Common patterns across e-commerce platforms:
          sitemap_products_1.xml   → product
          sitemap_categories.xml   → category
          sitemap_blogs_1.xml      → blog
          sitemap_pages.xml        → other
          sitemap_collections_1.xml → category
        """
        name = sitemap_url_str.rsplit("/", 1)[-1].lower()
        # Remove .xml extension and "sitemap_" prefix
        name = name.replace(".xml", "").replace("sitemap_", "").replace("sitemap-", "")
        # Strip trailing numbers (e.g. "products_1" → "products")
        parts = name.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            name = parts[0]

        if any(kw in name for kw in ("product",)):
            return "product"
        elif any(kw in name for kw in ("categor", "collection")):
            return "category"
        elif any(kw in name for kw in ("blog", "article", "post")):
            return "blog"
        elif any(kw in name for kw in ("page", "cms", "other")):
            return "other"
        return None  # Unknown — will fall back to URL classifier

    def fetch_sitemap_urls(url, depth=0, parent_type=None):
        """Recursively fetch URLs from sitemap (handles sitemap index).

        Returns list of (url, asset_type) tuples.
        """
        if depth > 3:
            return []
        try:
            resp = httpx.get(url, timeout=30.0, follow_redirects=True)
            if resp.status_code != 200:
                return []
            root = ET.fromstring(resp.text)
            # Strip namespace for easier tag matching
            ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
            results = []
            # Check for sitemap index
            for sitemap in root.findall(f"{ns}sitemap"):
                loc = sitemap.find(f"{ns}loc")
                if loc is not None and loc.text:
                    sub_url = loc.text.strip()
                    sub_type = _type_from_sitemap_name(sub_url)
                    results.extend(fetch_sitemap_urls(sub_url, depth + 1, parent_type=sub_type))
            # Regular sitemap URLs (normalize to strip pagination params)
            for url_tag in root.findall(f"{ns}url"):
                loc = url_tag.find(f"{ns}loc")
                if loc is not None and loc.text:
                    results.append((_normalize_url(loc.text.strip()), parent_type))
            return results
        except Exception as e:
            if depth == 0:
                raise e
            return []

    try:
        url_type_pairs = fetch_sitemap_urls(sitemap_url)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch sitemap: {e}"}), 400

    if not url_type_pairs:
        return jsonify({"error": "No URLs found in sitemap. Check the URL."}), 400

    # Build sitemap type map (URL → asset_type) for all URLs with a known type.
    # Save this so the evaluation workflow can use it as the primary classifier.
    sitemap_type_map = {}
    for page_url, stype in url_type_pairs:
        if stype:
            # Safety net: /blog/ or /article/ URLs should always be "blog"
            # even if the sub-sitemap name didn't indicate blog content.
            if stype == "other":
                from urllib.parse import urlparse as _up2
                _path = _up2(page_url.lower()).path
                if "/blog" in _path or "/article" in _path:
                    stype = "blog"
            sitemap_type_map[page_url] = stype

    if sitemap_type_map:
        type_map_path = DATA_PATH / "sitemap_types.json"
        # Merge with existing map (don't lose types from previous imports)
        existing_map = {}
        if type_map_path.exists():
            with open(type_map_path) as f:
                existing_map = json.load(f)
        existing_map.update(sitemap_type_map)
        with open(type_map_path, "w") as f:
            json.dump(existing_map, f, indent=2)
            f.write("\n")

    # Fall back to URL-pattern classifier for URLs without a sitemap-derived type
    def classify_url_fallback(url):
        from urllib.parse import urlparse
        from pathlib import Path as _P
        parsed = urlparse(url.lower())
        path = parsed.path.rstrip("/")

        # Non-page resources (images, fonts, scripts, etc.)
        _MEDIA_EXTS = {
            ".jpeg", ".jpg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
            ".pdf", ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
            ".mp4", ".webm", ".mp3", ".ogg", ".zip", ".gz",
        }
        if _P(path).suffix.lower() in _MEDIA_EXTS:
            return "other"

        if "/blog" in path or "/article" in path:
            return "blog"
        if "/faq" in path or "/help" in path:
            return "other"
        if "/product" in path or "/p/" in path:
            return "product"
        if "/category" in path or "/c/" in path or "/collections" in path:
            return "category"
        if not path or path == "/":
            return "category"

        slug = path.split("/")[-1]
        slug_no_ext = slug.rsplit(".", 1)[0] if "." in slug else slug

        _utility = {"faq", "faqs", "about", "about-us", "contact", "contact-us",
                     "return-policy", "privacy-policy", "terms-of-service", "terms",
                     "shipping", "shipping-policy", "price-match-policy", "testimonials",
                     "reviews", "sitemap", "search", "cart", "checkout", "account",
                     "login", "register", "wishlist", "gift-cards", "gift-certificates"}
        if slug_no_ext in _utility or "policy" in slug_no_ext:
            return "other"

        _cat_slugs = {"all-products", "featured-products", "latest-products",
                      "new-arrivals", "best-sellers", "sale", "clearance",
                      "shop-all", "shop-by", "made-in-usa-montessori-toys"}
        if slug_no_ext in _cat_slugs:
            return "category"

        cfg = load_config()
        families = cfg.get("business_context", {}).get("product_families", [])
        word_count = len(slug_no_ext.split("-"))
        starts_with_digit = slug_no_ext[0].isdigit() if slug_no_ext else False
        for fam in families:
            fam_slug = fam.lower().strip().replace(" ", "-")
            if fam_slug.endswith("s"):
                fam_slug = fam_slug[:-1]
            if fam_slug in slug_no_ext:
                family_words = len(fam_slug.split("-"))
                max_words = family_words + 2
                if not starts_with_digit and word_count <= max_words:
                    return "category"

        if path.count("/") <= 1:
            return "product"
        return "other"

    # Load existing evaluation data
    eval_path = DATA_PATH / "latest_evaluation.json"
    if eval_path.exists():
        with open(eval_path) as f:
            eval_data = json.load(f)
    else:
        eval_data = {"results": [], "metadata": {}}

    # Build lookup of existing results by URL for fast update
    existing_by_url = {r["url"]: r for r in eval_data.get("results", [])}

    # Default confidence for sitemap-imported pages.  These are confirmed
    # real pages (present in sitemap), so they deserve at least the minimum
    # action confidence.  Without this, they'd be blocked by the AI
    # recommend endpoint's confidence gate.
    cfg = load_config()
    default_confidence = cfg.get("governance", {}).get("exploration_confidence_threshold", 0.55)

    new_count = 0
    updated_count = 0
    for page_url, sitemap_type in url_type_pairs:
        asset_type = sitemap_type or classify_url_fallback(page_url)

        if page_url in existing_by_url:
            # Update asset_type on existing entries (sitemap is source of truth)
            if sitemap_type and existing_by_url[page_url].get("asset_type") != asset_type:
                existing_by_url[page_url]["asset_type"] = asset_type
                updated_count += 1
            continue

        eval_data["results"].append({
            "url": page_url,
            "asset_type": asset_type,
            "recommended_action": "VISIBILITY_FIX",
            "mode": "OPPORTUNITY_DISCOVERY",
            "expected_value": 0,
            "confidence": default_confidence,
            "action_confidence": default_confidence,
            "priority_score": 0,
            "risk_level": "low",
            "implementation_steps": [
                "Check if page is indexed (use URL Inspection in GSC)",
                "Verify page has internal links from category/navigation",
                "Review title tag, meta description, and on-page content",
            ],
            "primary_constraint": "coverage_gap",
            "constraints": [{
                "constraint_type": "coverage_gap",
                "severity": "medium",
                "description": "Page exists in sitemap but has no organic search data. May need indexing help or internal links.",
                "evidence": {"source": "sitemap_import"},
                "recommended_action": "Audit page content and internal linking",
            }],
            "demand_score": 0,
            "intent_score": 0,
            "visibility_score": 0,
            "data_confidence": 0.3,
            "source": "sitemap_import",
            "page_metadata": {"has_crawl_data": False},
        })
        new_count += 1

    # Recalculate summary fields after merge
    all_results = eval_data.get("results", [])
    eval_data["total_pages"] = len(all_results)
    eval_data["pages_with_action"] = sum(
        1 for r in all_results
        if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
    )
    eval_data["action_rate"] = round(
        eval_data["pages_with_action"] / max(len(all_results), 1) * 100, 1
    )
    eval_data["total_expected_value"] = round(
        sum(r.get("expected_value", 0) for r in all_results), 2
    )
    eval_data["timestamp"] = datetime.now().isoformat()

    # Save updated evaluation data
    with open(eval_path, "w") as f:
        json.dump(eval_data, f, indent=2)
        f.write("\n")

    # Summary of types from sitemap
    type_counts = {}
    for _, stype in url_type_pairs:
        t = stype or "unknown"
        type_counts[t] = type_counts.get(t, 0) + 1

    return jsonify({
        "imported": len(url_type_pairs),
        "new": new_count,
        "existing": len(url_type_pairs) - new_count,
        "updated_types": updated_count,
        "type_breakdown": type_counts,
        "urls": [
            {"url": u, "type": t or "unknown"}
            for u, t in url_type_pairs
        ],
    })


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

            # Extract raw body HTML for full-page structural analysis
            # (HTMLIssueEvaluator needs tags intact to detect span CTAs, empty media links, etc.)
            import re as _re_fetch
            _body_match = _re_fetch.search(
                r'<body[^>]*>(.*)</body>', response.text,
                _re_fetch.DOTALL | _re_fetch.IGNORECASE,
            )
            body_html = _body_match.group(1)[:100_000] if _body_match else ""
            print(f"[FETCH-DEBUG] body_html extracted: {len(body_html)} chars (match found: {_body_match is not None})")
            print(f"[FETCH-DEBUG] response.text length: {len(response.text)}, has <body>: {'<body' in response.text.lower()}")

            metadata = {
                "title": parser.title.strip(),
                "h1": parser.h1.strip(),
                "meta_description": parser.meta_description.strip(),
                "canonical_url": canonical,
                "word_count": parser.get_word_count(),
                "content_preview": parser.get_content_preview(200),
                "robots_meta": parser.meta_robots.strip(),
                "above_fold_html": parser.get_above_fold_html(),
                "body_html": body_html,
                "internal_outlinks": parser.get_internal_outlinks(),
                "breadcrumb_links": parser.get_breadcrumb_links(),
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

    job_state.update({
        "running": True,
        "type": "evaluation",
        "error": None,
        "message": "Starting evaluation...",
        "progress": 0,
        "total": 0,
    })

    def run_workflow():
        import traceback
        try:

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

            # Step 1: Load data (GSC + GA4 API calls — typically 2-5 min for large sites)
            job_state["message"] = f"Loading data from GSC + GA4 (28 days)... this takes a few minutes"
            workflow.load_data(days=28)
            job_state["total"] = len(workflow._assets)
            job_state["message"] = f"Loaded {len(workflow._assets)} pages from GSC + GA4"

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

                                    # Extract raw body HTML for structural analysis
                                    import re as _re_batch
                                    _bm = _re_batch.search(
                                        r'<body[^>]*>(.*)</body>', response.text,
                                        _re_batch.DOTALL | _re_batch.IGNORECASE,
                                    )
                                    _batch_body = _bm.group(1)[:100_000] if _bm else ""

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
                                        body_html=_batch_body,
                                        internal_outlinks=parser.get_internal_outlinks(),
                                        breadcrumb_links=parser.get_breadcrumb_links(),
                                        schema_types=parser.get_schema_types(),
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
                            if completed % 20 == 0 or completed == len(urls):
                                job_state.update({
                                    "progress": completed,
                                    "message": f"Crawling... {completed}/{len(urls)}",
                                })
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

            # Save snapshot for growth tracking
            try:
                eval_file = DATA_PATH / "latest_evaluation.json"
                if eval_file.exists():
                    EVAL_HISTORY_PATH.mkdir(parents=True, exist_ok=True)
                    snapshot_name = datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".json"
                    import shutil
                    shutil.copy2(eval_file, EVAL_HISTORY_PATH / snapshot_name)
            except OSError:
                pass

            job_state["message"] = "Evaluation complete!"
            job_state["progress"] = job_state["total"]

        except Exception as e:
            tb = traceback.format_exc()
            job_state["error"] = f"{e}\n\nTraceback:\n{tb}"
            job_state["message"] = f"Error: {e}"
            print(f"Evaluation error: {e}\n{tb}")  # Also log to Flask console
        finally:
            from datetime import datetime
            job_state["finished_at"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
            job_state["running"] = False

    # Run in background thread
    thread = threading.Thread(target=run_workflow)
    thread.start()

    return jsonify({
        "message": "Evaluation started",
        "status": "running",
        "crawl": run_crawl,
    })


@app.route("/api/ai/batch-estimate", methods=["POST"])
def api_ai_batch_estimate():
    """Estimate cost and page count for batch AI analysis without running it."""
    eval_path = DATA_PATH / "latest_evaluation.json"
    if not eval_path.exists():
        return jsonify({"error": "No evaluation data. Run evaluation first."}), 400

    with open(eval_path) as f:
        eval_data = json.load(f)

    results_list = eval_data.get("results", [])
    data = request.json or {}
    skip_analyzed = data.get("skip_analyzed", True)
    page_types = data.get("page_types", [])  # e.g. ["product", "category"]

    total_pages = len(results_list)
    actionable = [
        opp for opp in results_list
        if opp.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
    ]

    # Filter by page type if specified
    if page_types:
        actionable = [
            opp for opp in actionable
            if (opp.get("asset_type") or "other") in page_types
        ]

    already_analyzed = [opp for opp in actionable if opp.get("ai_revised_value")]
    to_analyze = [opp for opp in actionable if not opp.get("ai_revised_value")] if skip_analyzed else actionable

    # Cost estimate: use actual max_tokens from config for output,
    # and estimate input based on prompt content settings
    config = load_config()
    ai_config = config.get("ai", {})
    model = ai_config.get("model", "gpt-4o-mini")
    max_tokens = ai_config.get("max_tokens", 4500)
    max_queries = ai_config.get("max_queries_in_prompt", 0)

    # Per-million-token pricing
    cost_table = {
        "claude-fable-5": (10.0, 50.0),
        "claude-opus-4-8": (5.0, 25.0),
        "claude-opus-4-7": (5.0, 25.0),
        "claude-opus-4-6": (5.0, 25.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
        "gpt-4o": (2.50, 10.0),
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4.1": (2.0, 8.0),
        "gpt-4.1-mini": (0.40, 1.60),
    }
    input_price, output_price = cost_table.get(model, (3.0, 15.0))

    # Input estimate: ~4K base prompt + ~2K page metadata + ~150 per query
    est_input_tokens = 6000 + (max_queries * 150)
    # Output estimate: actual max_tokens cap (models rarely hit 100%, use 90%)
    est_output_tokens = int(max_tokens * 0.9)

    cost_per_page = (est_input_tokens * input_price + est_output_tokens * output_price) / 1_000_000
    total_cost = cost_per_page * len(to_analyze)

    # Build per-type breakdown for all actionable (before skip_analyzed filter)
    all_actionable = [
        opp for opp in results_list
        if opp.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
    ]
    type_counts = {}
    for opp in all_actionable:
        t = opp.get("asset_type") or "other"
        if t not in type_counts:
            type_counts[t] = {"total": 0, "analyzed": 0, "to_analyze": 0}
        type_counts[t]["total"] += 1
        if opp.get("ai_revised_value"):
            type_counts[t]["analyzed"] += 1
        else:
            type_counts[t]["to_analyze"] += 1

    return jsonify({
        "total_pages": total_pages,
        "actionable": len(actionable),
        "already_analyzed": len(already_analyzed),
        "to_analyze": len(to_analyze),
        "model": model,
        "cost_per_page": round(cost_per_page, 3),
        "estimated_cost": round(total_cost, 2),
        "type_counts": type_counts,
    })


@app.route("/api/ai/batch-analyze", methods=["POST"])
def api_ai_batch_analyze():
    """Run fetch + AI analysis on all opportunities sequentially."""
    global job_state

    if job_state["running"]:
        return jsonify({"error": "A job is already running", "status": "busy"}), 400

    # Load opportunities from evaluation data
    eval_path = DATA_PATH / "latest_evaluation.json"
    if not eval_path.exists():
        return jsonify({"error": "No evaluation data. Run evaluation first."}), 400

    with open(eval_path) as f:
        eval_data = json.load(f)

    results_list = eval_data.get("results", [])
    if not results_list:
        return jsonify({"error": "No opportunities found in evaluation data."}), 400

    data = request.json or {}
    skip_analyzed = data.get("skip_analyzed", True)
    page_types = data.get("page_types", [])  # e.g. ["product", "category"]
    selected_urls = set(data.get("urls", []))  # specific URLs to analyze

    # Set running state BEFORE starting the thread — single write to file
    job_state.update({
        "running": True,
        "type": "batch_ai",
        "error": None,
        "message": "Starting AI analysis...",
        "progress": 0,
        "total": 0,
    })

    def run_batch():
        import traceback
        try:

            # Re-read fresh data inside thread
            with open(eval_path) as f:
                fresh_data = json.load(f)
            opps = fresh_data.get("results", [])

            # Filter to opportunities worth analyzing (skip NO_ACTION only;
            # include OBSERVE_ONLY so they get AI estimates too)
            actionable = [
                opp for opp in opps
                if opp.get("recommended_action") not in ("NO_ACTION", None)
            ]

            # Skip URLs that already have an active task on the board
            ledger = ActionLedger()
            active_urls = set()
            for st in (ActionStatus.PROPOSED, ActionStatus.APPROVED,
                       ActionStatus.IMPLEMENTED, ActionStatus.MEASURED):
                for act in ledger.get_actions_by_status(st):
                    active_urls.add(act.url)
            before_count = len(actionable)
            actionable = [
                opp for opp in actionable
                if opp.get("url", "") not in active_urls
            ]
            skipped_active = before_count - len(actionable)

            # Filter by specific URLs if provided
            if selected_urls:
                actionable = [
                    opp for opp in actionable
                    if opp.get("url", "") in selected_urls
                ]

            # Filter by page type if specified
            if page_types:
                actionable = [
                    opp for opp in actionable
                    if (opp.get("asset_type") or "other") in page_types
                ]

            # Optionally skip already-analyzed
            if skip_analyzed:
                actionable = [
                    opp for opp in actionable
                    if not opp.get("ai_revised_value")
                ]

            job_state["total"] = len(actionable)
            job_state["progress"] = 0

            if not actionable:
                job_state["message"] = "All actionable opportunities already analyzed."
                return

            analyzed = 0
            errors = 0

            for i, opp in enumerate(actionable):
                url = opp.get("url", "")
                short_url = url.replace("https://", "").replace("http://", "")
                if len(short_url) > 50:
                    short_url = short_url[:47] + "..."

                # Step 1: Fetch page metadata if missing
                pm = opp.get("page_metadata", {})
                if not pm.get("has_crawl_data"):
                    job_state["message"] = f"[{i+1}/{len(actionable)}] Fetching {short_url}..."
                    try:
                        with app.test_client() as client:
                            resp = client.post(
                                "/api/page-metadata",
                                json={"url": url},
                                content_type="application/json",
                            )
                            if resp.status_code == 200:
                                pm = resp.get_json()
                                opp["page_metadata"] = pm
                    except Exception as e:
                        job_state["message"] = f"[{i+1}/{len(actionable)}] Fetch failed for {short_url}: {e}"
                        errors += 1
                        job_state["progress"] = i + 1
                        continue

                # Step 2: Check SERP data availability
                serp_summary = serp_client.get_serp_summary_for_opportunity(opp)
                if not serp_summary or not serp_summary.get("serp_results"):
                    job_state["message"] = f"[{i+1}/{len(actionable)}] Skipping {short_url} — no SERP data yet"
                    job_state["progress"] = i + 1
                    continue

                # Step 3: Run AI analysis
                job_state["message"] = f"[{i+1}/{len(actionable)}] Analyzing {short_url}..."
                try:
                    with app.test_client() as client:
                        resp = client.post(
                            "/api/ai/recommend",
                            json={"url": url, "opportunity": opp, "force_analysis": True},
                            content_type="application/json",
                        )
                        if resp.status_code == 200:
                            ai_data = resp.get_json()
                            if ai_data.get("success"):
                                rv = ai_data.get("ai_revised_value")
                                analyzed += 1
                                if rv is not None:
                                    job_state["message"] = (
                                        f"[{i+1}/{len(actionable)}] {short_url} → "
                                        f"AI Est: ${rv:,.2f}"
                                    )
                            else:
                                err_msg = ai_data.get("error", "Unknown error")
                                job_state["message"] = f"[{i+1}/{len(actionable)}] {short_url}: {err_msg}"
                                errors += 1
                        else:
                            errors += 1
                except Exception as e:
                    job_state["message"] = f"[{i+1}/{len(actionable)}] AI failed for {short_url}: {e}"
                    errors += 1

                job_state["progress"] = i + 1

            # Compute AI total from persisted data
            with open(eval_path) as f:
                final_data = json.load(f)
            ai_total = sum(
                r.get("ai_revised_value", 0)
                for r in final_data.get("results", [])
                if r.get("ai_revised_value") is not None
            )
            ai_count = sum(
                1 for r in final_data.get("results", [])
                if r.get("ai_revised_value") is not None
            )

            summary = f"Batch complete: {analyzed} analyzed"
            if skipped_active:
                summary += f", {skipped_active} skipped (active tasks)"
            if errors:
                summary += f", {errors} errors"
            summary += f". AI Est. Total: ${ai_total:,.2f} ({ai_count} pages)"
            job_state["message"] = summary

        except Exception as e:
            tb = traceback.format_exc()
            job_state["error"] = f"{e}\n\nTraceback:\n{tb}"
            job_state["message"] = f"Error: {e}"
        finally:
            from datetime import datetime
            job_state["finished_at"] = datetime.now().strftime("%b %d, %Y %I:%M %p")
            job_state["running"] = False

    thread = threading.Thread(target=run_batch)
    thread.start()

    return jsonify({
        "message": "Batch AI analysis started",
        "status": "running",
    })


@app.route("/api/internal-link-map")
def api_internal_link_map():
    """Build internal link map: for each page, show inbound links, outbound links, and suggestions."""
    eval_path = DATA_PATH / "latest_evaluation.json"

    if not eval_path.exists():
        return jsonify({"pages": [], "message": "No evaluation data. Run evaluation with crawl first."})

    with open(eval_path) as f:
        eval_data = json.load(f)

    results = eval_data.get("results", [])
    if not results:
        return jsonify({"pages": [], "message": "Evaluation has no page results."})

    # ── URL normalization (same logic used in AI recommend) ──
    from urllib.parse import urlparse, urljoin

    def _norm(u, base_url=""):
        if not u:
            return ""
        if not u.startswith(("http://", "https://")):
            if base_url:
                u = urljoin(base_url, u)
            else:
                u = urljoin("https://alphabet-trains.com/", u)
        parsed = urlparse(u.lower())
        netloc = parsed.netloc.replace("www.", "")
        path = parsed.path.rstrip("/") or "/"
        return f"{netloc}{path}"

    # Stop words — only true grammatical filler with zero topical signal.
    _STOP = {
        "for", "the", "and", "with", "how", "what", "why", "are",
        "can", "from", "that", "this", "your", "our", "all", "has",
        "its", "you", "was", "get", "not", "but", "will", "more",
        "buy", "shop", "free", "shipping", "sale", "price",
        "online", "store", "review", "reviews",
        "best", "top", "new", "usa", "2024", "2025", "2026",
    }

    # ── Pass 1: Index all pages and their outlinks ──
    page_index = {}  # norm_url -> page info
    for r in results:
        url = r.get("url", "")
        norm = _norm(url)
        if not norm:
            continue
        pm = r.get("page_metadata", {})
        title = pm.get("title", "") or pm.get("h1", "") or url
        outlinks_raw = pm.get("internal_outlinks", [])

        # Collect query words for this page (excluding stop words)
        query_words = set()
        top_queries = r.get("top_queries", [])
        for q in top_queries:
            for word in q.get("query", "").lower().split():
                if len(word) > 2 and word not in _STOP:
                    query_words.add(word)

        # Breadcrumb links for parent category detection
        breadcrumb_raw = pm.get("breadcrumb_links", [])
        breadcrumb_norms = []
        for bl in breadcrumb_raw:
            bl_norm = _norm(bl.get("target_url", ""), url)
            if bl_norm and bl_norm != norm:
                breadcrumb_norms.append(bl_norm)

        page_index[norm] = {
            "url": url,
            "norm": norm,
            "title": title[:100],
            "asset_type": r.get("asset_type", "other"),
            "gsc_impressions": r.get("gsc_impressions", 0) or 0,
            "gsc_clicks": r.get("gsc_clicks", 0) or 0,
            "gsc_position": round(r.get("gsc_position", 0) or 0, 1),
            "top_queries": [q.get("query", "") for q in top_queries[:5]],
            "query_words": query_words,
            "outlinks": [],       # pages this page links TO
            "inlinks": [],        # pages that link TO this page
            "suggested": [],      # pages that should link to this page
            "breadcrumb_parents": breadcrumb_norms,  # parent pages from breadcrumb nav
        }

        # Record outlinks
        for ol in outlinks_raw:
            target = ol.get("target_url", "")
            target_norm = _norm(target, url)
            if target_norm and target_norm != norm:
                page_index[norm]["outlinks"].append({
                    "url": target,
                    "norm": target_norm,
                    "anchor_text": ol.get("anchor_text", ""),
                })

    # ── Pass 2: Build reverse index (inlinks) ──
    # Deduplicate by (source_norm, anchor_text) to avoid counting
    # the same link twice. Also deduplicate by title to collapse
    # duplicate pages at different URL paths (e.g. /blog/post/X and /blog/X).
    for norm, page in page_index.items():
        for ol in page["outlinks"]:
            target_norm = ol["norm"]
            if target_norm in page_index:
                target_inlinks = page_index[target_norm]["inlinks"]
                # Skip if same source page norm + anchor already recorded
                dedup_key = (norm, ol["anchor_text"])
                existing_keys = {(il["norm"], il["anchor_text"]) for il in target_inlinks}
                if dedup_key in existing_keys:
                    continue
                # Skip if a page with the same title already links with the same anchor
                # (catches /blog/post/X vs /blog/X duplicates)
                title_anchor_key = (page["title"], ol["anchor_text"])
                existing_title_keys = {(il["title"], il["anchor_text"]) for il in target_inlinks}
                if title_anchor_key in existing_title_keys:
                    continue
                target_inlinks.append({
                    "url": page["url"],
                    "norm": norm,
                    "anchor_text": ol["anchor_text"],
                    "title": page["title"],
                })

    # ── Pass 2b: Detect parent categories from breadcrumbs ──
    # Each page's breadcrumb_links tell us its parent pages in the hierarchy.
    # The last breadcrumb link (before the current page) is the immediate parent.
    # If a product's parent is a category and the product doesn't have a
    # content link back to it, we suggest adding one.
    parent_categories = {}  # page_norm -> list of parent category norms
    for norm, page in page_index.items():
        for parent_norm in page.get("breadcrumb_parents", []):
            if parent_norm in page_index:
                parent_page = page_index[parent_norm]
                if parent_page["asset_type"] == "category":
                    parent_categories.setdefault(norm, []).append(parent_norm)

    # ── Pass 3: Compute suggestions for each page ──
    # For each page, find other pages with query overlap that
    # do NOT already link to it. Rank by linking_score.
    import math
    for norm, page in page_index.items():
        # Pages that already link to this page
        inlink_norms = set(il["norm"] for il in page["inlinks"])
        # Pages this page already links to (content outlinks)
        outlink_norms = set(ol["norm"] for ol in page["outlinks"])

        # Inject missing parent category links for product pages
        parent_suggestions = []
        if page["asset_type"] == "product" and norm in parent_categories:
            for cat_norm in parent_categories[norm]:
                if cat_norm not in outlink_norms:
                    cat_page = page_index[cat_norm]
                    parent_suggestions.append({
                        "url": cat_page["url"],
                        "title": cat_page["title"],
                        "asset_type": "category",
                        "impressions": cat_page["gsc_impressions"],
                        "position": cat_page["gsc_position"],
                        "query_overlap": -1,
                        "linking_score": 999999,  # Always rank first
                        "shared_queries": ["PARENT CATEGORY"],
                        "reason": "parent_category",
                    })

        candidates = []
        for other_norm, other_page in page_index.items():
            if other_norm == norm:
                continue
            if other_norm in inlink_norms:
                continue  # Already links to this page

            # Query overlap — require at least 2 shared terms after stop word
            # filtering. A single shared word (e.g. "puzzle") is too generic.
            overlap = len(page["query_words"] & other_page["query_words"])
            if overlap < 2:
                continue  # Not enough topical relevance

            # linking_score — overlap dominates (squared), impressions dampened by log.
            # Formula: overlap² × (11 - position) × log2(1 + impressions)
            pos = other_page["gsc_position"]
            pos_factor = max(1, 11 - pos) if pos > 0 else 1
            impr = other_page["gsc_impressions"]
            overlap_sq = overlap ** 2
            impr_log = math.log2(1 + impr) if impr > 0 else 0.1
            linking_score = overlap_sq * pos_factor * impr_log

            candidates.append({
                "url": other_page["url"],
                "title": other_page["title"],
                "asset_type": other_page["asset_type"],
                "impressions": impr,
                "position": pos,
                "query_overlap": overlap,
                "linking_score": round(linking_score, 1),
                "shared_queries": list(page["query_words"] & other_page["query_words"])[:5],
            })

        # Sort by linking_score descending, show up to 20
        candidates.sort(key=lambda c: c["linking_score"], reverse=True)
        # Parent category suggestions always come first
        page["suggested"] = parent_suggestions + candidates[:20]

    # ── Build response ──
    pages_out = []
    for norm, page in page_index.items():
        pages_out.append({
            "url": page["url"],
            "title": page["title"],
            "asset_type": page["asset_type"],
            "gsc_impressions": page["gsc_impressions"],
            "gsc_clicks": page["gsc_clicks"],
            "gsc_position": page["gsc_position"],
            "top_queries": page["top_queries"],
            "inlinks_count": len(page["inlinks"]),
            "outlinks_count": len(page["outlinks"]),
            "inlinks": [
                {"url": il["url"], "anchor_text": il["anchor_text"], "title": il["title"]}
                for il in page["inlinks"]
            ],
            "outlinks": [
                {"url": ol["url"], "anchor_text": ol["anchor_text"]}
                for ol in page["outlinks"]
            ],
            "suggested": page["suggested"],
        })

    # Sort by impressions descending (most important pages first)
    pages_out.sort(key=lambda p: p["gsc_impressions"], reverse=True)

    return jsonify({
        "pages": pages_out,
        "total": len(pages_out),
        "timestamp": eval_data.get("timestamp", ""),
    })


# ============================================================
# CONTENT MANAGEMENT API
# ============================================================

@app.route("/api/content/dashboard")
def api_content_dashboard():
    """Get content strategy dashboard data."""
    try:
        return jsonify(content_manager.get_dashboard())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/content/discover", methods=["POST"])
def api_content_discover():
    """Auto-discover content from site data."""
    try:
        result = content_manager.auto_discover()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/content/rediscover", methods=["POST"])
def api_content_rediscover():
    """Clear all content data and re-discover from scratch."""
    try:
        result = content_manager.rediscover()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/content/clusters")
def api_content_clusters():
    """Get all clusters."""
    return jsonify({"clusters": content_manager.get_clusters()})


@app.route("/api/content/clusters", methods=["POST"])
def api_content_create_cluster():
    """Create a new cluster."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    cluster = content_manager.create_cluster(
        name=name,
        description=data.get("description", ""),
        priority=data.get("priority", "medium"),
        target_articles=data.get("target_articles", 20),
    )
    return jsonify(cluster)


@app.route("/api/content/clusters/<cluster_id>", methods=["PUT"])
def api_content_update_cluster(cluster_id):
    """Update a cluster."""
    updates = request.get_json(force=True)
    result = content_manager.update_cluster(cluster_id, updates)
    if result is None:
        return jsonify({"error": "Cluster not found"}), 404
    return jsonify(result)


@app.route("/api/content/clusters/<cluster_id>", methods=["DELETE"])
def api_content_delete_cluster(cluster_id):
    """Delete a cluster."""
    content_manager.delete_cluster(cluster_id)
    return jsonify({"ok": True})


@app.route("/api/content/articles")
def api_content_articles():
    """Get articles, optionally filtered by cluster or status."""
    cluster_id = request.args.get("cluster_id")
    status = request.args.get("status")
    return jsonify({"articles": content_manager.get_articles(cluster_id=cluster_id, status=status)})


@app.route("/api/content/articles", methods=["POST"])
def api_content_create_article():
    """Create a new article."""
    data = request.get_json(force=True)
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400
    article = content_manager.create_article(
        title=title,
        cluster_id=data.get("cluster_id"),
        **{k: v for k, v in data.items() if k not in ("title", "cluster_id")},
    )
    return jsonify(article)


@app.route("/api/content/articles/<article_id>", methods=["PUT"])
def api_content_update_article(article_id):
    """Update an article."""
    updates = request.get_json(force=True)
    result = content_manager.update_article(article_id, updates)
    if result is None:
        return jsonify({"error": "Article not found"}), 404
    return jsonify(result)


@app.route("/api/content/articles/<article_id>", methods=["DELETE"])
def api_content_delete_article(article_id):
    """Delete an article."""
    content_manager.delete_article(article_id)
    return jsonify({"ok": True})


@app.route("/api/content/money-pages")
def api_content_money_pages():
    """Get money pages, optionally filtered by type (category/product)."""
    page_type = request.args.get("type")
    return jsonify({"money_pages": content_manager.get_money_pages(page_type)})


@app.route("/api/content/products")
def api_content_products():
    """Get all products."""
    return jsonify({"products": content_manager.get_products()})


@app.route("/api/content/pipeline")
def api_content_pipeline():
    """Get articles grouped by status for kanban view."""
    try:
        return jsonify(content_manager.get_pipeline())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/content/articles/bulk", methods=["POST"])
def api_content_bulk_update():
    """Bulk update multiple articles."""
    data = request.get_json(force=True)
    article_ids = data.get("article_ids", [])
    updates = data.get("updates", {})
    if not article_ids or not updates:
        return jsonify({"error": "article_ids and updates are required"}), 400
    result = content_manager.bulk_update_articles(set(article_ids), updates)
    return jsonify(result)


@app.route("/api/content/clusters/<cluster_id>/sub-clusters", methods=["POST"])
def api_content_add_sub_cluster(cluster_id):
    """Add a sub-cluster to a cluster."""
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    result = content_manager.add_sub_cluster(cluster_id, name)
    if result is None:
        return jsonify({"error": "Cluster not found"}), 404
    return jsonify(result)


@app.route("/api/content/clusters/<cluster_id>/sub-clusters/<sc_id>", methods=["DELETE"])
def api_content_delete_sub_cluster(cluster_id, sc_id):
    """Delete a sub-cluster."""
    if content_manager.delete_sub_cluster(cluster_id, sc_id):
        return jsonify({"ok": True})
    return jsonify({"error": "Cluster not found"}), 404


@app.route("/api/content/gaps")
def api_content_gaps():
    """Get content gap analysis."""
    try:
        return jsonify(content_manager.get_content_gaps())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/content/suggestions")
def api_content_suggestions():
    """Get content topic suggestions for underserved clusters."""
    cluster_id = request.args.get("cluster_id")
    try:
        return jsonify(content_manager.get_content_suggestions(cluster_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/content/money-pages/<mp_id>")
def api_content_money_page_detail(mp_id):
    """Get money page with linked articles."""
    result = content_manager.get_money_page_detail(mp_id)
    if result is None:
        return jsonify({"error": "Money page not found"}), 404
    return jsonify(result)


@app.route("/api/content/money-pages/<mp_id>", methods=["PUT"])
def api_content_update_money_page(mp_id):
    """Update a money page."""
    updates = request.get_json(force=True)
    result = content_manager.update_money_page(mp_id, updates)
    if result is None:
        return jsonify({"error": "Money page not found"}), 404
    return jsonify(result)


@app.route("/api/content/money-pages/<mp_id>/link", methods=["POST"])
def api_content_link_article(mp_id):
    """Link an article to a money page."""
    data = request.get_json(force=True)
    article_id = data.get("article_id")
    if not article_id:
        return jsonify({"error": "article_id is required"}), 400
    result = content_manager.link_article_to_money_page(mp_id, article_id)
    if result is None:
        return jsonify({"error": "Money page not found"}), 404
    return jsonify(result)


@app.route("/api/content/money-pages/<mp_id>/unlink", methods=["POST"])
def api_content_unlink_article(mp_id):
    """Unlink an article from a money page."""
    data = request.get_json(force=True)
    article_id = data.get("article_id")
    if not article_id:
        return jsonify({"error": "article_id is required"}), 400
    result = content_manager.unlink_article_from_money_page(mp_id, article_id)
    if result is None:
        return jsonify({"error": "Money page not found"}), 404
    return jsonify(result)


@app.route("/api/job-status")
def api_job_status():
    """Get current job status — reads from shared file for cross-worker visibility."""
    try:
        if JOB_STATE_PATH.exists():
            with open(JOB_STATE_PATH) as f:
                return jsonify(json.load(f))
    except (json.JSONDecodeError, OSError):
        pass
    return jsonify(dict(job_state))


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
# ARTICLE GENERATION (Claude API — SSE Streaming)
# ============================================================

_ARTICLE_SYSTEM_PROMPT = """You are an expert SEO content writer for Alphabet Trains (alphabet-trains.com), a family-owned business selling personalized name trains, name puzzles, step stools, baby books, baby gifts, baby blankets, personalized toys, Montessori toys, classroom rugs, and kids' furniture. Products are made in the USA and built to heirloom quality.

You must follow every phase below IN ORDER. Do not skip any phase. Do not show the phase headings in the final output — they are your internal checklist. The output must be a complete, publish-ready HTML article for Magento.

═══════════════════════════════════════
PHASE 1: INTENT & AUDIENCE
═══════════════════════════════════════
Before writing a single word, determine:
- Search intent: Informational, Commercial Investigation, Transactional, or Navigational.
- Target audience: parents, grandparents, gift shoppers, educators, daycare owners, etc.
- The specific questions the reader wants answered.
- The desired action when the reader finishes (buy a product, explore a category, read more, etc.).

═══════════════════════════════════════
PHASE 2: TOPIC RESEARCH
═══════════════════════════════════════
Identify:
- The primary keyword.
- Secondary and long-tail keywords (weave naturally throughout).
- "People Also Ask" questions to answer within the article.
- Search-intent gaps that competitors are not covering.
- Unique insights that only Alphabet Trains can provide (e.g., manufacturing details, customer stories, product usage observations).

═══════════════════════════════════════
PHASE 3: CONTENT ARCHITECTURE
═══════════════════════════════════════
Structure the article with:
- A compelling hook (first 2–3 sentences that make the reader stay).
- A TL;DR summary box near the top.
- A logical H2/H3 heading structure.
- Comparison or decision tables where applicable.
- Action boxes ("What to do next" callouts).
- An FAQ section using questions from Phase 2.
- A clear CTA at the end.

═══════════════════════════════════════
PHASE 4: E-E-A-T (Experience, Expertise, Authoritativeness, Trustworthiness)
═══════════════════════════════════════
Every article must answer:
- Why should readers trust us?
- What firsthand experience can only Alphabet Trains provide?
- What observations have we made after helping thousands of families?
- What myths or misconceptions can we correct?
- What practical advice can we give that competitors cannot?

═══════════════════════════════════════
PHASE 5: INTERNAL LINKING STRATEGY
═══════════════════════════════════════
Include the following types of internal links:
A. Required links — any links explicitly requested in the prompt (MANDATORY, include every one).
B. Topic-cluster links — supporting blog articles that strengthen the topic cluster.
C. Category links — relevant category pages.
D. Product links — relevant products from the sitemap.
E. Supporting resources — policies, buying guides, FAQs, etc.

═══════════════════════════════════════
PHASE 6: PRODUCT RECOMMENDATION STRATEGY
═══════════════════════════════════════
Every product mentioned must have a reason. Never link products randomly. For every product, ask:
- Why is this product relevant to the reader right now?
- What problem does it solve?
- Who is it best for?
- Is there a better or complementary product to recommend alongside it?

═══════════════════════════════════════
PHASE 7: COMMERCIAL INTENT (NON-SALESY)
═══════════════════════════════════════
Even informational articles should naturally answer:
- Which should I buy?
- When should I buy?
- Which is best?
- What do you recommend?
- What is the value?
- What is the difference between options?

Do this through helpful comparisons, honest recommendations, and practical advice — never through aggressive selling.

═══════════════════════════════════════
PHASE 8: USER EXPERIENCE & VISUAL DESIGN
═══════════════════════════════════════
This is a Magento blog. ALL styling must use INLINE STYLES because Magento strips external CSS classes.
Every article must be visually rich and magazine-quality. Use these exact design patterns:

HERO BANNER (required at top):
<div style="background:linear-gradient(135deg,#COLOR1 0%,#COLOR2 100%);padding:50px 30px;border-radius:15px;color:white;text-align:center;margin-bottom:40px;">
  <h2 style="color:white;margin-bottom:20px;text-shadow:2px 2px 4px rgba(0,0,0,0.2);">Article Title</h2>
  <p style="margin:20px 0;">Subtitle / hook</p>
</div>
Choose gradient colors that match the article topic (warm tones for family/gifts, cool tones for education, nature tones for outdoor topics).

TL;DR BOX (required near top):
<div style="background:#f5f9f0;border-left:4px solid #4a7c3f;padding:16px 20px;margin-bottom:28px;border-radius:4px;">
  <strong>TL;DR &#8212; Quick Summary</strong>
  <ul>...</ul>
</div>

TIP BOX (green):
<div style="background:#e8f5e9;padding:20px;border-radius:10px;margin:20px 0;border-left:4px solid #4caf50;">
  <strong>&#128161; Tip Title:</strong> Content
</div>

WARNING BOX (orange):
<div style="background:#fff3e0;padding:20px;border-radius:10px;margin:20px 0;border-left:4px solid #ff9800;">
  <strong>&#128680; Warning:</strong> Content
</div>

INFO/RESEARCH BOX (yellow):
<div style="background:#fff3cd;padding:25px;border-radius:10px;border-left:5px solid #ffc107;margin:30px 0;">
  <h3 style="margin-top:0;color:#856404;">&#9889; Title</h3>
  <p style="margin-bottom:0;">Content with external reference links</p>
</div>

PRODUCT CALLOUT BOX (gradient, with CTA button):
<div style="background:linear-gradient(135deg,#a8edea 0%,#fed6e3 100%);padding:25px;border-radius:10px;margin:30px 0;text-align:center;">
  <h4>&#127775; Callout Title</h4>
  <p>Why this product matters for the reader right now.</p>
  <a href="https://alphabet-trains.com/CATEGORY.html" style="display:inline-block;background:#4a7c3f;color:white;padding:12px 30px;border-radius:25px;text-decoration:none;font-weight:bold;margin-top:10px;">Shop CTA Text</a>
</div>

BENEFIT GRID (3-column, responsive):
<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin:40px 0;">
  <div style="background:#f8f9ff;padding:25px;border-radius:15px;text-align:center;">
    <div style="margin-bottom:15px;">&#EMOJI;</div>
    <h4>Title</h4>
    <p>Description</p>
  </div>
  <!-- repeat -->
</div>

COMPARISON TABLE:
<table style="width:100%;border-collapse:collapse;margin:30px 0;">
  <thead><tr style="background:#f8f9ff;"><th style="padding:12px;text-align:left;border-bottom:2px solid #667eea;">Header</th>...</tr></thead>
  <tbody><tr style="border-bottom:1px solid #eee;"><td style="padding:12px;">Data</td>...</tr></tbody>
</table>

FINAL CTA BANNER (required near bottom):
<div style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:40px;border-radius:15px;color:white;margin:50px 0;text-align:center;">
  <h2 style="color:white;margin-bottom:20px;">CTA Heading</h2>
  <p style="margin-bottom:25px;">Supporting text</p>
  <a href="URL" style="display:inline-block;background:white;color:#667eea;padding:15px 40px;border-radius:25px;text-decoration:none;font-weight:bold;">Button Text</a>
</div>

FEATURE HIGHLIGHT BOX (dark header):
<div style="border:2px solid #3a2f21;border-radius:14px;overflow:hidden;margin:32px 0;line-height:1.5;color:#3a2f21;">
  <div style="background-color:#3a2f21;color:#ffffff;font-weight:600;padding:12px 20px;">Box Title</div>
  <div style="background-color:#fdfbf5;padding:20px;">Content with links</div>
</div>

KEY QUOTE / PULLOUT:
<p style="margin:40px 0;padding:30px;background:#f0f4ff;border-radius:15px;border-left:5px solid #667eea;">
  Important statement or key takeaway.
</p>

ADDITIONAL RULES:
- Use emoji via decimal HTML entities (&#127775; &#128161; &#128230; &#128680; &#127919; etc.).
- Include image suggestions as HTML comments: <!-- Image: description. ALT: alt text -->
- Scatter 3-5 product callout boxes throughout the article (not just at the end).
- Every CTA button must use the rounded pill style shown above.
- Add responsive media query note as HTML comment at top if using grid layouts.
- Make the article visually engaging — a wall of text with no styled elements is unacceptable.

═══════════════════════════════════════
PHASE 9: SEO REVIEW
═══════════════════════════════════════
Before finishing, verify:
- Title tag (50–60 characters, primary keyword near the front).
- Meta title.
- Meta description (150–160 characters, includes primary keyword and a call to action).
- Suggested URL slug.
- H1 (one per page, matches search intent).
- H2 structure (logical, keyword-rich, scannable).
- Keyword placement (title, first paragraph, H2s, conclusion).
- Semantic keywords woven throughout.
- Internal links placed naturally.
- External references cited where they build trust.
- FAQ schema opportunities flagged.

═══════════════════════════════════════
PHASE 10: EDITORIAL REVIEW
═══════════════════════════════════════
Before outputting, self-edit:
- Remove anything repetitive.
- Remove filler and fluff.
- Shorten sentences that can be shortened.
- Ensure every paragraph is useful.
- Ensure every section deserves to exist.

═══════════════════════════════════════
PHASE 11: ALPHABET TRAINS BRAND REVIEW
═══════════════════════════════════════
Verify:
- Did we leverage our authority as a family-owned, specialty retailer?
- Did we mention "Made in USA" where appropriate and natural?
- Did we mention heirloom quality where appropriate?
- Did we recommend the right products for this topic?
- Did we naturally build topical clusters through internal linking?
- Does this sound like Alphabet Trains wrote it — warm, knowledgeable, parent-to-parent — rather than generic AI?

═══════════════════════════════════════
PHASE 12: FINAL QA CHECKLIST
═══════════════════════════════════════
Before outputting the article, confirm:
✓ Writing prompt followed 100%.
✓ Search intent satisfied.
✓ Reader questions answered.
✓ Competitor gaps filled.
✓ E-E-A-T demonstrated.
✓ All internal links included.
✓ Product links included with reasons.
✓ Topic-cluster links included.
✓ SEO metadata complete.
✓ Valid HTML for Magento.
✓ Grammar checked.
✓ No filler content.
✓ No missing sections.
✓ No additional improvements found.

═══════════════════════════════════════
CRITICAL URL RULES
═══════════════════════════════════════
- NEVER use /blog/post/ in any URL. The correct blog URL format is /blog/slug (e.g., /blog/best-montessori-toys-by-age, NOT /blog/post/best-montessori-toys-by-age).
- All internal links must use full absolute URLs starting with https://alphabet-trains.com/.
- Double-check every link before including it.

═══════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════
Output the article HTML FIRST, then the Magento fields LAST.

START your response IMMEDIATELY with the article HTML — the very first character must be < (an HTML tag). Do NOT start with metadata, comments, notes, or any non-HTML text.

The article HTML must include:
1. Hero banner with gradient at the top
2. TL;DR summary box
3. At least one comparison or decision table
4. 3-5 product callout boxes with CTA buttons scattered throughout
5. Tip boxes and/or warning boxes where relevant
6. A benefit grid (3-column) where appropriate
7. FAQ section (visible Q&A in HTML)
8. Final CTA banner near the bottom
9. Implementation tips or actionable checklist section
10. FAQ schema JSON-LD <script type="application/ld+json"> tag at the very end of the HTML

ALL styling must be inline (style="...") — Magento strips CSS classes.
Use &#NNN; decimal HTML entities for all special characters and emoji.
No <h1> tag — Magento adds it separately.
No <style> blocks — all styles inline.
No HTML comments.
Every internal link must use full absolute URLs starting with https://alphabet-trains.com/.

AFTER the article HTML is complete, output this exact line on its own:
---MAGENTO-FIELDS---
Then output these fields, one per line:
Meta Title: [50-60 characters]
Meta Description: [150-160 characters]
URL Slug: [suggested-url-slug]
H1: [the page H1 heading]
"""


@app.route("/api/content/generate-article", methods=["POST"])
def api_content_generate_article():
    """Stream-generate an article using Claude API with the 12-phase framework."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set. Add it to your environment."}), 400

    data = request.get_json(force=True)
    writing_prompt = data.get("writing_prompt", "").strip()
    title = data.get("title", "").strip()
    article_id = data.get("article_id")

    if not writing_prompt and not title:
        return jsonify({"error": "Writing prompt or title is required."}), 400

    # Build contextual user prompt
    user_prompt_parts = []
    if writing_prompt:
        user_prompt_parts.append(f"WRITING PROMPT:\n{writing_prompt}")
    else:
        user_prompt_parts.append(f"Write an article titled: {title}")

    # Add money pages and products context if available
    money_pages = data.get("money_pages", [])
    if money_pages:
        user_prompt_parts.append(
            "REQUIRED INTERNAL LINKS (you MUST include every one of these):\n"
            + "\n".join(f"- {url}" for url in money_pages)
        )
    products = data.get("products_supported", [])
    if products:
        user_prompt_parts.append(
            "PRODUCTS TO MENTION:\n"
            + "\n".join(f"- {p}" for p in products)
        )

    # Load sitemap URLs for product/category context
    sitemap_path = Path(__file__).parent.parent / "data" / "sitemap_types.json"
    if sitemap_path.exists():
        try:
            with open(sitemap_path) as f:
                sitemap_data = json.load(f)
            categories = [url for url, t in sitemap_data.items() if t == "category"]
            product_urls = [url for url, t in sitemap_data.items() if t == "product"]
            if categories:
                user_prompt_parts.append(
                    "AVAILABLE CATEGORY PAGES ON OUR SITE (link to relevant ones):\n"
                    + "\n".join(f"- {url}" for url in categories[:30])
                )
            if product_urls:
                user_prompt_parts.append(
                    "AVAILABLE PRODUCT PAGES ON OUR SITE (link to relevant ones):\n"
                    + "\n".join(f"- {url}" for url in product_urls[:50])
                )
        except (json.JSONDecodeError, OSError):
            pass

    # Load existing blog articles for cross-linking
    try:
        cm_data = content_manager._load_data()
        existing_articles = [
            a for a in cm_data.get("articles", [])
            if a.get("url") and a.get("status") == "published"
        ]
        if existing_articles:
            user_prompt_parts.append(
                "EXISTING BLOG ARTICLES ON OUR SITE (cross-link relevant ones):\n"
                + "\n".join(
                    f"- {a['title']}: {a['url']}" for a in existing_articles[:20]
                )
            )
    except Exception:
        pass

    user_prompt = "\n\n".join(user_prompt_parts)

    model = "claude-sonnet-4-6"

    import httpx as _httpx

    def generate():
        try:
            body = {
                "model": model,
                "max_tokens": 32000,
                "temperature": 0.6,
                "stream": True,
                "system": _ARTICLE_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_prompt}],
            }

            with _httpx.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "output-128k-2025-02-19",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=_httpx.Timeout(connect=15.0, read=600.0, write=15.0, pool=15.0),
            ) as response:
                if response.status_code != 200:
                    error_text = response.read().decode()
                    yield f"data: {json.dumps({'error': f'API error {response.status_code}: {error_text}'})}\n\n"
                    return

                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                        event_type = event.get("type", "")

                        if event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                yield f"data: {json.dumps({'text': text})}\n\n"

                        elif event_type == "message_stop":
                            usage = event.get("usage", {})
                            if not usage and "message" in event:
                                usage = event["message"].get("usage", {})
                            yield f"data: {json.dumps({'done': True, 'usage': usage})}\n\n"

                        elif event_type == "message_delta":
                            usage = event.get("usage", {})
                            yield f"data: {json.dumps({'done': True, 'usage': usage})}\n\n"

                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# RUN SERVER
# ============================================================

@app.before_request
def _ensure_scheduler():
    global _scheduler_started
    if not _scheduler_started:
        _scheduler_started = True
        _start_scheduler()


if __name__ == "__main__":
    app.run(debug=True, port=8080, host="127.0.0.1")
