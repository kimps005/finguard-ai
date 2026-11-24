"""Request-scoped LangGraph portfolio diagnosis."""

import asyncio
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import operator
import re
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from backend import llm, stock_analysis
from backend.broker import Balance
from backend.finance_calc import calculate_scorecard, simulate_purchase
from backend.portfolio_market import MarketData, ScoreInputs
from backend.report import account_view, diagnosis_report, quote_parts


class Query(TypedDict):
    user_query: str


class PortfolioState(Query, total=False):
    intent_type: Literal["fast_lookup", "deep_diagnosis", "simulation"]
    account_summary: dict
    holdings: list[dict]
    quant_scorecards: dict[str, dict]
    macro_context: dict | None
    stock_analysis_results: Annotated[list[dict], operator.add]
    verified_stock_results: list[dict]
    guardrail_warnings: list[str]
    simulation_result: dict | None
    final_report: str
    balance: Balance
    as_of: date
    score_inputs: dict[str, ScoreInputs]


class Intent(BaseModel):
    intent_type: Literal["fast_lookup", "deep_diagnosis", "simulation"]


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
    warnings = []
    try:
        # Preserve the fast path's first-token budget if the cloud classifier is slow.
        async with asyncio.timeout(0.25 if intent == "fast_lookup" else None):
            question = re.sub(r"\d[\d,./ -]*", "○", state["user_query"])
            response = await llm.invoke("router", "사용자 요청을 분류하라. 단순 조회는 fast_lookup, "
                                        "가정 매수는 simulation, 그 외 분석은 deep_diagnosis다.", question, Intent)
            intent = response.intent_type
    except (llm.LLMUnavailable, TimeoutError):
        warnings.append("라우터 LLM 응답을 받지 못해 로컬 규칙으로 요청을 분류했습니다.")
    return {"intent_type": intent, "verified_stock_results": [], "guardrail_warnings": warnings,
            "quant_scorecards": {}, "macro_context": None, "simulation_result": None}


def route_to_stock_workers(state: PortfolioState) -> str | list[Send]:
    stocks = [s for s in state["holdings"] if s["asset_type"] == "STOCK" and s["corp_code"]]
    if not stocks:
        return "synthesizer_node"
    top = sorted(stocks, key=lambda s: (-s["weight"], s["stock_code"]))[:3]
    return [Send("stock_worker", {
        "stock": stock, "query": state["user_query"],
        "corp_name": state["score_inputs"][stock["stock_code"]].stock.corp_name,
        "scorecard": state["quant_scorecards"][stock["stock_code"]],
        "events": state["score_inputs"][stock["stock_code"]].events or [],
    }) for stock in top]


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
        balance, on, macro, inputs = await market.collect()
        view = account_view(balance)
        for row in view["holdings"]:
            entry = inputs.get(row["stock_code"])
            row.update(asset_type=entry.stock.asset_type if entry else None,
                       corp_code=entry.stock.corp_code if entry else None)
        return {**view, "balance": balance, "as_of": on, "macro_context": macro, "score_inputs": inputs}

    async def quant_scorecard_node(state):
        cards = {}
        for holding in state["balance"].holdings:
            inputs = state["score_inputs"].get(holding.stock_code)
            if inputs is None:
                cards[holding.stock_code] = {"status": "unmapped"}
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
        result = await stock_analysis.analyze_stock(job, market.external)
        return {"stock_analysis_results": [result]}

    async def guardrail_node(state):
        verified = await asyncio.to_thread(stock_analysis.verify_results, state["stock_analysis_results"])
        verified["guardrail_warnings"] = state["guardrail_warnings"] + verified["guardrail_warnings"]
        return verified

    async def synthesizer_node(state):
        write = get_stream_writer()
        warnings = list(state["guardrail_warnings"])
        if state["intent_type"] == "fast_lookup":
            parts = list(quote_parts(state))
        else:
            summary, disclosures = "", []
            if state["holdings"]:
                try:
                    summary, disclosures, notes = await stock_analysis.synthesize(state)
                    warnings.extend(notes)
                except llm.LLMUnavailable as exc:
                    warnings.append(f"종합 LLM을 사용할 수 없어 로컬 정량 리포트를 제공합니다. [{exc.reason}]")
            parts = [diagnosis_report(state, summary, disclosures)]
        for part in parts:
            write({"text": part})
        return {"final_report": "".join(parts), "guardrail_warnings": warnings}

    # Only the query crosses the request boundary. No checkpointer retains reducer channels.
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
