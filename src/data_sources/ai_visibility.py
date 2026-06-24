"""AI Visibility Tracking — monitors brand mentions across AI engines.

Queries ChatGPT, Claude, Gemini, and Perplexity with configurable prompts
and checks whether the brand/site is mentioned in the response. Results
are stored with timestamps for trend analysis.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

DATA_PATH = Path(__file__).parent.parent.parent / "data"
VISIBILITY_PATH = DATA_PATH / "ai_visibility.json"

ENGINE_CONFIG = {
    "chatgpt": {"key_env": "OPENAI_API_KEY"},
    "claude": {"key_env": "ANTHROPIC_API_KEY"},
    "gemini": {"key_env": "GOOGLE_API_KEY"},
    "perplexity": {"key_env": "PERPLEXITY_API_KEY"},
}


def _load_data():
    if VISIBILITY_PATH.exists():
        try:
            with open(VISIBILITY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"prompts": [], "results": [], "config": {"brand_keywords": [], "site_domain": ""}}


def _save_data(data):
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = VISIBILITY_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(VISIBILITY_PATH)


def _query_chatgpt(prompt: str, api_key: str) -> Optional[str]:
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500},
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    return None


def _query_claude(prompt: str, api_key: str) -> Optional[str]:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()["content"][0]["text"]
    return None


def _query_gemini(prompt: str, api_key: str) -> Optional[str]:
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={api_key}",
        headers={"Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    if resp.status_code == 200:
        candidates = resp.json().get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text")
    return None


def _query_perplexity(prompt: str, api_key: str) -> Optional[str]:
    resp = httpx.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": "sonar", "messages": [{"role": "user", "content": prompt}], "max_tokens": 1500},
        timeout=30,
    )
    if resp.status_code == 200:
        return resp.json()["choices"][0]["message"]["content"]
    return None


_QUERY_FNS = {
    "chatgpt": _query_chatgpt,
    "claude": _query_claude,
    "gemini": _query_gemini,
    "perplexity": _query_perplexity,
}


def detect_mention(text: str, brand_keywords: list, site_domain: str) -> dict:
    text_lower = text.lower()
    matched_keywords = [kw for kw in brand_keywords if kw.lower() in text_lower]
    if site_domain and site_domain.lower() in text_lower:
        matched_keywords.append(site_domain)

    mentioned = len(matched_keywords) > 0
    context = ""
    if mentioned:
        for kw in matched_keywords:
            idx = text_lower.find(kw.lower())
            if idx >= 0:
                start = max(0, idx - 120)
                end = min(len(text), idx + len(kw) + 120)
                context = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
                break

    urls_cited = []
    if site_domain:
        urls_cited = re.findall(r'https?://[^\s\)\]]+' + re.escape(site_domain.lower()) + r'[^\s\)\]]*', text_lower)

    return {
        "mentioned": mentioned,
        "matched_keywords": matched_keywords,
        "urls_cited": urls_cited,
        "context": context,
    }


def check_prompt(prompt_text: str, engines: list = None, brand_keywords: list = None, site_domain: str = ""):
    if engines is None:
        engines = list(_QUERY_FNS.keys())
    if brand_keywords is None:
        data = _load_data()
        brand_keywords = data.get("config", {}).get("brand_keywords", [])
        site_domain = site_domain or data.get("config", {}).get("site_domain", "")

    results = []
    timestamp = datetime.now().isoformat()

    for engine in engines:
        cfg = ENGINE_CONFIG.get(engine, {})
        api_key = os.environ.get(cfg.get("key_env", ""), "")

        if not api_key:
            results.append({
                "prompt": prompt_text, "engine": engine, "timestamp": timestamp,
                "mentioned": False, "error": f"No API key ({cfg.get('key_env', '?')})",
            })
            continue

        query_fn = _QUERY_FNS.get(engine)
        if not query_fn:
            continue

        try:
            response_text = query_fn(prompt_text, api_key)
            if response_text:
                mention = detect_mention(response_text, brand_keywords, site_domain)
                results.append({
                    "prompt": prompt_text, "engine": engine, "timestamp": timestamp,
                    "mentioned": mention["mentioned"],
                    "matched_keywords": mention["matched_keywords"],
                    "urls_cited": mention["urls_cited"],
                    "context": mention["context"],
                    "response_length": len(response_text),
                })
            else:
                results.append({
                    "prompt": prompt_text, "engine": engine, "timestamp": timestamp,
                    "mentioned": False, "error": "Empty response",
                })
        except Exception as e:
            results.append({
                "prompt": prompt_text, "engine": engine, "timestamp": timestamp,
                "mentioned": False, "error": str(e),
            })

    return results


def run_visibility_check(prompt_ids: list = None):
    data = _load_data()
    prompts = data.get("prompts", [])
    config = data.get("config", {})

    if prompt_ids:
        prompts = [p for p in prompts if p["id"] in prompt_ids]

    all_results = []
    for prompt in prompts:
        results = check_prompt(
            prompt["text"],
            engines=prompt.get("engines", list(_QUERY_FNS.keys())),
            brand_keywords=config.get("brand_keywords", []),
            site_domain=config.get("site_domain", ""),
        )
        all_results.extend(results)

    data["results"].extend(all_results)
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    data["results"] = [r for r in data["results"] if r.get("timestamp", "") >= cutoff]
    _save_data(data)
    return all_results


def get_visibility_summary():
    data = _load_data()
    results = data.get("results", [])
    prompts = data.get("prompts", [])

    if not results:
        return {
            "prompts": prompts, "config": data.get("config", {}),
            "total_checks": 0, "overall_visibility": 0,
            "by_engine": {}, "by_prompt": {}, "trend": [],
            "latest_results": [], "available_engines": get_available_engines(),
        }

    latest = {}
    for r in results:
        key = f"{r['prompt']}|{r['engine']}"
        if key not in latest or r.get("timestamp", "") > latest[key].get("timestamp", ""):
            latest[key] = r

    latest_results = list(latest.values())
    non_error = [r for r in latest_results if not r.get("error")]
    mentioned_count = sum(1 for r in non_error if r.get("mentioned"))
    total = len(non_error) or 1

    by_engine = {}
    for r in latest_results:
        eng = r["engine"]
        if eng not in by_engine:
            by_engine[eng] = {"total": 0, "mentioned": 0, "errors": 0}
        if r.get("error"):
            by_engine[eng]["errors"] += 1
        else:
            by_engine[eng]["total"] += 1
            if r.get("mentioned"):
                by_engine[eng]["mentioned"] += 1
    for eng in by_engine:
        t = by_engine[eng]["total"] or 1
        by_engine[eng]["pct"] = round(by_engine[eng]["mentioned"] / t * 100)

    by_prompt = {}
    for r in latest_results:
        p = r["prompt"]
        if p not in by_prompt:
            by_prompt[p] = {"engines": {}, "mentioned_count": 0, "url_cited_count": 0, "total": 0}
        by_prompt[p]["engines"][r["engine"]] = {
            "mentioned": r.get("mentioned", False),
            "url_cited": bool(r.get("urls_cited")),
            "urls_cited": r.get("urls_cited", []),
            "context": r.get("context", ""),
            "timestamp": r.get("timestamp", ""),
            "error": r.get("error"),
        }
        if not r.get("error"):
            by_prompt[p]["total"] += 1
            if r.get("mentioned"):
                by_prompt[p]["mentioned_count"] += 1
            if r.get("urls_cited"):
                by_prompt[p]["url_cited_count"] += 1

    for p in by_prompt:
        bp = by_prompt[p]
        bp["tier"] = _compute_tier(bp)
        bp["recommendation"] = _get_recommendation(bp)

    from collections import defaultdict
    daily = defaultdict(lambda: {"mentioned": 0, "total": 0})
    for r in results:
        if r.get("error"):
            continue
        date = r.get("timestamp", "")[:10]
        if date:
            daily[date]["total"] += 1
            if r.get("mentioned"):
                daily[date]["mentioned"] += 1

    trend = []
    for date in sorted(daily.keys()):
        d = daily[date]
        trend.append({
            "date": date,
            "visibility_pct": round(d["mentioned"] / max(d["total"], 1) * 100),
            "checks": d["total"],
            "mentions": d["mentioned"],
        })

    return {
        "prompts": prompts, "config": data.get("config", {}),
        "total_checks": len(non_error),
        "overall_visibility": round(mentioned_count / total * 100),
        "by_engine": by_engine, "by_prompt": by_prompt,
        "trend": trend, "latest_results": latest_results,
        "available_engines": get_available_engines(),
    }


def _compute_tier(bp: dict) -> dict:
    total = bp["total"]
    mentioned = bp["mentioned_count"]
    url_cited = bp["url_cited_count"]

    if total == 0:
        return {"level": "unknown", "label": "No Data", "color": "gray"}
    if mentioned == 0:
        return {"level": "invisible", "label": "Invisible", "color": "red"}
    if mentioned <= total * 0.25:
        return {"level": "weak", "label": "Weak", "color": "orange"}
    if mentioned <= total * 0.5:
        return {"level": "partial", "label": "Partial", "color": "yellow"}
    if url_cited >= mentioned * 0.5:
        return {"level": "strong_cited", "label": "Strong + Cited", "color": "green"}
    return {"level": "strong", "label": "Strong", "color": "green"}


def _get_recommendation(bp: dict) -> list:
    tier = bp.get("tier", {}).get("level", "unknown")
    mentioned = bp["mentioned_count"]
    total = bp["total"]
    url_cited = bp["url_cited_count"]

    missing_engines = [
        eng for eng, d in bp.get("engines", {}).items()
        if not d.get("error") and not d.get("mentioned")
    ]
    cited_engines = [
        eng for eng, d in bp.get("engines", {}).items()
        if d.get("url_cited")
    ]

    recs = []

    if tier == "invisible":
        recs.append({
            "priority": "high",
            "action": "Create dedicated content targeting this query — a blog post, buying guide, or landing page optimized for this topic.",
        })
        recs.append({
            "priority": "high",
            "action": "Add comprehensive FAQ schema markup covering this topic on your most relevant existing page.",
        })
        recs.append({
            "priority": "medium",
            "action": "Build topical authority: publish 3-5 related articles that interlink and establish expertise.",
        })

    elif tier == "weak":
        recs.append({
            "priority": "high",
            "action": f"Strengthen existing content — you're only visible in {mentioned}/{total} engines. Expand depth, add data, improve structure.",
        })
        recs.append({
            "priority": "medium",
            "action": "Add JSON-LD structured data (Product, FAQ, HowTo) to help AI engines parse your content as a source.",
        })
        if missing_engines:
            names = ", ".join(e.capitalize() for e in missing_engines)
            recs.append({
                "priority": "medium",
                "action": f"Missing from: {names}. Research what sources these engines cite instead and match that content depth.",
            })

    elif tier == "partial":
        if missing_engines:
            names = ", ".join(e.capitalize() for e in missing_engines)
            recs.append({
                "priority": "medium",
                "action": f"Not mentioned in: {names}. Analyze competitor content these engines cite and differentiate.",
            })
        if url_cited == 0:
            recs.append({
                "priority": "medium",
                "action": "Mentioned by name but no URLs cited. Add authoritative, linkable content (guides, data, tools) that AI engines can reference directly.",
            })
        recs.append({
            "priority": "low",
            "action": "Earn backlinks and citations from authoritative sources — AI engines weight domain authority in source selection.",
        })

    elif tier in ("strong", "strong_cited"):
        recs.append({
            "priority": "low",
            "action": "Maintain position — keep content fresh and updated. Monitor for drops.",
        })
        if url_cited < mentioned:
            recs.append({
                "priority": "low",
                "action": f"URL cited in {url_cited}/{mentioned} mentions. Add more linkable assets (tools, calculators, downloadable guides) to increase citation rate.",
            })
        if cited_engines:
            names = ", ".join(e.capitalize() for e in cited_engines)
            recs.append({
                "priority": "low",
                "action": f"URLs cited by: {names}. Double down on what works — analyze cited pages and replicate the pattern.",
            })

    return recs


def get_available_engines():
    available = {}
    for engine, cfg in ENGINE_CONFIG.items():
        key = os.environ.get(cfg["key_env"], "")
        available[engine] = bool(key)
    return available


def add_prompt(text: str, engines: list = None):
    data = _load_data()
    prompt_id = f"p{len(data['prompts']) + 1}_{int(time.time())}"
    prompt = {
        "id": prompt_id, "text": text,
        "engines": engines or list(_QUERY_FNS.keys()),
        "created_at": datetime.now().isoformat(),
    }
    data["prompts"].append(prompt)
    _save_data(data)
    return prompt


def remove_prompt(prompt_id: str):
    data = _load_data()
    data["prompts"] = [p for p in data["prompts"] if p["id"] != prompt_id]
    data["results"] = [r for r in data["results"]
                       if not any(p["id"] == prompt_id and p["text"] == r.get("prompt")
                                  for p in _load_data().get("prompts", []))]
    _save_data(data)


def update_config(brand_keywords: list = None, site_domain: str = None):
    data = _load_data()
    if "config" not in data:
        data["config"] = {}
    if brand_keywords is not None:
        data["config"]["brand_keywords"] = brand_keywords
    if site_domain is not None:
        data["config"]["site_domain"] = site_domain
    _save_data(data)
