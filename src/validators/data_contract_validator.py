"""
Data Contract Validator — confirms data sources are operational and schema-compliant.

This is the minimum viable first step per doctrine:
- Fully reversible
- No action taken on assets
- Pure observation and validation
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from pydantic import ValidationError

from ..models.page_asset import PageAsset


class ValidationStatus(str, Enum):
    """Status of a validation check."""
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class ValidationCheck:
    """A single validation check result."""
    name: str
    status: ValidationStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Complete result of data contract validation."""
    overall_status: ValidationStatus
    checks: list[ValidationCheck]
    sample_assets: list[PageAsset]
    errors: list[str]

    @property
    def is_valid(self) -> bool:
        """Returns True if all critical checks passed."""
        return self.overall_status in (ValidationStatus.PASS, ValidationStatus.WARN)

    @property
    def can_proceed(self) -> bool:
        """Returns True if Governor can proceed with evaluation."""
        critical_failures = [
            c for c in self.checks
            if c.status == ValidationStatus.FAIL and "critical" in c.name.lower()
        ]
        return len(critical_failures) == 0

    def to_markdown(self) -> str:
        """Generate human-readable validation report."""
        lines = [
            "# Data Contract Validation Report",
            f"**Overall Status:** {self.overall_status.value.upper()}",
            "",
            "## Checks",
        ]

        for check in self.checks:
            icon = {
                ValidationStatus.PASS: "✓",
                ValidationStatus.FAIL: "✗",
                ValidationStatus.WARN: "⚠",
                ValidationStatus.SKIP: "○",
            }.get(check.status, "?")
            lines.append(f"- {icon} **{check.name}**: {check.message}")

        if self.errors:
            lines.extend(["", "## Errors"])
            for error in self.errors:
                lines.append(f"- {error}")

        if self.sample_assets:
            lines.extend(["", "## Sample Assets Validated"])
            for asset in self.sample_assets:
                lines.append(f"- `{asset.url}` ({asset.asset_type.value})")

        return "\n".join(lines)


