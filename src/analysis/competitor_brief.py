"""Competitor Brief — the SERP-optimization layer for the content writer.

Given a target keyword, this pulls the live top-ranking pages (via the existing
Serper client) and distills what it takes to compete, the way NeuronWriter does:

  • TERMS/ENTITIES the ranking pages share (document-frequency weighted n-grams) —
    the vocabulary Google expects the topic to cover;
  • the HEADINGS competitors use — the sub-topics/sections to include;
  • the QUESTIONS to answer (People-Also-Ask + question headings);
  • a TARGET LENGTH (median competitor word count) so depth matches the SERP.

It is injected with the fetchers (a SERP fetch and a page fetch) so the pure
extraction is testable and the network stays in the web layer. Everything is
grounded in real ranking pages — nothing invented. Degrades to None when SERP is
unavailable (no key / quota), and the drafter simply proceeds without it.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from urllib.parse import urlparse

# Marketplaces/UGC we skip when picking pages to learn from — their term mix is
# listings/boilerplate, not the editorial coverage we're trying to match.
_SKIP_DOMAINS = ("amazon.", "etsy.", "ebay.", "walmart.", "target.", "aliexpress.",
                 "alibaba.", "temu.", "wayfair.", "pinterest.", "youtube.", "reddit.",
                 "tiktok.", "facebook.", "instagram.")

_STOP = set("""a about above after again against all am an and any are aren't as at be
because been before being below between both but by can cannot could couldn't did
didn't do does doesn't doing don't down during each few for from further had hadn't
has hasn't have haven't having he he'd he'll he's her here here's hers herself him
himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself
let's me more most mustn't my myself no nor not of off on once only or other ought
our ours ourselves out over own same shan't she she'd she'll she's should shouldn't
so some such than that that's the their theirs them themselves then there there's
these they they'd they'll they're they've this those through to too under until up
very was wasn't we we'd we'll we're we've were weren't what what's when when's where
where's which while who who's whom why why's with won't would wouldn't you you'd
you'll you're you've your yours yourself yourselves will just also get make made use
using used one two three new best top guide tips ways list review reviews year years
old kids child children toddler toddlers baby babies also may many much well like
need want help great good right time way things find look looking choose choosing
""".split())

_TAG_DROP = re.compile(r"<(script|style|nav|footer|header|form|noscript|svg)\b.*?</\1>",
                       re.S | re.I)
_H_RE = re.compile(r"<h([23])\b[^>]*>(.*?)</h\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-z][a-z'\-]{1,}")


# Authority domains worth citing when they appear in the live SERP — real, ranking,
# live URLs beat a model-guessed deep path. .gov/.edu plus known child-dev orgs.
_AUTHORITY_RE = re.compile(
    r"(\.gov|\.edu)(/|$|:)|(?:healthychildren|aap|naeyc|cdc|nih|ncbi\.nlm|zerotothree|"
    r"montessori-ami|amshq|nifplay|developingchild\.harvard|who\.int|unicef|aacap|"
    r"aota|asha)\b", re.I)


def _authority_links(organic: list) -> list:
    """Real authority URLs present in the live SERP (one per domain), to hand the
    drafter as citeable sources instead of letting it guess a deep path."""
    out, seen = [], set()
    for r in (organic or []):
        u = r.get("url") or ""
        dom = _domain(u)
        if not dom or dom in seen:
            continue
        if _AUTHORITY_RE.search(u):
            seen.add(dom)
            out.append({"url": u, "title": (r.get("title") or "").strip(), "domain": dom})
    return out[:5]


def _domain(url: str) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def _clean_text(html: str) -> str:
    s = _TAG_DROP.sub(" ", html or "")
    s = _TAG.sub(" ", s)
    s = re.sub(r"&[a-z#0-9]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _headings(html: str) -> list:
    out = []
    for _lvl, inner in _H_RE.findall(html or ""):
        t = re.sub(r"\s+", " ", _TAG.sub("", inner)).strip()
        if 3 <= len(t) <= 90:
            out.append(t)
    return out


def _tokens(text: str) -> list:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2]


def _ngrams(tokens: list, n: int) -> list:
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def _is_question(s: str) -> bool:
    s = (s or "").strip()
    return s.endswith("?") or bool(re.match(r"(?i)^(what|how|why|when|where|which|who|can|are|is|do|does|should)\b", s))


# Competitor brand / self-promo phrasing. A scraped heading like "What makes Hazel
# & Fawn a trusted source" is a COMPETITOR promoting itself — it must never become
# a section or FAQ on our page. Drop any heading/question matching these.
_PROMO_RE = re.compile(
    r"(trusted source|why (?:choose|shop|buy)\b|best place to buy|about us|our (?:story|mission|"
    r"commitment|promise|values)|what makes .+\b(?:a |the )?(?:trusted|best|top|leading|reliable|"
    r"go-to|number[ -]?one)\b|customer (?:reviews|testimonials)|free shipping|returns?\b|"
    r"discount|coupon|promo code|why we|meet the team|\bgift (?:card|guide)s?\b)", re.I)


def _is_promo(s: str) -> bool:
    return bool(_PROMO_RE.search(s or ""))


def extract_from_pages(pages: list, keyword: str) -> dict:
    """Pure distillation of scraped competitor pages into a coverage spec.
    `pages` = [{url, html}]. Terms are document-frequency weighted (how many
    competitors use them), which is a far better 'must-cover' signal than raw count."""
    kw_tokens = set(_tokens(keyword))
    df = Counter()          # term -> # of competitor pages containing it
    heading_counter = Counter()
    word_counts = []
    for p in pages:
        html = p.get("html") or ""
        text = _clean_text(html)
        toks = _tokens(text)
        if len(toks) < 50:
            continue        # too thin to be a real ranking article
        word_counts.append(len(toks))
        seen = set()
        for n in (1, 2, 3):
            for g in _ngrams(toks, n):
                parts = g.split()
                # skip grams that are entirely the query's own words (always present)
                if all(w in kw_tokens for w in parts):
                    continue
                if g not in seen:
                    seen.add(g)
                    df[g] += 1
        for h in _headings(html):
            heading_counter[re.sub(r"\s+", " ", h.strip())] += 1

    n_pages = max(len(word_counts), 1)
    # Keep terms used by at least 2 competitors (or 1 if the field is tiny), favour
    # multi-word phrases, cap the list.
    min_df = 2 if n_pages >= 3 else 1
    terms = [{"term": t, "in_pages": c} for t, c in df.most_common(400)
             if c >= min_df and not t.isdigit()]
    # Prefer phrases (2-3 words) then strong unigrams; keep the top ~30.
    phrases = [t for t in terms if " " in t["term"]][:20]
    unigrams = [t for t in terms if " " not in t["term"]][:15]
    top_terms = (phrases + unigrams)[:30]

    headings = [h for h, _ in heading_counter.most_common(40)
                if not _is_question(h) and not _is_promo(h)][:18]
    heading_qs = [h for h in heading_counter if _is_question(h) and not _is_promo(h)]

    target_words = int(statistics.median(word_counts)) if word_counts else 0
    return {
        "terms": top_terms,
        "headings": headings,
        "heading_questions": heading_qs[:10],
        "target_words": target_words,
        "pages_analyzed": len(word_counts),
    }


def build_competitor_brief(keyword: str, fetch_serp_fn, fetch_page_fn,
                           own_domain: str = "", max_pages: int = 8) -> dict | None:
    """Live SERP → competitor coverage spec. `fetch_serp_fn(query)` returns the
    Serper result dict (organic_results, people_also_ask); `fetch_page_fn(url)`
    returns a page's HTML or None. Returns the brief, or None if the SERP is
    unavailable (caller proceeds without it)."""
    kw = (keyword or "").strip()
    if not kw:
        return None
    serp = fetch_serp_fn(kw)
    if not serp:
        return None
    own = (own_domain or "").lower().replace("www.", "")
    urls, seen_dom = [], set()
    for r in (serp.get("organic_results") or []):
        u = r.get("url") or ""
        dom = _domain(u)
        if not dom or dom in seen_dom:
            continue
        if own and own in dom:
            continue                       # don't learn from our own page
        if any(m in dom for m in _SKIP_DOMAINS):
            continue
        seen_dom.add(dom)
        urls.append(u)
        if len(urls) >= max_pages:
            break
    pages = []
    for u in urls:
        try:
            html = fetch_page_fn(u)
        except Exception:
            html = None
        if html:
            pages.append({"url": u, "html": html})
    paa = [q for q in (serp.get("people_also_ask") or []) if q]
    if not pages and not paa:
        return None
    spec = extract_from_pages(pages, kw) if pages else {
        "terms": [], "headings": [], "heading_questions": [],
        "target_words": 0, "pages_analyzed": 0}
    # Merge PAA with question-headings found on the pages; dedupe case-insensitively.
    qseen, questions = set(), []
    for q in paa + spec.pop("heading_questions", []):
        k = q.strip().lower()
        if k and k not in qseen and not _is_promo(q):   # never echo a competitor's self-promo
            qseen.add(k)
            questions.append(q.strip())
    spec["questions"] = questions[:10]
    spec["source_urls"] = urls
    spec["authority_links"] = _authority_links(serp.get("organic_results") or [])
    spec["keyword"] = kw
    return spec
