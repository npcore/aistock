# aistock

키움증권 REST API 기반 자동주식거래 시스템. 대상 시장은 국내주식(KOSPI/KOSDAQ), 국내 ETF, 미국주식.
전략은 뉴스·공시 기반이며, 공시는 키움에 없으므로 금감원 DART에서 받아온다.

**현재 상태**
- 1단계 시세 조회 — 완료 (실전 계좌로 검증)
- 4단계 DART 공시 수집 + 상시 폴링 루프 — 완료 (실제 인증키로 검증)
- 5단계 LLM 신호 생성 — 코드 완료, `ANTHROPIC_API_KEY` 발급 후 검증 필요
- 6단계 백테스팅 — 완료 (실제 공시·일봉으로 기계 검증)
- 7단계 반자동 매매 — 공시 신호 + 기술적 필터 → 텔레그램 [승인] 버튼 → 지정가 주문.
  **자동으로 나가는 주문은 없다.** 사람이 승인 버튼을 눌러야만 주문된다.

## 설치

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

`.env`에 자격증명을 넣는다 (`.env.example` 참고). 이미 `.env`가 생성되어 있고 실전투자 키가 들어 있다.

```bash
.venv/bin/aistock check
```

## 사용

```bash
aistock check                    # 토큰 발급 + 연동 확인
aistock quote 005930             # 국내 현재가 / PER·PBR·시총
aistock book 005930 --depth 10   # 국내 호가
aistock chart 005930 --days 20   # 국내 일봉 (수정주가 적용)
aistock us NVDA --exchange ND    # 미국 현재가
aistock etf --mngmcomp 3020      # ETF 전체시세 (KODEX)
```

DART 공시 (`DART_API_KEY` 필요):

```bash
aistock dart sync                     # 고유번호 인덱스 다운로드 (최초 1회, 이후 7일마다 자동 갱신)
aistock dart search 삼성전자          # 회사명 → 종목코드/corp_code
aistock dart list 005930 --days 30    # 종목별 최근 공시
aistock dart list 005930 --type B     # 주요사항보고만
aistock dart today --market K         # 당일 주요 공시 (코스닥)
aistock dart doc 20260805000123       # 공시 원문 텍스트 (LLM 입력용)
```

공시 상시 감시 (폴링 루프):

```bash
aistock dart watch                                # 주가 영향 유형(B/C/D/I), 30초 간격
aistock dart watch --stock 005930 035720          # 관심 종목만
aistock dart watch --interval 60 --types B        # 주요사항보고만, 1분 간격
aistock dart watch --all-types                    # 유형 구분 없이 (요청수 1/4, 유형 미표시)
aistock dart watch --cycles 2 --replay            # 이미 뜬 공시도 포함해 2사이클만 (테스트)
```

기록은 `logs/disclosures.jsonl`(이벤트)과 `logs/documents/{접수번호}.txt`(원문 전문)에 쌓인다.

LLM 신호 생성 (`ANTHROPIC_API_KEY` 필요, **API 비용 발생**):

```bash
aistock signal 20260805000123        # 공시 하나 분석 (프롬프트 튜닝용)
aistock signal 20260805000123 --force # 1단계에서 걸러져도 원문 분석 강행
aistock dart watch --signals          # 감시 루프에 신호 생성 붙이기
```

신호는 `logs/signals.jsonl`에 쌓인다.

백테스팅 (키움 일봉 필요, LLM 불필요):

```bash
aistock backtest                          # 5거래일 보유, 다음날 시가 진입
aistock backtest --horizon 20             # 보유 기간 변경
aistock backtest --min-confidence 70      # 임계값별 성적
aistock backtest --compare-entry          # 진입 가정 두 개를 나란히 (look-ahead 크기 확인)
aistock backtest --slippage 0.05          # 슬리피지 0.05% 추가 (기본 0)
aistock backtest --commission 0.0036      # 본인 계좌 수수료율로 교체
aistock backtest --gross                  # 비용 미반영 총수익
aistock backtest --no-benchmark           # 지수 대비 비교 생략
```

**표의 수치는 기본이 순수익(비용 차감 후)이고, 지수 대비 초과수익이 함께 나온다.**

라이브러리로:

```python
from aistock import KiwoomClient
from aistock.market import domestic, overseas
from aistock.dart import CorpCodeIndex, DartClient, document_text, market_moving, recent_for_stock

with KiwoomClient.create() as client:
    info = domestic.stock_info(client, "005930")
    bars = domestic.daily_chart(client, "005930", "20260805", max_pages=5)
    nvda = overseas.quote(client, "NVDA", "ND")

with DartClient.create() as dart:
    index = CorpCodeIndex.load(client=dart)          # 종목코드 ↔ corp_code
    filings = recent_for_stock(dart, index, "005930", days=30)
    today = market_moving(dart)                      # 주가 영향 유형만 (B/C/D/I)
    text = document_text(dart, filings[0].rcept_no)  # 원문 평문 → LLM에 전달
```

## 반자동 매매 (텔레그램 승인)

```bash
aistock bot                                     # 승인 버튼을 처리하는 프로세스 (launchd로 상시 실행 중)
aistock dart watch --signals --propose          # 신호 → 주문 제안 전송
aistock dart watch --signals --propose --telegram  # 신호 알림도 함께
```

