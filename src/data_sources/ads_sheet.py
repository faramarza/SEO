"""Read-only reader for the Google Ads export Sheet.

We deliberately do NOT use the Google Ads API (it needs a developer token). Instead,
a Google Ads Script the owner installs exports the last 30 days of search-term and
keyword performance to a Google Sheet daily; Governor reads that Sheet here.

Auth reuses the SAME service account as GA4/GSC (credentials.json): the owner shares
the Sheet read-only with the service-account email, and we read it with the Sheets
API (google-api-python-client, already a dependency). No new secret, no write scope.

Everything here is read-only and never raises — on any problem it returns empty rows
plus an 'error' string the report surfaces honestly (never a fabricated number).

Expected tab layout (header row + data), written by the Ads Script:
  SearchTerms: search_term | campaign | clicks | cost | conversions | conversion_value
  Keywords:    keyword | match_type | campaign | clicks | cost | conversions | conversion_value
Column matching is by header NAME (case/space-insensitive), so extra columns or a
different order won't break it.
"""

from __future__ import annotations

from typing import Optional


_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

# Header aliases -> canonical field. Matching is on a normalized header (lowercased,
# non-alphanumerics stripped) so "Conv. value", "conversion_value" etc. all map.
_ALIASES = {
    "searchterm": "search_term", "search_term": "search_term", "query": "search_term",
    "keyword": "keyword", "criteria": "keyword",
    "matchtype": "match_type", "match_type": "match_type",
    "campaign": "campaign", "campaignname": "campaign",
    "clicks": "clicks",
    "cost": "cost", "costmicros": "cost_micros", "spend": "cost",
    "conversions": "conversions", "conv": "conversions",
    "conversionvalue": "conversion_value", "convvalue": "conversion_value",
    "totalconvvalue": "conversion_value", "value": "conversion_value",
}
_NUMERIC = {"clicks", "cost", "conversions", "conversion_value", "cost_micros"}


def _norm_header(h: str) -> str:
    return "".join(ch for ch in (h or "").lower() if ch.isalnum())


def _to_float(v) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "").replace("$", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


class AdsSheetClient:
    """Reads the Ads export Sheet with the shared service account. Configured via the
    paid_search.ads_sheet config block (sheet_id + tab names)."""

    def __init__(self, sheet_id: str = "", credentials_path: Optional[str] = None,
                 search_terms_tab: str = "SearchTerms", keywords_tab: str = "Keywords"):
        self.sheet_id = (sheet_id or "").strip()
        self.credentials_path = credentials_path
        self.search_terms_tab = search_terms_tab or "SearchTerms"
        self.keywords_tab = keywords_tab or "Keywords"
        self._service = None
        self._last_error = None

    @classmethod
    def from_config(cls, config: dict) -> "AdsSheetClient":
        ps = (config or {}).get("paid_search", {}) or {}
        sheet = ps.get("ads_sheet", {}) or {}
        creds = (((config or {}).get("data_sources", {}) or {}).get("ga4", {}) or {}
                 ).get("credentials_path") or "credentials.json"
        return cls(sheet_id=sheet.get("sheet_id", ""), credentials_path=creds,
                   search_terms_tab=sheet.get("search_terms_tab", "SearchTerms"),
                   keywords_tab=sheet.get("keywords_tab", "Keywords"))

    def configured(self) -> bool:
        return bool(self.sheet_id)

    def _get_service(self):
        if self._service is not None:
            return self._service
        if not self.sheet_id or not self.credentials_path:
            self._last_error = "Ads Sheet not configured (paid_search.ads_sheet.sheet_id)."
            return None
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            creds = service_account.Credentials.from_service_account_file(
                self.credentials_path, scopes=[_SHEETS_SCOPE])
            self._service = build("sheets", "v4", credentials=creds,
                                  cache_discovery=False)
            return self._service
        except Exception as e:
            self._last_error = f"Sheets auth failed: {e}"
            return None

    def _read_tab(self, tab: str) -> list[dict]:
        """Read one tab into a list of field dicts, matching columns by header name.
        Empty list on any problem (sets self._last_error)."""
        service = self._get_service()
        if service is None:
            return []
        try:
            resp = (service.spreadsheets().values()
                    .get(spreadsheetId=self.sheet_id, range=f"{tab}").execute())
            values = resp.get("values", [])
        except Exception as e:
            self._last_error = f"Could not read tab '{tab}': {e}"
            return []
        if len(values) < 2:
            return []
        header = [_norm_header(h) for h in values[0]]
        fields = [_ALIASES.get(h) for h in header]
        rows = []
        for raw in values[1:]:
            rec = {}
            for i, field in enumerate(fields):
                if not field:
                    continue
                val = raw[i] if i < len(raw) else ""
                rec[field] = _to_float(val) if field in _NUMERIC else str(val).strip()
            # Google Ads Scripts often export cost in micros; normalize to currency.
            if "cost" not in rec and "cost_micros" in rec:
                rec["cost"] = round(rec.pop("cost_micros") / 1_000_000.0, 2)
            rec.pop("cost_micros", None)
            if any(rec.get(k) for k in ("search_term", "keyword")):
                rows.append(rec)
        return rows

    def get_search_terms(self) -> list[dict]:
        """[{search_term, campaign, clicks, cost, conversions, conversion_value}]."""
        return self._read_tab(self.search_terms_tab)

    def get_keywords(self) -> list[dict]:
        """[{keyword, match_type, campaign, clicks, cost, conversions, conversion_value}]."""
        return self._read_tab(self.keywords_tab)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error
