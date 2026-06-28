"""AI Visibility Tracking — monitors brand mentions across AI engines.

Queries ChatGPT, Claude, Gemini, and Perplexity with configurable prompts
and checks whether the brand/site is mentioned in the response. Results
are stored with timestamps for trend analysis.
"""

import csv
import io
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
QUEUE_PATH = DATA_PATH / "keyword_queue.json"

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
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {})
            msg = err.get("message", resp.text[:300])
        except Exception:
            msg = resp.text[:300]
        raise RuntimeError(f"Gemini API {resp.status_code}: {msg}")
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

    all_urls = re.findall(r'https?://[^\s\)\]\">]+', text)
    own_domain = site_domain.lower().replace("www.", "") if site_domain else ""
    competitor_urls = []
    seen_domains = set()
    for url in all_urls:
        domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0].lower()
        if own_domain and own_domain in domain:
            continue
        if domain not in seen_domains and '.' in domain:
            seen_domains.add(domain)
            competitor_urls.append(url.rstrip('.,;:'))

    competitor_brands = []
    brand_kw_lower = {kw.lower() for kw in brand_keywords}
    for domain in seen_domains:
        name = domain.split('.')[0]
        if name in ("amazon", "ebay", "walmart", "target", "etsy", "google", "youtube", "wikipedia", "reddit"):
            continue
        if name in brand_kw_lower:
            continue
        if own_domain and name in own_domain:
            continue
        if len(name) > 2:
            competitor_brands.append(name)

    return {
        "mentioned": mentioned,
        "matched_keywords": matched_keywords,
        "urls_cited": urls_cited,
        "context": context,
        "competitor_urls": competitor_urls[:10],
        "competitor_brands": competitor_brands[:10],
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
                    "competitor_urls": mention.get("competitor_urls", []),
                    "competitor_brands": mention.get("competitor_brands", []),
                    "response_excerpt": response_text[:500],
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
            by_prompt[p] = {"engines": {}, "mentioned_count": 0, "url_cited_count": 0, "total": 0,
                            "competitor_urls": [], "competitor_brands": [], "response_excerpts": {}}
        by_prompt[p]["engines"][r["engine"]] = {
            "mentioned": r.get("mentioned", False),
            "url_cited": bool(r.get("urls_cited")),
            "urls_cited": r.get("urls_cited", []),
            "context": r.get("context", ""),
            "timestamp": r.get("timestamp", ""),
            "error": r.get("error"),
            "competitor_urls": r.get("competitor_urls", []),
            "competitor_brands": r.get("competitor_brands", []),
        }
        if not r.get("error"):
            by_prompt[p]["total"] += 1
            if r.get("mentioned"):
                by_prompt[p]["mentioned_count"] += 1
            if r.get("urls_cited"):
                by_prompt[p]["url_cited_count"] += 1
            for cu in r.get("competitor_urls", []):
                if cu not in by_prompt[p]["competitor_urls"]:
                    by_prompt[p]["competitor_urls"].append(cu)
            for cb in r.get("competitor_brands", []):
                if cb.lower() not in {b.lower() for b in by_prompt[p]["competitor_brands"]}:
                    by_prompt[p]["competitor_brands"].append(cb)
            if r.get("response_excerpt"):
                by_prompt[p]["response_excerpts"][r["engine"]] = r["response_excerpt"]

    inventory_pages = {}
    if INVENTORY_PATH.exists():
        try:
            with open(INVENTORY_PATH) as f:
                inventory_pages = json.load(f).get("pages", {})
        except (json.JSONDecodeError, OSError):
            pass

    for p in by_prompt:
        bp = by_prompt[p]
        bp["tier"] = _compute_tier(bp)
        bp["recommendation"] = _get_recommendation(bp, prompt_text=p, pages=inventory_pages)

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


def _find_matching_pages(prompt_text: str, pages: dict) -> list:
    """Find pages from the inventory that match a keyword prompt."""
    kw_words = set(re.findall(r'[a-z0-9]+', prompt_text.lower()))
    stop = {"the", "and", "for", "with", "from", "best", "top", "a", "of", "in", "to",
            "is", "are", "my", "your", "our", "how", "what", "where", "buy", "get", "on"}
    kw_words -= stop
    if not kw_words:
        return []

    product_families = _load_product_families()
    product_nouns = set()
    modifiers = {"play", "art", "name", "step", "wooden", "wood"}
    for pf in product_families:
        for word in pf.lower().split():
            if len(word) > 2 and word not in modifiers:
                product_nouns.add(word)
                if word.endswith("ies") and len(word) > 4:
                    product_nouns.add(word[:-3] + "y")
                elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
                    product_nouns.add(word[:-1])
                else:
                    product_nouns.add(word + "s")

    kw_product_words = kw_words & product_nouns
    kw_numbers = {w for w in kw_words if w.isdigit()}

    non_product_words = {
        "carpet", "carpets", "rug", "rugs", "runner", "runners",
        "mat", "mats", "flooring", "kidsoft",
        "rectangle", "square", "oval",
    }
    kw_has_carpet = kw_words & {"carpet", "carpets", "rug", "rugs", "runner", "mat", "mats"}

    matches = []
    for url, page_data in pages.items():
        if not isinstance(page_data, dict):
            continue
        title = (page_data.get("title") or "").lower()
        h1 = (page_data.get("h1") or "").lower()
        path = re.sub(r'https?://[^/]+', '', url).lower()
        page_text = f"{title} {h1} {path}"
        page_words = set(re.findall(r'[a-z0-9]+', page_text))

        is_carpet_page = bool(page_words & non_product_words) or bool(re.search(r'\d+ft', page_text))
        if not kw_has_carpet and is_carpet_page:
            continue

        if kw_numbers and not kw_numbers.issubset(page_words):
            continue

        if kw_product_words:
            if not kw_product_words.issubset(page_words):
                continue
            overlap = kw_words & page_words
            matches.append({"url": url, "title": page_data.get("title", ""),
                            "h1": page_data.get("h1", ""), "overlap": len(overlap)})
        else:
            overlap = kw_words & page_words
            if len(overlap) >= max(2, len(kw_words) * 0.75):
                matches.append({"url": url, "title": page_data.get("title", ""),
                                "h1": page_data.get("h1", ""), "overlap": len(overlap)})

    matches.sort(key=lambda m: m["overlap"], reverse=True)
    return matches[:3]


def _extract_content_signals(excerpts: dict) -> list:
    """Extract content themes AI responses emphasize (pattern-matched, not bigrams)."""
    if not excerpts:
        return []
    combined = " ".join(excerpts.values()).lower()

    signals = [
        ("buying guides", [r'\bbuying guide\b', r'\bguide to\b', r'\bhow to choose\b', r'\bhow to pick\b', r'\bwhat to look for\b']),
        ("product comparisons", [r'\bcompar', r'\bvs\.?\s', r'\bversus\b', r'\balternative']),
        ("reviews", [r'\breview', r'\brating', r'\btested\b']),
        ("age-specific guidance", [r'\bage.appropriate\b', r'\bage \d', r'\byear.old', r'\btoddler', r'\bpreschool', r'\bdevelopmental stage']),
        ("developmental benefits", [r'\bfine motor\b', r'\bhand.eye\b', r'\bcognitive\b', r'\bsensory\b', r'\bproblem.solving\b', r'\bcritical thinking\b', r'\bcreativity\b', r'\bimagination\b']),
        ("safety and materials", [r'\bnon.toxic\b', r'\bbpa.free\b', r'\blead.free\b', r'\bsafety\b', r'\bsolid wood\b', r'\bhardwood\b', r'\bsustainable\b', r'\bnatural\b']),
        ("educational value", [r'\bmontessori\b', r'\bwaldorf\b', r'\bstem\b', r'\beducational\b', r'\blearning through play\b']),
        ("price and value", [r'\bbudget\b', r'\bprice range\b', r'\baffordable\b', r'\bpremium\b', r'\bworth\b', r'\binvestment\b']),
        ("brand rankings", [r'\bbest.{1,20}brand', r'\btop.{1,20}brand', r'\bpopular brand', r'\brecommend']),
    ]

    found = []
    for signal_name, patterns in signals:
        for pat in patterns:
            if re.search(pat, combined):
                found.append(signal_name)
                break

    return found


_SIGNAL_ACTIONS = {
    "buying guides": "add a buying guide — what to look for when choosing, top picks with pros/cons",
    "product comparisons": "add side-by-side product comparisons with specs and ratings",
    "reviews": "add hands-on review content with photos, testing notes, and honest ratings",
    "age-specific guidance": "add age-specific recommendations with milestones (what skills develop at each age)",
    "developmental benefits": "explain which skills each product develops (fine motor, problem-solving, spatial reasoning)",
    "safety and materials": "detail materials (wood type, paint), certifications (CPSC, ASTM), and safety testing",
    "educational value": "explain the educational approach (Montessori, STEM, Waldorf) and specific learning outcomes",
    "price and value": "add price tiers with budget vs premium picks and what justifies the price difference",
    "brand rankings": "add a brand comparison — what makes each brand unique, quality and price differences",
}


def _get_recommendation(bp: dict, prompt_text: str = "", pages: dict = None) -> list:
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

    matching_pages = _find_matching_pages(prompt_text, pages or {})
    has_page = len(matching_pages) > 0

    comp_urls = bp.get("competitor_urls", [])
    has_competitors = len(comp_urls) > 0

    response_excerpts = bp.get("response_excerpts", {})
    content_signals = _extract_content_signals(response_excerpts)

    recs = []

    if has_competitors:
        domains = []
        for u in comp_urls[:8]:
            d = re.sub(r'https?://(www\.)?', '', u).split('/')[0]
            if d not in domains:
                domains.append(d)
        if domains:
            recs.append({
                "priority": "info",
                "action": f"AI engines cite instead: {', '.join(domains)}",
            })

    if content_signals:
        actions = [_SIGNAL_ACTIONS.get(s, s) for s in content_signals[:3]]
        recs.append({
            "priority": "gap",
            "action": f"What AI engines want for \"{prompt_text}\": {'; '.join(actions)}.",
        })

    if tier == "invisible":
        if has_page:
            pg = matching_pages[0]
            if has_competitors:
                comp_domain = re.sub(r'https?://(www\.)?', '', comp_urls[0]).split('/')[0]
                recs.append({
                    "priority": "high",
                    "action": f'Study {comp_urls[0]} and compare against your page {pg["url"]}. '
                              f'Note what {comp_domain} covers that you don\'t — then add those sections.',
                })
            elif content_signals:
                top_action = _SIGNAL_ACTIONS.get(content_signals[0], content_signals[0])
                recs.append({
                    "priority": "high",
                    "action": f'Your page {pg["url"]} isn\'t cited. Top fix: {top_action}.',
                })
            else:
                recs.append({
                    "priority": "high",
                    "action": f'Your page {pg["url"]} exists but no AI engine cites it for "{prompt_text}". '
                              f'Expand with in-depth content — guides, comparisons, or how-to sections.',
                })
        else:
            if content_signals:
                top_action = _SIGNAL_ACTIONS.get(content_signals[0], content_signals[0])
                recs.append({
                    "priority": "high",
                    "action": f'No page on your site targets "{prompt_text}". '
                              f'Create one — start by: {top_action}.',
                })
            else:
                recs.append({
                    "priority": "high",
                    "action": f'No page on your site targets "{prompt_text}". '
                              f'Create a dedicated page with in-depth guides and comparisons.',
                })

    elif tier == "weak":
        engines_str = ", ".join(e.capitalize() for e in missing_engines) if missing_engines else "some engines"
        if has_page:
            pg = matching_pages[0]
            if has_competitors and content_signals:
                top_action = _SIGNAL_ACTIONS.get(content_signals[0], content_signals[0])
                recs.append({
                    "priority": "high",
                    "action": f'Mentioned in {mentioned}/{total} engines but missing from {engines_str}. '
                              f'On {pg["url"]}: {top_action}.',
                })
            elif has_competitors:
                comp_domain = re.sub(r'https?://(www\.)?', '', comp_urls[0]).split('/')[0]
                recs.append({
                    "priority": "high",
                    "action": f'Mentioned in {mentioned}/{total} engines. '
                              f'Compare {pg["url"]} against {comp_domain} to find coverage gaps.',
                })
            else:
                recs.append({
                    "priority": "high",
                    "action": f'Mentioned in {mentioned}/{total} engines, missing from {engines_str}. '
                              f'Expand {pg["url"]} with more detailed content.',
                })
        else:
            recs.append({
                "priority": "high",
                "action": f'Mentioned in {mentioned}/{total} engines without a dedicated page. '
                          f'Create one to appear in more engines.',
            })

    elif tier == "partial":
        if missing_engines:
            names = ", ".join(e.capitalize() for e in missing_engines)
            if content_signals:
                top_action = _SIGNAL_ACTIONS.get(content_signals[0], content_signals[0])
                recs.append({
                    "priority": "medium",
                    "action": f"Missing from {names}. To reach them: {top_action}.",
                })
            else:
                recs.append({
                    "priority": "medium",
                    "action": f"Missing from {names}. Review what those engines recommend for \"{prompt_text}\" "
                              f"and address the gap.",
                })
        if url_cited == 0:
            recs.append({
                "priority": "medium",
                "action": "Named but no URLs cited — engines know you but don't link to you. "
                          "Add unique data, original research, or authoritative depth.",
            })

    elif tier in ("strong", "strong_cited"):
        if has_page:
            recs.append({
                "priority": "low",
                "action": f'Strong position for "{prompt_text}". Keep {matching_pages[0]["url"]} updated.',
            })
        else:
            recs.append({
                "priority": "low",
                "action": f'Strong position for "{prompt_text}". Keep content fresh and monitor for drops.',
            })
        if cited_engines:
            names = ", ".join(e.capitalize() for e in cited_engines)
            recs.append({
                "priority": "low",
                "action": f"URLs cited by {names}. Use this content as a template for other keywords.",
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


# ── Keyword Queue System ──

def _load_queue():
    if QUEUE_PATH.exists():
        try:
            with open(QUEUE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "settings": {"batch_size": 20},
        "imports": [],
        "keywords": [],
    }


def _save_queue(queue):
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(queue, f, indent=2)
    tmp.replace(QUEUE_PATH)


def _detect_delimiter(csv_text: str) -> str:
    first_line = csv_text.split("\n", 1)[0]
    if "\t" in first_line:
        return "\t"
    return ","


def _clean_header(s: str) -> str:
    s = re.sub(r"[^\x20-\x7E]", "", s).strip().strip('"').strip("'").strip()
    return s.lower()


def _parse_ahrefs_csv(csv_text: str) -> list:
    csv_text = csv_text.lstrip("﻿￾\xef\xbb\xbf")

    delimiter = _detect_delimiter(csv_text)
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    rows = []
    col_map = {}
    competitor_pos_cols = []

    if reader.fieldnames:
        lower_fields = {_clean_header(f): f for f in reader.fieldnames}
        for target, candidates in [
            ("keyword", ["keyword"]),
            ("volume", ["volume", "monthly volume", "search volume", "volume ▼"]),
            ("kd", ["kd", "keyword difficulty"]),
            ("cpc", ["cpc"]),
            ("serp_features", ["serp features", "sf"]),
            ("intents", ["intents", "intent"]),
            ("traffic", ["traffic", "org. traffic", "organic traffic"]),
            ("position", ["position", "org. pos.", "org. pos", "organic position"]),
            ("url", ["url", "current url"]),
        ]:
            for c in candidates:
                if c in lower_fields:
                    col_map[target] = lower_fields[c]
                    break
            if target not in col_map:
                for field_lower, field_orig in lower_fields.items():
                    if target == "traffic" and "organic traffic" in field_lower:
                        col_map[target] = field_orig
                        break
                    if target == "position" and "organic position" in field_lower:
                        col_map[target] = field_orig
                        break
                    if target == "url" and field_lower.endswith(": url"):
                        col_map[target] = field_orig
                        break

        for field_lower, field_orig in lower_fields.items():
            if "organic position" in field_lower and field_orig != col_map.get("position"):
                competitor_pos_cols.append(field_orig)

    competitor_domains = set()
    if reader.fieldnames:
        for f in reader.fieldnames:
            match = re.match(r'["\s]*(?:https?://)?(?:www\.)?([a-z0-9-]+\.[a-z]+)/?.*:\s', _clean_header(f))
            if match:
                domain = match.group(1)
                competitor_domains.add(domain.split(".")[0])

    if "keyword" not in col_map:
        return [], list(competitor_domains)

    for row in reader:
        kw = row.get(col_map.get("keyword", ""), "").strip()
        if not kw:
            continue
        vol_raw = re.sub(r"[^\d.]", "", str(row.get(col_map.get("volume", ""), "0") or "0"))
        kd_raw = re.sub(r"[^\d.]", "", str(row.get(col_map.get("kd", ""), "0") or "0"))
        cpc_raw = re.sub(r"[^\d.]", "", str(row.get(col_map.get("cpc", ""), "0") or "0"))
        traffic_raw = re.sub(r"[^\d.]", "", str(row.get(col_map.get("traffic", ""), "0") or "0"))
        pos_raw = re.sub(r"[^\d.]", "", str(row.get(col_map.get("position", ""), "0") or "0"))

        serp_raw = str(row.get(col_map.get("serp_features", ""), "") or "")
        serp_features = [s.strip().lower() for s in serp_raw.split(",") if s.strip()]

        intents_raw = str(row.get(col_map.get("intents", ""), "") or "")
        intents = [s.strip().upper() for s in re.split(r"[,\s]+", intents_raw) if s.strip()]

        competitors_ranking = 0
        for comp_col in competitor_pos_cols:
            comp_pos = re.sub(r"[^\d.]", "", str(row.get(comp_col, "") or ""))
            if comp_pos:
                pos_val = int(float(comp_pos))
                if 0 < pos_val <= 10:
                    competitors_ranking += 1

        has_shopping = any("shopping" in f for f in serp_features)
        has_ai_overview = any("ai overview" in f for f in serp_features)
        is_informational = "I" in intents and "C" not in intents and "T" not in intents

        rows.append({
            "keyword": kw,
            "volume": int(float(vol_raw)) if vol_raw else 0,
            "kd": int(float(kd_raw)) if kd_raw else 0,
            "cpc": round(float(cpc_raw), 2) if cpc_raw else 0,
            "traffic": int(float(traffic_raw)) if traffic_raw else 0,
            "position": int(float(pos_raw)) if pos_raw else 0,
            "url": row.get(col_map.get("url", ""), "") or "",
            "serp_features": serp_features,
            "intents": intents,
            "has_shopping": has_shopping,
            "has_ai_overview": has_ai_overview,
            "is_informational": is_informational,
            "competitors_ranking": competitors_ranking,
        })

    return rows, list(competitor_domains)


def _load_product_families() -> list:
    config_path = DATA_PATH.parent / "config" / "defaults.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
            return config.get("business_context", {}).get("product_families", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


INVENTORY_PATH = DATA_PATH / "page_inventory.json"


def _load_site_terms() -> set:
    """Extract product/category terms from the page inventory (sitemap data)."""
    if not INVENTORY_PATH.exists():
        return set()
    try:
        with open(INVENTORY_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()

    pages = data.get("pages", {})
    if not pages:
        return set()

    stop_words = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was",
        "has", "have", "had", "our", "your", "all", "can", "will", "just",
        "more", "also", "than", "each", "about", "new", "best", "top",
        "com", "www", "http", "https", "html", "htm", "php", "asp",
        "page", "home", "index", "category", "product", "products",
        "shop", "store", "buy", "cart", "checkout", "account", "login",
        "search", "tag", "tags", "blog", "post", "posts", "news",
        "contact", "about", "faq", "help", "privacy", "terms", "policy",
        "shipping", "returns", "return", "order", "orders",
    }

    bigrams = set()
    for url, page_data in pages.items():
        if isinstance(page_data, dict):
            title = page_data.get("title", "")
            h1 = page_data.get("h1", "")
        else:
            continue

        path = re.sub(r'https?://[^/]+', '', url)
        path_words = [w.lower() for w in re.split(r'[-_/.]', path)
                       if len(w) > 2 and w.lower() not in stop_words]

        for text in [title, h1]:
            if not text:
                continue
            text_clean = re.sub(r'[|–—\-].*$', '', text).strip()
            words = [w.lower() for w in re.findall(r'[a-zA-Z]+', text_clean)
                     if len(w) > 2 and w.lower() not in stop_words]
            for i in range(len(words) - 1):
                bigrams.add(f"{words[i]} {words[i+1]}")
            for i in range(len(words) - 2):
                bigrams.add(f"{words[i]} {words[i+1]} {words[i+2]}")

        for i in range(len(path_words) - 1):
            bigrams.add(f"{path_words[i]} {path_words[i+1]}")

    return bigrams


def _load_brand_terms() -> list:
    config_path = DATA_PATH.parent / "config" / "defaults.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
            return config.get("data_sources", {}).get("google_ads", {}).get("brand_terms", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def import_keywords_csv(csv_text: str, filters: dict = None) -> dict:
    if filters is None:
        filters = {}

    parsed = _parse_ahrefs_csv(csv_text)
    all_rows, detected_competitors = parsed

    if not all_rows:
        delimiter = _detect_delimiter(csv_text)
        try:
            reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
            found_cols = [_clean_header(f) for f in (reader.fieldnames or [])][:10]
        except Exception:
            found_cols = []
        first_50 = csv_text[:200].replace("\n", " | ")
        return {
            "error": f"Could not parse CSV. No 'Keyword' column found. "
                     f"Detected delimiter: {'TAB' if delimiter == chr(9) else repr(delimiter)}. "
                     f"First columns found: {found_cols}. "
                     f"File starts with: {first_50}"
        }

    product_families = _load_product_families()
    brand_terms = _load_brand_terms()
    brand_terms_lower = {t.lower() for t in brand_terms}
    site_terms = _load_site_terms()
    has_site_data = len(site_terms) > 0

    if has_site_data:
        too_generic = {
            "toy", "toys", "name", "step", "play", "art",
            "supply", "supplies", "gift", "gifts", "idea", "ideas",
            "set", "sets", "game", "games", "best", "top", "new",
            "kid", "kids", "old", "year", "boy", "girl", "baby",
        }
        product_vocab = set()
        for pf in product_families:
            for word in pf.lower().split():
                if len(word) > 2 and word not in too_generic:
                    product_vocab.add(word)
                    if word.endswith("ies") and len(word) > 4:
                        product_vocab.add(word[:-3] + "y")
                    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
                        product_vocab.add(word[:-1])
                    else:
                        product_vocab.add(word + "s")

        relevant_site_terms = set()
        for term in site_terms:
            term_words = set(term.split())
            if term_words & product_vocab:
                relevant_site_terms.add(term)

        specific_terms = relevant_site_terms
        generic_terms = set()
        for pf in product_families:
            pf_lower = pf.lower()
            if " " in pf_lower:
                specific_terms.add(pf_lower)
                singular = re.sub(r's$', '', pf_lower)
                if singular != pf_lower:
                    specific_terms.add(singular)
            else:
                generic_terms.add(pf_lower)
                if pf_lower.endswith("s") and len(pf_lower) > 3:
                    generic_terms.add(pf_lower[:-1])
    else:
        generic_words = {
            "toy", "toys", "store", "stores", "shop", "shops", "best", "top", "new",
            "name", "step", "play", "art", "supply", "supplies",
            "stool", "stools", "book", "books", "rug", "rugs",
            "kitchen", "kitchens", "game", "games", "gift", "gifts",
            "set", "sets", "food",
        }
        specific_terms = set()
        generic_terms = set()
        for pf in product_families:
            pf_lower = pf.lower()
            if " " in pf_lower:
                specific_terms.add(pf_lower)
                singular = re.sub(r's$', '', pf_lower)
                if singular != pf_lower:
                    specific_terms.add(singular)
            for word in pf_lower.split():
                if len(word) <= 2:
                    continue
                if word in generic_words:
                    generic_terms.add(word)
                    if word.endswith("ies") and len(word) > 4:
                        generic_terms.add(word[:-3] + "y")
                    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
                        generic_terms.add(word[:-1])
                else:
                    specific_terms.add(word)
                    if word.endswith("ies") and len(word) > 4:
                        specific_terms.add(word[:-3] + "y")
                    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
                        specific_terms.add(word[:-1])
        generic_terms -= {"toy", "toys"}

    child_context = {
        "kid", "kids", "children", "child", "baby", "toddler", "infant",
        "montessori", "toy", "toys", "pretend", "wooden", "play",
        "nursery", "preschool", "waldorf", "educational",
        "sticker", "coloring", "activity", "busy", "abc", "alphabet",
        "learning", "doll", "dollhouse", "craft", "crafts",
        "personalized", "custom", "toddlers", "kit", "kits",
    }

    specific_pats = [re.compile(r'\b' + re.escape(t) + r'(?:s|es)?\b') for t in specific_terms]
    generic_pats = [re.compile(r'\b' + re.escape(t) + r'\b') for t in generic_terms]
    child_ctx_pats = [re.compile(r'\b' + re.escape(w) + r's?\b') for w in child_context]

    auto_must_contain = sorted(specific_terms | generic_terms)

    auto_exclude_domains = set()
    auto_exclude = []
    for c in detected_competitors:
        c_lower = c.lower()
        if c_lower in brand_terms_lower:
            continue
        auto_exclude_domains.add(c_lower)
        auto_exclude.append(c_lower)
        no_suffix = re.sub(r'(toys|shop|store|online|com)$', '', c_lower).rstrip('.')
        if no_suffix and no_suffix != c_lower and len(no_suffix) > 3:
            auto_exclude.append(no_suffix)

    auto_exclude.extend(["for adults", "gardening", "espresso", "cocktail",
                          "invitations", "invites", "gadgets", "ukulele",
                          "parenting", "porsche", "birthday book",
                          "outdoor play", "active play", "water play",
                          "outdoor toys", "bath toys", "pool toys",
                          "beach toys", "fidget toys"])

    total_in_csv = len(all_rows)
    min_volume = filters.get("min_volume", 100)
    max_kd = filters.get("max_kd")
    user_must_contain = [t.lower().strip() for t in filters.get("must_contain", []) if t.strip()]
    user_exclude = [t.lower().strip() for t in filters.get("exclude_terms", []) if t.strip()]

    for t in user_must_contain:
        specific_pats.append(re.compile(r'\b' + re.escape(t) + r'(?:s|es)?\b'))
    exclude_terms = list(set(auto_exclude + user_exclude))

    skip_informational = filters.get("skip_informational", False)
    require_commercial = filters.get("require_commercial", False)
    min_cpc = filters.get("min_cpc")
    require_shopping = filters.get("require_shopping", False)
    only_ai_overview = filters.get("only_ai_overview", False)
    min_competitors = filters.get("min_competitors")

    queue = _load_queue()
    existing_keywords = {kw["keyword"].lower() for kw in queue["keywords"]}
    vis_data = _load_data()
    existing_prompts = {p["text"].lower() for p in vis_data.get("prompts", [])}

    filtered = []
    rejections = {}
    max_samples = 5

    def _reject(reason, kw_text, volume):
        if reason not in rejections:
            rejections[reason] = {"count": 0, "samples": []}
        rejections[reason]["count"] += 1
        if len(rejections[reason]["samples"]) < max_samples:
            rejections[reason]["samples"].append({"keyword": kw_text, "volume": volume})

    for row in all_rows:
        kw_lower = row["keyword"].lower()
        if kw_lower in existing_keywords or kw_lower in existing_prompts:
            _reject("Already imported or tracked", row["keyword"], row["volume"])
            continue
        if row["volume"] < min_volume:
            _reject(f"Volume below {min_volume}", row["keyword"], row["volume"])
            continue
        if max_kd is not None and row["kd"] > max_kd:
            _reject(f"Keyword difficulty above {max_kd}", row["keyword"], row["volume"])
            continue
        has_specific = any(p.search(kw_lower) for p in specific_pats)
        has_generic = any(p.search(kw_lower) for p in generic_pats)
        has_child_ctx = any(p.search(kw_lower) for p in child_ctx_pats)
        if not has_specific and not (has_generic and has_child_ctx):
            _reject("No product family match", row["keyword"], row["volume"])
            continue
        if exclude_terms and any(re.search(r'\b' + re.escape(t) + r'\b', kw_lower) for t in exclude_terms):
            _reject("Excluded term", row["keyword"], row["volume"])
            continue
        kw_nospace = kw_lower.replace(" ", "").replace("-", "")
        if auto_exclude_domains and any(dom in kw_nospace or kw_nospace in dom for dom in auto_exclude_domains):
            _reject("Competitor brand", row["keyword"], row["volume"])
            continue
        if len(row["keyword"].split()) <= 1:
            _reject("Single word", row["keyword"], row["volume"])
            continue
        if skip_informational and row.get("is_informational"):
            _reject("Informational intent", row["keyword"], row["volume"])
            continue
        if require_commercial and not (row.get("has_shopping") or row.get("cpc", 0) > 0.05):
            _reject("Not commercial", row["keyword"], row["volume"])
            continue
        if min_cpc is not None and row.get("cpc", 0) < min_cpc:
            _reject(f"CPC below {min_cpc}", row["keyword"], row["volume"])
            continue
        if require_shopping and not row.get("has_shopping"):
            _reject("No shopping SERP", row["keyword"], row["volume"])
            continue
        if only_ai_overview and not row.get("has_ai_overview"):
            _reject("No AI overview", row["keyword"], row["volume"])
            continue
        if min_competitors is not None and row.get("competitors_ranking", 0) < min_competitors:
            _reject(f"Fewer than {min_competitors} competitors ranking", row["keyword"], row["volume"])
            continue
        filtered.append(row)

    def _score(row):
        score = row["volume"]
        if row.get("has_shopping"):
            score *= 1.5
        if row.get("has_ai_overview"):
            score *= 1.3
        if row.get("cpc", 0) > 0:
            score *= 1.2
        if row.get("is_informational"):
            score *= 0.5
        if row.get("competitors_ranking", 0) >= 2:
            score *= 1.2
        return score

    filtered.sort(key=_score, reverse=True)

    import_id = f"imp_{int(time.time())}"
    batch_size = queue["settings"].get("batch_size", 20)
    existing_batches = max((kw.get("batch", 0) for kw in queue["keywords"]), default=0)

    for i, row in enumerate(filtered):
        batch_num = existing_batches + (i // batch_size) + 1
        queue["keywords"].append({
            "keyword": row["keyword"],
            "volume": row["volume"],
            "kd": row["kd"],
            "cpc": row["cpc"],
            "traffic": row.get("traffic", 0),
            "position": row.get("position", 0),
            "url": row.get("url", ""),
            "has_shopping": row.get("has_shopping", False),
            "has_ai_overview": row.get("has_ai_overview", False),
            "is_informational": row.get("is_informational", False),
            "competitors_ranking": row.get("competitors_ranking", 0),
            "import_id": import_id,
            "batch": batch_num,
            "status": "queued",
            "prompt_id": None,
            "last_checked_at": None,
            "next_recheck_at": None,
            "last_result": None,
        })

    queue["imports"].append({
        "id": import_id,
        "imported_at": datetime.now().isoformat(),
        "filters": filters,
        "stats": {
            "total_in_csv": total_in_csv,
            "after_filters": len(filtered),
            "duplicates_skipped": total_in_csv - len(filtered) - (total_in_csv - len(all_rows)),
        },
    })

    _save_queue(queue)

    total_batches = max((kw["batch"] for kw in queue["keywords"]), default=0)
    return {
        "import_id": import_id,
        "total_in_csv": total_in_csv,
        "imported": len(filtered),
        "total_batches": total_batches,
        "batch_size": batch_size,
        "rejections": rejections,
        "auto_detected": {
            "product_families": auto_must_contain,
            "site_terms_count": len(specific_terms),
            "used_site_data": has_site_data,
            "competitors_excluded": auto_exclude,
            "must_contain_used": auto_must_contain,
            "exclude_used": exclude_terms,
        },
    }


def get_queue_summary() -> dict:
    sync_queue_prompts()
    queue = _load_queue()
    keywords = queue["keywords"]

    by_status = {}
    for kw in keywords:
        s = kw["status"]
        by_status[s] = by_status.get(s, 0) + 1

    by_batch = {}
    for kw in keywords:
        b = kw["batch"]
        if b not in by_batch:
            by_batch[b] = {"total": 0, "queued": 0, "active": 0, "retained": 0, "archived": 0}
        by_batch[b]["total"] += 1
        by_batch[b][kw["status"]] += 1

    next_batch = None
    for b in sorted(by_batch.keys()):
        if by_batch[b]["queued"] > 0:
            next_batch = b
            break

    recheck_due = sum(
        1 for kw in keywords
        if kw["status"] == "archived"
        and kw.get("next_recheck_at")
        and kw["next_recheck_at"] <= datetime.now().isoformat()
    )

    return {
        "settings": queue["settings"],
        "total_keywords": len(keywords),
        "by_status": by_status,
        "by_batch": by_batch,
        "next_batch": next_batch,
        "recheck_due": recheck_due,
        "imports": queue.get("imports", []),
        "keywords": keywords,
    }


def sync_queue_prompts() -> int:
    """Ensure active queue keywords have matching prompts. Fix broken ones."""
    queue = _load_queue()
    data = _load_data()
    existing_prompts = {p["text"].lower(): p["id"] for p in data.get("prompts", [])}

    repaired = 0
    for kw in queue["keywords"]:
        if kw["status"] in ("active", "archived", "retained") and not kw.get("prompt_id"):
            kw["status"] = "queued"
            repaired += 1
            continue
        if kw["status"] != "active":
            continue
        if kw["keyword"].lower() in existing_prompts:
            kw["prompt_id"] = existing_prompts[kw["keyword"].lower()]
            continue
        prompt = add_prompt(kw["keyword"])
        kw["prompt_id"] = prompt["id"]
        repaired += 1

    if repaired:
        _save_queue(queue)
    return repaired


def activate_next_batch() -> dict:
    queue = _load_queue()
    keywords = queue["keywords"]

    queued_batches = sorted(set(
        kw["batch"] for kw in keywords if kw["status"] == "queued"
    ))
    if not queued_batches:
        return {"error": "No more batches in queue.", "activated": 0}

    batch_num = queued_batches[0]
    batch_keywords = [kw for kw in keywords if kw["batch"] == batch_num and kw["status"] == "queued"]

    data = _load_data()
    activated = []
    for kw in batch_keywords:
        prompt_id = f"p{len(data['prompts']) + 1}_{int(time.time())}_{len(activated)}"
        prompt = {
            "id": prompt_id, "text": kw["keyword"],
            "engines": list(_QUERY_FNS.keys()),
            "created_at": datetime.now().isoformat(),
        }
        data["prompts"].append(prompt)
        kw["status"] = "active"
        kw["prompt_id"] = prompt_id
        kw["activated_at"] = datetime.now().isoformat()
        activated.append(kw["keyword"])

    _save_data(data)
    _save_queue(queue)

    remaining_queued = sum(1 for kw in keywords if kw["status"] == "queued")
    remaining_batches = len(set(kw["batch"] for kw in keywords if kw["status"] == "queued"))

    return {
        "batch": batch_num,
        "activated": len(activated),
        "keywords": activated,
        "remaining_keywords": remaining_queued,
        "remaining_batches": remaining_batches,
    }


def process_batch_results():
    queue = _load_queue()
    vis_data = _load_data()
    results = vis_data.get("results", [])

    active_keywords = [kw for kw in queue["keywords"] if kw["status"] == "active"]
    if not active_keywords:
        return {"processed": 0}

    latest = {}
    for r in results:
        key = f"{r['prompt']}|{r['engine']}"
        if key not in latest or r.get("timestamp", "") > latest[key].get("timestamp", ""):
            latest[key] = r

    processed = 0
    retained = 0
    archived = 0

    for kw in active_keywords:
        kw_results = [r for r in latest.values() if r.get("prompt") == kw["keyword"]]
        if not kw_results:
            continue

        non_error = [r for r in kw_results if not r.get("error")]
        mentioned_engines = [r["engine"] for r in non_error if r.get("mentioned")]
        was_mentioned = len(mentioned_engines) > 0

        kw["last_checked_at"] = datetime.now().isoformat()
        kw["last_result"] = {
            "mentioned": was_mentioned,
            "engines_mentioned": mentioned_engines,
            "engines_total": len(non_error),
        }

        if was_mentioned:
            kw["status"] = "retained"
            retained += 1
        else:
            kw["status"] = "archived"
            kw["next_recheck_at"] = (datetime.now() + timedelta(days=30)).isoformat()
            archived += 1

        processed += 1

    _save_queue(queue)

    return {
        "processed": processed,
        "retained": retained,
        "archived": archived,
    }


def reactivate_rechecks() -> dict:
    queue = _load_queue()
    now = datetime.now().isoformat()

    recheck_kws = [
        kw for kw in queue["keywords"]
        if kw["status"] == "archived"
        and kw.get("next_recheck_at")
        and kw["next_recheck_at"] <= now
    ]

    data = _load_data()
    existing_prompts = {p["text"].lower(): p["id"] for p in data.get("prompts", [])}
    activated = []
    for kw in recheck_kws:
        if kw["keyword"].lower() in existing_prompts:
            kw["prompt_id"] = existing_prompts[kw["keyword"].lower()]
        else:
            prompt = add_prompt(kw["keyword"])
            kw["prompt_id"] = prompt["id"]
        kw["status"] = "active"
        kw["activated_at"] = datetime.now().isoformat()
        kw["next_recheck_at"] = None
        activated.append(kw["keyword"])

    _save_queue(queue)
    return {"reactivated": len(activated), "keywords": activated}


def update_queue_settings(batch_size: int = None):
    queue = _load_queue()
    if batch_size is not None:
        queue["settings"]["batch_size"] = batch_size
    _save_queue(queue)


def delete_keywords(keywords_to_delete: list[str]) -> dict:
    """Delete specific keywords from the queue."""
    queue = _load_queue()
    to_delete = {k.lower() for k in keywords_to_delete}
    removed = []
    kept = []
    for kw in queue["keywords"]:
        if kw["keyword"].lower() in to_delete:
            if kw.get("prompt_id") and kw["status"] in ("active", "retained", "archived"):
                remove_prompt(kw["prompt_id"])
            removed.append(kw["keyword"])
        else:
            kept.append(kw)
    queue["keywords"] = kept
    _save_queue(queue)
    return {"deleted": len(removed), "remaining": len(kept)}


def activate_selected(keywords_to_activate: list[str]) -> dict:
    """Activate specific keywords by adding them as prompts."""
    queue = _load_queue()
    data = _load_data()
    to_activate = {k.lower() for k in keywords_to_activate}
    activated = []
    for kw in queue["keywords"]:
        if kw["keyword"].lower() in to_activate and kw["status"] == "queued":
            prompt_id = f"p{len(data['prompts']) + 1}_{int(time.time())}_{len(activated)}"
            prompt = {
                "id": prompt_id, "text": kw["keyword"],
                "engines": list(_QUERY_FNS.keys()),
                "created_at": datetime.now().isoformat(),
            }
            data["prompts"].append(prompt)
            kw["status"] = "active"
            kw["prompt_id"] = prompt_id
            kw["activated_at"] = datetime.now().isoformat()
            activated.append(kw["keyword"])
    _save_data(data)
    _save_queue(queue)
    remaining_queued = sum(1 for kw in queue["keywords"] if kw["status"] == "queued")
    return {
        "activated": len(activated),
        "keywords": activated,
        "remaining_keywords": remaining_queued,
    }


def clear_queue():
    queue = _load_queue()
    active_prompt_ids = [kw["prompt_id"] for kw in queue["keywords"]
                         if kw.get("prompt_id") and kw["status"] in ("active", "retained")]
    for pid in active_prompt_ids:
        remove_prompt(pid)
    queue["keywords"] = []
    queue["imports"] = []
    _save_queue(queue)
