"""
Google Ads governed write layer.

Kept strictly separate from the read-only GoogleAdsClient. Every mutation:
  - is gated by an explicit `allow_writes` flag (default OFF),
  - defaults to dry_run (reports what it WOULD do, changes nothing),
  - caps budget changes to a configured percentage,
  - refuses to touch shared budgets,
  - returns a structured before/after result for the audit ledger.

Recommendations are pure read-only logic and safe to call anytime.
"""

from pathlib import Path
from typing import Optional


def recommend_paid_actions(
    campaigns: list[dict],
    break_even_roas: float = 4.0,
    min_spend_pause: float = 25.0,
    min_spend_roas: float = 50.0,
    budget_step_pct: float = 20.0,
) -> list[dict]:
    """Propose governed paid actions from campaign performance.

    campaigns: list of dicts with campaign_id, name, type, status, cost,
               conversions, conversion_value, roas.
    Returns a list of proposals (no side effects).
    """
    recs = []
    active = [c for c in campaigns if (c.get("status") or "").upper() == "ENABLED"]

    for c in active:
        cost = c.get("cost", 0) or 0
        conv = c.get("conversions", 0) or 0
        roas = c.get("roas", 0) or 0
        name = c.get("name") or c.get("campaign_name") or c.get("campaign_id")

        # 1. Zero-converting spenders. CRITICAL: 0 tracked conversions can mean
        #    broken conversion tracking, NOT zero sales — recommending a hard PAUSE
        #    there can kill a genuinely profitable campaign (exactly the trap when
        #    tags are misconfigured). So this is a NON-executable "verify tracking
        #    first" advisory, not an auto-appliable PAUSE.
        if cost >= min_spend_pause and conv == 0:
            recs.append({
                "type": "VERIFY_TRACKING",
                "campaign_id": str(c.get("campaign_id")),
                "campaign_name": name,
                "rationale": (f"${cost:,.0f} spent with 0 TRACKED conversions. Do NOT pause yet — "
                              f"0 conversions is just as often broken conversion tracking as it is "
                              f"real waste. First confirm the conversion tag fires (Google Ads → "
                              f"Goals/Conversions, and test a purchase). Only if tracking is verified "
                              f"working AND it's still 0 after ~2 weeks of clean data is this true "
                              f"waste worth pausing."),
                "risk": "low",
                "params": {},
            })
            continue

        if cost < min_spend_roas:
            continue

        # 2. Scale winners (near or above break-even — worth finding more volume).
        # BUT only if there's impression-share headroom to capture. When we know
        # the campaign is losing little/no IS to budget, raising budget just
        # wastes spend — a profitable campaign already at ~95% IS can't grow.
        lost_budget = c.get("lost_is_budget")  # 0..1, or None if unknown
        imp_share = c.get("impression_share")  # 0..1, or None if unknown
        if roas >= break_even_roas * 0.9:
            budget_throttled = (lost_budget is None) or (lost_budget >= 0.10)
            if budget_throttled:
                near = "" if roas >= break_even_roas else " (near break-even)"
                is_note = (f" Losing {lost_budget*100:.0f}% of impressions to budget — headroom to scale."
                           if lost_budget else "")
                recs.append({
                    "type": "ADJUST_BUDGET",
                    "campaign_id": str(c.get("campaign_id")),
                    "campaign_name": name,
                    "rationale": f"ROAS {roas:.1f}x vs break-even {break_even_roas:.1f}x{near} — best performer, scale up.{is_note}",
                    "risk": "medium",
                    "params": {"direction": "increase", "step_pct": budget_step_pct},
                })
            elif imp_share is not None and imp_share >= 0.85:
                # Profitable but already dominant — different lever (bids/new terms).
                recs.append({
                    "type": "OBSERVE",
                    "campaign_id": str(c.get("campaign_id")),
                    "campaign_name": name,
                    "rationale": (f"ROAS {roas:.1f}x and already {imp_share*100:.0f}% impression share "
                                  f"(only {(lost_budget or 0)*100:.0f}% lost to budget) — raising budget won't "
                                  f"capture more; expand keywords/audiences or raise targets instead."),
                    "risk": "low",
                    "params": {},
                })
        # 3. Trim clear losers (well below break-even, but converting)
        elif 0 < roas < break_even_roas * 0.6:
            recs.append({
                "type": "ADJUST_BUDGET",
                "campaign_id": str(c.get("campaign_id")),
                "campaign_name": name,
                "rationale": f"ROAS {roas:.1f}x well below break-even {break_even_roas:.1f}x — trim spend.",
                "risk": "low",
                "params": {"direction": "decrease", "step_pct": budget_step_pct},
            })

    return recs


