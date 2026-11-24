"""Deterministic report text and account views built only from local, verified data.

These outputs must stay correct when every LLM is down: the synthesizer streams
diagnosis_report() with or without a cloud summary.
"""

from dataclasses import asdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re

from backend.citations import AliasBook
from backend.stock_analysis import ANSWER_HEADING, EVIDENCE_HEADING


def account_view(balance):
    return {"account_summary": {"total_value": balance.total_value,
                                "cash_deposit": balance.cash_deposit,
                                "cash_weight": balance.cash_weight},
            "holdings": [asdict(h) for h in balance.holdings]}


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
    # Report links get their own namespace, numbered in the order the report cites them.
    report_book = AliasBook()
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
                        report_alias = report_book.alias(receipt)
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
    return f"{ANSWER_HEADING}\n\n{core}\n\n{EVIDENCE_HEADING}\n\n{grounds}"
