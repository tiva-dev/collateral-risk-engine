from enum import Enum


class AssetType(str, Enum):
    CASH = "cash"
    BOND = "bond"
    BOND_FUND = "bond_fund"
    ETF = "etf"
    LISTED_EQUITY = "listed_equity"
    HIGH_VOLATILITY_EQUITY = "high_volatility_equity"
    CRYPTO = "crypto"
    OPTION = "option"
    PRIVATE_ASSET = "private_asset"
    OTHER = "other"


class RiskAppetite(str, Enum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class MarginState(str, Enum):
    SAFE = "safe"
    WATCH = "watch"
    RESTRICT_NEW_BORROWING = "restrict_new_borrowing"
    MARGIN_CALL = "margin_call"
    LIQUIDATION = "liquidation"


class PortfolioActionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    REPAYMENT = "repayment"
    CREDIT_DRAW = "credit_draw"


class TransferDirection(str, Enum):
    IN = "in"
    OUT = "out"


class RiskDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUIRE_REPAYMENT = "require_repayment"
    REDUCE_AVAILABLE_CREDIT = "reduce_available_credit"
    MARGIN_CALL = "margin_call"
    LIQUIDATION = "liquidation"


class DataMode(str, Enum):
    PROVIDED_BY_US = "provided_by_us"
    CLIENT_SUPPLIED = "client_supplied"
    HYBRID = "hybrid"