class GoogleAdsWriter:
    """Executes a small, guarded set of Google Ads mutations."""

    def __init__(
        self,
        credentials_path: str,
        customer_id: str,
        login_customer_id: Optional[str] = None,
        allow_writes: bool = False,
        max_budget_change_pct: float = 50.0,
    ):
        self.credentials_path = credentials_path
        self.customer_id = str(customer_id).replace("-", "")
        self.login_customer_id = (str(login_customer_id).replace("-", "")
                                  if login_customer_id else None)
        self.allow_writes = allow_writes
        self.max_budget_change_pct = max_budget_change_pct
        self._client = None
        self._last_error = None

    def _init(self) -> bool:
        if self._client is not None:
            return True
        try:
            from google.ads.googleads.client import GoogleAdsClient as GAdsClient
            if not (self.credentials_path and Path(self.credentials_path).exists()):
                self._last_error = f"credentials file not found ({self.credentials_path})"
                return False
            self._client = GAdsClient.load_from_storage(self.credentials_path)
            if self.login_customer_id:
                try:
                    self._client.login_customer_id = str(self.login_customer_id)
                except Exception:
                    pass
            return True
        except ImportError:
            self._last_error = "google-ads package not installed"
        except Exception as e:
            self._last_error = f"init error: {e}"
        return False

    def set_campaign_status(self, campaign_id: str, enable: bool,
                            dry_run: bool = True) -> dict:
        """Pause (enable=False) or enable (enable=True) a campaign."""
        target = "ENABLED" if enable else "PAUSED"
        if not self.allow_writes and not dry_run:
            return {"success": False, "message": "writes disabled (set allow_writes)"}
        if not self._init():
            return {"success": False, "message": self._last_error}
        try:
            from google.api_core import protobuf_helpers
            svc = self._client.get_service("CampaignService")
            op = self._client.get_type("CampaignOperation")
            campaign = op.update
            campaign.resource_name = svc.campaign_path(self.customer_id, campaign_id)
            campaign.status = (self._client.enums.CampaignStatusEnum.ENABLED if enable
                               else self._client.enums.CampaignStatusEnum.PAUSED)
            self._client.copy_from(op.update_mask,
                                   protobuf_helpers.field_mask(None, campaign._pb))
            if dry_run:
                return {"success": True, "dry_run": True,
                        "would": f"set campaign {campaign_id} to {target}"}
            resp = svc.mutate_campaigns(customer_id=self.customer_id, operations=[op])
            return {"success": True, "new_status": target,
                    "resource_name": resp.results[0].resource_name}
        except Exception as e:
            return {"success": False, "message": f"mutate failed: {e}"}

    def _get_budget(self, campaign_id: str):
        """Return (budget_resource_name, current_daily_amount, is_shared) or (None, ...)."""
        ga = self._client.get_service("GoogleAdsService")
        q = (
            "SELECT campaign.id, campaign_budget.resource_name, "
            "campaign_budget.amount_micros, campaign_budget.explicitly_shared "
            f"FROM campaign WHERE campaign.id = {int(campaign_id)}"
        )
        for row in ga.search(customer_id=self.customer_id, query=q):
            return (row.campaign_budget.resource_name,
                    row.campaign_budget.amount_micros / 1_000_000,
                    bool(row.campaign_budget.explicitly_shared))
        return (None, None, None)

    def adjust_budget(self, campaign_id: str, direction: str, step_pct: float,
                      dry_run: bool = True) -> dict:
        """Increase/decrease a campaign's daily budget by step_pct, capped by
        max_budget_change_pct. Refuses shared budgets."""
        if direction not in ("increase", "decrease"):
            return {"success": False, "message": f"bad direction: {direction}"}
        if not self.allow_writes and not dry_run:
            return {"success": False, "message": "writes disabled (set allow_writes)"}
        if not self._init():
            return {"success": False, "message": self._last_error}
        try:
            budget_rn, current, shared = self._get_budget(campaign_id)
            if not budget_rn:
                return {"success": False, "message": "campaign or budget not found"}
            if shared:
                return {"success": False,
                        "message": "budget is shared across campaigns — refusing to change"}

            pct = min(abs(step_pct), self.max_budget_change_pct) / 100.0
            new_amount = current * (1 + pct) if direction == "increase" else current * (1 - pct)
            new_amount = round(max(new_amount, 1.0), 2)  # never below $1/day

            if dry_run:
                return {"success": True, "dry_run": True, "current": current,
                        "new": new_amount,
                        "would": f"change daily budget ${current:,.2f} -> ${new_amount:,.2f}"}

            from google.api_core import protobuf_helpers
            svc = self._client.get_service("CampaignBudgetService")
            op = self._client.get_type("CampaignBudgetOperation")
            b = op.update
            b.resource_name = budget_rn
            b.amount_micros = int(round(new_amount * 1_000_000))
            self._client.copy_from(op.update_mask, protobuf_helpers.field_mask(None, b._pb))
            svc.mutate_campaign_budgets(customer_id=self.customer_id, operations=[op])
            return {"success": True, "current": current, "new": new_amount}
        except Exception as e:
            return {"success": False, "message": f"mutate failed: {e}"}