흐름:

```
공시 → Claude 신호 (BUY/SELL, 확신도)
  → 확신도 ≥ TRADE_MIN_CONFIDENCE, 정규장 시간인가
  → 매수: 기술적 필터 · 보유 여부 · 하루 한도 · 예산 → 걸리면 'blocked'로 기록만
    매도: 보유 중일 때만, 매도가능수량 전부 (차트로 막지 않는다)
  → 텔레그램에 [✅ 승인] [❌ 거절] 버튼과 함께 제안
  → 승인 시점에 다시 확인: 만료(10분) · 장중 · 가격 이탈(2%) → 지정가 주문
```

기술적 필터(매수만): 일봉 60개 미만 / 현재가·20일선이 모두 60일선 아래(하락 추세) /
RSI14 ≥ 75(과열) / 당일 +15% 이상(추격 매수) 중 하나라도 걸리면 제안하지 않는다.

- 제안은 `data/cache/proposals.json`에 쌓인다. 감시 프로세스와 봇 프로세스가 함께 쓰므로
  파일 잠금 아래에서 상태를 바꾼다. `pending → executing` 전이가 한 번만 성공하므로
  버튼을 두 번 눌러도 주문은 한 번만 나간다.
- 봇에서 `/proposals`로 오늘의 제안과 결과(주문·보류·만료 사유)를 본다.
- 한도는 `.env`의 `TRADE_*`로 조정한다 (`.env.example` 참고). `MAX_ORDER_AMOUNT`도 그대로 적용된다.
- **장 마감 후 공시**는 바로 제안하지 않고 `deferred`로 보관한다(텔레그램엔 버튼 없는 안내만).
  다음 거래일 09:05 이후 봇이 그때 가격·차트로 다시 평가해 제안한다.
  신호 시점 대비 5% 넘게 갭상승했으면 이미 가격에 반영된 것으로 보고 매수 제안하지 않는다.
  3일 넘게 묵은 신호는 버린다. 공휴일에는 풀지 않는다.
- **손절·익절**: 봇이 장중 5분마다 잔고를 보고, 수익률이 -5% 이하(손절)나 +10% 이상(익절)이면
  매도 제안을 보낸다. 같은 종목·같은 종류는 하루 한 번만 묻는다 — 거절하면 그날은 다시 안 묻는다.
  **직접 산 종목을 포함해 계좌의 모든 보유 종목**이 대상이다. 끄려면 `TRADE_STOP_LOSS_PCT=0` 등.

상시 실행 (launchd):

| 서비스 | 하는 일 | 시간 |
|---|---|---|
| `com.hansc.aistock.bot` | 명령·승인 버튼, 장외 신호 재평가, 손절·익절 점검 | 상시 |
| `com.hansc.aistock.watch` | `dart watch --signals --propose` (Claude API 비용 발생) | 평일 07:30–19:00 |

```bash
launchctl kickstart -k gui/$(id -u)/com.hansc.aistock.bot     # 코드 수정 후 봇 재시작
launchctl bootout gui/$(id -u)/com.hansc.aistock.watch        # 감시 서비스 끄기
tail -f logs/watch.log
```

19:00–07:30 사이에 뜬 공시는 아침에 시작할 때 '이미 있던 공시'로 건너뛴다.

## 미국주식 (TQQQ) — 텔레그램 승인

모든 미국 주문은 [✅ 승인] 버튼을 거친다. 지정가만 받는다(3배 레버리지라 시장가는 체결가 예측 불가).
매수 제안 수량은 기본이 **외화 주문가능금액 전부**(수수료 몫 0.5% 제외)이고 1건 상한도 없다(사용자 설정, `.env`).
나눠 사려면 `TRADE_US_BUDGET_USD`에 1건 금액을 넣는다.

```
/buy TQQQ 10 68.20     → 바로 주문하지 않고 '주문 확인' 메시지 + [승인] 버튼
/sell TQQQ 10 70.50
/us                    → 200일선·50일선·RSI와 지금의 신호
/balance               → 국내·미국 잔고와 예수금
```

봇이 뉴욕 10:00 이후 5분마다 차트를 보고 제안한다 (`trading/us_trader.py`):

| 조건 | 제안 |
|---|---|
| 미보유 · 200일선을 +3% 넘게 상향 돌파 | 매수 |
| 미보유 · 200일선 위 · 50일선 > 200일선 · RSI14 ≤ 40 | 매수 (눌림) |
| 보유 · 수익률 ≤ -10% | 손절 |
| 보유 · 수익률 ≥ +20% | 익절 |
| 보유 · 200일선보다 3% 넘게 아래 | 매도 (추세 이탈) |

- **지정가 걸어두기 (매일)**: 미보유·관망이면 뉴욕 10:00 이후 "신호 가격 $X에 외화 전액 지정가를
  걸어둘까요? [승인]"을 보낸다. 승인하면 주문이 걸려 있다가 닿으면 자동 체결된다(당일 유효).
  같은 날 걸어둔 주문·대기 제안·키움 미체결 매수가 있으면 신호 매수 제안을 또 보내지 않는다(이중 매수 방지).
  `/us`에 현재 신호 가격이 나온다. 끄려면 `TRADE_US_RESTING=0`.
