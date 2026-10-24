"""Sales engine — turn the data Governor already has into money.

This module answers two questions for a non-technical operator:

  1. "Where am I losing sales right now?"  -> build_sales_leaks()
  2. "What do I do TODAY to get some back?" -> build_daily_plan()

Everything is computed from numbers the evaluation already persists in
`latest_evaluation.json`:

  * eval_data["ga4_account"]  -> site-wide {sessions, add_to_carts, purchases,
    revenue, aov} across all channels (the real funnel + a reliable AOV).
  * eval_data["results"][*]   -> per page: ga4_sessions (ORGANIC), ga4_purchases,
    ga4_add_to_carts, ga4_revenue, and for matched products item_purchases /
    item_added_to_cart / item_revenue (all channels).

Hard rule: NEVER invent a number. If the data to size a leak isn't present, the
leak is either omitted or shown without a dollar figure — and sales_readiness()
tells the operator exactly what to connect so it CAN be sized. Dollar figures are
estimates over the evaluation window (≈ a month) and are labelled as such.

No third-party imports — pure stdlib so it is trivially testable.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Optional

# ── Tunables (conservative on purpose) ──────────────────────────────────────
# A healthy cart→purchase ("close") rate. Below this is a real, recoverable leak.
TARGET_CLOSE = 0.30
# Don't flag a product as leaky on a handful of carts — too noisy to act on.
MIN_CARTS = 5
# A product/category page needs this many organic visitors before "no sales"
# means something rather than just being quiet.
MIN_SESSIONS = 30
# Cap the assumed conversion rate we credit dead traffic with, so leak #3 can
# never over-promise even if the site CVR is freakishly high.
MAX_ASSUMED_CVR = 0.05
# A product "converts well" (worth sending more traffic to) at this multiple of
# the site's own average conversion rate.
WINNER_CVR_MULT = 1.5

_BOOK_RE = re.compile(
    r"\b(book|storybook|story ?book|i ?see ?me|iseeme|goodnight|bedtime|"
    r"personalized (?:book|story))\b", re.I)


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _aov(account: dict) -> Optional[float]:
    """Average order value from account totals (all channels). None if unknown —
    callers must treat None as 'can't put a dollar figure on it yet'."""
    aov = account.get("aov")
    if aov:
        return float(aov)
    rev, orders = _num(account.get("revenue")), _num(account.get("purchases"))
    return round(rev / orders, 2) if rev > 0 and orders > 0 else None


def _site_cvr(account: dict) -> Optional[float]:
    sess, orders = _num(account.get("sessions")), _num(account.get("purchases"))
    return (orders / sess) if sess > 0 and orders > 0 else None


def _is_product(r: dict) -> bool:
    return (r.get("asset_type") or "").lower() in ("product", "category")


def _label(r: dict) -> str:
    return (r.get("title") or r.get("url") or "").strip() or r.get("url", "")


# ── Leak builders ────────────────────────────────────────────────────────────

