# \[최종 동결 마스터 PRD v3.7\] FinGuard AI: 개인 계좌 및 다중 금융 데이터 기반 멀티 에이전트 포트폴리오 진단 플랫폼

## 1. 프로젝트 개요 (Overview)

* **제품명:** FinGuard AI

* **타겟 시장 및 범위:** 국내 주식 포트폴리오 전용 (Domestic Stock Market Only — 일반 상장주식, 우선주 및 ETF/ETN 혼합 계좌 지원)

* **제품 형태:** Single-Tenant(단일 사용자) 전용 초개인화 AI 금융 리스크 진단 플랫폼 (로그인/회원가입 배제, Next.js PIN 프록시 및 `.env` 기반 보안 관리)

* **한 줄 정의:** 사용자의 실제 국내 주식 계좌 데이터를 중심축으로, 전자공시(DART)·의무보호예수·실시간 뉴스·거시경제 지표를 병렬 에이전트로 교차 검증하여 초개인화된 리스크 진단과 대응 시나리오를 제공하는 AI 금융 비서 플랫폼.

* **핵심 엔지니어링 원칙 (8대 수칙):**

  1. **Read-Only & 안전성:** LLM의 주문 오작동 리스크를 원천 차단하기 위해 매매 실행 API는 배제하고 조회, 분석, 시뮬레이션에 집중.

  2. **Router 최상단 배치 & Fast Path 경량화 (**$\text{TTFT} \le 1.0\text{s}$ **달성):** 최상단 Router 분기 후 `fast_lookup`은 DART/ECOS/RAG/ETL을 우회하고 전용 경량 시세·잔고 노드(`Fast Quote Node`)만 거쳐 즉시 스트리밍.

  3. **Single-Tenant 프로세스 & GPU 메모리 격리 (`workers = 1`):** Uvicorn 워커 1개 동결로 VRAM 모델 중복 적재(OOM)를 원천 차단하며, CPU 바운드 연산(`kiwipiepy` 등)은 `asyncio.to_thread()`로 비동기 처리.

  4. **통합 GPU 동시성 제어 (`gpu_semaphore`) & Blackwell/FP16 직접 로드:** CUDA 12.8+ 환경에서 `sentence-transformers`/`CrossEncoder` FP16(`torch.float16`)으로 `BAAI/bge-m3` 임베딩과 `BAAI/bge-reranker-v2-m3` 리랭킹 모델을 직접 로드하고, 공유 `gpu_semaphore = asyncio.Semaphore(1)`을 적용하여 RTX 5060 8GB VRAM 환경의 CUDA OOM(VRAM 2.2GB 유지) 및 커널 호환성 에러를 원천 차단.

  5. **정형 vs 비정형 데이터 처리 완벽 분리 (Zero-Hallucination):**

     * **정형 데이터** (재무제표, PER/PBR, 이격도, 수급, 평단가, 100점 스코어카드): DART 정형 API(XBRL `account_id` 기반), 증권사 API, DB 캐싱된 CB/BW 희석률(`overhang_events`), Python `Decimal` 연산 Engine으로 100% 처리.

     * **비정형 데이터** (사업 내용, CB/유증 특약, 뉴스): DART 표 Markdown 변환 & 헤더 보존 청킹, PostgreSQL 15+ `kiwipiepy` 형태소 토큰화 `TSVECTOR` GIN 인덱스(`ts_rank` 정렬) + `pgvector` 코사인 Exact Scan(`corp_code` 인덱스 태핑) 및 로컬 Reranker 사용.

  6. **종목 단위 통합 워커 Fan-Out & 워커 단위 `doc_map` 캡슐화:** 비중 상위 Top-N 종목별 통합 워커로 LLM 호출을 단축하고, 각 워커의 반환 객체 내부에 `"doc_map": {"DOC-1": "rcept_no", ...}`를 캡슐화하며, Guardrail 검증 결과는 리듀서가 없는 별도 상태 필드(`verified_stock_results`)로 분리하여 병렬 키 충돌 및 상태 중복 누적을 구조적으로 방지. (Fan-Out 대상 종목 0개 시 `"synthesizer_node"` 문자열 엣지 반환으로 LangGraph 조기 종료 방지)

  7. **`DOC-N` 인용 별칭 기반 Guardrail & 형태소 문장 단위 Drop Fallback:** LLM 컨텍스트 주입 시 14자리 공시번호 대신 `DOC-1`, `DOC-2` 별칭을 부여하고, `kiwi.split_into_sents()`로 문장을 분리한 뒤 매핑 실패 인용 문장만 정밀 Drop 처리 후 경고 플래그 전달.

  8. **금융 데이터 엣지케이스 완전 정복:** 우선주 `crno` 기반 보통주 `corp_code` 전파 매핑, 동일 회계기준(`fs_div`) 쌍 기반 DART 4분기 역산, ETF/ETN 비재무 종목 파이프라인 자동 우회, 부동소수점 기울기 허용 오차, 장중(15:40 KST 이전) 5거래일 확정 수급 적용, 금융업종 부채/영업이익 예외 분기, DART `status: 013` Fallback 및 `UNIQUE NULLS NOT DISTINCT` 복합키 Upsert 적용.

