"""Magento 2 REST client for the SEO loop — deliberately narrow.

This client can read products/categories and write EXACTLY two fields:
meta_title and meta_description. The denylist is enforced in code, not in a
prompt: the payload builders only ever emit the allowed attributes, and
`_assert_safe_payload` re-checks every outgoing body so a future refactor
can't quietly widen the surface. Prices, url_key, status, categories, stock,
and anything Awin-facing are structurally unreachable.

Auth: a Magento INTEGRATION token (Admin → System → Integrations) whose role
is scoped to Catalog only — never an admin login token.

Env:
  MAGENTO_BASE_URL   e.g. https://alphabet-trains.com  (no trailing /rest)
  MAGENTO_TOKEN      the integration access token
"""

import json
import os
import re

try:
    import httpx
    _HTTPX = True
except ImportError:  # pragma: no cover - httpx is in requirements
    _HTTPX = False

# The ONLY attributes this module is allowed to write. Everything else is
# denied structurally.
ALLOWED_WRITE_FIELDS = frozenset({"meta_title", "meta_description"})

_TIMEOUT = 30.0


class MagentoError(Exception):
    pass


def _assert_safe_payload(payload: dict, path_sku: str = None):
    """Refuse any payload that touches a field outside the allowlist.
    Raises instead of filtering: a denylist violation is a bug, not an input.
    'sku' is permitted ONLY as an identifier echoing the URL path (Magento's
    product save rejects partial updates without it) — a sku that differs
    from the path is a rename attempt and is refused."""
    ent = payload.get("product") or payload.get("category") or {}
    for key in ent:
        if key == "custom_attributes":
            continue
        if key == "sku" and "product" in payload:
            # Only valid as an identifier echoing the URL path — callers must
            # pass path_sku; a missing or different value is a rename attempt.
            if path_sku is None or ent["sku"] != path_sku:
                raise MagentoError("Payload sku must echo the path sku — refused.")
            continue
        raise MagentoError(f"Denylisted top-level field in write payload: {key}")
    for attr in ent.get("custom_attributes", []):
        code = attr.get("attribute_code")
        if code not in ALLOWED_WRITE_FIELDS:
            raise MagentoError(f"Denylisted attribute in write payload: {code}")


def _slug_of(url: str) -> str:
    """URL → Magento url_key: last path segment, .html stripped."""
    path = re.sub(r"[?#].*$", "", url or "").rstrip("/")
    seg = path.rsplit("/", 1)[-1]
    return seg[:-5] if seg.endswith(".html") else seg