def _leak_checkout(account: dict) -> Optional[dict]:
    """Site-wide cart→purchase abandonment. The single biggest, most universal
    leak: people who put something in the cart and never buy."""
    carts, orders = _num(account.get("add_to_carts")), _num(account.get("purchases"))
    if carts < MIN_CARTS or orders <= 0:
        return None
    close = orders / carts
    aov = _aov(account)
    dollars = None
    if close < TARGET_CLOSE and aov:
        dollars = round((TARGET_CLOSE - close) * carts * aov)
    sev = "high" if (dollars or 0) >= 500 else "medium" if close < TARGET_CLOSE else "low"
    return {
        "id": "checkout_abandonment",
        "category": "conversion",
        "severity": sev,
        "dollars": dollars,
        "title": "People add to cart and don't buy",
        "what": (f"{int(carts)} add-to-carts turned into only {int(orders)} orders "
                 f"({close*100:.0f}% close rate). A healthy store closes ~{int(TARGET_CLOSE*100)}%."),
        "why": ("This is pure lost money — these shoppers already wanted the product "
                "enough to add it. Something between the cart and the thank-you page "
                "is stopping them: surprise shipping cost, a forced account, too many "
                "form fields, or no trust/returns reassurance."),
        "steps": [
            "Buy one of your own products on your phone, start to finish. Time it and "
            "note every moment you hesitate — that's your list.",
            "Show the shipping cost (or 'free shipping over $X') BEFORE checkout, not as "
            "a surprise at the end.",
            "Allow guest checkout — never force account creation to buy.",
            "Cut the checkout to the fewest fields possible; enable address autofill.",
            "Put returns/guarantee + a security badge right next to the Pay button.",
            "Turn on an abandoned-cart email (recover ~10% of these for near-zero cost).",
        ],
        "evidence": {"add_to_carts": int(carts), "orders": int(orders),
                     "close_rate_pct": round(close * 100, 1),
                     "target_pct": int(TARGET_CLOSE * 100), "aov": aov},
        "where": [],
    }


def _leak_leaky_products(results: list, account: dict) -> list:
    """Product pages where lots of people add to cart but few buy — your worst
    offenders, ranked by estimated lost dollars."""
    aov = _aov(account)
    out = []
    for r in results:
        if not _is_product(r):
            continue
        carts = _num(r.get("item_added_to_cart")) or _num(r.get("ga4_add_to_carts"))
        buys = _num(r.get("item_purchases")) or _num(r.get("ga4_purchases"))
        if carts < MIN_CARTS:
            continue
        close = (buys / carts) if carts else 0
        if close >= TARGET_CLOSE:
            continue
        lost_orders = (TARGET_CLOSE - close) * carts
        unit = (_num(r.get("item_revenue")) / buys) if buys and _num(r.get("item_revenue")) else aov
        dollars = round(lost_orders * unit) if unit else None
        steps = [
            f"Open {r.get('url','the page')} and look at it as a first-time buyer on a phone.",
            "Make the price, shipping and 'in stock' status impossible to miss above the fold.",
            "Add 3–5 real customer reviews near the Add-to-Cart button (social proof closes carts).",
            "State the returns / satisfaction guarantee on the page, not buried in a footer link.",
        ]
        if _BOOK_RE.search(_label(r)):
            steps.insert(1, "BOOKS: add a personalization preview or a 'free proof before we "
                            "print' promise — the missing preview is your known book leak.")
        out.append({
            "id": "leaky_product:" + (r.get("url") or _label(r)),
            "category": "conversion",
            "severity": "high" if (dollars or 0) >= 300 else "medium",
            "dollars": dollars,
            "title": f"Cart abandoned: {_label(r)}",
            "what": (f"{int(carts)} added to cart, only {int(buys)} bought "
                     f"({close*100:.0f}% close)."),
            "why": ("People want this product — they added it — but the page or checkout "
                    "loses them. Fixing one high-traffic product page is the fastest money "
                    "on the site."),
            "steps": steps,
            "evidence": {"add_to_carts": int(carts), "orders": int(buys),
                         "close_rate_pct": round(close * 100, 1)},
            "where": [r.get("url")] if r.get("url") else [],
        })
    out.sort(key=lambda x: (x["dollars"] is None, -(x["dollars"] or 0)))
    return out