## 2. 타겟 유저 및 핵심 유스케이스 (User Stories)

* **타겟 유저:** 국내 주식(보통주, 우선주 및 ETF/ETN 혼합)을 보유하고 있으나, 보유 종목의 공시, 보호예수 해제 일정, 환율·금리 변동 영향, 실시간 악재 뉴스를 종합적으로 추적하기 어려운 개인 투자자.

* **핵심 유스케이스:**

  1. **단순 조회 (Fast Path):** *"내 잔고랑 현재가 얼마야?"* 요청 시 Router가 `fast_lookup`으로 판별, DART/RAG/ETL을 우회하고 `Fast Quote Node`에서 증권사 잔고·현재가만 단일 조회한 뒤 1.0초 이내 스트리밍 시작.

  2. **원클릭 종합 리스크 스캔 (Warm/Cold Path):** *"내 계좌 지금 위험한 거 없어?"* 요청 시 Router가 `deep_diagnosis`로 판별, 보유 종목 Top-N 통합 워커가 공시 악재, 미상환 CB 희석률, 뉴스 이상 징후를 비중 순으로 브리핑 (우선주는 보통주 공시 추적, ETF/ETN은 공시·재무 단계를 우회하여 수급·수익률 브리핑).

  3. **이벤트·공시 맞춤형 영향도 정밀 진단:** *"보유 중인 A 종목 최근 전환사채 발행이랑 보호예수 해제가 내 평단가 기준으로 얼마나 위험해?"* 요청 시 원문 조항과 `overhang_events` 캐싱 데이터를 결합하여 대응 전략 제시.

  4. **매크로-기업 펀더멘털 연결 진단:** *"최근 원/달러 환율 급등이 내 포트폴리오에 유리해, 불리해?"* 요청 시 ECOS 환율 추이(Node 2 수집 `macro_context`)와 DART 정기보고서 내 외화부채/수출 비중을 대조해 유불리 분석.

  5. **예수금 기반 물타기/리밸런싱 정밀 시뮬레이션:** *"주문 가능 현금의 40%로 손실 중인 B 종목을 추매하면 평단가와 비중이 어떻게 변하고, 물타기 적합도는 어때?"* 요청 시 정확한 수치 계산과 함께 100점 만점 차등 정량 스코어카드를 결합하여 실행 가치 평가.

## 3. 시스템 아키텍처 및 기술 스택 (Tech Stack)

### 3.1 프론트엔드, 백엔드 및 보안 인프라

* **Frontend:** React / Next.js 14 (App Router, TypeScript, Tailwind CSS, Lucide Icons)

  * 좌측 계좌/예수금 요약 카드 + 우측 에이전트 실시간 진행 타임라인 트래커 & 마크다운 리포트 스트리밍.

  * **보안 & 프록시:** Next.js Route Handler(`app/api/chat/route.ts`)에서 단일 `APP_ACCESS_PIN` 헤더 검증 후 Docker 내부 네트워크의 FastAPI SSE 스트림을 프록시.

