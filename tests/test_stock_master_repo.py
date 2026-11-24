import asyncio
from decimal import Decimal
import sys

from backend import stock_master
from backend.sync_stock_master import UPSERT


def run(coro):
    if sys.platform == "win32":  # libpq async connections require a selector loop.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coro)
    return asyncio.run(coro)


ROWS = [
    ("005930", "00126380", "KR7005930003", "1301110006246", "삼성전자", "STOCK", "264", 5969782550, "KOSPI"),
    ("005935", "00126380", "KR7005931001", "1301110006246", "삼성전자우", "STOCK", "264", 822886700, "KOSPI"),
    ("028260", "00149655", "KR7028260008", "1101110015762", "삼성물산", "STOCK", "461", 169976544, None),
    ("069500", None, "KR7069500007", None, "KODEX 200", "ETF", None, 3000000, "KOSPI"),
]


def test_fetch_returns_complete_rows_and_omits_unknown_codes(db):
    db.cursor().executemany(UPSERT, ROWS)
    stocks = run(stock_master.fetch_stocks(["005935", "069500", "999999"]))
    assert set(stocks) == {"005935", "069500"}
    preferred = stocks["005935"]
    # Preferred shares carry the common share's DART corp_code.
    assert (preferred.corp_code, preferred.corp_name, preferred.market) == ("00126380", "삼성전자우", "KOSPI")
    assert preferred.listed_shares == Decimal(822886700) and isinstance(preferred.listed_shares, Decimal)
    assert stocks["069500"].corp_code is None and stocks["069500"].asset_type == "ETF"
    assert run(stock_master.fetch_stocks([])) == {}


def test_search_ranks_code_exact_prefix_then_substring(db):
    db.cursor().executemany(UPSERT, ROWS)
    names = lambda query: [row["corp_name"] for row in run(stock_master.search_stocks(query))]
    assert names("005930") == ["삼성전자"]
    assert names("삼성전자") == ["삼성전자", "삼성전자우"]
    # Same rank and name length: the lower stock code wins.
    assert names("삼성") == ["삼성전자", "삼성물산", "삼성전자우"]
    assert names("kodex") == ["KODEX 200"]
    assert names("200") == ["KODEX 200"]
    assert run(stock_master.search_stocks("")) == []