def _leak_dead_traffic(results: list, account: dict) -> list:
    """Product/category pages that get real organic visitors and sell NOTHING —
    not even an add-to-cart. Buyers landing and leaving."""
    aov = _aov(account)
    cvr = _site_cvr(account)
    assumed = min(cvr, MAX_ASSUMED_CVR) if cvr else None
    out = []
    for r in results:
        if not _is_product(r):
            continue
        sess = _num(r.get("ga4_sessions"))
        buys = _num(r.get("item_purchases")) or _num(r.get("ga4_purchases"))
        carts = _num(r.get("item_added_to_cart")) or _num(r.get("ga4_add_to_carts"))
        if sess < MIN_SESSIONS or buys > 0 or carts > 0:
            continue
        dollars = round(sess * assumed * aov) if (assumed and aov) else None
        out.append({
            "id": "dead_traffic:" + (r.get("url") or _label(r)),
            "category": "conversion",
            "severity": "medium" if (dollars or 0) >= 150 else "low",
            "dollars": dollars,
            "title": f"Traffic but zero sales: {_label(r)}",
            "what": (f"{int(sess)} organic visits in the window, 0 add-to-carts, 0 orders."),
            "why": ("Visitors arrive and leave without even trying to buy. Either the page "
                    "doesn't match what they searched for, the offer/price isn't clear, or "
                    "there's no reason to trust you yet."),
            "steps": [
                f"Check the search terms bringing people to {r.get('url','this page')} "
                "(GSC) — does the page actually answer them?",
                "Make the main offer + a clear call-to-action visible without scrolling.",
                "Add reviews / photos / a guarantee so a stranger has a reason to buy.",
                "If the page is informational (a guide), link it to the matching product "
                "page — a reader is not a buyer until you point them at the thing to buy.",
            ],
            "evidence": {"organic_sessions": int(sess), "add_to_carts": 0, "orders": 0},
            "where": [r.get("url")] if r.get("url") else [],
        })
    out.sort(key=lambda x: (x["dollars"] is None, -(x["dollars"] or 0)))
    return out


def _winners_starved(results: list, account: dict) -> list:
    """The happy opposite: products that CONVERT well but barely get traffic.
    Tells the operator where to point ad spend / internal links for fast wins."""
    cvr = _site_cvr(account)
    if not cvr:
        return []
    aov = _aov(account)
    out = []
    for r in results:
        if not _is_product(r):
            continue
        sess = _num(r.get("ga4_sessions"))
        buys = _num(r.get("item_purchases")) or _num(r.get("ga4_purchases"))
        if buys <= 0 or sess < 10:
            continue
        pcvr = buys / sess
        if pcvr < cvr * WINNER_CVR_MULT:
            continue
        per_100 = round(pcvr * 100 * aov) if aov else None
        out.append({
            "id": "winner:" + (r.get("url") or _label(r)),
            "category": "growth",
            "severity": "opportunity",
            "dollars": None,  # upside, not a leak — don't book it as recovered money
            "title": f"Proven converter, starved: {_label(r)}",
            "what": (f"Converts at {pcvr*100:.1f}% (vs site {cvr*100:.1f}%) on only "
                     f"{int(sess)} organic visits."
                     + (f" ~${per_100} per 100 visitors." if per_100 else "")),
            "why": ("This product already sells when people see it. The cheapest growth "
                    "you have is sending it MORE of the traffic you're already paying for."),
            "steps": [
                "Point your ad budget at this product first — it has the best proven close.",
                "Link to it from your homepage and your most-visited pages.",
                "Feature it in any roundup pitch / gift guide — lead with a proven winner.",
            ],
            "evidence": {"organic_sessions": int(sess), "orders": int(buys),
                         "product_cvr_pct": round(pcvr * 100, 2),
                         "site_cvr_pct": round(cvr * 100, 2)},
            "where": [r.get("url")] if r.get("url") else [],
        })
    out.sort(key=lambda x: -(x["evidence"]["product_cvr_pct"]))
    return out