- 1분마다 점검한다(`TRADE_US_CHECK_SEC`). 못 보고 만료된 매수 제안은 15분 뒤 조건이 그대로면 다시 알린다.
- 같은 종목·같은 종류 제안은 뉴욕 날짜로 하루 한 번(거절 시). NYSE 휴장일엔 제안·주문 모두 안 한다
  (`market_hours.US_HOLIDAYS` — **매년 갱신 필요**, 2027년까지 들어 있다).
- 승인 시점에 다시 확인: 10분 만료 · 미국 정규장 · 휴장일 · 제안가 대비 2% 이상 불리한 이동.
  직접 입력한 주문(`/buy TQQQ ...`)은 일부러 건 가격이므로 가격 이탈은 보지 않는다.
- **키움 미국 일봉은 액면분할 수정이 안 돼 있다.** TQQQ 2025-11-20 2:1 분할이 '반토막'으로 찍힌다.
  `split_adjusted_closes()`가 시가 갭(×1.8 이상 / ×0.55 이하)으로 분할을 감지해 보정한다.

백테스트 (2024-05-29 ~ 2026-10-16, 599거래일, 매 제안 승인·종가 체결·편도 비용 0.25% 가정):

| | 수익 | 최대낙폭 | 매매 |
|---|---|---|---|
| 규칙 (기본값) | +91.8% | -28.3% | 19회 |
| 그냥 보유 | +110.9% | -58.3% | 1회 |

상승장 한 구간뿐이라 표본이 작다. 규칙은 수익을 조금 덜 가져가는 대신 낙폭을 절반으로 줄였다.
기본값은 이 결과에 맞춰 조정하지 않았다(과최적화 방지).

## 경제 봇 (텔레그램 대화)

봇에 `/` 없이 말을 걸면 Claude(Opus 5.5)가 답한다. 예: "TQQQ 81불에 팔았는데 언제 다시 살까?"

- 스스로 근거를 찾는다: 시세·이동평균·RSI·고점 대비 낙폭(미국은 분할 보정), 계좌 잔고, 웹 검색(뉴스·실적·FOMC 일정).
- 매매 기록·원칙을 말하면 메모로 기억한다 (`/notes`, `/forget`). 최근 20턴의 대화 맥락을 유지한다 (`/reset`).
- **주문 도구가 없다.** 의견만 낸다. 사려면 `/buy TQQQ …` → [승인].
- 답 하나에 30초~1분. 백그라운드 스레드에서 돌아 그동안에도 승인 버튼·명령은 동작한다. 한 번에 한 질문.
- 비용: 질문마다 Claude API + 웹 검색 비용이 든다. 하루 상한 `ADVISOR_DAILY_LIMIT`(기본 40).
- 기록: `data/cache/advisor_chat.json`

## 계정 추가 (계정마다 따로)

계정마다 **키움 계좌·텔레그램 봇·설정·기록이 따로** 돈다. 코드는 같고 `AISTOCK_PROFILE`로 나뉜다.

```bash
aistock profile create wife                 # profiles/wife/.env 템플릿 (권한 600)
aistock profile create isa --copy-anthropic-key   # 경제 봇 Claude 키를 메인 것으로 (비용은 메인)
# → .env에 이 계좌 명의 키움 앱키·계좌번호, @BotFather로 만든 새 봇 토큰을 채운다
# → 새 봇에게 텔레그램으로 아무 메시지나 보낸다
aistock profile start wife                  # chat_id 찾기 → 검증 → launchd 상시 실행
aistock profile list                        # 계정·실행 상태
aistock profile stop wife
AISTOCK_PROFILE=wife aistock balance        # 그 계정으로 CLI 명령
```

- 추가 계정 기본값: **TQQQ + 경제 봇만** (`TRADE_KR_ENABLED=0` — 국내 주문·공시 제안·국내 손절익절 끔).
  공시 분석(Claude 비용)은 메인 계정에서만 돈다.
- **메인 .env를 이어받지 않는다.** 빈 칸이 메인 계좌 키로 채워지면 그 계정 봇이 메인 계좌로 주문한다.
- `start`는 앱키·봇 토큰이 메인·다른 계정과 겹치면 거부하고, 실제 키움 토큰 발급·봇 연결까지 확인한 뒤에 서비스를 올린다.
- 다른 사람 명의 계좌는 그분이 직접 키움 REST API를 신청해 앱키를 받아야 하고, TQQQ는 해외 레버리지 ETP 교육 이수가 필요하다.
- 서비스: `com.hansc.aistock.bot.<이름>`, 로그: `profiles/<이름>/logs/bot.log`

## 메리츠증권 (조회 전용)

메리츠 Open API(베타)로 메리츠 계좌의 예수금·국내 보유·해외 보유·외화 잔고를 읽는다. **주문 경로는 없다.**

```bash
# .env 에 MERITZ_APP_KEY / MERITZ_APP_SECRET (https://openapi.imeritz.com 에서 계좌별 신청)
aistock meritz check      # 토큰 발급 + 예수금 조회로 연결 확인
aistock meritz balance    # 잔고·외화
```

