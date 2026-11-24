from datetime import date
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import MagicMock
from zipfile import ZipFile

from pglast import ast, parse_sql
from pglast.enums import ConstrType
import pytest

from backend import sync_stock_master as batch


MIGRATION = Path(__file__).resolve().parents[1] / "backend/migrations/001_module1.sql"
ISSUER = "1234567890123"
COMMON_ISIN = "KR7005930003"
PREFERRED_ISIN = "KR7005931001"
ETF_ISIN = "KR7069500007"
ETN_ISIN = "KRG580000010"


@pytest.fixture
def snapshot():
    listings = [
        {"srtnCd": "A005935", "isinCd": PREFERRED_ISIN, "crno": ISSUER, "itmsNm": "삼성전자우"},
        {"srtnCd": "A005930", "isinCd": COMMON_ISIN, "crno": ISSUER, "itmsNm": "삼성전자"},
        {"srtnCd": "069500", "isinCd": ETF_ISIN, "crno": ISSUER, "itmsNm": "KODEX 200"},
    ]
    quotes = {
        "STOCK": [
            {"isinCd": PREFERRED_ISIN, "lstgStCnt": "822886700"},
            {"isinCd": COMMON_ISIN, "lstgStCnt": "5969782550"},
        ],
        "ETF": [{"isinCd": ETF_ISIN, "stLstgCnt": 3000000}],
        "ETN": [{"isinCd": ETN_ISIN, "srtnCd": "Q580001", "itmsNm": "예시 ETN", "lstgScrtCnt": 900000}],
    }
    return listings, quotes, {"005930": "00126380", "069500": "00999999"}


def test_preferred_inherits_common_corp_code_by_crno(snapshot):
    stocks = {stock.stock_code: stock for stock in batch.build_stock_master(*snapshot)}
    preferred = stocks["005935"]
    assert preferred.corp_code == stocks["005930"].corp_code == "00126380"
    assert preferred.crno == ISSUER
    assert preferred.asset_type == "STOCK"
    assert preferred.listed_shares == Decimal("822886700")
    assert isinstance(preferred.listed_shares, Decimal)


def test_preferred_can_inherit_when_common_quote_is_absent(snapshot):
    listings, quotes, dart = snapshot
    quotes["STOCK"].pop()
    stocks = {stock.stock_code: stock for stock in batch.build_stock_master(listings, quotes, dart)}
    assert stocks["005935"].corp_code == "00126380"


@pytest.mark.parametrize("crno", [None, "", "9999999999999"])
def test_unmatched_preferred_stays_unmapped(snapshot, crno):
    listings, quotes, dart = snapshot
    listings[0]["crno"] = crno
    preferred = next(stock for stock in batch.build_stock_master(listings, quotes, dart)
                     if stock.stock_code == "005935")
    assert preferred.corp_code is None


def test_ambiguous_crno_mapping_stays_unmapped(snapshot):
    listings, quotes, dart = snapshot
    listings.append({"srtnCd": "001000", "isinCd": "KR7001000000", "crno": ISSUER})
    dart["001000"] = "00000001"
    preferred = next(stock for stock in batch.build_stock_master(listings, quotes, dart)
                     if stock.stock_code == "005935")
    assert preferred.corp_code is None


def test_etf_and_etn_use_feed_classification_and_no_corp_code(snapshot):
    stocks = {stock.stock_code: stock for stock in batch.build_stock_master(*snapshot)}
    assert stocks["069500"].asset_type == "ETF"
    assert stocks["069500"].corp_code is None
    assert stocks["069500"].listed_shares == Decimal("3000000")
    assert stocks["580001"].asset_type == "ETN"
    assert stocks["580001"].corp_code is None
    assert stocks["580001"].listed_shares == Decimal("900000")


@pytest.mark.parametrize("invalid", [None, "0", "1.5", "9223372036854775808", "NaN"])
def test_invalid_listed_quantity_rejects_snapshot(snapshot, invalid):
    snapshot[1]["ETF"][0]["stLstgCnt"] = invalid
    with pytest.raises(batch.SnapshotError, match="Invalid listed quantity"):
        batch.build_stock_master(*snapshot)


def test_dart_zip_parses_listed_issuers_and_preserves_leading_zeroes():
    archive = BytesIO()
    with ZipFile(archive, mode="w") as package:
        package.writestr("CORPCODE.xml", """<result>
          <list><stock_code>005930</stock_code><corp_code>00126380</corp_code></list>
          <list><stock_code> </stock_code><corp_code>12345678</corp_code></list>
        </result>""")
    assert batch.parse_dart(archive.getvalue()) == {"005930": "00126380"}


def test_fsc_reads_all_pages_for_same_date(monkeypatch):
    requests = []

    def download(url, params):
        requests.append(params)
        row = {"isinCd": f"KR000000000{params['pageNo']}"}
        return json.dumps({"response": {
            "header": {"resultCode": "00"},
            "body": {"totalCount": 2, "items": {"item": [row]}},
        }}).encode()

    monkeypatch.setattr(batch, "download", download)
    rows = batch.fsc_rows(batch.LISTINGS, "test-key", date(2026, 11, 19))
    assert [row["isinCd"] for row in rows] == ["KR0000000001", "KR0000000002"]
    assert [(request["pageNo"], request["basDt"]) for request in requests] == [
        (1, "20261119"), (2, "20261119"),
    ]