def _leak_no_reviews(results: list, reviews: dict) -> list:
    """Products with real traffic but no stored reviews — a social-proof leak we
    can actually fix with the review engine. Only emitted when we KNOW the review
    store is wired (reviews is a dict); never fabricates a count."""
    if reviews is None:
        return []
    have = set()
    for url in (reviews or {}):
        have.add(url)
    out = []
    for r in results:
        if (r.get("asset_type") or "").lower() != "product":
            continue
        sess = _num(r.get("ga4_sessions"))
        url = r.get("url", "")
        if sess < MIN_SESSIONS or url in have:
            continue
        out.append({
            "id": "no_reviews:" + (url or _label(r)),
            "category": "discovery",
            "severity": "low",
            "dollars": None,
            "title": f"No reviews yet: {_label(r)}",
            "what": f"{int(sess)} organic visits, no customer reviews on file.",
            "why": ("Reviews are the #1 thing that converts a hesitant shopper, and they're "
                    "exactly what Google and AI engines quote. Free to collect, high payoff."),
            "steps": [
                "Email the last 10 buyers of this product and ask for a one-line review.",
                "Paste what they say into Governor's Content Studio review box so it's reused.",
                "Add review schema so the stars show in Google and AI answers.",
            ],
            "evidence": {"organic_sessions": int(sess), "reviews_on_file": 0},
            "where": [url] if url else [],
        })
    out.sort(key=lambda x: -x["evidence"]["organic_sessions"])
    return out


def build_sales_leaks(eval_data: dict, config: dict = None,
                      reviews: dict = None) -> dict:
    """Top-level: every money leak + growth opening, from real persisted data.

    Returns {leaks, winners, totals, generated_from}. `leaks` is sorted by
    estimated monthly dollars (sized leaks first, then unsized). Nothing is
    invented — an empty list means the data to see leaks isn't there yet (see
    sales_readiness)."""
    eval_data = eval_data or {}
    account = eval_data.get("ga4_account") or {}
    results = eval_data.get("results") or []

    leaks = []
    co = _leak_checkout(account)
    if co:
        leaks.append(co)
    leaks += _leak_leaky_products(results, account)
    leaks += _leak_dead_traffic(results, account)
    leaks += _leak_no_reviews(results, reviews)

    # Sized leaks first (by $), then unsized, with a stable severity tiebreak.
    sev_rank = {"high": 0, "medium": 1, "low": 2, "opportunity": 3}
    leaks.sort(key=lambda x: (x["dollars"] is None, -(x["dollars"] or 0),
                              sev_rank.get(x["severity"], 9)))

    winners = _winners_starved(results, account)
    recoverable = sum(l["dollars"] for l in leaks if l.get("dollars"))
    return {
        "leaks": leaks,
        "winners": winners,
        "totals": {
            "recoverable_monthly": round(recoverable) if recoverable else None,
            "leak_count": len(leaks),
            "aov": _aov(account),
            "site_cvr_pct": round(_site_cvr(account) * 100, 2) if _site_cvr(account) else None,
        },
        "generated_from": eval_data.get("timestamp", ""),
    }


# ── Readiness: exactly what the module needs to work ─────────────────────────

def sales_readiness(eval_data: dict, reviews: dict = None) -> dict:
    """Plain-English checklist of what Governor needs to find sales leaks. Each
    item: {key, ok, label, fix}. `ready` is true when the essentials are met."""
    eval_data = eval_data or {}
    account = eval_data.get("ga4_account") or {}
    results = eval_data.get("results") or []

    has_eval = bool(results)
    has_funnel = _num(account.get("add_to_carts")) > 0 and _num(account.get("purchases")) > 0
    has_aov = _aov(account) is not None
    has_items = any(r.get("item_purchases") is not None for r in results)
    has_reviews = bool(reviews)

    checks = [
        {"key": "evaluation", "ok": has_eval,
         "label": "A recent site evaluation (with crawl)",
         "fix": "Run an evaluation with crawl on the Setup page — it's the page list "
                "everything else reads from."},
        {"key": "ga4_ecommerce", "ok": has_funnel,
         "label": "GA4 ecommerce events (add_to_cart + purchase)",
         "fix": "This is the big one. Your Magento store must send GA4 ecommerce events — "
                "at least add_to_cart and purchase (view_item helps too). Without them "
                "Governor can SEE visits but is blind to the cart→buy leak. Set this up in "
                "Google Tag Manager / your GA4 ecommerce tagging, then re-run the evaluation."},
        {"key": "aov", "ok": has_aov,
         "label": "Average order value (for dollar figures)",
         "fix": "Comes automatically once GA4 purchase events carry revenue. Until then, "
                "leaks still rank by size but can't show a dollar amount."},
        {"key": "item_sales", "ok": has_items,
         "label": "Per-product sales matching (sharper product leaks)",
         "fix": "Optional. Enabled by GA4 item-scoped ecommerce (item_name on purchase "
                "events). Improves which exact product pages are flagged; without it "
                "Governor falls back to page-level GA4 numbers."},
        {"key": "reviews", "ok": has_reviews,
         "label": "Review store (social-proof leaks)",
         "fix": "Optional. Paste customer reviews into Content Studio; Governor then knows "
                "which products lack social proof."},
    ]
    essentials = ("evaluation", "ga4_ecommerce")
    ready = all(c["ok"] for c in checks if c["key"] in essentials)
    return {"ready": ready, "checks": checks}


