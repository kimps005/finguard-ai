# FinGuard AI 에이전트 협업 규칙

## 절대 수칙

1. Windows의 모든 Python 실행 및 패키지 설치는 반드시 `.venv\Scripts\python.exe` 및 `.venv\Scripts\pip.exe`만 사용한다. Linux·macOS와 Docker에서는 대응하는 `.venv/bin/python`, `.venv/bin/pip`를 사용한다.
2. `fastembed` 설치 및 사용을 금지한다. `BAAI/bge-m3`와 `BAAI/bge-reranker-v2-m3`는 `sentence-transformers`의 `SentenceTransformer`, `CrossEncoder`에서 `torch.float16`으로 직접 로드한다.
3. 모든 금융·수량·비중 계산에는 `float` 대신 Python `decimal.Decimal`만 사용한다.
4. FastAPI Uvicorn은 `workers=1`로 실행한다. 임베딩과 리랭킹은 단일 공유 `gpu_semaphore = asyncio.Semaphore(1)`로 제어한다.
5. 단일 모듈 구현과 검증을 마친 뒤 먼저 `git commit`하고, 에이전트 교대 전에 `/clear` 또는 새 셸로 세션을 초기화한다.

## 명세와 변경 규칙

- 기본 명세는 `docs/finguard_ai_prd_v3.8.md`다. 구현 전에 해당 모듈의 요구사항과 완료 기준을 읽고 모듈 범위를 지킨다.
- `overhang_events`의 `UNIQUE NULLS NOT DISTINCT` 복합키와 `stock_master`의 `crno` 기반 우선주 `corp_code` 전파 규칙을 유지한다.
- LangGraph Fan-Out 대상이 비어 있으면 문자열 `"synthesizer_node"`를 반환한다. `verified_stock_results`에는 `operator.add` 리듀서를 추가하지 않는다.
- Node 1은 `ROUTER_MODEL=qwen3.7-plus`, Node 4는 `WORKER_MODEL=qwen3.8-flash`를 사용한다. DashScope의 429·403·할당량 오류는 즉시 Gemini로 전환한다.
- Node 6 및 RAGAS Judge는 `SYNTHESIZER_MODEL=gemini-3.8-flash`를 사용한다. `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, `GEMINI_API_KEY`의 실제 값은 로컬 `.env`에 설정한다. [모델 배치도와 환경변수 표](README.md#langgraph-워크플로우)
- Workers·Synthesizer의 외부 입력은 종목명·비중(%)·수익률(%)·정량 점수·DOC-N 공시 청크로 제한한다. LLM 전면 장애에도 로컬 점수·희석률·시뮬레이션·근거 목록과 경고를 SSE로 반환한다.
- KIS 실전 계좌는 `IS_VIRTUAL=false`와 실전 자격증명을 사용한다. 모의 계좌용 `KIS_VIRTUAL_*`는 선택 사항이다.
- PRD의 핵심 아키텍처를 변경해야 할 때는 개발자와 먼저 논의한다.

## 모듈 완료 및 교대

- PRD 7절의 모듈 하나를 완료하기 전에는 교대하지 않는다.
- Windows에서는 `.venv\Scripts\python.exe -m pytest`로, Linux·macOS에서는 `./.venv/bin/python -m pytest`로 테스트를 실행한다. 모듈별 완료 기준도 확인한다.
- 변경사항을 확인하고 스테이징한 뒤 `git commit -m "feat(module-N): ..."` 형식으로 커밋한다. 커밋과 검증을 완료한 후 세션을 초기화하고 다음 에이전트에게 넘긴다.