* **Backend:** Python 3.11, FastAPI (Docker 내부 노출 전용 `expose: 8000`, `workers = 1` 지정)

* **Agent Orchestration:** LangGraph (`StateGraph`, 최상단 Router 분기, `Fast Quote Node` 경량 분기, Top-N 종목별 통합 Fan-Out `Send` API)

* **Database & Cache:**

  * **PostgreSQL 15+ & `pgvector`:** `stock_master` 통합 매핑 테이블, `overhang_events`(미상환 CB/BW 희석률 캐싱), DART 공시 청크 저장. `kiwipiepy` 형태소 토큰화 기반 `TSVECTOR` GIN 키워드 인덱스(`search_tsv`) + `corp_code` B-Tree 인덱스 기반 Exact Cosine Scan.

  * **Redis:** 컨테이너 재시작 시에도 증권사 OAuth 토큰, ECOS 환율/금리 데이터(24시간 TTL) 및 공공 API 캐시 유지, `etl_lock:{corp_code}` (`SETNX`, TTL 10분) 중복 백그라운드 인제스트 방지.

### 3.2 종목 식별자 통합 매핑 & 오버행 DB 스키마

```sql
CREATE TABLE stock_master (
    stock_code VARCHAR(10) PRIMARY KEY,               -- 단축코드 (예: 005930, 005935, 069500)
    corp_code VARCHAR(10),                            -- DART 고유번호 (우선주는 보통주 corp_code 전파 매핑, ETF/ETN은 NULL)
    isin_cd VARCHAR(12) NOT NULL,                     -- 표준코드 (예: KR7005930003)
    crno VARCHAR(13),                                 -- 법인등록번호 (우선주 매핑 키)
    corp_name VARCHAR(100) NOT NULL,                  -- 종목/법인명
    asset_type VARCHAR(10) NOT NULL DEFAULT 'STOCK',  -- 'STOCK' | 'ETF' | 'ETN' | 'REIT'
    induty_code VARCHAR(10),                          -- 업종코드 (금융업 예외 처리용)
    listed_shares BIGINT NOT NULL,                    -- 총 상장주식수 (오버행 희석률 계산용)
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_stock_master_corp ON stock_master(corp_code) WHERE corp_code IS NOT NULL;
CREATE INDEX idx_stock_master_isin ON stock_master(isin_cd);
CREATE INDEX idx_stock_master_crno ON stock_master(crno);

CREATE TABLE overhang_events (
    id SERIAL PRIMARY KEY,
    corp_code VARCHAR(10) NOT NULL,
    bond_type_issue_no VARCHAR(50),                   -- 사모 CB 등 회차 미기재 시 NULL 허용
    board_resolution_date DATE NOT NULL,
    event_type VARCHAR(20) NOT NULL,                  -- 'CB' | 'BW' | 'EB'
    convertible_shares BIGINT NOT NULL,               -- 전환 가능 주식수
    conversion_price NUMERIC(18, 2),
    exercise_start_date DATE,
    exercise_end_date DATE,
    rcept_no VARCHAR(14) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- PostgreSQL 15+ NULLS NOT DISTINCT: 회차가 NULL인 사모 공시 기재정정 시에도 중복 적재 방지
    CONSTRAINT uq_overhang_event UNIQUE NULLS NOT DISTINCT (corp_code, bond_type_issue_no, board_resolution_date)
);
```

* **자동 동기화 및 우선주 매핑 배치:**

  1. DART `corpCode.xml`과 공공데이터포털 `금융위원회_KRX상장종목정보` API를 결합하여 `stock_master`를 자동 Upsert.

  2. `asset_type='STOCK'`이나 `corp_code`가 빈 우선주 종목은 동일한 법인등록번호(`crno`)를 가진 보통주의 `corp_code`를 탐색하여 자동으로 상속 적재. (`corp_code`가 아예 없는 ETF/ETN은 `asset_type='ETF'`/`'ETN'`으로 관리)