class MagentoClient:
    """Reads and writes run at STORE-VIEW scope (env MAGENTO_STORE_CODE,
    auto-detected otherwise, falling back to 'default'): writing the global
    ('all') scope is invisible on the storefront whenever a product carries a
    store-view-level override for the same attribute — and metas usually do."""

    def __init__(self, base_url=None, token=None, store_code=None):
        self.base_url = (base_url or os.environ.get("MAGENTO_BASE_URL", "")).rstrip("/")
        self.token = token or os.environ.get("MAGENTO_TOKEN", "")
        self._store_code = store_code or os.environ.get("MAGENTO_STORE_CODE") or None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token and _HTTPX)

    def write_scopes(self):
        """Candidate scopes for WRITES, in order: explicit env override, then
        detected store-view codes (so the storefront-visible value changes even
        when a store-view override masks the global one), then 'all' as the
        last resort. Reads always use 'all' — it's guaranteed valid and
        url_key lookups are store-agnostic."""
        if self._store_code:
            return [self._store_code, "all"]
        codes = []
        try:
            views = self._req("GET", "/store/storeViews", scope="all") or []
            codes = [v.get("code") for v in views
                     if v.get("code") and v.get("code") != "admin"
                     and v.get("is_active", 1)]
            if "default" in codes:  # prefer the conventional default first
                codes = ["default"] + [c for c in codes if c != "default"]
        except MagentoError:
            pass  # token may be Catalog-only — fall through to 'all'
        return (codes or []) + ["all"]

    def _req(self, method, path, payload=None, scope="all"):
        if not self.configured:
            raise MagentoError("Magento not configured (MAGENTO_BASE_URL / MAGENTO_TOKEN)")
        url = f"{self.base_url}/rest/{scope}/V1{path}"
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        try:
            resp = httpx.request(method, url, headers=headers,
                                 content=json.dumps(payload) if payload is not None else None,
                                 timeout=_TIMEOUT, follow_redirects=False)
        except Exception as e:
            raise MagentoError(f"Magento request failed: {e}") from e
        if resp.status_code >= 400:
            raise MagentoError(f"Magento {method} {path} → {resp.status_code}: "
                               f"{resp.text[:300]}")
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise MagentoError(f"Magento returned non-JSON for {path}") from e

    # ------------------------------------------------------------- reads
    @staticmethod
    def _attr(entity, code, default=""):
        for a in entity.get("custom_attributes", []) or []:
            if a.get("attribute_code") == code:
                return a.get("value") or default
        return entity.get(code, default) or default

    def find_product_by_url(self, url: str):
        """Resolve a storefront URL to a product via its url_key. Returns
        {sku, name, meta_title, meta_description} or None."""
        slug = _slug_of(url)
        if not slug:
            return None
        q = (f"/products?searchCriteria[filterGroups][0][filters][0][field]=url_key"
             f"&searchCriteria[filterGroups][0][filters][0][value]={slug}"
             f"&searchCriteria[filterGroups][0][filters][0][conditionType]=eq"
             f"&searchCriteria[pageSize]=2")
        items = (self._req("GET", q) or {}).get("items") or []
        if len(items) != 1:
            return None  # ambiguous or missing — never guess a write target
        p = items[0]
        return {"entity_type": "product", "sku": p.get("sku", ""),
                "name": p.get("name", ""),
                "meta_title": self._attr(p, "meta_title"),
                "meta_description": self._attr(p, "meta_description")}

    def find_category_by_url(self, url: str):
        slug = _slug_of(url)
        if not slug:
            return None
        q = (f"/categories/list?searchCriteria[filterGroups][0][filters][0][field]=url_key"
             f"&searchCriteria[filterGroups][0][filters][0][value]={slug}"
             f"&searchCriteria[filterGroups][0][filters][0][conditionType]=eq"
             f"&searchCriteria[pageSize]=2")
        items = (self._req("GET", q) or {}).get("items") or []
        if len(items) != 1:
            return None
        c = items[0]
        return {"entity_type": "category", "category_id": c.get("id"),
                "name": c.get("name", ""),
                "meta_title": self._attr(c, "meta_title"),
                "meta_description": self._attr(c, "meta_description")}

    def resolve_url(self, url: str, asset_type: str = ""):
        """Find the editable entity behind a storefront URL. Tries the likely
        type first (by classified asset_type), then the other."""
        order = (["category", "product"] if asset_type == "category"
                 else ["product", "category"])
        for kind in order:
            try:
                found = (self.find_category_by_url(url) if kind == "category"
                         else self.find_product_by_url(url))
            except MagentoError:
                found = None
            if found:
                return found
        return None

    def resolve_diag(self, url: str) -> str:
        """Human-readable reason a URL couldn't be resolved — counts matches
        per entity type so 'wrong url_key' and 'ambiguous' are distinguishable."""
        slug = _slug_of(url)
        parts = [f"url_key '{slug}':"]
        for label, path in (("products", "/products"), ("categories", "/categories/list")):
            q = (f"{path}?searchCriteria[filterGroups][0][filters][0][field]=url_key"
                 f"&searchCriteria[filterGroups][0][filters][0][value]={slug}"
                 f"&searchCriteria[filterGroups][0][filters][0][conditionType]=eq"
                 f"&searchCriteria[pageSize]=3")
            try:
                n = len((self._req("GET", q) or {}).get("items") or [])
                parts.append(f"{n} {label}")
            except MagentoError as e:
                # Keep the tail of the error — that's where the status code and
                # server message live (the head is just the long request path).
                parts.append(f"{label} lookup failed (…{str(e)[-160:]})")
        parts.append("— if 0/0, the Magento url_key differs from the URL slug; "
                     "check the entity's Search Engine Optimization section in admin.")
        return " ".join(parts)

    # ------------------------------------------------------------ writes
    @staticmethod
    def _meta_payload(root_key, meta_title=None, meta_description=None):
        attrs = []
        if meta_title is not None:
            attrs.append({"attribute_code": "meta_title", "value": meta_title})
        if meta_description is not None:
            attrs.append({"attribute_code": "meta_description", "value": meta_description})
        if not attrs:
            raise MagentoError("Nothing to write")
        return {root_key: {"custom_attributes": attrs}}

    def _write(self, path, payload):
        """PUT through the write-scope ladder: a scope whose store code the
        server rejects ('store is not found' 404) falls through to the next;
        any other error is real and raised as-is."""
        last = None
        for scope in self.write_scopes():
            try:
                return self._req("PUT", path, payload, scope=scope)
            except MagentoError as e:
                msg = str(e).lower()
                if "store" in msg and "not found" in msg:
                    last = e
                    continue
                raise
        raise last or MagentoError("No usable write scope found")

    def update_product_meta(self, sku, meta_title=None, meta_description=None):
        payload = self._meta_payload("product", meta_title, meta_description)
        # Magento's product save rejects partial updates without the sku in the
        # body ("The product was unable to be saved") — echo the path sku.
        payload["product"]["sku"] = sku
        _assert_safe_payload(payload, path_sku=sku)
        # url-encode the sku path segment minimally
        safe_sku = sku.replace("/", "%2F").replace(" ", "%20")
        return self._write(f"/products/{safe_sku}", payload)

    def update_category_meta(self, category_id, meta_title=None, meta_description=None):
        payload = self._meta_payload("category", meta_title, meta_description)
        _assert_safe_payload(payload)
        return self._write(f"/categories/{int(category_id)}", payload)

    def write_meta(self, entity, meta_title=None, meta_description=None):
        """Write to whichever entity resolve_url() returned."""
        if entity.get("entity_type") == "product":
            return self.update_product_meta(entity["sku"], meta_title, meta_description)
        if entity.get("entity_type") == "category":
            return self.update_category_meta(entity["category_id"],
                                             meta_title, meta_description)
        raise MagentoError(f"Unknown entity type: {entity.get('entity_type')}")