- 텔레그램 `/balance` 끝에 "🏦 메리츠증권" 섹션이 붙고, 경제 봇도 메리츠 잔고를 본다.
- 명세에서 확인한 함정: `mac_address` 헤더 필수 · 토큰은 form-urlencoded · 업무 오류도 HTTP 500 +
  `rsp_cd` · 연속조회 키는 쿼리스트링으로 · 수량·단가가 소수 문자열. (`brokers/meritz.py` 상단 주석)
- 메리츠는 해외 주문·예약주문 API도 제공한다. 실계좌로 조회가 확인되면 주문을 여는 순서로 간다.

## 관심종목 브리핑 · 보유 종목 실시간

- **매일 브리핑** (`trading/daily_brief.py`): 키움 앱 관심종목 + 보유 종목.
  국내 평일 08:40, 미국 뉴욕 09:00(개장 30분 전·미국 거래일). 현재가·200/50일선·RSI·규칙 판단,
  보유 종목은 손절·익절·추세이탈 가격과 "익절가에 걸어두기" 명령, 미보유는 매수 신호 가격과 명령.
  명령에는 [▶] 버튼(미국은 [승인] 단계로, 국내는 복사만). `/brief`로 언제든.
  보낸 날짜를 `data/cache/daily_brief.json`에 남겨 재시작해도 하루 한 번.
- **보유 종목 실시간**: 장중 1분마다 **보유한 모든 종목**(국내·미국)을 같은 규칙으로 본다.
  매도(손절·익절·추세 이탈)와 **추가 매수**(상승 추세 중 눌림, `TRADE_ADD_ON_PULLBACK`).
  국내는 손절 -5%·익절 +10%, 미국은 -10%·+20%. 같은 종목·종류는 하루 한 번.
- 손절·익절은 일봉이 모자란(신규상장) 종목에서도 수익률만으로 판단한다.
- **매수 보류** (미국, 매도는 그대로):
  - 경제 일정 (`trading/events.py`, `TRADE_US_EVENT_HOLD`): FOMC(14:00 ET)는 회의 첫날~발표 30분 뒤,
    CPI·고용보고서(08:30 ET)는 전 거래일 하루. 일정은 federalreserve.gov·bls.gov 공식표에서 옮김
    (2026-10~2027-12) — 연말 전에 갱신.
  - 기초지수 (`TRADE_US_UNDERLYING`): TQQQ→QQQ 등. 기초지수가 200일선 아래이거나 50일선이
    200일선 아래면 보류. 조회 실패 시엔 막지 않는다.
  - 걸어두기·추가 매수도 보류. 직접 `/buy`는 막지 않고 ⚠️ 경고만. `/us`·브리핑에 📅 일정과 🧭 기초지수 표시.
- **제안 검토** (`trading/review.py`, `TRADE_REVIEW`): 매수·매도 제안이 나가면 그 메시지에 답장으로
  ① 최근 3개월 차트(종가·50/200일선·제안가·RSI)가 몇 초 안에, ② 경제 봇 의견(🟢 찬성 / 🟡 주의 /
  🔴 반대 + 근거: 뉴스·FOMC·실적 일정·QQQ·비중)이 1분 안팎에 붙는다. 별도 스레드라 제안·승인을 막지
  않는다. 같은 종목·방향은 2시간 안 의견을 재사용, 하루 20건 상한(넘으면 차트만). 주문 경로는 없다.

## 구조

```
src/aistock/
  config.py          .env → Settings (real/demo 모드별 URL·키 분기)
  auth.py            토큰 발급·디스크 캐싱·만료 10분 전 자동 갱신
  client.py          공통 REST 호출기 — 헤더, 연속조회, rate limit, 401 재시도
  spec.py            공식 API 스펙(337개 TR) 조회 유틸
  market/domestic.py 국내주식·ETF 시세
  market/overseas.py 미국주식 시세
  dart/client.py     DART HTTP 클라이언트 — crtfc_key 주입, status 판정
  dart/corp_code.py  종목코드 ↔ corp_code 인덱스 (두 시스템의 접착제)
  dart/disclosure.py 공시검색·기업개황·원문 텍스트 추출
  dart/errors.py     DART 상태코드 13종 → 예외 매핑
  pipeline/poller.py 공시 폴링 루프 — 신규 감지 + 원문 회수
  pipeline/events.py HEADLINE / DOCUMENT 2단계 이벤트
  pipeline/state.py  seen·pending 영속화 (재시작 내구성)
  pipeline/handlers.py 콘솔·JSONL·원문 아카이브
  signal/schema.py   신호 스키마 (structured outputs로 강제)
  signal/prompts.py  시스템 프롬프트 (캐시 대상 — 변동 값 금지)
  signal/analyzer.py Claude 호출 — 2단계 분류/분석
  signal/handler.py  폴러에 붙는 신호 생성 핸들러
  trading/indicators.py SMA·RSI (순수 함수)
  trading/technical.py  매수 기술적 필터
  trading/sizing.py     호가단위·수량
  trading/policy.py     TRADE_* 한도
  trading/proposals.py  제안 저장소 (프로세스 간 파일 잠금, 상태 전이)
  trading/proposer.py   신호 → 제안 → 텔레그램 버튼
  trading/executor.py   승인 → 재확인 → 지정가 주문 (국내·미국)
  trading/daily_brief.py 매일 관심·보유 브리핑
  trading/review.py    제안마다 3개월 차트 + 경제 봇 의견
  trading/events.py    FOMC·CPI·고용 일정 (매수 보류)
  trading/us_trader.py  미국 차트 규칙 · 분할 보정 · /buy TQQQ 확인 제안
  us_order.py           미국 지정가 주문·취소·미체결 (달러 상한)
  account.py            국내·미국 잔고, 예수금
  brokers/meritz.py     메리츠증권 조회 (토큰·예수금·보유·외화)
  profiles.py           계정 프로필 생성·검증·launchd
  advisor.py            경제 봇 — 도구(시세·잔고·메모·웹 검색) 루프, 대화 기록
  backtest/prices.py 일봉 조회·디스크 캐시, 거래일 탐색
  backtest/costs.py  수수료·증권거래세·슬리피지
  backtest/benchmark.py 지수(업종) 일봉 — KOSPI/KOSDAQ 대비 초과수익
  backtest/engine.py 진입/청산 가정 → 수익률, 중복 병합
  backtest/report.py 집계·출력
  cli.py             확인용 CLI
reference/
  kiwoom_api_spec.json  키움 공식 전체 API 스펙 (337 TR + 오류코드 37종)
```