### 3.3 로컬 GPU 최적화 (RTX 5060 8GB VRAM / Blackwell 방어)

* **런타임 환경:** RTX 5060(`sm_120`, Blackwell 아키텍처) 커널 호환을 위해 Docker 베이스 이미지 및 PyTorch 환경을 **CUDA 12.8 이상**으로 고정.

* **PyTorch FP16 직접 로딩:**

  * `BAAI/bge-m3` 임베딩: `SentenceTransformer("BAAI/bge-m3", model_kwargs={"torch_dtype": torch.float16})`로 로드 (\~1.1GB VRAM).

  * `BAAI/bge-reranker-v2-m3` 리랭커: `CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=1024, model_kwargs={"torch_dtype": torch.float16})`로 직접 로드 (\~1.1GB VRAM).

  * 라이브러리 지원 제약이 있는 외부 래퍼(`fastembed`)를 제거하고 PyTorch FP16으로 상주 VRAM을 **총 2.2GB**로 안정화.

* **GPU 통합 세마포어:** 임베딩 및 리랭킹 연산부 전체를 `gpu_semaphore = asyncio.Semaphore(1)`로 단일 제어하여 CUDA 메모리 충돌을 차단하고, 리랭킹 후보 청크들은 고정 미니배치(`batch_size=8`, `max_length=1024`)로 분할 추론하여 어텐션 메모리 스파이크 및 OOM 원천 차단.

## 4. 외부 API 연동 & DART 파싱 실무 방어 전략

1. **증권사 Open API 이원화 & Rate Limiting 방어:**

   * 계좌/잔고 조회만 `.env` 내 `IS_VIRTUAL=true/false` 분기를 태우고, 시세·일봉·수급 데이터는 항상 실전 도메인(또는 PyKRX fallback)에서 조회.

   * `asyncio.gather()` 호출 시 `asyncio.Semaphore(2)` + 지수 백오프(Exponential Backoff) 적용.

2. **DART 공시 파싱 지뢰 방어 & `overhang_events` 복합키 Upsert:**

   * **기재정정 복합키 식별:** `cvbdIsDecsn.json` 등 정형 API 파싱 결과를 `overhang_events` 테이블에 적재할 때 `UNIQUE NULLS NOT DISTINCT (corp_code, bond_type_issue_no, board_resolution_date)` 기준으로 `ON CONFLICT DO UPDATE` 실행.

   * **재무제표 4분기 역산 (`fs_div` 쌍 일치 & XBRL `account_id` 우선 매핑):**

     * 동일 회계연도의 연간 사업보고서와 3분기 보고서는 반드시 **동일한 재무제표 구분(`fs_div`) 쌍**(둘 다 `CFS`가 존재할 때만 `CFS`, 하나라도 없으면 둘 다 `OFS`)으로 묶어 차감하여 `연간(CFS) - 3분기(OFS)` 오차를 원천 차단.

     * 계정 매핑 시 기업마다 상이한 `account_nm` 문자열 대신 표준 XBRL `account_id`(`ifrs-full_ProfitLossFromOperatingActivities`, `dart_OperatingIncomeLoss`)를 1순위로 매칭하고, 한글명 정규식은 2순위 Fallback으로 적용.

     * 전년도 사업보고서 미제출 기간(1\~3월)에 `status: 013` 반환 시 전전년도 연간 보고서 또는 영업잠정실적 공시로 Fallback.

   * **Markdown 변환 및 헤더 보존 청킹:** 모든 `<table>`은 Markdown 표로 변환 후 표 단위 원자적 청킹 유지. 표 분할 발생 시 헤더 행(`| 항목 | 금액 | ... |`)을 모든 분할 청크 상단에 자동 복제.

