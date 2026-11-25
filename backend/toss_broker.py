"""Read-only domestic account access through the Toss Securities Open API.

Toss supplies only the account (holdings and buying power). Quotes, bars and investor
flows still come from BrokerConnector's KIS live domain with its PyKRX fallback, so
KIS_APP_KEY/KIS_APP_SECRET remain useful in Toss mode.
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from hashlib import sha256
import os

from backend.broker import (
    ZERO, Balance, BrokerConfigurationError, BrokerError, Holding, RetryableBrokerError, master_identity, number,
)


TOSS_API = "https://openapi.tossinvest.com"


class TossTokenRejected(BrokerConfigurationError):
    """401 on an API call: Toss keeps one live token per client and revokes older ones."""

    def __init__(self):
        super().__init__("toss_access_denied", "live")


@dataclass(frozen=True)
class TossSettings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    account_seq: str = ""

    @classmethod
    def from_env(cls):
        return cls(*(os.getenv(f"TOSS_{key}", "").strip() for key in (
            "CLIENT_ID", "CLIENT_SECRET", "ACCOUNT_SEQ",
        )))


def toss_account(settings: TossSettings):
    """Account factory for BrokerConnector(account=...)."""
    return lambda connector: TossAccount(connector, settings)


class TossAccount:
    """Uses the connector's shared HTTP client, request semaphore, retries and OAuth cache."""

    def __init__(self, connector, settings: TossSettings):
        self.connector = connector
        self.settings = settings
        self._account_seq = None
        self._account_lock = asyncio.Lock()

    async def _request(self, method, path, **kwargs):
        async with self.connector.semaphore:
            response = await self.connector.http.request(method, TOSS_API + path, **kwargs)
        status = response.status_code
        if status in {408, 429} or status >= 500:
            raise RetryableBrokerError(f"Toss HTTP {status}")
        if status == 401 and path != "/oauth2/token":
            raise TossTokenRejected()
        if status in {401, 403} or (path == "/oauth2/token" and status == 400):
            raise BrokerConfigurationError("toss_access_denied", "live")
        if not response.is_success:
            raise BrokerError(f"Toss HTTP {status}")
        try:
            body = response.json(parse_float=Decimal)
        except ValueError as exc:
            raise BrokerError("Invalid Toss JSON response") from exc
        if not isinstance(body, dict):
            raise BrokerError("Invalid Toss response object")
        return body

    def _cache_key(self):
        settings = self.settings
        if not settings.client_id or not settings.client_secret:
            raise BrokerConfigurationError("toss_account_not_configured", "live")
        fingerprint = sha256(f"{settings.client_id}\0{settings.client_secret}".encode()).hexdigest()
        return f"finguard:toss:oauth:{fingerprint}"

    async def _token(self):
        settings = self.settings
        key = self._cache_key()

        async def issue():
            return await self._request("POST", "/oauth2/token", data={
                "grant_type": "client_credentials",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
            })

        return await self.connector._cached_oauth(key, issue)

    async def _get(self, path, *, account_seq=None, params=None):
        async def send():
            token = await self._token()
            headers = {"Authorization": f"Bearer {token}"}
            if account_seq is not None:
                headers["X-Tossinvest-Account"] = account_seq
            try:
                return await self.connector._retry(
                    lambda: self._request("GET", path, headers=headers, params=params))
            except TossTokenRejected:
                await self.connector._drop_oauth(self._cache_key(), token)
                raise

        try:
            body = await send()
        except TossTokenRejected:
            body = await send()
        if "result" not in body:
            raise BrokerError("Toss response is missing result")
        return body["result"]

    async def _select_account(self):
        async with self._account_lock:
            if self._account_seq is not None:
                return self._account_seq
            accounts = await self._get("/api/v1/accounts")
            if not isinstance(accounts, list):
                raise BrokerError("Invalid Toss account list")
            if any(not isinstance(row, dict) for row in accounts):
                raise BrokerError("Invalid Toss account entry")
            accounts = [
                str(row["accountSeq"]) for row in accounts
                if row.get("accountType") == "BROKERAGE"
                and type(row.get("accountSeq")) is int
            ]
            configured = self.settings.account_seq
            if configured and configured in accounts:
                self._account_seq = configured
            elif not configured and len(accounts) == 1:
                self._account_seq = accounts[0]
            else:
                raise BrokerConfigurationError("toss_account_selection_required", "live")
            return self._account_seq

    async def get_balance(self) -> Balance:
        overview = await self._get("/api/v1/holdings", account_seq=await self._select_account())
        try:
            domestic = [(row, number(row["quantity"])) for row in overview["items"]
                        if row["marketCountry"] == "KR" and row["currency"] == "KRW"]
            positions = [(row, qty) for row, qty in domestic if qty != ZERO]
            total = number(overview["marketValue"]["amount"]["krw"])
            codes = list(dict.fromkeys(row["symbol"] for row, _ in positions))
            master = await self.connector.stock_master(codes)
            holdings = []
            for row, quantity in positions:
                code = row["symbol"]
                asset_type, corp_code, isin = master_identity(master.get(code))
                value = number(row["marketValue"]["amount"])
                holdings.append(Holding(
                    code, row["name"], asset_type, corp_code, isin, quantity,
                    number(row["averagePurchasePrice"]), number(row["lastPrice"]),
                    number(row["marketValue"]["purchaseAmount"]), value,
                    number(row["profitLoss"]["amount"]), number(row["profitLoss"]["rate"]) * 100,
                    value / total if total else ZERO,
                ))
        except (KeyError, TypeError, ValueError, DecimalException) as exc:
            raise BrokerError("Invalid Toss holdings response") from exc
        return Balance(None, total, None, tuple(holdings))

    async def get_orderable_cash(self, holding: Holding | None = None) -> Decimal:
        payload = await self._get("/api/v1/buying-power", account_seq=await self._select_account(),
                                  params={"currency": "KRW"})
        try:
            return number(payload["cashBuyingPower"])
        except (KeyError, TypeError, ValueError, DecimalException) as exc:
            raise BrokerError("Invalid Toss buying power response") from exc
