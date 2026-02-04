"""
ProfitModel — minimum viable profit calculation.

Doctrine: Profit over traffic, always.
Optimize for Risk-Adjusted Incremental Profit (RAIP).
"""

from pydantic import BaseModel, Field, model_validator


class ProfitModel(BaseModel):
    """
    Profit model for RAIP calculation.

    RAIP = (Expected Incremental Gross Profit × Confidence Score) − Downside Risk

    If RAIP ≤ 0 → NO ACTION
    """
    aov: float = Field(
        default=53.19,
        gt=0,
        description="Average Order Value"
    )
    gross_margin_low: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Conservative gross margin estimate"
    )
    gross_margin_high: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description="Optimistic gross margin estimate"
    )
    evaluation_window_days: int = Field(
        default=28,
        ge=7,
        le=90,
        description="Rolling window for metric evaluation"
    )

    @model_validator(mode="after")
    def validate_margins(self) -> "ProfitModel":
        """Ensure margin_low <= margin_high."""
        if self.gross_margin_low > self.gross_margin_high:
            raise ValueError("gross_margin_low must be <= gross_margin_high")
        return self

    @property
    def gross_margin_conservative(self) -> float:
        """
        Use the low margin for RAIP calculations (conservative).

        Doctrine: Search engine hostility assumption.
        """
        return self.gross_margin_low

    @property
    def gross_profit_per_order(self) -> float:
        """Conservative gross profit per order."""
        return self.aov * self.gross_margin_conservative

    def estimate_incremental_profit(
        self,
        incremental_orders: float,
        use_conservative: bool = True
    ) -> float:
        """
        Estimate incremental gross profit from additional orders.

        Args:
            incremental_orders: Expected additional orders
            use_conservative: Use low margin (True) or high margin (False)
        """
        margin = self.gross_margin_low if use_conservative else self.gross_margin_high
        return incremental_orders * self.aov * margin

    def orders_from_revenue(self, revenue: float) -> float:
        """Calculate implied orders from revenue."""
        if self.aov == 0:
            return 0.0
        return revenue / self.aov