3. **`pgvector` Exact Scan + `TSVECTOR` 하이브리드 랭킹 검색:**

   * 전역 HNSW 인덱스 사후 필터링 대신, `corp_code` B-Tree 인덱스를 타고 해당 기업 청크 범위(수백\~수천 건) 내에서 Exact Cosine Scan을 수행하여 검색 누락률 0.0% 달성.

   * 키워드 검색은 `kiwipiepy` 추출 형태소 문자열을 `TSVECTOR` 컬럼(`search_tsv`)에 저장하고 GIN 인덱스 + `ts_rank`로 관련도 순위 정렬.

4. **금융위 의무보호예수 API (`isin_cd`) Fallback & 비주식 자산 우회:**

   * `stock_master.asset_type != 'STOCK'`(ETF/ETN)인 보유 종목은 DART 공시·의무보호예수·재무 스코어카드 연산을 자동 우회(Bypass)하고 가격·수익률·수급 요약만 반환.

   * 일반 주식 중 우선주/권리변동 종목의 `isin_cd` 조회 실패 시 DART 증자결정 및 증권발행실적보고서 비정형 RAG 검색으로 자동 Fallback 후 `stock_master.listed_shares` 분모로 희석률(%) 산출.

## 5. LangGraph 에이전트 워크플로우 및 상태 설계

### 5.1 전체 실행 흐름 (Graph Architecture)

```
                     [User Query] (Next.js PIN 검증 완료)
                          │
                          ▼
                 [1. Router Node]
                  │              │
     (fast_lookup)│              │(deep_diagnosis / simulation)
                  ▼              ▼
       [1-B. Fast Quote Node]   [2. Market Data & Background ETL Node]
       (경량 잔고/현재가 단일 조회) (증권사 시세/잔고, ECOS 환율/금리, DART ETL, overhang 적재)
                  │              │
                  │              ▼
                  │     [3. Quant Scorecard & Context Node]
                  │     (ETF 우회 분기 + FSC 보호예수/overhang 기반 100점 차등 채점 확정)
                  │              │
                  │              ▼
                  │     [4. Integrated Top-N Stock Fan-Out (Send API)]
                  │     ├── Stock Worker 1 (DOC-N 청크 + 오버행 + 뉴스 통합 분석, doc_map 캡슐화)
                  │     ├── Stock Worker 2 (DOC-N 청크 + 오버행 + 뉴스 통합 분석, doc_map 캡슐화)
                  │     └── (대상 0개 시 "synthesizer_node" 엣지 우회 반환)
                  │              │
                  │              ▼
                  │     [5. Structured Guardrail Node]
                  │     (kiwi.split_into_sents() 문장 분리 → doc_map 역매핑 → 실패 문장 Drop 후 verified_stock_results 기록)
                  │              │
                  └──────────────┴────────┐
                                          ▼
                               [6. Fast Quote / Synthesizer Node]
                               (Next.js로 SSE 스트리밍 출력)
```

### 5.2 Fan-Out 조기 종료 방지 조건부 라우팅

```python
def route_to_stock_workers(state: PortfolioState):
    target_stocks = [h for h in state["holdings"] if h.get("asset_type") == "STOCK" and h.get("corp_code")]
    if not target_stocks:
        # Fan-Out 대상이 0개일 경우 빈 리스트 []를 반환하면 LangGraph가 즉시 END로 종료되는 버그 방지
        return "synthesizer_node"
    
    top_n = sorted(target_stocks, key=lambda x: x["weight"], reverse=True)[:3]
    return [Send("stock_worker_node", {"stock": stock}) for stock in top_n]
```

### 5.3 LangGraph State 스키마 정의