# ── Daily plan: "just tell me what to do today" ──────────────────────────────

# Rotating second slot — one pillar per weekday so all three (conversion /
# discovery / content) get worked over a week, not just the top leak forever.
_WEEKLY = {
    0: {"category": "discovery", "title": "Ask for reviews",
        "minutes": 15,
        "why": "Reviews convert hesitant shoppers and are what Google & AI engines quote.",
        "how": ["Email your last ~10 buyers and ask for one line about what they liked.",
                "Paste each reply into Content Studio's review box so it gets reused.",
                "Aim for 3 new reviews on your best-selling product this week."]},
    1: {"category": "discovery", "title": "Advance one roundup placement",
        "minutes": 20,
        "why": "Roundups are what LLMs cite — and you're already on AWIN, so the rail exists.",
        "how": ["Pick ONE 'best personalized gift / Montessori toy' article you want to be in.",
                "Find the writer's name on the byline; note the site.",
                "In AWIN: check that site is a publisher you can recruit, or add it to your "
                "hit-list; if they're not on AWIN, plan to gift them the product instead."]},
    2: {"category": "content", "title": "Point the writer at a money page",
        "minutes": 20,
        "why": "Commercial pages (products / buying guides) make money; blog filler doesn't.",
        "how": ["Pick one product family that converts.",
                "In Content Studio, draft a buying-guide / comparison page for it (not a "
                "generic blog post).",
                "Link it to the product pages it talks about."]},
    3: {"category": "growth", "title": "Feed your proven winners",
        "minutes": 15,
        "why": "Sending traffic to products that already convert is the cheapest growth there is.",
        "how": ["Open the 'Proven converters' list on this page.",
                "Move some ad budget toward the top one.",
                "Add a homepage / top-page link to it."]},
    4: {"category": "conversion", "title": "Polish one product page",
        "minutes": 20,
        "why": "Small trust/clarity fixes on a high-traffic page pay off immediately.",
        "how": ["Pick your highest-traffic product page.",
                "Add or refresh reviews; make price + shipping + guarantee obvious.",
                "View it on a phone and fix whatever looks cramped."]},
    5: {"category": "discovery", "title": "One inbound pitch",
        "minutes": 15,
        "why": "Writers who ASK for products feature them far more than cold outreach.",
        "how": ["Check Featured.com / Qwoted / SourceBottle for a relevant request.",
                "Respond with a product photo + one-line pitch.",
                "Log it so you can track if it lands."]},
    6: {"category": "conversion", "title": "Rest / review the week",
        "minutes": 10,
        "why": "Check the leak numbers moved; pick what matters tomorrow.",
        "how": ["Glance at the top leak's close rate vs last week.",
                "Mark any fix you shipped as done so a new one surfaces."]},
}

