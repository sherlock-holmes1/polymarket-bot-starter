"""Risk management — filled in during Step 6 of the tutorial.

Three layers:
  1. Per-trade size limits (fractional Kelly)
  2. Maximum total open exposure (position cap)
  3. Daily loss circuit-breaker
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.utils import get_logger

logger = get_logger(__name__)


@dataclass
class RiskManager:
    bankroll_pusd: float
    kelly_fraction: float = 0.25
    max_open_exposure_frac: float = 0.20
    daily_loss_limit_frac: float = 0.08

    _open_exposure_pusd: float = 0.0
    _daily_pnl_pusd: float = 0.0
    _pnl_date: date = field(default_factory=date.today)

    def kelly_size(self, edge: float, odds: float) -> float:
        if edge <= 0 or odds <= 0:
            return 0.0
        raw = self.bankroll_pusd * (edge / odds) * self.kelly_fraction
        # Clamp to [1.0, bankroll * max_open_exposure_frac]
        return max(1.0, min(raw, self.bankroll_pusd * self.max_open_exposure_frac))

    def can_trade(self, proposed_size: float) -> tuple[bool, str]:
        self._rollover_if_new_day()
        if self._open_exposure_pusd + proposed_size > self.bankroll_pusd * self.max_open_exposure_frac:
            return False, "Position cap would be exceeded"
        if self._daily_pnl_pusd <= -self.bankroll_pusd * self.daily_loss_limit_frac:
            return False, "Daily loss limit hit — circuit breaker tripped"
        return True, "ok"

    def record_trade_open(self, size: float) -> None:
        self._open_exposure_pusd += size

    def record_trade_close(self, size: float, pnl: float) -> None:
        self._rollover_if_new_day()
        self._open_exposure_pusd = max(0.0, self._open_exposure_pusd - size)
        self._daily_pnl_pusd += pnl

    def _rollover_if_new_day(self) -> None:
        today = date.today()
        if today != self._pnl_date:
            logger.info(f"Risk day rolled: {self._pnl_date} -> {today} (daily_pnl reset)")
            self._pnl_date = today
            self._daily_pnl_pusd = 0.0
