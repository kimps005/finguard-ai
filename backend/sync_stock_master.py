"""Import a KRX daily snapshot into stock_master."""

import argparse
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import os
import re
import sys
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import psycopg


DART_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
FSC_URL = "https://apis.data.go.kr/1160100"
LISTINGS = "GetKrxListedInfoService_V2/getItemInfo_V2"
PRICE_FEEDS = {
    "STOCK": ("GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2", "lstgStCnt"),
    "ETF": ("GetSecuritiesProductInfoService_V2/getETFPriceInfo_V2", "stLstgCnt"),
    "ETN": ("GetSecuritiesProductInfoService_V2/getETNPriceInfo_V2", "lstgScrtCnt"),
}
KST = timezone(timedelta(hours=9))

UPSERT = """
INSERT INTO stock_master
    (stock_code, corp_code, isin_cd, crno, corp_name, asset_type, induty_code, listed_shares, market)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (stock_code) DO UPDATE SET
    corp_code = EXCLUDED.corp_code,
    isin_cd = EXCLUDED.isin_cd,
    crno = EXCLUDED.crno,
    corp_name = EXCLUDED.corp_name,
    asset_type = EXCLUDED.asset_type,
    induty_code = EXCLUDED.induty_code,
    listed_shares = EXCLUDED.listed_shares,
    market = COALESCE(EXCLUDED.market, stock_master.market),
    updated_at = CURRENT_TIMESTAMP
WHERE (stock_master.corp_code, stock_master.isin_cd, stock_master.crno,
       stock_master.corp_name, stock_master.asset_type,
       stock_master.induty_code, stock_master.listed_shares, stock_master.market)
IS DISTINCT FROM
      (EXCLUDED.corp_code, EXCLUDED.isin_cd, EXCLUDED.crno,
       EXCLUDED.corp_name, EXCLUDED.asset_type, EXCLUDED.induty_code, EXCLUDED.listed_shares,
       COALESCE(EXCLUDED.market, stock_master.market))
"""


class SnapshotError(RuntimeError):
    """The data sources did not return a usable snapshot."""


@dataclass(frozen=True)
class Stock:
    stock_code: str
    corp_code: str | None
    isin_cd: str
    crno: str | None
    corp_name: str
    asset_type: str
    induty_code: str | None
    listed_shares: Decimal
    market: str | None = None


def download(url: str, params: dict) -> bytes:
    try:
        with urlopen(f"{url}?{urlencode(params)}", timeout=20) as response:
            return response.read()
    except (URLError, TimeoutError) as exc:
        # The request URL contains the credential.
        status = getattr(exc, "code", type(exc).__name__)
        raise SnapshotError(f"Request failed for {url} ({status})") from None


def parse_dart(archive: bytes) -> dict[str, str]:
    try:
        with ZipFile(BytesIO(archive)) as package:
            root = ElementTree.fromstring(package.read("CORPCODE.xml"))
    except (BadZipFile, KeyError, ElementTree.ParseError):
        raise SnapshotError("DART did not return a valid CORPCODE.xml ZIP") from None

    codes = {}
    for issuer in root.findall("list"):
        stock_code = (issuer.findtext("stock_code") or "").strip()
        corp_code = (issuer.findtext("corp_code") or "").strip()
        if not stock_code:
            continue
        if not re.fullmatch(r"[0-9]{8}", corp_code):
            raise SnapshotError(f"Invalid DART code for {stock_code}")
        previous = codes.setdefault(stock_code, corp_code)
        if previous != corp_code:
            raise SnapshotError(f"Conflicting DART codes for {stock_code}")
    if not codes:
        raise SnapshotError("DART returned no listed issuers")
    return codes


def fsc_rows(operation: str, key: str, base_date: date) -> list[dict]:
    rows = []
    page = 1
    expected_total = None
    while True:
        payload = download(f"{FSC_URL}/{operation}", {
            "serviceKey": key,
            "resultType": "json",
            "basDt": base_date.strftime("%Y%m%d"),
            "numOfRows": 1000,
            "pageNo": page,
        })
        try:
            response = json.loads(payload, parse_float=Decimal)["response"]
            code = str(response["header"]["resultCode"])
            if code == "03" and page == 1:
                return []
            if code not in {"00", "0"}:
                raise SnapshotError(f"FSC {operation} returned code {code}")
            body = response["body"]
            total = int(body["totalCount"])
            items = body.get("items") or {}
            batch = items.get("item", []) if isinstance(items, dict) else items
            if isinstance(batch, dict):
                batch = [batch]
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise ValueError("invalid items")
        except (ValueError, KeyError, TypeError):
            raise SnapshotError(f"Invalid FSC response from {operation} (page {page})") from None

        if expected_total is None:
            expected_total = total
        rows.extend(batch)
        if total != expected_total or len(rows) > total or (not batch and len(rows) < total):
            raise SnapshotError(f"Inconsistent FSC pagination for {operation}")
        if len(rows) == total:
            return rows
        page += 1