## 프로토콜 메모

키움 공식 레포에서 확인한 실제 스펙이다. 문서 없이 추측하면 틀리는 지점들.

- **도메인**: 실전 `https://api.kiwoom.com`, 모의 `https://mockapi.kiwoom.com`. WebSocket은 같은 호스트의 `:10000`.
- **토큰**: `POST /oauth2/token`에 `{grant_type, appkey, secretkey}` → `{token, token_type, expires_dt}`.
  `expires_dt`는 **KST 기준** `YYYYMMDDHHMMSS`다. UTC로 읽으면 9시간 일찍 만료된 것으로 오판한다. 유효기간은 24시간.
- **TR 호출**: 전부 `POST`. TR 코드는 body가 아니라 `api-id` 헤더로 넘긴다.
- **연속조회**: 응답 헤더의 `cont-yn`/`next-key`를 다음 요청 헤더에 그대로 실어 보낸다. `client.collect()`가 처리한다.
- **에러**: HTTP 200이어도 `return_code != 0`이면 실패다. 상태 코드만 보면 조용히 잘못된 데이터를 쓰게 된다.
- **`stex_tp`가 TR마다 다르다**:
  - 국내 시세/주문: `1`=KRX, `2`=NXT, `3`=통합 (일부 TR은 `0`=통합)
  - 국내 주문의 `dmst_stex_tp`: `KRX`, `NXT`, `SOR`, `%`=전체
  - 미국 시세: `NY`=NYSE, `ND`=NASDAQ, `NA`=AMEX
  - 숫자/문자를 섞어 쓰면 에러 대신 빈 결과가 오는 경우가 있다.
- **종목코드**도 거래소별로 다르다: KRX `005930`, NXT `005930_NX`, SOR `005930_AL`.
- **호가 필드**: 1호가만 `sel_fpr_bid`/`buy_fpr_bid`이고 2~10호가는 `sel_2th_pre_bid` 형태다.
- **rate limit**: 공식 수치가 명시되어 있지 않아 초당 4회로 보수적으로 잡았다 (`client.DEFAULT_RATE_LIMIT`).

## DART 메모

- **인증키**: https://opendart.fss.or.kr 에서 무료 발급(40자리). `.env`의 `DART_API_KEY`.
- **한도**: 일 20,000건. 초당 상한은 명시되어 있지 않아 5회로 잡았다 (`dart.client.DEFAULT_RATE_LIMIT`).
- **회사 식별자가 다르다**: DART는 8자리 `corp_code`, 키움은 6자리 종목코드다. `CorpCodeIndex`가 잇는다.
  비상장사의 `stock_code`는 빈 값이 아니라 **공백 문자열**로 오므로 strip 없이 판정하면 상장사로 오인한다.
  키움이 주는 `005930_AL`/`_NX` 접미사도 떼고 조회해야 한다. 둘 다 테스트로 고정해뒀다.
- **`status`가 "000"이 아니면 실패**다. HTTP는 200으로 온다.
  단 **`013`(조회 결과 없음)은 실패가 아니라 빈 결과**다 — 예외로 던지면 정상 조회가 죽는다.
- **바이너리 엔드포인트**(`corpCode.xml`, `document.xml`)는 실패 시 ZIP 대신 XML 에러 문서가 온다.
  ZIP은 `PK`로 시작하므로 그걸로 판별한다.
- **기간 제한**: `corp_code` 없이 검색하면 조회 기간이 3개월로 제한된다.
- **유형 다중 선택 불가**: `pblntf_ty`는 하나만 받는다. `market_moving()`은 유형별로 나눠 호출한 뒤
  접수번호로 중복을 제거한다.