```python
from typing import Annotated, TypedDict, List, Dict, Optional
import operator

class PortfolioState(TypedDict):
    user_query: str
    intent_type: str                                              # "fast_lookup" | "deep_diagnosis" | "simulation"
    account_summary: Dict                                         # 총 평가액, 예수금, 현금 비중 (Decimal 연산)
    holdings: List[Dict]                                          # 보유 종목 리스트 (asset_type, 비중, 평단가, 손익률, PER/PBR)
    quant_scorecards: Dict[str, Dict]                             # 종목별 100점 만점 차등 정량 스코어카드 (ETF는 요약 객체)
    macro_context: Optional[Dict]                                 # ECOS 환율/금리 수집 데이터 (Node 2 주입)
    stock_analysis_results: Annotated[List[Dict], operator.add]   # Fan-Out 워커 원본 병렬 수집 버퍼
    verified_stock_results: List[Dict]                            # Guardrail 검증 완료 결과 (리듀서 없음 - 중복 누적 방지)
    guardrail_warnings: List[str]                                 # 검증 실패로 Drop된 인용 경고 메시지
    simulation_result: Optional[Dict]                             # 물타기/리밸런싱 Python 정량 연산 결과
    final_report: str                                             # 최종 종합 진단 리포트
```

### 5.4 100점 정량 스코어카드 엣지케이스 방어 알고리즘 (Node 3 계산)

* **비주식(`asset_type != 'STOCK'`) 사전 분기:** ETF/ETN 종목은 100점 스코어카드 채점 대상에서 제외하고, 현재가·평단가·손익률·최근 5일 수급 요약 메타데이터만 `quant_scorecards[stock_code]`에 적재.

1. **실적 개선도 (25점):**

   * 동일 `fs_div` 쌍(`CFS-CFS` 우선, 불가 시 `OFS-OFS`) 및 XBRL `account_id` 기준 4분기 역산 공식 적용: $Q_4 = \text{사업보고서(연간)} - \text{3분기 누적}$.

   * **금융업종 예외:** `induty_code` 기반 금융업 판단 시 표준 영업이익 대신 영업수익/당기순이익 계정 태그 사용 (누락 시 중립 15점 처리).

   * 영업이익 YoY/QoQ 흑자 확대: 25점 | 흑자 전환: 20점 | 적자 폭 축소: 10점 | 적자 지속/악화: 0점

2. **오버행 안전성 (25점):** `(FSC 보호예수 API + overhang_events DB 희석률 합산 / stock_master.listed_shares)`

   * 향후 30일 내 물량 0%: 25점 | 3% 미만: 15점 | 3%\~5% 미만: 5점 | 5% 이상: 0점

3. **수급 전환도 (25점):**

   * **장중 처리 규칙:** 15:40 KST 이전 요청 시 당일 미확정 수급을 제외하고 전일 기준 과거 5거래일 확정 데이터 산정.

   * 최근 5일 외인+기관 쌍끌이 순매수: 25점 | 둘 중 1개 주체 순매수: 15점 | 순매도세 둔화: 5점 | 순매도 지속: 0점

4. **단기 모멘텀 강도 (25점):** (20일 이동평균선 기준)

   * **부동소수점 오차 허용:** 평행 기울기는 $\lvert\text{일평균 변화율}\rvert \le 0.05\%$ 구간으로 판별.

   * 20일선 상회 & 기울기 상승: 25점 | 20일선 상회 & 기울기 평행: 15점 | 20일선 이탈(단기 과매도): 10점 | 20일선 하향 돌파 지속: 0점

## 6. 비기능 요구사항 및 검증 지표 (NFR & Evaluation)

1. **지연 시간 (Latency) 분리 수치:**

   * **TTFE (Time to First Event - UI 타임라인 시작):** $\le 0.5\text{초}$.

   * **Fast Path TTFT (단순 잔고/시세 첫 토큰):** $\le 1.0\text{초}$ (`Fast Quote Node` 단일 조회 후 스트리밍).

   * **Deep Analysis Total Duration (전체 리포트 완료 기준):**

     * **Warm Cache (공시 DB 기적재 종목):** $\le 12.0\text{초}$ (Top-3 종목 통합 워커 기준).

     * **Cold Cache (최초 동기 공시 인제스트 포함 종목):** $\le 25.0\text{초}$.

