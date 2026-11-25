"""Request-scoped LangGraph portfolio diagnosis."""

import asyncio
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import logging
import operator
import re
from typing import Annotated, Literal, NotRequired, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from backend import llm, price_card, stock_analysis
from backend.card_validation import AI_UNAVAILABLE_WARNING
from backend.broker import Balance
from backend.finance_calc import calculate_scorecard, simulate_purchase
from backend.portfolio_market import MarketData, ScoreInputs
from backend.report import account_view, answer_report, diagnosis_report, quote_parts


logger = logging.getLogger(__name__)
MAX_TARGETS = 3
# Chat waits this long for the analyst's card after the local card, then answers with the local card.
CARD_WAIT_SECONDS = 40
SLOW_CARD_WARNING = "가격 카드 AI 분석이 늦어 기본 계산 카드로 답합니다."


class Query(TypedDict):
    user_query: str
    # The stock whose price card is on screen; it is answered about even when not held.
    selected_stock_code: NotRequired[str | None]


class PortfolioState(Query, total=False):
    # Public: streamed to the client in the final "done" event (main.PUBLIC_FIELDS).
    intent_type: Literal["fast_lookup", "deep_diagnosis", "simulation"]
    account_summary: dict
    holdings: list[dict]
    quant_scorecards: dict[str, dict]
    macro_context: dict | None
    verified_stock_results: list[dict]  # No reducer: the guardrail writes it once, so it never accumulates.
    guardrail_warnings: list[str]
    simulation_result: dict | None
    final_report: str
    # Internal: typed inputs shared between nodes, never serialized to the client.
    stock_analysis_results: Annotated[list[dict], operator.add]  # Fan-out buffer, one entry per worker.
    balance: Balance
    as_of: date
    score_inputs: dict[str, ScoreInputs]


async def router_node(state):
    query = re.sub(r"\s+", "", state["user_query"].lower())
    lookup = re.search(r"잔고|잔액|현금|예수금|현재가|주가|시세|평단|평가액|수익률|보유|비중|평가손익|fast_lookup", query)
    diagnosis = re.search(
        r"분석|위험|리스크|진단|공시|환율|금리|수급|뉴스|전망|영향|이유|왜|악재|어때|유리|불리|"
        r"보호예수|오버행|실적|추천|전환사채|신주인수권|증자|희석|채권|모멘텀|스코어|금융|cb|bw", query)
    if re.search(r"물타|추매|추가매수|시뮬|리밸런|매수하면|사면|simulation", query):
        intent = "simulation"
    elif lookup and not diagnosis:
        intent = "fast_lookup"
    else:
        intent = "deep_diagnosis"
    return {"intent_type": intent, "verified_stock_results": [], "guardrail_warnings": [],
            "quant_scorecards": {}, "macro_context": None, "simulation_result": None}


def target_codes(state) -> list[str]:
    """The selected stock, then the holdings the question names in order, at most three.

    A question that names no holding is about the portfolio: the selected stock is followed by the
    heaviest STOCK holdings. Score inputs are not required: a selected stock the account does not
    hold has none, and price_card decides held or not on its own.
    """
    query, holdings = state["user_query"], state["holdings"]
    codes = [selected] if (selected := state.get("selected_stock_code")) else []
    mentions = []
    for holding in holdings:
        found = [query.find(label) for label in (holding["stock_code"], holding["name"]) if label and label in query]
        if found:
            mentions.append((min(found), holding["stock_code"]))
    if mentions:
        codes += [code for _, code in sorted(mentions)]
    else:
        stocks = [h for h in holdings if h["asset_type"] == "STOCK"]
        codes += [h["stock_code"] for h in sorted(stocks, key=lambda h: (-h["weight"], h["stock_code"]))]
    return list(dict.fromkeys(codes))[:MAX_TARGETS]


def route_to_stock_workers(state: PortfolioState) -> str | list[Send]:
    codes = target_codes(state)
    if not codes:
        return "synthesizer_node"
    names = {h["stock_code"]: h["name"] for h in state["holdings"]}
    return [Send("stock_worker", {"stock_code": code, "name": names.get(code)}) for code in codes]