- **원문 XML은 표준 파서로 안 열린다**. DART 자체 마크업이 섞여 있어 태그 제거 방식이 실전에서 안정적이다.
- **원문은 목록보다 늦게 올라온다** — 실시간 전략에서 가장 중요한 제약이다.
  공시는 접수 즉시 목록(`list.json`)에 뜨지만 원문 파일은 시차를 두고 올라오고,
  그 사이 `document.xml`은 `014`(또는 `013`)를 돌려준다.
  실측(2026-08-06): 당일 최신 4건 실패 / 같은 날 이른 건과 전일 이전은 100% 제공.
  공시 유형이나 제출 주체(회사 vs 거래소)와는 무관하다.
  → 폴링 루프는 `try_document_text()`를 쓴다. 아직이면 `None`을 돌려주므로 재시도 대상으로 큐에 남기면 된다.
  → 급한 신호는 목록 단계에서 이미 확보되는 **보고서명(`report_nm`)만으로도 낼 수 있다.**
    실제로 "주요사항보고서(유상증자결정)", "[기재정정]주요사항보고서(회사합병결정)"처럼
    보고서명 자체에 사건 종류가 다 들어 있다.
- **인증키가 쿼리스트링으로 나간다.** httpx는 INFO 레벨에서 전체 URL을 찍으므로
  `httpx` 로거를 WARNING으로 낮추지 않으면 키가 터미널·로그에 그대로 남는다 (`cli.py`에서 처리).

공시유형(`pblntf_ty`): A=정기공시, B=주요사항보고, C=발행공시, D=지분공시, E=기타공시,
F=외부감사관련, G=펀드공시, H=자산유동화, I=거래소공시, J=공정위공시.
이 중 **B/C/D/I**를 주가 영향 유형으로 보고 `MARKET_MOVING_TYPES`에 넣었다.

## 폴링 루프 설계

공시가 두 단계로 도착한다는 제약을 그대로 구조에 반영했다. 한 건의 공시가 이벤트를 두 번 낸다.

```
HEADLINE  새 공시 감지. 보고서명까지만 안다. 지연 없음.
   ↓  (수 초 ~ 수 분)
DOCUMENT  같은 공시의 원문 확보. text에 평문이 담긴다.
```

급한 신호는 HEADLINE에서 내고, 정밀 분석은 DOCUMENT를 기다린다.
보고서명만으로도 사건 종류는 대부분 판별된다 — "주요사항보고서(유상증자결정)", "(회사합병결정)" 등.

- **재시작 내구성**: `seen`(발행 완료)과 `pending`(원문 대기)을 `data/cache/dart_poller_state.json`에
  원자적으로 저장한다. 프로세스가 죽어도 중복 발행하거나 대기 건을 잃지 않는다.
- **부트스트랩**: 첫 실행에서 당일 공시 수백 건이 한꺼번에 신호로 쏟아지지 않도록,
  이미 떠 있는 건은 이벤트 없이 `seen` 처리한다. `--replay`로 끌 수 있다.
- **포기 조건**: 원문이 끝내 안 올라오는 건은 40회 시도 후 큐에서 뺀다. `seen`은 7일 후 정리한다.
- **핸들러 격리**: 핸들러 하나가 예외를 던져도 루프는 계속 돈다. 다음 단계의 LLM 신호 생성은
  `handlers`에 함수를 하나 더 붙이면 연결된다.

**요청 한도 계산** (일 20,000건):

| 설정 | 장중(6.5h) 목록 조회 | 비고 |
|---|---|---|
| 30초 간격 × 4유형 (기본) | 약 3,120건 | 원문 재시도 포함해도 여유 |
| 30초 간격 × `--all-types` | 약 780건 | 유형 정보를 잃는 대신 1/4 |
| 5초 간격 × 4유형 | 약 18,720건 | 한도 근접 — 시작 시 경고가 뜬다 |

원문 재시도도 건당 1요청을 먹는다. `poller.requests_made`로 실제 소진량을 볼 수 있다.

## LLM 신호 생성 메모

폴러의 2단계 구조를 그대로 재사용한다. 원문을 기다리지 않고 먼저 분류하고, 원문이 오면 분석한다.

```
HEADLINE → triage()   보고서명만, effort=low   → 원문 읽을 가치가 있나?
DOCUMENT → analyze()  원문까지, effort=high    → 신호
```

**1단계에서 거르는 만큼이 그대로 비용 절감이다.** 비싼 건 원문 분석 쪽(수만 자 입력)이고,
당일 주요 공시 374건 중 상당수는 보고서명만 봐도 주가와 무관하다.

- **모델**: `claude-opus-5`. `--model`로 교체 가능.
- **구조화 출력**: `client.beta.messages.parse(output_format=PydanticModel)`로 스키마를 강제한다.
  자유 텍스트를 파싱하면 장중에 형식이 어긋나는 순간 신호가 통째로 날아간다.
- **프롬프트 캐싱**: 시스템 프롬프트에 `cache_control`을 걸어둔다.
  **`signal/prompts.py`에 종목명·날짜·시각 같은 변동 값을 절대 넣지 말 것** — 캐시는 접두사
  일치라, 한 바이트만 달라져도 그 뒤가 전부 무효화된다. 변동 값은 전부 user 메시지에 있다.
- **거절 처리**: 안전 분류기가 거절하면 HTTP 200에 `stop_reason: "refusal"`로 온다.
  `content`를 먼저 읽으면 엉뚱한 곳에서 터지므로 `stop_reason`을 먼저 본다.
  서버측 `fallbacks="default"`를 켜둬서 거절 시 대체 모델로 자동 재시도된다.