2. **RAGAS 정량 평가 데이터셋 구성 전략 (Module 8):**

   * 5대 핵심 공시 이벤트 유형별 4개 종목씩 균등 샘플링 (총 20건 벤치마크):

     1. 전환사채(CB)/신주인수권부사채(BW) 발행 (4건)

     2. 유상증자 결정 (4건)

     3. 최대주주 변경 (4건)

     4. 단일판매·공급계약 체결 (4건)

     5. 영업잠정실적 공시 (4건)

   * **목표 지표:** Faithfulness $\ge 0.90$, Context Precision $\ge 0.85$, 정형 수치 오차율 $0.0\%$.

## 7. 기능 단위 개발 로드맵 (8개 독립 모듈)

개발 시 단일 모듈 완성 후 `Git Commit` $\rightarrow$ `/clear` 실행 원칙을 준수하여 컨텍스트 오염을 방지한다.

| 모듈 | 기능명 | 핵심 구현 범위 및 산출물 | 완료 기준 및 검증 방법 | 
| ----- | ----- | ----- | ----- | 
| **Module 1** | **인프라 & DB 스토리지** | `docker-compose.yml` (CUDA 12.8+ & PostgreSQL 15+ `pgvector`), `stock_master`(`crno` 매핑 기반 우선주 `corp_code` 전파 적재), `overhang_events`(`UNIQUE NULLS NOT DISTINCT`) 마이그레이션 배치 | `docker compose up` 후 스키마 생성 및 보통주/우선주/ETF 혼합 `stock_master` 자동 Upsert 확인 $\rightarrow$ Commit & `/clear` | 
| **Module 2** | **증권사 REST API 커넥터** | 증권사 OAuth Redis 싱글톤, 잔고/시세/수급 이원화 호출, `Semaphore(2)`, `Decimal` 캐스팅, 장중 수급 5일 계산 | 실전/모의 도메인 분기 및 수급 5일 확정 계산 확인 $\rightarrow$ Commit & `/clear` | 
| **Module 3** | **결정론적 금융 연산 Engine** | Trailing PER/PBR, 동일 `fs_div` 쌍 & XBRL `account_id` 기반 4분기 역산, ETF 우회 처리, 물타기 시뮬레이터, 100점 정량 스코어카드 Engine | `pytest tests/test_finance_calc.py` 100% 통과 (수치 오차 0%) $\rightarrow$ Commit & `/clear` | 
| **Module 4** | **DART ETL & `kiwipiepy` RAG** | `overhang_events` `NULLS NOT DISTINCT` Upsert, Markdown 표 청킹, `TSVECTOR` GIN `ts_rank` + `corp_code` Exact Scan, PyTorch FP16 `bge-m3`/`bge-reranker-v2-m3` `gpu_semaphore(1)` | `overhang_events` 기재정정 중복 방지 및 VRAM 2.2GB 이내 Exact Scan 검색 확인 $\rightarrow$ Commit & `/clear` | 
| **Module 5** | **외부 데이터 커넥터 3종** | 금융위 의무보호예수 API (`isin_cd` 및 Fallback), 네이버 뉴스 API, ECOS 환율/금리 API (`macro_context` 채우기) | 비동기 수집 및 Redis 캐싱 확인, 보호예수 Fallback 검증 $\rightarrow$ Commit & `/clear` | 
| **Module 6** | **LangGraph 멀티 에이전트** | `PortfolioState`(`verified_stock_results` 분리), `Fast Quote Node` 경량 분기, Top-N 통합 워커 Fan-Out (0개 시 우회 분기), `kiwi.split_into_sents()` Guardrail & Drop | Plan Mode로 구조 확정. Fast Path $< 1.0\text{s}$, empty Fan-Out 무사통과 및 `DOC-N` 문장 단위 Drop 검증 $\rightarrow$ Commit & `/clear` | 
| **Module 7** | **Next.js 14 대시보드 & 보안** | Next.js 14 App Router 대시보드 UI, Next.js PIN 프록시 Route Handler, SSE 스트리밍 연동 | PIN 검증, 무인증 통신 차단 및 브리핑 마크다운 스트리밍 확인 $\rightarrow$ Commit & `/clear` | 
| **Module 8** | **RAGAS 평가 & README** | 5대 이벤트별 20건 RAGAS 평가 스크립트 실행, Warm/Cold Latency 벤치마크 측정, `README.md` 작성 | `README.md` 내 Mermaid 다이어그램 및 RAGAS 정량 지표 수치 기록 | 

