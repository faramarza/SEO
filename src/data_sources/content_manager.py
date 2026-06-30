"""Content Management — SEO strategy, topic clusters, article planning.

Manages topic clusters, articles, money pages, and products. Auto-discovers
content structure from page inventory and keyword data.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent.parent / "data"
CONTENT_PATH = DATA_PATH / "content_manager.json"
INVENTORY_PATH = DATA_PATH / "page_inventory.json"
QUEUE_PATH = DATA_PATH / "keyword_queue.json"
CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "defaults.json"
SITEMAP_PATH = DATA_PATH / "sitemap_types.json"


def _load_data():
    if CONTENT_PATH.exists():
        try:
            with open(CONTENT_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "clusters": [],
        "articles": [],
        "money_pages": [],
        "products": [],
        "last_analysis": None,
    }


def _save_data(data):
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = CONTENT_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(CONTENT_PATH)


def _load_inventory():
    if INVENTORY_PATH.exists():
        try:
            with open(INVENTORY_PATH) as f:
                return json.load(f).get("pages", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_keywords():
    if QUEUE_PATH.exists():
        try:
            with open(QUEUE_PATH) as f:
                return json.load(f).get("keywords", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _load_sitemap_urls():
    """Load the set of URLs from the imported sitemap."""
    if SITEMAP_PATH.exists():
        try:
            with open(SITEMAP_PATH) as f:
                return set(json.load(f).keys())
        except (json.JSONDecodeError, OSError):
            pass
    return set()


_DOMAIN_NOISE = {"alphabet", "trains", "train", "com", "www", "blog", "post", "https", "http"}

_NOT_CRAWLED = re.compile(r'^\[?NOT[_ ]?CRAWLED\]?$', re.IGNORECASE)


def _clean_title(title):
    """Return empty string for placeholder titles like [NOT_CRAWLED]."""
    if not title or _NOT_CRAWLED.match(title.strip()):
        return ""
    return title.strip()


def _title_from_slug(slug):
    """Generate a human-readable title from a URL slug."""
    name = slug.rsplit("/", 1)[-1]
    name = re.sub(r'\.html?$', '', name)
    return name.replace("-", " ").replace("_", " ").strip().title()


def _match_cluster(title, h1, url, clusters):
    """Match an article to the best cluster based on title/h1/URL content."""
    path = re.sub(r'https?://[^/]+', '', url)
    path_words = path.replace('-', ' ').replace('_', ' ')
    text = f"{title} {h1} {path_words}".lower()

    matches = []
    for cluster in clusters:
        # Use the regex pattern from _TOPIC_PATTERNS if this cluster was auto-discovered
        cl_name = cluster["name"]
        for pattern, topic_name in _TOPIC_PATTERNS:
            if topic_name == cl_name and re.search(pattern, text):
                matches.append((cluster["id"], len(cl_name)))
                break
        else:
            # Fallback: word matching for product family clusters
            name_words = [w for w in cl_name.lower().split() if len(w) > 2 and w not in _DOMAIN_NOISE]
            if not name_words:
                continue
            score = sum(1 for w in name_words if w in text)
            if score >= len(name_words):
                matches.append((cluster["id"], score))

    if not matches:
        return None
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches[0][0]


_SKIP_PATHS = {
    "/checkout", "/cart", "/account", "/login", "/search", "/privacy",
    "/terms", "/contact", "/about-us", "/shipping", "/return",
    "/sitemap", "/wishlist", "/customer", "/catalogsearch", "/review",
}

_BLOG_SKIP_PATTERNS = re.compile(r'/blog/(author|archive|tag|category|page)/')

_CATEGORY_PATTERNS = re.compile(
    r'^/('
    r'name-trains|wooden-name-puzzles|personalized-step-stools|'
    r'personalized-baby-books|personalized-books-for-kids|'
    r'personalized-baby-gifts|personalized-toys|personalized-coloring-books|'
    r'personalized-growth-charts|personalized-baby-blankets|'
    r'montessori-toys[^/]*|made-in-usa-montessori-toys|'
    r'classroom-rugs[^/]*|playroom-carpets|sensory-carpets|bilingual-rugs-for-kids|'
    r'kids-furniture|kids-chairs|kids-tables|classroom-furniture|playroom-furniture|'
    r'kids-puzzles|kids-educational-toys|kids-step-stools|'
    r'wooden-blocks|wooden-train-sets|sorting-toys|stacking-toys|sensory-toys|'
    r'magnetic-toys|pretend-play-toys|stem-toys|toy-boxes|dollhouse-furniture|'
    r'number-trains|nursery-decor|'
    r'shop-by-brands[^/]*|'
    r'specials|featured-products|free-montessori-printables'
    r')\.html$',
    re.IGNORECASE,
)


_TOPIC_PATTERNS = [
    (r'\bpersonaliz', "Personalized Gifts & Toys"),
    (r'\bmontessori\b|\bwaldorf\b', "Montessori Education"),
    (r'\balphabet\b|\bletter|\bliteracy|\bphonics|\bname.recogni', "Early Literacy & Alphabet Learning"),
    (r'\bchild\s*develop|\bdevelopment.milestone|\bfine.motor|\bgross.motor|\bsensory', "Child Development"),
    (r'\bwooden.toy|\bwood.toy', "Wooden Toys"),
    (r'\beducational.toy|\blearning.toy|\bstem\b', "Educational Toys"),
    (r'\bclassroom.rug|\bcarpet|\brug', "Classroom Rugs"),
    (r'\bgift.guide|\bgifts?\s+for|\bbest.gift|\bbuying.guide', "Gift Buying Guides"),
    (r'\bautis|\bspecial.need|\blearning.dis(order|abilit)|\banxiety|\bsensory.process', "Autism & Special Needs"),
    (r'\bplay.based|\blearn.*through.play|\bimagina|\bpretend.play|\bcircle.time', "Play-Based Learning"),
    (r'\bfor\s+\d+.year.old|\bby\s+age|\bmonth.old|\btoddler|\bpreschool|\bbaby\b|\bnewborn', "Learning by Age"),
    (r'\btoy.safe|\bsustainab|\bnon.toxic|\bbpa.free|\beco.friend|\bmade.in.usa', "Toy Safety & Sustainability"),
    (r'\bchristmas|\bholiday|\bbirthday|\bvalentine|\bhalloween|\bseason|\bback.to.school|\bmother|father.s.day', "Seasonal & Holiday Gifts"),
]


def _discover_topic_clusters(inventory):
    """Extract topic themes from blog titles/h1s/URLs."""
    topic_counts = {}
    for url, page_data in inventory.items():
        if not isinstance(page_data, dict):
            continue
        if "/blog/" not in url and "/post/" not in url:
            continue
        path = re.sub(r'https?://[^/]+', '', url).replace('-', ' ').replace('_', ' ')
        text = f"{page_data.get('title', '')} {page_data.get('h1', '')} {path}".lower()
        for pattern, topic_name in _TOPIC_PATTERNS:
            if re.search(pattern, text):
                topic_counts[topic_name] = topic_counts.get(topic_name, 0) + 1
    return {name: count for name, count in topic_counts.items() if count >= 2}


def auto_discover():
    """Analyze existing site data and populate clusters, articles, money pages."""
    data = _load_data()
    inventory = _load_inventory()
    config = _load_config()
    keywords = _load_keywords()

    product_families = config.get("business_context", {}).get("product_families", [])

    existing_cluster_names = {c["name"].lower() for c in data["clusters"]}

    # Discover topic clusters from blog content (not product families — those stay as products)
    topic_counts = _discover_topic_clusters(inventory)
    for j, (topic_name, count) in enumerate(sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)):
        if topic_name.lower() not in existing_cluster_names:
            data["clusters"].append({
                "id": f"cl_{int(time.time())}_t{j}",
                "name": topic_name,
                "description": f"Auto-discovered from {count} blog articles",
                "priority": "high" if count >= 10 else "medium",
                "status": "active",
                "target_articles": max(count * 2, 10),
                "pillar_page": None,
                "money_pages": [],
                "sub_clusters": [],
                "created_at": datetime.now().isoformat(),
            })
            existing_cluster_names.add(topic_name.lower())

    existing_urls = {a["url"] for a in data["articles"]}
    existing_mp_urls = {mp["url"] for mp in data["money_pages"]}

    # Deduplicate blog URLs: many posts appear at both /blog/post/slug and /blog/slug
    # Keep only the canonical (non-/post/) version; track seen slugs
    seen_slugs = set()
    for a in data["articles"]:
        slug = re.sub(r'.*/blog(/post)?/', '', a.get("url", "")).rstrip("/").lower()
        seen_slugs.add(slug)

    blog_candidates = []
    non_blog_pages = []
    for url, page_data in inventory.items():
        if not isinstance(page_data, dict):
            continue
        path = re.sub(r'https?://[^/]+', '', url).lower()
        if any(s in path for s in _SKIP_PATHS):
            continue
        if path in ("/", "") or path.rstrip("/") in ("/blog", "/faqs"):
            continue
        is_blog = "/blog/" in url or "/post/" in url
        is_faq = "/faq" in path
        if is_blog:
            blog_candidates.append((url, page_data))
        elif not is_faq:
            non_blog_pages.append((url, page_data))

    # Process blog articles — deduplicate and skip non-article pages
    for url, page_data in blog_candidates:
        if _BLOG_SKIP_PATTERNS.search(url):
            continue
        slug = re.sub(r'.*/blog(/post)?/', '', url).rstrip("/").lower()
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        if url in existing_urls:
            continue
        title = _clean_title(page_data.get("title", ""))
        h1 = _clean_title(page_data.get("h1", ""))
        cluster_id = _match_cluster(title, h1, url, data["clusters"])
        data["articles"].append({
            "id": f"art_{int(time.time())}_{len(data['articles'])}",
            "title": title or h1 or _title_from_slug(slug),
            "url": url,
            "cluster_id": cluster_id,
            "sub_cluster_id": None,
            "status": "published",
            "primary_keyword": None,
            "secondary_keywords": [],
            "search_intent": "informational",
            "search_volume": 0,
            "keyword_difficulty": 0,
            "business_value": "medium",
            "revenue_potential": "medium",
            "evergreen": True,
            "target_word_count": 1500,
            "products_supported": [],
            "money_pages": [],
            "notes": "",
            "created_at": datetime.now().isoformat(),
        })

    # Process non-blog pages — classify as category or product
    for url, page_data in non_blog_pages:
        if url in existing_mp_urls:
            continue
        title = _clean_title(page_data.get("title", ""))
        h1 = _clean_title(page_data.get("h1", ""))
        path = re.sub(r'https?://[^/]+', '', url).rstrip("/")
        is_category = bool(_CATEGORY_PATTERNS.search(path))
        data["money_pages"].append({
            "id": f"mp_{int(time.time())}_{len(data['money_pages'])}",
            "url": url,
            "title": title or h1 or _title_from_slug(path),
            "type": "category" if is_category else "product",
            "status": "active",
            "supporting_articles": [],
            "target_articles": 5 if is_category else 0,
            "authority_score": 0,
            "created_at": datetime.now().isoformat(),
        })

    existing_product_names = {p["name"].lower() for p in data["products"]}
    for i, pf in enumerate(product_families):
        if pf.lower() not in existing_product_names:
            matching_url = ""
            pf_words = {w for w in pf.lower().split() if len(w) > 2}
            for mp in data["money_pages"]:
                mp_text = f"{mp.get('title', '')} {mp.get('url', '')}".lower()
                if pf_words and all(w in mp_text for w in pf_words):
                    matching_url = mp["url"]
                    break
            data["products"].append({
                "id": f"prod_{int(time.time())}_{i}",
                "name": pf.title(),
                "url": matching_url,
                "category": "",
                "supporting_articles": [],
                "created_at": datetime.now().isoformat(),
            })

    # Link articles to money pages and products by keyword overlap
    for article in data["articles"]:
        art_text = f"{article.get('title', '')} {article.get('url', '')}".lower()
        if not article.get("products_supported"):
            for prod in data["products"]:
                prod_words = {w for w in prod["name"].lower().split() if len(w) > 2}
                if prod_words and all(w in art_text for w in prod_words):
                    article["products_supported"].append(prod["name"])

    # Re-assign clusters for articles that have no cluster or were mis-assigned
    for article in data["articles"]:
        new_cluster = _match_cluster(
            article.get("title", ""), "", article.get("url", ""), data["clusters"]
        )
        if new_cluster:
            article["cluster_id"] = new_cluster

    # Validate money pages against sitemap — pages not in the sitemap
    # are likely discontinued and should not appear in suggestions
    sitemap_urls = _load_sitemap_urls()
    if sitemap_urls:
        for mp in data["money_pages"]:
            if mp.get("status") == "inactive":
                continue
            url = mp["url"].lower().rstrip("/")
            if url not in sitemap_urls and not any(url == s.rstrip("/") for s in sitemap_urls):
                mp["status"] = "inactive"

    data["last_analysis"] = datetime.now().isoformat()
    _save_data(data)

    return {
        "clusters": len(data["clusters"]),
        "articles": len(data["articles"]),
        "money_pages": len(data["money_pages"]),
        "products": len(data["products"]),
    }


def rediscover():
    """Clear all content data and re-run discovery from scratch."""
    old_data = _load_data()
    inactive_urls = {mp["url"] for mp in old_data.get("money_pages", [])
                     if mp.get("status") == "inactive"}
    _save_data({
        "clusters": [],
        "articles": [],
        "money_pages": [],
        "products": [],
        "last_analysis": None,
    })
    result = auto_discover()
    if inactive_urls:
        data = _load_data()
        for mp in data["money_pages"]:
            if mp["url"] in inactive_urls:
                mp["status"] = "inactive"
        _save_data(data)
    return result


def get_dashboard():
    """Get dashboard overview data."""
    data = _load_data()
    clusters = data.get("clusters", [])
    articles = data.get("articles", [])
    money_pages = data.get("money_pages", [])
    products = data.get("products", [])

    status_counts = {}
    for a in articles:
        s = a.get("status", "idea")
        status_counts[s] = status_counts.get(s, 0) + 1

    cluster_stats = []
    for c in clusters:
        cluster_articles = [a for a in articles if a.get("cluster_id") == c["id"]]
        published = sum(1 for a in cluster_articles if a.get("status") == "published")
        target = c.get("target_articles", 20)
        cluster_stats.append({
            "id": c["id"],
            "name": c["name"],
            "priority": c.get("priority", "medium"),
            "status": c.get("status", "active"),
            "total_articles": len(cluster_articles),
            "published": published,
            "target": target,
            "completion": round(published / max(target, 1) * 100),
            "sub_clusters": len(c.get("sub_clusters", [])),
        })
    cluster_stats.sort(key=lambda c: c["completion"])

    product_stats = []
    for p in products:
        supporting = [a for a in articles
                      if p["name"].lower() in [s.lower() for s in a.get("products_supported", [])]]
        product_stats.append({
            "id": p["id"],
            "name": p["name"],
            "url": p.get("url", ""),
            "supporting_count": len(supporting),
        })
    product_stats.sort(key=lambda p: p["supporting_count"])

    unlinked = sum(1 for a in articles if not a.get("products_supported"))

    recent = sorted(
        [a for a in articles if a.get("created_at")],
        key=lambda a: a["created_at"],
        reverse=True,
    )[:10]

    return {
        "total_articles": len(articles),
        "total_clusters": len(clusters),
        "total_money_pages": len(money_pages),
        "total_products": len(products),
        "status_counts": status_counts,
        "cluster_stats": cluster_stats,
        "product_stats": product_stats,
        "recent_articles": recent,
        "unlinked_articles": unlinked,
        "last_analysis": data.get("last_analysis"),
    }


# ── CRUD ──

def create_cluster(name, description="", priority="medium", target_articles=20):
    data = _load_data()
    cluster = {
        "id": f"cl_{int(time.time())}",
        "name": name,
        "description": description,
        "priority": priority,
        "status": "active",
        "target_articles": target_articles,
        "pillar_page": None,
        "money_pages": [],
        "sub_clusters": [],
        "created_at": datetime.now().isoformat(),
    }
    data["clusters"].append(cluster)
    _save_data(data)
    return cluster


def update_cluster(cluster_id, updates):
    data = _load_data()
    for c in data["clusters"]:
        if c["id"] == cluster_id:
            for k, v in updates.items():
                if k != "id":
                    c[k] = v
            _save_data(data)
            return c
    return None


def delete_cluster(cluster_id):
    data = _load_data()
    data["clusters"] = [c for c in data["clusters"] if c["id"] != cluster_id]
    _save_data(data)


def create_article(title, cluster_id=None, **kwargs):
    data = _load_data()
    article = {
        "id": f"art_{int(time.time())}",
        "title": title,
        "url": kwargs.get("url", ""),
        "cluster_id": cluster_id,
        "sub_cluster_id": kwargs.get("sub_cluster_id"),
        "status": kwargs.get("status", "idea"),
        "primary_keyword": kwargs.get("primary_keyword"),
        "secondary_keywords": kwargs.get("secondary_keywords", []),
        "search_intent": kwargs.get("search_intent", "informational"),
        "search_volume": kwargs.get("search_volume", 0),
        "keyword_difficulty": kwargs.get("keyword_difficulty", 0),
        "business_value": kwargs.get("business_value", "medium"),
        "revenue_potential": kwargs.get("revenue_potential", "medium"),
        "evergreen": kwargs.get("evergreen", True),
        "target_word_count": kwargs.get("target_word_count", 1500),
        "products_supported": kwargs.get("products_supported", []),
        "money_pages": kwargs.get("money_pages", []),
        "notes": kwargs.get("notes", ""),
        "created_at": datetime.now().isoformat(),
    }
    data["articles"].append(article)
    _save_data(data)
    return article


def update_article(article_id, updates):
    data = _load_data()
    for a in data["articles"]:
        if a["id"] == article_id:
            for k, v in updates.items():
                if k != "id":
                    a[k] = v
            _save_data(data)
            return a
    return None


def delete_article(article_id):
    data = _load_data()
    data["articles"] = [a for a in data["articles"] if a["id"] != article_id]
    _save_data(data)


def get_clusters():
    return _load_data().get("clusters", [])


def get_articles(cluster_id=None, status=None):
    articles = _load_data().get("articles", [])
    if cluster_id:
        articles = [a for a in articles if a.get("cluster_id") == cluster_id]
    if status:
        articles = [a for a in articles if a.get("status") == status]
    return articles


def get_money_pages(page_type=None):
    pages = _load_data().get("money_pages", [])
    if page_type:
        pages = [mp for mp in pages if mp.get("type") == page_type]
    return pages


def get_products():
    return _load_data().get("products", [])


def get_money_page_detail(mp_id):
    data = _load_data()
    mp = next((m for m in data["money_pages"] if m["id"] == mp_id), None)
    if not mp:
        return None
    linked_ids = set(mp.get("supporting_articles", []))
    linked = [a for a in data["articles"] if a["id"] in linked_ids]
    return {**mp, "linked_articles": linked}


def update_money_page(mp_id, updates):
    data = _load_data()
    for mp in data["money_pages"]:
        if mp["id"] == mp_id:
            for k, v in updates.items():
                if k != "id":
                    mp[k] = v
            _save_data(data)
            return mp
    return None


def link_article_to_money_page(mp_id, article_id):
    data = _load_data()
    for mp in data["money_pages"]:
        if mp["id"] == mp_id:
            linked = mp.get("supporting_articles", [])
            if article_id not in linked:
                linked.append(article_id)
                mp["supporting_articles"] = linked
            _save_data(data)
            return mp
    return None


def unlink_article_from_money_page(mp_id, article_id):
    data = _load_data()
    for mp in data["money_pages"]:
        if mp["id"] == mp_id:
            linked = mp.get("supporting_articles", [])
            mp["supporting_articles"] = [a for a in linked if a != article_id]
            _save_data(data)
            return mp
    return None


def get_pipeline():
    """Get articles grouped by status for the pipeline/kanban view."""
    data = _load_data()
    articles = data.get("articles", [])
    clusters = data.get("clusters", [])
    cluster_map = {c["id"]: c["name"] for c in clusters}

    columns = ["idea", "draft", "writing", "review", "published"]
    pipeline = {s: [] for s in columns}

    for a in articles:
        status = a.get("status", "idea")
        if status not in pipeline:
            pipeline[status] = []
        pipeline[status].append({
            "id": a["id"],
            "title": a["title"],
            "url": a.get("url", ""),
            "cluster_id": a.get("cluster_id"),
            "cluster_name": cluster_map.get(a.get("cluster_id"), ""),
            "primary_keyword": a.get("primary_keyword", ""),
            "business_value": a.get("business_value", "medium"),
            "created_at": a.get("created_at", ""),
        })

    for col in pipeline:
        pipeline[col].sort(key=lambda a: a.get("created_at", ""), reverse=True)

    return {"columns": columns, "pipeline": pipeline}


def bulk_update_articles(article_ids, updates):
    """Apply the same updates to multiple articles at once."""
    data = _load_data()
    updated = 0
    for a in data["articles"]:
        if a["id"] in article_ids:
            for k, v in updates.items():
                if k != "id":
                    a[k] = v
            updated += 1
    if updated:
        _save_data(data)
    return {"updated": updated}


def add_sub_cluster(cluster_id, name):
    data = _load_data()
    for c in data["clusters"]:
        if c["id"] == cluster_id:
            subs = c.get("sub_clusters", [])
            sc = {
                "id": f"sc_{int(time.time())}_{len(subs)}",
                "name": name,
                "created_at": datetime.now().isoformat(),
            }
            subs.append(sc)
            c["sub_clusters"] = subs
            _save_data(data)
            return sc
    return None


def delete_sub_cluster(cluster_id, sub_cluster_id):
    data = _load_data()
    for c in data["clusters"]:
        if c["id"] == cluster_id:
            c["sub_clusters"] = [
                sc for sc in c.get("sub_clusters", [])
                if sc["id"] != sub_cluster_id
            ]
            for a in data["articles"]:
                if a.get("sub_cluster_id") == sub_cluster_id:
                    a["sub_cluster_id"] = None
            _save_data(data)
            return True
    return False


def get_content_gaps():
    data = _load_data()
    keywords = _load_keywords()
    clusters = data.get("clusters", [])
    articles = data.get("articles", [])
    money_pages = data.get("money_pages", [])
    products = data.get("products", [])

    gaps = {
        "underserved_clusters": [],
        "unsupported_products": [],
        "thin_money_pages": [],
        "unassigned_keywords": [],
        "orphan_articles": [],
    }

    for c in clusters:
        cluster_arts = [a for a in articles if a.get("cluster_id") == c["id"]]
        published = sum(1 for a in cluster_arts if a.get("status") == "published")
        target = c.get("target_articles", 20)
        if published < target * 0.5:
            gaps["underserved_clusters"].append({
                "id": c["id"],
                "name": c["name"],
                "published": published,
                "target": target,
                "gap": target - published,
                "priority": c.get("priority", "medium"),
            })
    gaps["underserved_clusters"].sort(key=lambda x: x["gap"], reverse=True)

    for p in products:
        supporting = [a for a in articles
                      if p["name"].lower() in [s.lower() for s in a.get("products_supported", [])]]
        if len(supporting) < 3:
            gaps["unsupported_products"].append({
                "id": p["id"],
                "name": p["name"],
                "url": p.get("url", ""),
                "supporting_count": len(supporting),
            })
    gaps["unsupported_products"].sort(key=lambda x: x["supporting_count"])

    for mp in money_pages:
        if mp.get("type") == "product":
            continue
        linked = mp.get("supporting_articles", [])
        target = mp.get("target_articles", 5)
        if len(linked) < target * 0.3:
            gaps["thin_money_pages"].append({
                "id": mp["id"],
                "url": mp.get("url", ""),
                "title": mp.get("title", ""),
                "linked_count": len(linked),
                "target": target,
            })
    gaps["thin_money_pages"].sort(key=lambda x: x["linked_count"])

    article_keywords = set()
    for a in articles:
        if a.get("primary_keyword"):
            article_keywords.add(a["primary_keyword"].lower())

    active_keywords = [kw for kw in keywords if kw.get("status") in ("active", "retained")]
    for kw in active_keywords:
        kw_text = kw.get("keyword", "").lower()
        if kw_text and kw_text not in article_keywords:
            gaps["unassigned_keywords"].append({
                "keyword": kw["keyword"],
                "volume": kw.get("volume", 0),
                "kd": kw.get("kd", 0),
            })
    gaps["unassigned_keywords"].sort(key=lambda x: x["volume"], reverse=True)
    gaps["unassigned_keywords"] = gaps["unassigned_keywords"][:50]

    gaps["orphan_articles"] = [
        {"id": a["id"], "title": a["title"], "url": a.get("url", ""), "status": a.get("status", "idea")}
        for a in articles
        if not a.get("cluster_id")
    ]

    gaps["summary"] = {
        "underserved_clusters": len(gaps["underserved_clusters"]),
        "unsupported_products": len(gaps["unsupported_products"]),
        "thin_money_pages": len(gaps["thin_money_pages"]),
        "unassigned_keywords": len(gaps["unassigned_keywords"]),
        "orphan_articles": len(gaps["orphan_articles"]),
        "total_gaps": (len(gaps["underserved_clusters"]) + len(gaps["unsupported_products"])
                       + len(gaps["thin_money_pages"]) + len(gaps["unassigned_keywords"])
                       + len(gaps["orphan_articles"])),
    }

    return gaps


_CONTENT_TEMPLATES = {
    "Montessori Education": [
        "Montessori at Home: A Beginner's Guide for Parents",
        "Montessori vs Traditional Preschool: Which Is Right for Your Child?",
        "How to Set Up a Montessori Playroom on a Budget",
        "Montessori Activities for {age}: Skills They'll Actually Use",
        "Why Montessori Toys Are Worth the Investment",
        "Montessori Discipline: Positive Guidance Without Punishment",
        "The Science Behind Montessori: What Research Says",
        "Montessori Gift Guide: Toys That Actually Follow the Method",
    ],
    "Personalized Gifts & Toys": [
        "Best Personalized Gifts for {age} ({year} Guide)",
        "Why Kids Love Seeing Their Name on Toys (The Psychology Behind It)",
        "Personalized vs Generic Toys: Which Builds More Confidence?",
        "Unique Personalized Baby Shower Gift Ideas",
        "How Personalized Books Help Kids Learn to Read Their Name",
        "Custom Name Puzzles: How They Boost Letter Recognition",
        "Personalized Birthday Gift Ideas by Age",
        "The Best Engraved & Customized Wooden Toys for Kids",
    ],
    "Early Literacy & Alphabet Learning": [
        "When Should Kids Learn the Alphabet? A Developmental Timeline",
        "Fun Ways to Teach Letter Recognition Without Flashcards",
        "How Wooden Letter Toys Build Pre-Reading Skills",
        "Name Writing Activities for Preschoolers",
        "Alphabet Toys That Actually Work: Expert Recommendations",
        "How to Help a Late Reader: Activities That Build Confidence",
        "Phonics vs Whole Language: What Parents Need to Know",
        "Bilingual Literacy: Teaching Kids Two Languages at Once",
    ],
    "Child Development": [
        "Fine Motor Milestones: What to Expect by Age",
        "Best Toys for Developing Hand-Eye Coordination",
        "How Sensory Play Builds Neural Pathways in Toddlers",
        "Gross Motor Activities That Need Zero Equipment",
        "Screen Time Alternatives That Kids Actually Prefer",
        "How Open-Ended Toys Foster Creativity and Problem Solving",
        "The Role of Pretend Play in Emotional Development",
        "Why Boredom Is Good for Kids (And How to Handle It)",
    ],
    "Classroom Rugs": [
        "How to Choose the Right Classroom Rug Size for Your Space",
        "Circle Time Rugs: Best Options for Preschool Classrooms",
        "Sensory Carpets: How Texture Helps Neurodiverse Learners",
        "Classroom Rug Safety: What Certifications to Look For",
        "Best Classroom Rugs for Reading Corners and Quiet Zones",
        "How Alphabet Rugs Support Literacy in Early Childhood",
        "Bilingual Classroom Rugs: Teaching Two Languages Through Play",
        "Montessori Classroom Setup: Choosing the Right Floor Coverings",
    ],
    "Gift Buying Guides": [
        "Best Educational Toys for {age} ({year} Gift Guide)",
        "Holiday Gift Guide: Wooden Toys Kids Will Actually Play With",
        "Birthday Gift Ideas for Kids Who Have Everything",
        "Best Big Brother & Big Sister Gifts for New Siblings",
        "Teacher Gift Guide: Classroom Supplies They'll Love",
        "Eco-Friendly Gift Ideas for Environmentally Conscious Parents",
        "Last-Minute Personalized Gifts That Ship Fast",
        "Best Gifts for Grandparents to Give Grandkids",
    ],
    "Autism & Special Needs": [
        "Best Sensory Toys for Children on the Autism Spectrum",
        "How to Adapt Circle Time for Special Needs Students",
        "Calming Toys and Tools for Anxious Children",
        "How Educational Toys Help Children with Learning Disabilities",
        "Inclusive Classroom Design: Creating Spaces for Every Learner",
        "Fidget Toys That Actually Help Focus (Not Just Distract)",
        "Speech Development Toys for Late Talkers",
        "How Personalized Items Help Special Needs Kids Feel Included",
    ],
    "Play-Based Learning": [
        "Why Play-Based Learning Outperforms Worksheets in Early Ed",
        "Circle Time Activities That Keep Preschoolers Engaged",
        "How to Turn Any Toy Into a Learning Opportunity",
        "Imaginative Play: Why Pretend Kitchens and Dollhouses Matter",
        "Outdoor Learning Activities for Every Season",
        "STEM Activities for Preschoolers Using Everyday Objects",
        "How Block Play Teaches Math Concepts to Toddlers",
        "The Power of Unstructured Play in Early Childhood",
    ],
    "Toy Safety & Sustainability": [
        "How to Check If a Toy Is Actually Non-Toxic",
        "Why Wooden Toys Are More Sustainable Than Plastic",
        "Made in USA vs Imported Toys: Safety Standards Compared",
        "BPA, Lead, and Phthalates: A Parent's Guide to Toy Safety",
        "How to Build an Eco-Friendly Toy Collection for Kids",
        "The Real Cost of Cheap Toys: Safety, Durability, and Waste",
        "Sustainable Gift Wrapping Ideas for Kids' Birthdays",
        "How to Declutter Toys Responsibly: Donate, Recycle, Repurpose",
    ],
    "Educational Toys": [
        "STEM Toys That Actually Teach Science and Engineering",
        "Best Learning Toys by Age: From Baby to Kindergarten",
        "How Building Blocks Teach Math and Spatial Reasoning",
        "Coding Toys for Kids: Are They Worth It?",
        "Musical Toys That Build Rhythm and Coordination",
        "How Puzzles Develop Problem-Solving Skills in Toddlers",
        "Best Educational Toys for Kids with Short Attention Spans",
        "Magnetic Tiles vs Building Blocks: Which Is Better?",
    ],
    "Learning by Age": [
        "Best Toys and Activities for {age}",
        "Developmental Milestones for {age}: What to Expect",
        "How to Choose Age-Appropriate Toys (Complete Guide)",
        "Montessori Activities for Newborns: 0-3 Month Ideas",
        "What Should a 2-Year-Old Be Learning? A Parent's Checklist",
        "Preschool Readiness: Skills Your 4-Year-Old Should Practice",
        "Kindergarten Prep Activities You Can Do at Home",
        "Toddler vs Preschooler Toys: When to Level Up",
    ],
    "Wooden Toys": [
        "Why Wooden Toys Are Better for Development Than Plastic",
        "Best Wooden Toys for Babies and Toddlers ({year})",
        "How to Care for and Clean Wooden Toys",
        "Heirloom Wooden Toys That Last Generations",
        "Wooden Train Sets: A Complete Buying Guide",
        "Wooden Blocks: The Most Underrated Educational Toy",
        "Are Wooden Toys Worth the Price? A Honest Parent Review",
        "Sustainable Wooden Toy Brands Made in the USA",
    ],
    "Seasonal & Holiday Gifts": [
        "Best Christmas Gifts for Toddlers ({year})",
        "Valentine's Day Gifts Kids Will Actually Use",
        "Easter Basket Ideas: Educational Toys Instead of Candy",
        "Back-to-School Gifts for Preschoolers and Kindergartners",
        "Halloween Treats for Kids: Non-Candy Gift Ideas",
        "Best Birthday Party Favors That Aren't Junk",
        "Teacher Appreciation Gifts from the Classroom",
        "New Baby Sibling Gift Ideas for the Older Child",
    ],
}

_AGE_GROUPS = [
    "newborns (0-3 months)", "babies (3-12 months)", "1-year-olds",
    "2-year-olds", "3-year-olds", "4-year-olds", "5-year-olds", "6-year-olds",
]


_PRODUCT_CLUSTER_MAP = {
    "name trains": ["Personalized Gifts & Toys", "Early Literacy & Alphabet Learning", "Wooden Toys"],
    "personalized name puzzles": ["Personalized Gifts & Toys", "Early Literacy & Alphabet Learning"],
    "personalized step stools": ["Personalized Gifts & Toys", "Child Development"],
    "personalized baby books": ["Personalized Gifts & Toys", "Early Literacy & Alphabet Learning", "Gift Buying Guides"],
    "personalized baby gifts": ["Personalized Gifts & Toys", "Gift Buying Guides", "Seasonal & Holiday Gifts"],
    "personalized toys": ["Personalized Gifts & Toys", "Educational Toys"],
    "montessori toys": ["Montessori Education", "Educational Toys", "Child Development"],
    "classroom rugs": ["Classroom Rugs", "Play-Based Learning"],
    "kids furniture": ["Montessori Education", "Classroom Rugs"],
}

_CLUSTER_MATCH_NOISE = {"for", "the", "and", "toys", "by", "a", "an", "in", "of", "to"}


_REVENUE_CLUSTERS = {
    "Personalized Gifts & Toys", "Gift Buying Guides",
    "Seasonal & Holiday Gifts", "Educational Toys", "Wooden Toys",
}


def _score_suggestion(sug, gap_ratio, cl_name):
    """Score a suggestion 0-100 based on business impact."""
    type_base = {"product_support": 45, "money_page_support": 30, "topic_gap": 12}
    score = type_base.get(sug["type"], 10)
    score += min(30, gap_ratio * 30)
    if sug["type"] == "product_support":
        existing = int(re.search(r'(\d+) supporting', sug.get("reason", "0")).group(1)) if re.search(r'(\d+) supporting', sug.get("reason", "")) else 0
        score += (3 - existing) * 5
    elif sug["type"] == "money_page_support":
        score += 10
    if cl_name in _REVENUE_CLUSTERS:
        score += 10
    return min(100, round(score))


def get_content_suggestions(cluster_id=None):
    """Generate specific article topic suggestions based on gaps."""
    data = _load_data()
    clusters = data.get("clusters", [])
    articles = data.get("articles", [])
    products = data.get("products", [])
    money_pages = data.get("money_pages", [])
    year = datetime.now().year

    active_money_pages = [mp for mp in money_pages if mp.get("status", "active") != "inactive"]

    existing_titles = {a["title"].lower() for a in articles}
    seen_titles = set()

    target_clusters = clusters
    if cluster_id:
        target_clusters = [c for c in clusters if c["id"] == cluster_id]

    all_suggestions = []
    for cluster in target_clusters:
        cl_articles = [a for a in articles if a.get("cluster_id") == cluster["id"]]
        published = sum(1 for a in cl_articles if a.get("status") == "published")
        target = cluster.get("target_articles", 20)
        if published >= target:
            continue

        cl_name = cluster["name"]
        gap_ratio = (target - published) / max(target, 1)
        templates = _CONTENT_TEMPLATES.get(cl_name, [])
        cl_suggestions = []

        # Product-focused suggestions — only for clusters the product maps to
        for prod in products:
            relevant_clusters = _PRODUCT_CLUSTER_MAP.get(prod["name"].lower(), [])
            if cl_name not in relevant_clusters:
                continue
            prod_articles = [a for a in articles
                            if prod["name"].lower() in [s.lower() for s in a.get("products_supported", [])]]
            if len(prod_articles) < 3:
                idea = f"Complete Guide to {prod['name']}: Benefits, Reviews & Best Picks"
                idea_key = idea.lower()
                if idea_key not in existing_titles and idea_key not in seen_titles:
                    seen_titles.add(idea_key)
                    cl_suggestions.append({
                        "title": idea,
                        "type": "product_support",
                        "reason": f"Product '{prod['name']}' has only {len(prod_articles)} supporting articles",
                        "priority": "high",
                    })

        # Money page support suggestions — require meaningful word overlap
        cl_match_words = {w for w in cl_name.lower().split()
                         if len(w) > 2 and w not in _CLUSTER_MATCH_NOISE}
        for mp in active_money_pages:
            if mp.get("type") != "category":
                continue
            linked = len(mp.get("supporting_articles", []))
            if linked >= mp.get("target_articles", 5):
                continue
            mp_title = mp.get("title", "")
            mp_path = re.sub(r'https?://[^/]+', '', mp.get("url", ""))
            mp_text = f"{mp_title} {mp_path.replace('-', ' ')}".lower()
            overlap = sum(1 for w in cl_match_words if w in mp_text)
            if overlap >= 1 and cl_match_words:
                short_title = mp_title.split('–')[0].split(':')[0].split('|')[0].strip()
                idea = f"Everything You Need to Know About {short_title}"
                idea_key = idea.lower()
                if idea_key not in existing_titles and idea_key not in seen_titles:
                    seen_titles.add(idea_key)
                    cl_suggestions.append({
                        "title": idea,
                        "type": "money_page_support",
                        "reason": f"Category page '{mp_title[:50]}' has {linked} linked articles",
                        "priority": "high",
                        "money_page_url": mp.get("url", ""),
                    })

        # Template-based suggestions with age group variations
        for tmpl in templates:
            if "{age}" in tmpl:
                for age in _AGE_GROUPS[:4]:
                    idea = tmpl.replace("{age}", age).replace("{year}", str(year))
                    idea_key = idea.lower()
                    if idea_key not in existing_titles and idea_key not in seen_titles:
                        seen_titles.add(idea_key)
                        cl_suggestions.append({
                            "title": idea,
                            "type": "topic_gap",
                            "reason": f"Fills content gap in '{cl_name}' cluster",
                            "priority": "medium",
                        })
                        break
            else:
                idea = tmpl.replace("{year}", str(year))
                idea_key = idea.lower()
                if idea_key not in existing_titles and idea_key not in seen_titles:
                    seen_titles.add(idea_key)
                    cl_suggestions.append({
                        "title": idea,
                        "type": "topic_gap",
                        "reason": f"Fills content gap in '{cl_name}' cluster",
                        "priority": "medium",
                    })

        # Score each suggestion
        for s in cl_suggestions:
            s["score"] = _score_suggestion(s, gap_ratio, cl_name)

        # Find related money pages and products for this cluster
        related_money_pages = []
        for mp in active_money_pages:
            if mp.get("type") != "category":
                continue
            mp_text = f"{mp.get('title', '')} {mp.get('url', '')}".lower()
            if cl_match_words and any(w in mp_text for w in cl_match_words):
                related_money_pages.append({
                    "url": mp.get("url", ""),
                    "title": mp.get("title", ""),
                    "anchor_text": mp.get("title", "").split("–")[0].split(":")[0].split("|")[0].strip(),
                })
        related_products = []
        for prod in products:
            relevant_clusters = _PRODUCT_CLUSTER_MAP.get(prod["name"].lower(), [])
            if cl_name in relevant_clusters:
                related_products.append({"name": prod["name"], "url": prod.get("url", "")})

        # Enrich each suggestion with internal links and writing prompt
        for s in cl_suggestions:
            s["cluster_name"] = cl_name
            s["cluster_id"] = cluster["id"]
            s["internal_links"] = related_money_pages[:5]
            s["related_products"] = related_products[:5]
            s["related_articles"] = [
                {"title": a["title"], "url": a.get("url", "")}
                for a in cl_articles[:5]
            ]
            s["writing_prompt"] = _build_writing_prompt(
                s["title"], cl_name, related_money_pages[:3],
                related_products[:3], [a["title"] for a in cl_articles[:5]],
            )

        # Limit per cluster: cap at 5
        cl_suggestions.sort(key=lambda x: x["score"], reverse=True)
        all_suggestions.extend(cl_suggestions[:5])

    # Sort globally by score, assign rank
    all_suggestions.sort(key=lambda x: x["score"], reverse=True)
    for i, s in enumerate(all_suggestions, 1):
        s["rank"] = i

    # Build impact summary
    total = len(all_suggestions)
    by_type = {}
    for s in all_suggestions:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    clusters_covered = len({s["cluster_name"] for s in all_suggestions})
    products_mentioned = len({s["title"] for s in all_suggestions if s["type"] == "product_support"})
    category_pages_supported = len({s.get("money_page_url") for s in all_suggestions
                                    if s["type"] == "money_page_support" and s.get("money_page_url")})

    hours_per_article = 4
    total_hours = total * hours_per_article
    high_impact = sum(1 for s in all_suggestions if s["score"] >= 70)
    med_impact = sum(1 for s in all_suggestions if 45 <= s["score"] < 70)
    low_impact = sum(1 for s in all_suggestions if s["score"] < 45)
    high_hours = high_impact * hours_per_article

    # Quick-win: top 10 articles
    top10_types = {}
    for s in all_suggestions[:10]:
        top10_types[s["type"]] = top10_types.get(s["type"], 0) + 1

    summary = {
        "total_articles": total,
        "total_hours": total_hours,
        "high_impact": high_impact,
        "high_hours": high_hours,
        "med_impact": med_impact,
        "low_impact": low_impact,
        "clusters_covered": clusters_covered,
        "products_mentioned": products_mentioned,
        "category_pages_supported": category_pages_supported,
        "top10_types": top10_types,
        "by_type": {
            "product_support": by_type.get("product_support", 0),
            "money_page_support": by_type.get("money_page_support", 0),
            "topic_gap": by_type.get("topic_gap", 0),
        },
    }

    return {"suggestions": all_suggestions, "summary": summary}


def _build_writing_prompt(title, cluster_name, money_pages, products, existing_titles):
    """Build a full writing prompt/content brief for an article."""
    mp_links = ""
    if money_pages:
        mp_links = "\n".join(
            f"  - [{mp['anchor_text']}]({mp['url']})" for mp in money_pages
        )
    prod_mentions = ""
    if products:
        prod_mentions = ", ".join(p["name"] for p in products)

    existing = ""
    if existing_titles:
        existing = "\n".join(f"  - {t}" for t in existing_titles[:5])

    prompt = f"""Write a comprehensive, SEO-optimized blog post titled: "{title}"