- **긴 원문**: 회사합병 공시는 12만 자를 넘는다. `MAX_DOCUMENT_CHARS`(6만 자)에서 자르되
  **자를 때는 반드시 경고 로그를 남긴다** — 조용히 자르면 근거가 빠진 줄 모르고 신호를 믿게 된다.
- **재시작 시**: 1단계 분류 기록이 없으면 거르지 않고 분석한다. 비용을 조금 더 쓰는 쪽이
  신호를 놓치는 쪽보다 낫다.

**`actionable`은 `action ∈ {buy, sell}` + `confidence ≥ 70`으로 잡아뒀지만, 이 임계값은
근거 없는 임시값이다.** 백테스팅으로 정할 것.

## 백테스팅 메모

### look-ahead bias — 이 시스템에서 제일 큰 함정

DART 공시는 07:30~18:00 아무 때나 접수되는데, 우리는 접수 **날짜**만 알고 시각을 모른다
(접수번호의 일련번호는 시각이 아니다). 15:30 장마감 뒤 공시를 그날 종가로 체결했다고 치면
실제로는 불가능했던 거래가 되어 성과가 부풀려진다.

**실측**: 동일한 신호 67건을 진입 가정만 바꿔 돌린 결과다.

| 진입 | 적중률 | 평균 | 중앙 |
|---|---|---|---|
| `next_open` (다음 거래일 시가, 기본) | 40.3% | −0.29% | −0.09% |
| `same_close` (당일 종가, 낙관) | 61.2% | **+3.61%** | +2.62% |

같은 데이터인데 가정 하나로 "적중률 61%, 평균 +3.6%의 수익 전략"이 만들어진다. 전부 착시다.
**그래서 기본값은 `next_open`이다.** 당일 반응을 통째로 놓쳐 신호 가치를 과소평가하지만,
과대평가보다는 낫다. `--compare-entry`로 두 값을 나란히 보면 그 격차가 곧
'당일 반응을 잡을 수 있느냐'의 가치다 — 실시간 감시 루프를 개선할 이유이기도 하다.

### 중복 신호 병합

한 회사가 하루에 공시를 여러 건 내는 일은 흔하다(유상증자와 전환사채를 같은 날 공시 등).
그대로 두면 진입·청산이 동일한 거래가 여러 번 계산되어 표본이 부풀고, 공시가 잦은 종목이
통계를 과대 대표한다. **실측에서 82건 중 15건(18%)이 중복이었고, 병합하자 전체 평균이
+0.82% → −0.29%로 부호가 뒤집혔다.** 기본으로 병합하며 `--keep-duplicates`로 끌 수 있다.

### 거래비용

국내 주식은 **매도할 때만** 증권거래세를 낸다. 손실이 나도 붙는다.

| 항목 | 기본값 | 적용 |
|---|---|---|
| 위탁수수료 | 0.015% | 매수·매도 각각 (키움 온라인 기준) |
| 증권거래세 | 0.20% | 매도 시에만 |
| 슬리피지 | 0% | 기본 미반영 — `--slippage`로 지정 |
| **왕복 합계** | **약 0.23%** | |

**세율은 2026-01-01부터 인상됐다** (금융투자소득세 폐지에 따른 환원). 코스피는 증권거래세
0.05% + 농어촌특별세 0.15%, 코스닥은 증권거래세 0.20%로 둘 다 합계 0.20%다.
2025년에는 둘 다 0.15%였다. **긴 기간을 백테스팅할 때 현재 세율을 그대로 쓰면 비용이
왜곡되므로** `backtest/costs.py`의 `STT_RATES`를 그 시점 기준으로 바꿔 돌릴 것.

슬리피지 기본값이 0인 이유는, 어떤 값을 넣어도 근거 없는 추정치가 되기 때문이다.
'넣지 않았음'을 출력에 명시하고 판단은 쓰는 사람에게 맡긴다.

**실측 (동일 신호 67건, 5거래일 보유)**:

| 비용 수준 | 평균 | 중앙 |
|---|---|---|
| 미반영 (총수익) | −0.29% | −0.09% |
| 수수료 + 거래세 | −0.52% | −0.32% |
| + 슬리피지 0.05% | −0.61% | −0.42% |
| + 슬리피지 0.10% | −0.71% | −0.52% |

5거래일 보유 전략의 기대수익이 0.23%를 못 넘으면 총수익이 양수여도 실질 손실이다.
`hit`(적중) 판정도 **비용 차감 후** 기준이다 — 총수익 기준 적중률은 의미가 없다.

### 지수 대비 초과수익 — 평균 수익률만 보면 안 되는 이유

지수가 오른 기간이면 아무 종목이나 사도 평균은 양수가 되고, 빠진 기간이면 좋은 신호도
음수가 된다. **판단 기준은 평균이 아니라 '초과'와 '승률'이다.**

비교는 **같은 날짜, 같은 진입 방식**으로 맞춘다. 신호가 다음 거래일 시가에 들어갔으면 지수도
그날 시가 기준이다. 그래야 시장 타이밍이 아니라 종목 선택의 기여만 남는다.
시장별로 벤치마크가 다르다 — 코스피는 KOSPI(001), 코스닥은 KOSDAQ(101), 코넥스는 전용
지수가 없어 KOSDAQ으로 대신한다(`backtest/benchmark.py`의 `INDEX_BY_MARKET`).
공매도 신호는 지수도 부호를 뒤집어 비교한다(지수가 내리면 그만큼 더 내려야 초과).