# Small 10-minute momentum tasks (slot 3), rotated by day-of-month parity.
_MOMENTUM = [
    {"title": "Reply to one customer review", "minutes": 10,
     "why": "Public replies build trust and show the store is alive.",
     "how": ["Find one recent review (good or bad) and reply briefly and warmly."]},
    {"title": "Add one trust line to a product page", "minutes": 10,
     "why": "A single reassurance ('Ships in 2 days • Easy returns') lifts conversion.",
     "how": ["Add a short trust line right under the Add-to-Cart button on one page."]},
    {"title": "Screenshot your checkout on a phone", "minutes": 10,
     "why": "You can't fix a leak you haven't watched happen.",
     "how": ["Start a real purchase on your phone; screenshot every step; note friction."]},
]


def _parse_date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat((s or "")[:10])
    except (ValueError, TypeError):
        return None


def _streak(state: dict, today: date) -> int:
    """Consecutive days (ending today or yesterday) with at least one item done."""
    days = state.get("days") or {}
    done_dates = {d for d, v in days.items() if (v or {}).get("done")}
    n, cur = 0, today
    if today.isoformat() not in done_dates:
        cur = today - timedelta(days=1)  # today not done yet; count the run up to yesterday
    while cur.isoformat() in done_dates:
        n += 1
        cur -= timedelta(days=1)
    return n


def build_daily_plan(leaks_bundle: dict, state: dict = None,
                     today: date = None) -> dict:
    """The ordered, max-3-item 'do today' list. Idiot-proof: item 1 is always the
    biggest unfixed money leak, item 2 rotates through the three growth pillars by
    weekday, item 3 is a 10-minute momentum task.

    `state` is the persisted sales_plan store: {"fixed": {leak_id: true},
    "days": {"YYYY-MM-DD": {"done": [item_id, ...]}}}. Marking items done lives in
    the API; this function only reads state to compute status + streak."""
    state = state or {}
    today = today or date.today()
    tkey = today.isoformat()
    fixed = state.get("fixed") or {}
    done_today = set(((state.get("days") or {}).get(tkey) or {}).get("done") or [])

    leaks = (leaks_bundle or {}).get("leaks") or []
    items = []

    # Slot 1 — the biggest money leak that isn't marked fixed.
    top = next((l for l in leaks if not fixed.get(l["id"])), None)
    if top:
        d = top.get("dollars")
        items.append({
            "id": f"leak:{top['id']}",
            "slot": 1,
            "category": top["category"],
            "title": top["title"],
            "minutes": 30,
            "why": (f"Your biggest fixable leak — about ${d:,}/month on the line. "
                    if d else "Your top money leak right now. ") + top["why"],
            "how": top["steps"],
            "dollars": d,
            "leak_id": top["id"],
            "done": f"leak:{top['id']}" in done_today,
        })

    # Slot 2 — rotating pillar by weekday.
    wk = dict(_WEEKLY[today.weekday()])
    items.append({
        "id": f"weekly:{today.weekday()}",
        "slot": 2,
        "category": wk["category"],
        "title": wk["title"],
        "minutes": wk["minutes"],
        "why": wk["why"],
        "how": wk["how"],
        "dollars": None,
        "done": f"weekly:{today.weekday()}" in done_today,
    })

    # Slot 3 — 10-minute momentum task.
    mo = _MOMENTUM[today.toordinal() % len(_MOMENTUM)]
    items.append({
        "id": f"momentum:{today.toordinal() % len(_MOMENTUM)}",
        "slot": 3,
        "category": "momentum",
        "title": mo["title"],
        "minutes": mo["minutes"],
        "why": mo["why"],
        "how": mo["how"],
        "dollars": None,
        "done": f"momentum:{today.toordinal() % len(_MOMENTUM)}" in done_today,
    })

    done_count = sum(1 for i in items if i["done"])
    return {
        "date": tkey,
        "items": items,
        "done_count": done_count,
        "total": len(items),
        "all_done": done_count == len(items) and len(items) > 0,
        "streak": _streak(state, today),
    }
