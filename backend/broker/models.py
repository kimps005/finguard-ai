"""Broker-neutral records, errors and parsing helpers shared by every account and market source."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


ZERO = Decimal(0)


class BrokerError(RuntimeError):
    pass


class BrokerConfigurationError(BrokerError):
    def __init__(self, code: str, mode: str):
        self.code = code
        self.mode = mode
        super().__init__(f"{code} ({mode})")


class RetryableBrokerError(BrokerError):
    pass


@dataclass(frozen=True)
class Holding:
    stock_code: str
    name: str
    asset_type: str | None
    corp_code: str | None
    isin_cd: str | None
    quantity: Decimal
    average_price: Decimal
    current_price: Decimal
    purchase_amount: Decimal
    market_value: Decimal
    profit_loss: Decimal
    profit_loss_pct: Decimal
    weight: Decimal


@dataclass(frozen=True)
class Balance:
    cash_deposit: Decimal | None
    total_value: Decimal
    cash_weight: Decimal | None
    holdings: tuple[Holding, ...]


@dataclass(frozen=True)
class Quote:
    stock_code: str
    current_price: Decimal
    source: str
    session: date | None = None
    change_pct: Decimal | None = None


@dataclass(frozen=True)
class DailyBar:
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass
class Candle:
    time: date | int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class InvestorFlow:
    session: date
    foreign_net_buy: Decimal
    institution_net_buy: Decimal


@dataclass(frozen=True)
class FiveDayFlow:
    days: tuple[InvestorFlow, ...]
    foreign_net_buy: Decimal
    institution_net_buy: Decimal

    @property
    def as_of(self) -> date:
        return self.days[-1].session


def number(value) -> Decimal:
    parsed = Decimal(str(value).strip().replace(",", ""))
    if not parsed.is_finite():
        raise ValueError("Non-finite broker number")
    return parsed


def symbol(code: str) -> str:
    code = code.strip()
    if len(code) == 7 and code[0] in {"A", "Q"}:
        return code[1:]
    return code


def day_string(value: date) -> str:
    return value.strftime("%Y%m%d")


def master_identity(stock) -> tuple[str | None, str | None, str | None]:
    """Holding identifiers from a stock_master row; all None for an unmapped security."""
    if stock is None:
        return None, None, None
    return stock.asset_type, stock.corp_code, stock.isin_cd
