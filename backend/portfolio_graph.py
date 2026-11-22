"""Request-scoped LangGraph portfolio diagnosis."""

import asyncio
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import operator
import re
from typing import Annotated, Literal, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from backend import stock_analysis
from backend.broker import Balance
from backend.finance_calc import calculate_scorecard, simulate_purchase
from backend.portfolio_market import MarketData, ScoreInputs


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


def router_node(state):
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


def account_view(balance):
    return {"account_summary": {"total_value": balance.total_value,
                                "cash_deposit": balance.cash_deposit,
                                "cash_weight": balance.cash_weight},
            "holdings": [asdict(h) for h in balance.holdings]}


def route_to_stock_workers(state: PortfolioState) -> str | list[Send]:
    stocks = [s for s in state["holdings"] if s["asset_type"] == "STOCK" and s["corp_code"]]
    if not stocks:
        return "synthesizer_node"
    top = sorted(stocks, key=lambda s: (-s["weight"], s["stock_code"]))[:3]
    return [Send("stock_worker", {
        "stock": stock, "query": state["user_query"], "macro": state["macro_context"],
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


def report_parts(state):
    summary = state["account_summary"]
    if summary["cash_deposit"] is None:
        yield f"국내 주식 평가액 {summary['total_value']:,}원 · 예수금 조회 불가\n"
    else:
        yield (f"총 평가액 {summary['total_value']:,}원 · 예수금 {summary['cash_deposit']:,}원 "
               f"({summary['cash_weight']:.2%})\n")
    for h in state["holdings"]:
        yield (f"\n- {h['name']} ({h['stock_code']}) · {h['quantity']:,}주 · "
               f"현재가 {h['current_price']:,}원 · 평단가 {h['average_price']:,}원 · "
               f"수익률 {h['profit_loss_pct']}% · 비중 {h['weight']:.2%}")
        card = state["quant_scorecards"].get(h["stock_code"], {})
        if "scores" in card:
            total = card["total"]
            yield f" · 정량 점수 {total}/100" if total is not None else " · 정량 점수 자료 부족"
            if (dilution := card["dilution_pct"]) is not None:
                yield f" · 오버행 희석률 {dilution:f}%"
        elif "five_day_flow" in card:
            flow = card["five_day_flow"]
            yield (f" · 5일 순매수 외국인 {flow['foreign_net_buy']:,}주, "
                   f"기관 {flow['institution_net_buy']:,}주")
    macro = state["macro_context"] or {}
    for key, label in (("usd_krw", "원/달러"), ("base_rate", "기준금리"),
                       ("treasury_3y", "국고채 3년"), ("treasury_10y", "국고채 10년")):
        if macro.get(key):
            obs = macro[key]
            yield f"\n- {label} {obs['value']} {obs['unit']} ({obs['date']})"
    if result := state["simulation_result"]:
        yield "\n\n### 매수 시뮬레이션\n"
        if "message" in result:
            yield result["message"]
        else:
            weight = result["weight"]
            average = result["average_price"]
            yield ("\n| 항목 | 시뮬레이션 결과 |\n| --- | ---: |\n"
                   f"| 종목 | {result['stock_code']} |\n"
                   f"| 매수 수량 | {result['buy_quantity']:,}주 |\n"
                   f"| 투자 금액 | {result['invested_amount']:,}원 |\n"
                   f"| 예상 평단가 | {f'{average:,.2f}원' if average is not None else '—'} |\n"
                   f"| 남는 주문 가능 현금 | {result['remaining_cash']:,}원 |\n")
            if weight is not None:
                yield f"| 예상 비중 | {weight:.2%} |\n"
    for row in state["verified_stock_results"]:
        if row["analysis_text"]:
            yield f"\n\n### {row['stock_code']}\n{row['analysis_text']}"
    if state["guardrail_warnings"]:
        yield "\n\n근거 또는 형식을 확인할 수 없는 분석 내용은 제외했습니다."


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
        result = await stock_analysis.analyze_stock(job, market.external, market.broker.http)
        return {"stock_analysis_results": [result]}

    async def guardrail_node(state):
        return await asyncio.to_thread(stock_analysis.verify_results, state["stock_analysis_results"])

    async def synthesizer_node(state):
        write = get_stream_writer()
        parts = []
        for part in report_parts(state):
            write({"text": part})
            parts.append(part)
        return {"final_report": "".join(parts)}

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