def listed_shares(value: object, isin: str) -> Decimal:
    try:
        shares = Decimal(str(value))
        if shares.is_finite() and 0 < shares <= 9223372036854775807:
            if shares == shares.to_integral_value():
                return shares
    except InvalidOperation:
        pass
    raise SnapshotError(f"Invalid listed quantity for {isin}")


def short_code(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[AQ][0-9A-Z]{6}", value):
        return value[1:]
    return value


def build_stock_master(
    listings: list[dict], quotes: dict[str, list[dict]], dart_codes: dict[str, str]
) -> list[Stock]:
    by_isin = {row["isinCd"]: row for row in listings}
    if len(by_isin) != len(listings):
        raise SnapshotError("Duplicate ISIN in KRX listings")

    securities = {}
    for asset_type, price_rows in quotes.items():
        _, shares_field = PRICE_FEEDS[asset_type]
        for quote in price_rows:
            isin = quote["isinCd"]
            listing = by_isin.get(isin, {})
            code = short_code(listing.get("srtnCd") or quote["srtnCd"])
            crno = (listing.get("crno") or "").strip() or None
            name = listing.get("itmsNm") or quote["itmsNm"]
            issuer_name = listing.get("corpNm") or name
            resolved_type = asset_type
            if asset_type == "STOCK" and "부동산투자회사" in "".join(issuer_name.split()):
                resolved_type = "REIT"

            corp_code = dart_codes.get(code) if asset_type == "STOCK" else None
            if code in securities:
                raise SnapshotError(f"Duplicate short code in FSC price feeds: {code}")
            securities[code] = Stock(
                code, corp_code, isin, crno, name, resolved_type,
                (listing.get("indutyCode") or "").strip() or None,
                listed_shares(quote.get(shares_field), isin),
                (quote.get("mrktCtg") or listing.get("mrktCtg") or "").strip() or None,
            )

    # DART supplies the issuer's listed code. Direct matches can seed the link
    # even when the common share does not have a quote on the snapshot date.
    ordinary_codes: dict[str, set[str]] = defaultdict(set)
    etp_isins = {
        quote["isinCd"] for kind in ("ETF", "ETN") for quote in quotes.get(kind, [])
    }
    for listing in listings:
        crno = (listing.get("crno") or "").strip()
        code = short_code(listing["srtnCd"])
        corp_code = dart_codes.get(code)
        issuer_name = (listing.get("corpNm") or listing.get("itmsNm") or "")
        if (
            crno and corp_code
            and listing["isinCd"] not in etp_isins
            and "부동산투자회사" not in "".join(issuer_name.split())
        ):
            ordinary_codes[crno].add(corp_code)

    result = []
    for code, stock in sorted(securities.items()):
        candidates = ordinary_codes.get(stock.crno) if stock.crno else None
        if (
            stock.asset_type == "STOCK" and stock.corp_code is None
            and candidates and len(candidates) == 1
        ):
            stock = replace(stock, corp_code=next(iter(candidates)))
        result.append(stock)
    return result


def sync(base_date: date | None, dart_key: str, fsc_key: str) -> tuple[date, int]:
    dates = [base_date] if base_date else [
        datetime.now(KST).date() - timedelta(days=offset) for offset in range(14)
    ]
    for day in dates:
        listings = fsc_rows(LISTINGS, fsc_key, day)
        if not listings:
            continue
        quotes = {
            asset_type: fsc_rows(operation, fsc_key, day)
            for asset_type, (operation, _) in PRICE_FEEDS.items()
        }
        if not all(quotes.values()):
            continue

        dart_codes = parse_dart(download(DART_URL, {"crtfc_key": dart_key}))
        stocks = build_stock_master(listings, quotes, dart_codes)
        with psycopg.connect() as connection, connection.cursor() as cursor:
            cursor.executemany(UPSERT, [
                (stock.stock_code, stock.corp_code, stock.isin_cd, stock.crno,
                 stock.corp_name, stock.asset_type, stock.induty_code, int(stock.listed_shares), stock.market)
                for stock in stocks
            ])
        return day, len(stocks)
    raise SnapshotError("No complete KRX snapshot for the requested date range")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-date", type=date.fromisoformat, help="KRX date (YYYY-MM-DD)")
    args = parser.parse_args()
    dart_key = os.environ.get("DART_API_KEY", "").strip()
    fsc_key = os.environ.get("FSC_SERVICE_KEY", "").strip()
    if not dart_key or not fsc_key:
        parser.error("DART_API_KEY and FSC_SERVICE_KEY are both required")

    try:
        base_date, count = sync(args.base_date, dart_key, fsc_key)
    except (SnapshotError, psycopg.Error, KeyError) as exc:
        print(f"stock_master sync failed: {exc}", file=sys.stderr)
        return 1
    print(f"stock_master synced {count} securities for {base_date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