@pytest.mark.parametrize("complete", [True, False])
def test_sync_writes_mixed_snapshot_only_after_all_feeds_arrive(snapshot, monkeypatch, complete):
    listings, quotes, _ = snapshot
    if not complete:
        quotes["ETN"].clear()
    operations = {batch.LISTINGS: listings}
    operations.update({
        operation: quotes[kind] for kind, (operation, _) in batch.PRICE_FEEDS.items()
    })
    archive = BytesIO()
    with ZipFile(archive, mode="w") as package:
        package.writestr("CORPCODE.xml", """<result><list>
          <stock_code>005930</stock_code><corp_code>00126380</corp_code>
        </list></result>""")

    def download(url, params):
        if url == batch.DART_URL:
            assert params == {"crtfc_key": "dart-test"}
            return archive.getvalue()
        assert params["serviceKey"] == "fsc-test"
        assert params["basDt"] == "20261119"
        rows = operations[url.removeprefix(f"{batch.FSC_URL}/")]
        return json.dumps({"response": {
            "header": {"resultCode": "00"},
            "body": {"totalCount": len(rows), "items": {"item": rows}},
        }}).encode()

    connect = MagicMock()
    monkeypatch.setattr(batch, "download", download)
    monkeypatch.setattr(batch.psycopg, "connect", connect)
    day = date(2026, 11, 19)
    if not complete:
        with pytest.raises(batch.SnapshotError, match="No complete KRX snapshot"):
            batch.sync(day, "dart-test", "fsc-test")
        connect.assert_not_called()
        return

    assert batch.sync(day, "dart-test", "fsc-test") == (day, 4)
    connect.assert_called_once_with()
    connection = connect.return_value.__enter__.return_value
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.executemany.assert_called_once_with(batch.UPSERT, [
        ("005930", "00126380", COMMON_ISIN, ISSUER, "삼성전자", "STOCK", None, 5969782550, None),
        ("005935", "00126380", PREFERRED_ISIN, ISSUER, "삼성전자우", "STOCK", None, 822886700, None),
        ("069500", None, ETF_ISIN, ISSUER, "KODEX 200", "ETF", None, 3000000, None),
        ("580001", None, ETN_ISIN, None, "예시 ETN", "ETN", None, 900000, None),
    ])


def test_stock_master_schema_and_upsert_columns():
    statements = [item.stmt for item in parse_sql(MIGRATION.read_text(encoding="utf-8"))]
    stock = next(stmt for stmt in statements
                 if isinstance(stmt, ast.CreateStmt) and stmt.relation.relname == "stock_master")
    columns = {col.colname: col for col in stock.tableElts if isinstance(col, ast.ColumnDef)}
    assert any(c.contype == ConstrType.CONSTR_PRIMARY for c in columns["stock_code"].constraints)
    assert columns["corp_code"].constraints is None
    assert columns["crno"].constraints is None
    asset_constraints = columns["asset_type"].constraints
    assert {item.contype for item in asset_constraints} == {
        ConstrType.CONSTR_NOTNULL, ConstrType.CONSTR_DEFAULT,
    }
    assert columns["listed_shares"].typeName.names[-1].sval == "int8"
    assert {stmt.idxname for stmt in statements if isinstance(stmt, ast.IndexStmt)} == {
        "idx_stock_master_corp", "idx_stock_master_isin", "idx_stock_master_crno",
    }
    assert any(isinstance(stmt, ast.CreateExtensionStmt) and stmt.extname == "vector"
               for stmt in statements)

    bound_sql = batch.UPSERT
    for index in range(1, 10):
        bound_sql = bound_sql.replace("%s", f"${index}", 1)
    upsert = parse_sql(bound_sql)[0].stmt
    assert upsert.relation.relname == "stock_master"
    assert [column.name for column in upsert.cols] == [
        "stock_code", "corp_code", "isin_cd", "crno",
        "corp_name", "asset_type", "induty_code", "listed_shares", "market",
    ]
    assert upsert.onConflictClause.infer.indexElems[0].name == "stock_code"


def test_overhang_schema_has_nulls_not_distinct_composite_key():
    statements = [item.stmt for item in parse_sql(MIGRATION.read_text(encoding="utf-8"))]
    overhang = next(stmt for stmt in statements
                    if isinstance(stmt, ast.CreateStmt) and stmt.relation.relname == "overhang_events")
    columns = {col.colname: col for col in overhang.tableElts if isinstance(col, ast.ColumnDef)}
    assert columns["bond_type_issue_no"].constraints is None
    price_type = columns["conversion_price"].typeName
    assert price_type.names[-1].sval == "numeric"
    assert [modifier.val.ival for modifier in price_type.typmods] == [18, 2]

    unique = next(constraint for constraint in overhang.tableElts
                  if isinstance(constraint, ast.Constraint) and constraint.conname == "uq_overhang_event")
    assert unique.contype == ConstrType.CONSTR_UNIQUE
    assert unique.nulls_not_distinct is True
    assert [key.sval for key in unique.keys] == [
        "corp_code", "bond_type_issue_no", "board_resolution_date",
    ]