## 8. AI 에이전트 교대 개발 가이드라인 (Codex & Claude Code 협업 규칙)

본 프로젝트는 OpenAI Codex와 Anthropic Claude Code를 교대로 활용하여 개발하며, 컨텍스트 파편화 및 환경 꼬임을 방지하기 위해 다음 4대 교대 수칙을 철저히 준수한다.

### 8.1 에이전트 컨텍스트 주입 파일 작성 (`AGENTS.md` / `CLAUDE.md`)
프로젝트 루트 디렉토리에 Codex용 `AGENTS.md`와 Claude Code용 `CLAUDE.md`를 신규 생성하고, 아래의 동일한 내용을 작성하여 에이전트가 자동 참조하도록 설정한다.

```markdown
# FinGuard AI - Agent Engineering Rules

## 1. Absolute Context Directive
- Primary Specification: `docs/finguard_ai_prd_v3.7.md`
- Always read the PRD module section before implementing code. Never alter architectural constraints.

## 2. Python Environment & Execution Constraints
- Python Environment: ALWAYS use `.venv` executable (e.g., `.venv/Scripts/python.exe` or `.venv/bin/python`).
- Package Management: Always install dependencies inside `.venv`.
- Process Concurrency: Uvicorn MUST run with `workers=1` (Single-Tenant VRAM protection).

## 3. Mandatory Engineering Rules
- Model Loading: NEVER use `fastembed`. ALWAYS use `sentence-transformers` & `CrossEncoder` with `torch.float16` for `BAAI/bge-m3` and `BAAI/bge-reranker-v2-m3`.
- Financial Calculations: ALWAYS use Python `decimal.Decimal` (No `float` rounding errors).
- DB Schema: Respect `UNIQUE NULLS NOT DISTINCT` on `overhang_events` and `crno`-based `corp_code` inheritance for preferred stocks (`stock_master`).
- LangGraph Fan-Out: Fan-Out function MUST return `"synthesizer_node"` string if target stock list is empty.
```

### 8.2 모듈 단위 엄격 교대 수칙
1. **단위 작업 완결:** 교대는 반드시 PRD 7절의 **Module (1\~8) 단위 작업이 100% 완료된 시점**에만 수행한다. 모듈 중간 구현 중 교대하는 행위는 엄격히 금지한다.
2. **Commit 후 교대:** 모듈 완결 후 단위 테스트(`pytest`) 통과를 확인하고 `git add .` $\rightarrow$ `git commit -m "feat(module-N): ..."` 완료 상태에서만 에이전트를 변경한다.
3. **교대 직후 세션 초기화:** 새 에이전트를 시작할 때는 이전 세션의 잔여 컨텍스트 오염을 막기 위해 반드시 세션 초기화(`/clear` 또는 새 셸 기동)를 실행한다.

### 8.3 가상환경 실행 표준화
* 모든 명령어 실행 시 시스템 파이썬 대신 저장소 내 가상환경 경로를 명시한다:
  * Windows: `.venv\Scripts\python.exe -m pytest` 또는 `.venv\Scripts\pip.exe install ...`
  * Linux/macOS: `./.venv/bin/python -m pytest`

### 8.4 변경 사항 불변 규칙 (Immutability Rules)
* Codex 또는 Claude Code가 코드를 작성할 때 다음 핵심 아키텍처는 에이전트 판단으로 임의 수정할 수 없으며, 수정이 필요할 시 반드시 개발자와 논의한다:
  1. `fastembed` 도입 금지 (`sentence-transformers` FP16 고정)
  2. Uvicorn 멀티 워커(`workers > 1`) 변경 금지
  3. `quant_scorecards` 및 금융 금액 계산의 `float` 전환 금지 (`Decimal` 고정)
  4. LangGraph `verified_stock_results` 상태 필드의 리듀서(`operator.add`) 추가 금지