**실측 (동일 신호 67건, 5거래일 보유, 코스닥 69%)**:

| 구분 | 건수 | 평균 | 지수 | 초과 | 승률 |
|---|---|---|---|---|---|
| 전체 | 67 | −0.52% | −1.69% | **+1.18%p** | 64.2% |
| buy | 31 | −3.17% | −8.89% | **+5.71%p** | 80.6% |
| sell | 36 | +1.77% | +4.50% | −2.73%p | 50.0% |

평균만 보면 `buy`는 −3.17%로 실패지만, 같은 기간 지수가 −8.89%였다. 5.7%p를 이겼고
10건 중 8건이 지수를 앞섰다. 반대로 `sell`은 평균 +1.77%로 성공처럼 보이지만 지수 대비로는
뒤진다. **벤치마크 없이는 두 결론 모두 정반대로 읽힌다.**

초과수익은 전략(비용 차감) − 지수(비용 미반영)라 전략에 불리한 쪽으로 기울어 있다.
지수를 ETF로 들고 있어도 비용이 드니 보수적인 방향이며, 그대로 둔다.

### 아직 반영되지 않은 것

- **공매도 실행 가능성** — `sell` 수익률은 부호 반전일 뿐이다. 국내 개인은 공매도가 사실상
  어려우므로 '방향 적중' 지표로만 읽어야 한다. 대차수수료도 빠져 있다.
- **상한가·거래정지** — 진입하려는 날 상한가면 체결이 안 될 수 있다. 현재는 시가에 무조건
  체결된다고 가정한다.
- **배당·권리락** — 일봉은 수정주가(`upd_stkpc_tp="1"`)를 쓰지만 배당 수익은 빠져 있다.

### 기계 검증 방법

LLM 신호가 없어도 틀은 검증할 수 있다. 실제 공시에 단순 규칙(유상증자→sell 등)을 붙여
임시 신호를 만들면 가격 조회·거래일 계산·집계가 실제 데이터로 돌아간다.
실측에서 이 방식으로 82건/65종목을 돌려 제외 0건을 확인했다. 참고로 그 단순 규칙의
적중률은 40.3%로 동전 던지기보다 낮았다 — 틀이 근거 없이 좋은 숫자를 만들지 않는다는 확인이다.

## 새 TR 추가하기

파라미터를 기억에 의존하지 말고 스펙에서 확인한다.

```bash
python -m aistock.spec --search 뉴스     # 이름으로 TR 검색
python -m aistock.spec ka10001           # 파라미터 상세
```

`tests/test_spec.py`가 코드에 하드코딩된 경로·파라미터를 공식 스펙과 대조하므로,
새 TR을 감싸면 `WRAPPED`에 추가한다.

## 다음 단계

2. **계좌·주문** — 잔고 조회, 주문 실행. 모의투자(`KIWOOM_MODE=demo`) 키를 별도 발급해서 먼저 검증할 것.
3. **실시간 시세** — WebSocket 구독 (`0B` 체결, `0D` 호가), 재연결·재등록 처리.
4. ~~**공시 수집**~~ — DART 연동 완료. **키움 REST API에는 뉴스·공시 TR이 없다** (337개 전체 검색 시 0건,
   `python -m aistock.spec --search 공시`). 남은 것:
   - ~~공시 폴링 루프~~ — 완료 (`aistock dart watch`).
   - **뉴스**: 네이버 뉴스 검색 API 등 별도 확보 필요 (아직 미연동).
     폴링 루프의 이벤트 구조를 그대로 재사용할 수 있다.
   - 키움이 보조로 줄 수 있는 신호: 거래량급증(ka10023), 전일대비등락률상위(ka10027),
     프로그램매매(ka90003~), 기관/외국인 수급.
5. ~~**LLM 신호 생성**~~ — 완료 (`aistock signal`, `dart watch --signals`).
6. ~~**백테스팅**~~ — 틀 + 거래비용 + 지수 대비 초과수익 완료 (`aistock backtest`).
   **아직 LLM 신호로는 돌려보지 못했다** — `ANTHROPIC_API_KEY`가 있어야 `signals.jsonl`이 쌓인다.
   남은 것: `confidence` 임계값 결정 (신호가 쌓여야 가능).
7. **리스크 관리 · 주문** — 포지션 사이징, 손절, 일일 손실 한도. 백테스팅 통과 후에.

## 주의

- `.env`, `*appkey*.txt`, `*secretkey*.txt`는 `.gitignore`에 있다. 절대 커밋하지 말 것.
- 현재 `.env`의 `KIWOOM_MODE=real`이라 **실전투자 계좌에 연결된다.** 지금은 조회 전용이라 안전하지만,
  주문 기능을 붙이기 전에 모의투자 키를 발급받아 `demo`로 검증할 것.
- 원본 키 파일(`<계좌번호>_appkey.txt`, `<계좌번호>_secretkey.txt`)은 `.env`로 옮긴 뒤에도 그대로 남아 있다. 필요 없으면 지워도 된다.