def account_numbers(broker) -> list[str]:
    """Configured account identifiers (KIS CANO for both domains, Toss account seq) to mask in questions."""
    settings = getattr(broker, "settings", None)
    candidates = [getattr(getattr(settings, domain, None), "account_no", None) for domain in ("live", "virtual")]
    candidates.append(getattr(getattr(getattr(broker, "account", None), "settings", None), "account_seq", None))
    return [value.strip() for value in candidates if isinstance(value, str) and value.strip()]


def card_row(stock_code, band):
    """A worker row: the card, plus the doc_map/disclosures shape the local report links filings from."""
    docs = band["disclosures"]
    return {"stock_code": stock_code, "card": band,
            "doc_map": {doc["alias"]: doc["receipt"] for doc in docs},
            "disclosures": [{"source": doc["alias"], "title": doc["title"], "text": ""} for doc in docs],
            "news": band["news"]}


async def simulation(query, balance, market):
    labels = {label: h.stock_code for h in balance.holdings for label in (h.stock_code, h.name) if label}
    pattern = "|".join(map(re.escape, sorted(labels, key=len, reverse=True)))
    codes = {labels[m[0]] for m in re.finditer(pattern, query)} if pattern else set()
    if len(codes) != 1:
        return {"message": "시뮬레이션할 보유 종목명 또는 종목코드 한 개를 지정해 주세요."}
    holding = next(h for h in balance.holdings if h.stock_code in codes)
    budgets = re.findall(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*(%|퍼센트|만원|억원|원)", query)
    if len(budgets) != 1:
        return {"message": "사용할 주문 가능 현금의 비율(%) 또는 투자 금액(원)을 한 개 지정해 주세요."}
    value, unit = budgets[0]
    amount = Decimal(value.replace(",", ""))
    if unit in ("%", "퍼센트"):
        kwargs = {"ratio": amount / 100}
    else:
        kwargs = {"amount": amount * {"원": 1, "만원": 10000, "억원": 100000000}[unit]}
    cash = await market.orderable_cash(holding)
    try:
        result = simulate_purchase(holding, balance, cash, **kwargs)
    except ValueError:
        return {"message": "투자 금액은 0원 이상이며 주문 가능 현금 이내여야 합니다."}
    return {"stock_code": holding.stock_code, "orderable_cash": cash, **asdict(result)}


def build_graph(market: MarketData):
    async def fast_quote_node(state):
        return account_view(await market.broker.get_balance())

    async def market_data_node(state):
        balance, on, macro, inputs, warnings = await market.collect()
        view = account_view(balance)
        # Public rows carry the mapping the score inputs were collected with. A holding without
        # inputs keeps the broker's mapping; fan-out skips it because it has no score_inputs.
        for row in view["holdings"]:
            if entry := inputs.get(row["stock_code"]):
                row.update(asset_type=entry.stock.asset_type, corp_code=entry.stock.corp_code)
        return {**view, "balance": balance, "as_of": on, "macro_context": macro, "score_inputs": inputs,
                "guardrail_warnings": state["guardrail_warnings"] + warnings}

    async def quant_scorecard_node(state):
        cards = {}
        for holding in state["balance"].holdings:
            inputs = state["score_inputs"].get(holding.stock_code)
            if inputs is None:
                # Mapped in stock_master but a required input (five-day flow) failed.
                cards[holding.stock_code] = {"status": "unavailable" if holding.asset_type else "unmapped"}
                continue
            cards[holding.stock_code] = calculate_scorecard(
                inputs.stock, holding, inputs.flow, as_of=state["as_of"], reports=inputs.reports,
                bars=inputs.bars, releases=inputs.releases, overhang_events=inputs.events)
        result = None
        if state["intent_type"] == "simulation":
            result = await simulation(state["user_query"], state["balance"], market)
        return {"quant_scorecards": cards, "simulation_result": result}

    def fan_out_node(state) -> Command[Literal["stock_worker", "synthesizer_node"]]:
        return Command(goto=route_to_stock_workers(state))

    async def stock_worker(job):
        code, label = job["stock_code"], job["name"] or job["stock_code"]
        # The first update is a cached card or the local card; the analyst's card follows it.
        updates = price_card.card_updates(market, code)
        try:
            try:
                band = (await anext(updates))["band"]
            except Exception as exc:  # CancelledError is not an Exception and still propagates.
                # Provider errors can carry credential-bearing URLs; log the type only.
                logger.warning("Price card for chat unavailable (%s)", type(exc).__name__)
                row = {"stock_code": code, "card": None, "doc_map": {}, "disclosures": [], "news": [],
                       "warnings": [f"{label} 가격 카드를 만들 수 없어 카드 없이 답변합니다."]}
                return {"stock_analysis_results": [row]}
            warning = None
            try:
                # Bound the wait for the analyst; a cancelled build releases its lock and caches nothing.
                async with asyncio.timeout(CARD_WAIT_SECONDS):
                    async for update in updates:
                        band = update["band"]
            except TimeoutError:
                logger.warning("Price card analysis for chat timed out; answering with the local card")
                warning = f"{label} {SLOW_CARD_WARNING}"
            except Exception as exc:
                logger.warning("Price card analysis for chat failed (%s)", type(exc).__name__)
                warning = f"{label} 가격 카드: {AI_UNAVAILABLE_WARNING}"
            row = card_row(code, band)
            if warning:
                row["warnings"] = [warning]
            return {"stock_analysis_results": [row]}
        finally:
            await updates.aclose()

    async def guardrail_node(state):
        # Cards were validated when built; record them once and surface their warnings.
        rows = state["stock_analysis_results"]
        warnings = list(state["guardrail_warnings"])
        for row in rows:
            warnings += row.get("warnings", [])
            if band := row.get("card"):
                warnings += [f"{band['name']} 가격 카드: {warning}" for warning in band["warnings"]]
        return {"verified_stock_results": rows, "guardrail_warnings": list(dict.fromkeys(warnings))}

    async def synthesizer_node(state):
        write = get_stream_writer()
        warnings = list(state["guardrail_warnings"])
        if state["intent_type"] == "fast_lookup":
            parts = list(quote_parts(state))
        else:
            report = None
            if state["holdings"] or state["verified_stock_results"]:
                try:
                    core, sources, notes = await stock_analysis.answer(state, account_numbers(market.broker))
                    warnings.extend(notes)
                    report = answer_report(state, core, sources)
                except llm.LLMUnavailable as exc:
                    warnings.append(f"답변 모델을 사용할 수 없어 로컬 정량 리포트를 제공합니다. [{exc.reason}]")
                except Exception as exc:  # any other answer failure still ends with the local report
                    logger.warning("Answer failed, using local report (%s)", type(exc).__name__)
                    warnings.append("답변 모델을 사용할 수 없어 로컬 정량 리포트를 제공합니다. [응답 처리 오류]")
            parts = [report or diagnosis_report(state)]
        for part in parts:
            write({"text": part})
        return {"final_report": "".join(parts), "guardrail_warnings": warnings}

    # Only the query and the selected code cross the request boundary. No checkpointer retains reducer channels.
    graph = StateGraph(PortfolioState, input_schema=Query)
    for node in (router_node, fast_quote_node, market_data_node, quant_scorecard_node,
                 fan_out_node, stock_worker, guardrail_node, synthesizer_node):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "router_node")
    graph.add_conditional_edges("router_node", lambda s: s["intent_type"], {
        "fast_lookup": "fast_quote_node", "deep_diagnosis": "market_data_node",
        "simulation": "market_data_node"})
    graph.add_edge("fast_quote_node", "synthesizer_node")
    graph.add_edge("market_data_node", "quant_scorecard_node")
    graph.add_edge("quant_scorecard_node", "fan_out_node")
    graph.add_edge("stock_worker", "guardrail_node")
    graph.add_edge("guardrail_node", "synthesizer_node")
    graph.add_edge("synthesizer_node", END)
    return graph.compile()