Topic Cluster: {cluster_name}
Target Length: 1,500-2,000 words
Search Intent: Informational / Commercial Investigation
Target Audience: Parents, teachers, and gift-givers looking for educational products for children

STRUCTURE:
- Hook opening that addresses the reader's pain point or question
- 5-7 subheadings (H2s) covering different aspects of the topic
- Practical tips, comparisons, or actionable advice in each section
- Conclusion with a clear call-to-action"""

    if mp_links:
        prompt += f"""

INTERNAL LINKS TO INCLUDE (link naturally within the content):
{mp_links}"""

    if prod_mentions:
        prompt += f"""

PRODUCTS TO MENTION/RECOMMEND:
  {prod_mentions}
  Mention these products naturally where relevant — don't force them."""

    if existing:
        prompt += f"""

RELATED ARTICLES TO CROSS-LINK:
{existing}
  Reference or link to these existing articles where it adds value."""

    prompt += """

SEO GUIDELINES:
- Include the primary keyword in the title, first paragraph, and 2-3 subheadings
- Use related long-tail keywords naturally throughout
- Add alt text suggestions for any recommended images
- End with a FAQ section (3-5 questions) using "People Also Ask" style questions

TONE: Warm, knowledgeable, parent-to-parent. Not salesy — genuinely helpful.
Write for parents who want the best for their kids but are overwhelmed by choices."""

    return prompt
