"""Request-scoped LangGraph portfolio diagnosis."""

import asyncio
from dataclasses import asdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
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


def quote_parts(state):
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


def won(amount):
    return "자료 없음" if amount is None else f"{amount.to_integral_value(rounding=ROUND_HALF_UP):,}원"


def disclosure_link(alias, title, receipt):
    return f"[{alias}](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}) · {' '.join(title.split())}"


def diagnosis_report(state, summary="", disclosures=()):
    query = state["user_query"]
    simulation = state["simulation_result"] or {}
    holdings = state["holdings"]
    selected = [h for h in holdings if h["stock_code"] == simulation.get("stock_code")
                or h["stock_code"] in query or (h["name"] and h["name"] in query)]
    selected = selected or sorted(holdings, key=lambda h: -h["weight"])[:3]
    answer, evidence = [], []
    if "message" in simulation:
        answer.append(simulation["message"])
    elif simulation:
        average, weight = simulation["average_price"], simulation["weight"]
        simulated_name = next(h["name"] for h in holdings if h["stock_code"] == simulation["stock_code"])
        answer.append(f"**{simulated_name}** 현재가에 {simulation['buy_quantity']:,}주 체결을 가정하면 예상 평단가는 "
                      f"{won(average)}이고, 남는 주문 가능 현금은 {won(simulation['remaining_cash'])}입니다."
                      + (f" 예상 비중은 {weight:.2%}입니다." if weight is not None else ""))
        evidence.append(f"매수 시뮬레이션 · 투자 금액 {won(simulation['invested_amount'])}, "
                        f"매수 수량 {simulation['buy_quantity']:,}주, 예상 평단가 {won(average)}")

    rows = {row["stock_code"]: row for row in state["stock_analysis_results"]}
    local_aliases = {}
    for h in selected:
        code, name = h["stock_code"], h["name"]
        price, average = won(h["current_price"]), won(h["average_price"])
        card = state["quant_scorecards"].get(code, {})
        inputs = state["score_inputs"].get(code)
        prices = f"{name} · 현재가 {price}, 평단가 {average}"
        has_ma20 = inputs is not None and len(inputs.bars) >= 20
        if has_ma20:
            ma20 = sum((bar.close for bar in inputs.bars[-20:]), Decimal(0)) / 20
            current = h["current_price"]
            relation = "위" if current > ma20 else "아래" if current < ma20 else "수준"
            scenario = (f"**{name}** 현재가 {price}는 20일선({won(ma20)}) {relation}입니다. "
                        "20일선 위에서 순매수가 이어지면 반등 여지가 있고, "
                        "20일선 아래에서 순매도가 이어지면 추가 하락 위험이 커집니다.")
            prices += f", 20일선 {won(ma20)}"
        else:
            scenario = f"**{name}** 현재가 {price}이며 20일선 자료가 부족해 추세를 판단하기 어렵습니다."
        scenario += f" 평단가는 {average}입니다."
        if "손절" in query and has_ma20:
            scenario += " 20일선 아래에서 순매도가 지속되는 구간은 하방 위험을 재평가할 기준입니다."
        if re.search(r"목표|매도|익절", query):
            scenario += " 평단가 회복 여부는 손익을 비교할 기준이며, 목표 매도가 범위를 산정할 자료는 부족합니다."
        if re.search(r"물타|추매|추가\s*매수", query):
            scenario += " 현재가가 평단가보다 낮을 때 추가 체결은 평단가를 낮추고 보유 수량을 늘립니다."
        evidence.append(prices)
        if inputs:
            flow = inputs.flow
            scenario += (f" 최근 5거래일 순매수는 외국인 {flow.foreign_net_buy:,}주, "
                         f"기관 {flow.institution_net_buy:,}주입니다.")
            evidence.append(f"{name} · 최근 5거래일 순매수 외국인 {flow.foreign_net_buy:,}주, "
                            f"기관 {flow.institution_net_buy:,}주")
        dilution, total = card.get("dilution_pct"), card.get("total")
        if dilution is not None:
            scenario += f" 오버행 희석률은 {dilution:f}%입니다."
            evidence.append(f"{name} · 오버행 희석률 {dilution:f}%")
            if dilution > 0:
                scenario += " 해당 물량이 실제로 출회되면 매물 부담이 커질 수 있습니다."
            if dilution > 0 and not summary and inputs:
                on = state["as_of"]
                receipts = {event["rcept_no"] for event in inputs.events or []
                            if event["convertible_shares"] > 0
                            and event["corp_code"] == inputs.stock.corp_code
                            and event["board_resolution_date"] <= on
                            and (event.get("exercise_end_date") or date.max) >= on}
                row = rows.get(code, {})
                titles = {doc["source"]: doc["title"] for doc in row.get("disclosures", [])}
                for alias, receipt in row.get("doc_map", {}).items():
                    if receipt in receipts:
                        report_alias = local_aliases.setdefault(receipt, f"DOC-{len(local_aliases) + 1}")
                        scenario += f" 오버행 산정에는 [{report_alias}]의 전환가능 물량이 반영됐습니다."
                        evidence.append(f"{name} · " + disclosure_link(
                            report_alias, titles.get(alias, "오버행 공시"), receipt))
        if total is not None:
            scenario += f" 수급·모멘텀·실적·오버행을 합산한 점수는 {total}/100입니다."
            scores = card["scores"]
            evidence.append(f"{name} · 수급 {scores['flow']}, 모멘텀 {scores['momentum']}, "
                            f"실적 {scores['earnings']}, 오버행 {scores['overhang']}점 (합계 {total}/100)")
        if simulation.get("stock_code") == code:
            answer[0] += " " + scenario
        else:
            answer.append(scenario)

    if summary:
        answer.append(summary)
    evidence.extend(disclosure_link(alias, title, receipt) for alias, title, receipt in disclosures)
    core = "\n\n".join(answer) or "보유 종목이 없어 종목별 위험과 가격 시나리오를 산정할 수 없습니다."
    grounds = "\n".join(f"- {item}" for item in evidence) or "- 보유 종목 없음"
    return f"{stock_analysis.ANSWER_HEADING}\n\n{core}\n\n{stock_analysis.EVIDENCE_HEADING}\n\n{grounds}"


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