class DataContractValidator:
    """
    Validates that all data sources conform to the expected contract.

    Checks:
    1. GSC credentials/connectivity (if provided)
    2. GA4 credentials/connectivity (if provided)
    3. Page inventory existence
    4. Schema compliance for sample assets
    5. Tracking sanity (clicks ≈ sessions)
    """

    def __init__(
        self,
        gsc_client: Optional[Any] = None,
        ga4_client: Optional[Any] = None,
    ):
        """
        Initialize validator with optional API clients.

        If clients are None, those checks will be skipped.
        """
        self.gsc_client = gsc_client
        self.ga4_client = ga4_client
        self._checks: list[ValidationCheck] = []
        self._errors: list[str] = []
        self._sample_assets: list[PageAsset] = []

    def _add_check(
        self,
        name: str,
        status: ValidationStatus,
        message: str,
        details: dict[str, Any] | None = None
    ) -> None:
        """Record a validation check result."""
        self._checks.append(ValidationCheck(
            name=name,
            status=status,
            message=message,
            details=details or {},
        ))

    def validate_schema_compliance(self, raw_asset: dict[str, Any]) -> Optional[PageAsset]:
        """
        Validate a raw asset dict against the PageAsset schema.

        Returns the validated PageAsset or None if validation fails.
        """
        try:
            asset = PageAsset.model_validate(raw_asset)
            return asset
        except ValidationError as e:
            self._errors.append(f"Schema validation failed: {e}")
            return None

    def validate_gsc_connectivity(self) -> ValidationCheck:
        """Check GSC API connectivity."""
        if self.gsc_client is None:
            return ValidationCheck(
                name="GSC Connectivity",
                status=ValidationStatus.SKIP,
                message="No GSC client provided",
            )

        try:
            # Attempt a minimal API call
            if hasattr(self.gsc_client, "test_connection"):
                result = self.gsc_client.test_connection()
                if result:
                    return ValidationCheck(
                        name="GSC Connectivity",
                        status=ValidationStatus.PASS,
                        message="GSC API connection successful",
                    )
            return ValidationCheck(
                name="GSC Connectivity",
                status=ValidationStatus.WARN,
                message="GSC client provided but connection not verified",
            )
        except Exception as e:
            return ValidationCheck(
                name="GSC Connectivity",
                status=ValidationStatus.FAIL,
                message=f"GSC connection failed: {str(e)}",
            )

    def validate_ga4_connectivity(self) -> ValidationCheck:
        """Check GA4 API connectivity."""
        if self.ga4_client is None:
            return ValidationCheck(
                name="GA4 Connectivity",
                status=ValidationStatus.SKIP,
                message="No GA4 client provided",
            )

        try:
            if hasattr(self.ga4_client, "test_connection"):
                result = self.ga4_client.test_connection()
                if result:
                    return ValidationCheck(
                        name="GA4 Connectivity",
                        status=ValidationStatus.PASS,
                        message="GA4 API connection successful",
                    )
            return ValidationCheck(
                name="GA4 Connectivity",
                status=ValidationStatus.WARN,
                message="GA4 client provided but connection not verified",
            )
        except Exception as e:
            return ValidationCheck(
                name="GA4 Connectivity",
                status=ValidationStatus.FAIL,
                message=f"GA4 connection failed: {str(e)}",
            )

    def validate_page_inventory(
        self,
        inventory: list[dict[str, Any]]
    ) -> ValidationCheck:
        """Validate that page inventory exists and is non-empty."""
        if not inventory:
            return ValidationCheck(
                name="Critical: Page Inventory",
                status=ValidationStatus.FAIL,
                message="Page inventory is empty - Governor cannot operate",
            )

        return ValidationCheck(
            name="Critical: Page Inventory",
            status=ValidationStatus.PASS,
            message=f"Page inventory contains {len(inventory)} assets",
            details={"count": len(inventory)},
        )

    def validate_sample_assets(
        self,
        raw_assets: list[dict[str, Any]],
        sample_size: int = 5
    ) -> ValidationCheck:
        """
        Validate schema compliance for a sample of assets.

        Per doctrine: compute a single PageAsset for 5 URLs as a smoke test.
        """
        sample = raw_assets[:sample_size]
        validated = []
        failed = 0

        for raw in sample:
            asset = self.validate_schema_compliance(raw)
            if asset:
                validated.append(asset)
            else:
                failed += 1

        self._sample_assets = validated

        if failed == len(sample):
            return ValidationCheck(
                name="Schema Compliance",
                status=ValidationStatus.FAIL,
                message=f"All {len(sample)} sample assets failed validation",
            )
        elif failed > 0:
            return ValidationCheck(
                name="Schema Compliance",
                status=ValidationStatus.WARN,
                message=f"{failed}/{len(sample)} sample assets failed validation",
                details={"passed": len(validated), "failed": failed},
            )
        else:
            return ValidationCheck(
                name="Schema Compliance",
                status=ValidationStatus.PASS,
                message=f"All {len(validated)} sample assets validated successfully",
            )

    def validate_tracking_sanity(
        self,
        assets: list[PageAsset]
    ) -> ValidationCheck:
        """
        Check tracking sanity: GSC clicks should roughly equal GA4 sessions.

        Healthy ratio: 0.5 to 2.0 (generous bounds for noise).
        If failed → Governor should enter OBSERVE_ONLY.
        """
        if not assets:
            return ValidationCheck(
                name="Tracking Sanity",
                status=ValidationStatus.SKIP,
                message="No assets to validate",
            )

        assets_with_data = [
            a for a in assets
            if a.gsc.clicks_28d > 0
        ]

        if not assets_with_data:
            return ValidationCheck(
                name="Tracking Sanity",
                status=ValidationStatus.WARN,
                message="No assets have GSC click data to validate",
            )

        failed_sanity = [
            a for a in assets_with_data
            if not a.tracking_sanity_ok
        ]

        if len(failed_sanity) > len(assets_with_data) * 0.5:
            return ValidationCheck(
                name="Tracking Sanity",
                status=ValidationStatus.FAIL,
                message=f"{len(failed_sanity)}/{len(assets_with_data)} assets have tracking discrepancies",
                details={"failed_urls": [a.url for a in failed_sanity[:5]]},
            )
        elif failed_sanity:
            return ValidationCheck(
                name="Tracking Sanity",
                status=ValidationStatus.WARN,
                message=f"{len(failed_sanity)}/{len(assets_with_data)} assets have tracking discrepancies",
            )
        else:
            return ValidationCheck(
                name="Tracking Sanity",
                status=ValidationStatus.PASS,
                message="Tracking data is consistent across validated assets",
            )

    def run_full_validation(
        self,
        page_inventory: list[dict[str, Any]],
    ) -> ValidationResult:
        """
        Run complete data contract validation.

        Returns a ValidationResult with all checks and sample assets.
        """
        self._checks = []
        self._errors = []
        self._sample_assets = []

        # Run all checks
        self._checks.append(self.validate_gsc_connectivity())
        self._checks.append(self.validate_ga4_connectivity())
        self._checks.append(self.validate_page_inventory(page_inventory))

        if page_inventory:
            self._checks.append(self.validate_sample_assets(page_inventory))
            self._checks.append(self.validate_tracking_sanity(self._sample_assets))

        # Determine overall status
        if any(c.status == ValidationStatus.FAIL for c in self._checks):
            overall = ValidationStatus.FAIL
        elif any(c.status == ValidationStatus.WARN for c in self._checks):
            overall = ValidationStatus.WARN
        elif all(c.status == ValidationStatus.SKIP for c in self._checks):
            overall = ValidationStatus.SKIP
        else:
            overall = ValidationStatus.PASS

        return ValidationResult(
            overall_status=overall,
            checks=self._checks,
            sample_assets=self._sample_assets,
            errors=self._errors,
        )
