# 오류 모니터링 도입 가이드 (상용 필수)

참조 구현: `Dev\3. Chatbot\src\lib\monitoring.ts` (의존성 0, 검증 완료)

## 원칙
- **npm 설치 금지** — OneDrive에서 설치가 실패하고 빌드 리스크가 있다. 공식 SDK 대신 위 참조 구현을 복사·이식한다.
- **DSN 미설정 시 no-op** — `process.env.SENTRY_DSN` 이 없으면 아무 동작도 하지 않아야 한다.
- **DSN 하드코딩 절대 금지** — 환경변수로만 주입(Vercel Environment Variables).
- **전송 전 PII 마스킹** — 주민등록번호·카드·휴대전화·이메일·계좌. 참조 구현의 `scrub()` 그대로 사용.
  - **경계에 `\b` 를 쓰지 말 것**(2026-10-16 정정). 파이썬·JS 모두 유니코드 모드에서 **한글이 낱말 문자**라, 조사·단위가 숫자에 붙는 실제 문장(「…1234567이에요」·「…2222번으로」)에서는 끝쪽 `\b` 가 성립하지 않아 **규칙이 한 건도 걸리지 않는다**. 숫자 아님 단정(`(?<!\d)`·`(?!\d)`)으로 둘 것 — 더 긴 숫자열 안쪽의 부분 일치를 막는 원래 목적은 그대로 지켜진다. 영문·공백 뒤 숫자만 쓰는 테스트 문장은 이 결함을 **잡지 못한다**(한국어 문장을 회귀에 넣을 것: `tests/test_monitoring.py::TestScrubKoreanBoundary`).
  - 한계: 표기 패턴 방어다. 「공일공 일일일일」처럼 **말로 읽은 번호**는 가려지지 않는다.
- **전송 실패가 서비스에 영향 없어야 함** — 모든 예외 흡수, 재던지기 금지.

## 이식 절차
1. 참조 구현을 프로젝트 언어에 맞게 복사
   - TypeScript(PMS·D-ARS·AICC-Core): 거의 그대로. import 경로만 조정
   - 이음(Vite/JS): `src/eum/monitoring.js` 로 이식. `import.meta.env.VITE_SENTRY_DSN` 사용
   - Callbot(Python): `api/monitoring.py` 로 이식. `os.environ.get("SENTRY_DSN")`, urllib 사용
2. 전역 오류 지점에 연결
   - Next.js: API 라우트 try/catch + `app/global-error.tsx`
   - 이음: ErrorBoundary + window.onerror
   - Callbot: 각 API 핸들러 except 블록
3. 테스트 동반 — no-op 동작, 마스킹, DSN 미하드코딩을 불변식으로 검증
4. `COMMERCIAL_READINESS.md` 의 "에러 모니터링" 항목을 `[x]` 로 변경

## 환경변수 (사람이 Vercel에 등록)
- Next.js/Python: `SENTRY_DSN`
- 이음(Vite): `VITE_SENTRY_DSN`

## 이식 현황
- [x] **Callbot(AICC Portal)** — `api/monitoring.py`. 배선: `api/chat.py`·`api/assist.py`·`api/ops_stats.py` except 블록, 상태는 `/api/health` 의 `monitoring` 필드(값 미노출, 설정 여부만). 테스트 `tests/test_monitoring.py` (`python3 tests/test_monitoring.py`, 16건).
  - 남은 작업: Vercel에 `SENTRY_DSN` 등록(사람). 등록 전까지 no-op 이므로 배포 리스크 없음.

## 구조화 로깅 (`api/_log.py`)
오류 모니터링과 **같은 request_id** 를 사용하므로 로그 ↔ Sentry 이벤트를 상호 추적할 수 있다.

- 출력: 요청당 JSON 1줄(stdout) — `request_id·route·method·path·status·duration_ms·error_code·code`.
- 요청 ID: 인바운드 `x-request-id`/`x-vercel-id` 승계, 없으면 생성. 응답 헤더 `X-Request-Id` 로 반환. 교차출처에서도 읽히도록 **실제 응답**에 `Access-Control-Expose-Headers: X-Request-Id, Retry-After, X-RateLimit-*`(`_errors.EXPOSE_HEADERS`)를 함께 내보낸다 — 프리플라이트(OPTIONS)의 `Access-Control-Allow-Headers` 는 *요청* 헤더용이라 이 역할을 하지 못한다(2026-10-01 정정).
- **PII 미기록**: 경로에서 쿼리스트링 제거, 예외 *메시지*는 남기지 않고 에러코드만(`ValueError`→`VALUE_ERROR`), 보조 필드는 `monitoring.scrub()` 통과.
- **기본 접근로그 침묵**: `BaseHTTPRequestHandler` 기본 로그는 쿼리스트링을 그대로 stderr 에 찍어 `?phone=010-…`·`?text=<발화 원문>`·`?t=<웹훅 토큰>` 이 유출된다. 핸들러에 `log_message = _log.suppress_access_log` 배선으로 차단. **신규 핸들러 추가 시 반드시 동일 배선할 것** — 이제 말로만 있는 규칙이 아니라 `tests/test_logging.py::test_every_handler_silences_default_access_log` 가 `api/` 의 모든 핸들러 파일을 훑어 강제한다(2026-10-01: `speech`·`voice`·`wellbeing`·`health` 4개가 빠져 있던 것을 이 회귀로 잡았다).
- **레벨 규약**: `2xx/3xx=info · 4xx=warn · 5xx=error`(`_log.level_for`). 성공 종료(`finish`)와 오류 종료(`fail`)가 **같은 규칙**을 쓴다 — 예전에는 `fail` 이 무조건 `error` 였고, `_errors.handle` 이 입력검증 실패(400·413)까지 그 경로로 보내 **사용자 오타가 서비스 장애와 같은 레벨**로 쌓였다(level=error 알림이 그만큼 울리면 진짜 5xx 가 묻힌다). `error_code` 는 4xx 에서도 남겨 어느 검증에서 걸렸는지 집계할 수 있다.
- **배선 현황**: `api/` 의 **모든 핸들러**가 요청 1건 = 로그 1줄(`_log.begin`)까지 배선됐다(2026-10-03: 마지막으로 남아 있던 `voice·wellbeing·health` 결선). `test_structured_logging_does_not_regress` 가 '미배선' 칸이 다시 생기는 것을 막고, `tests/test_request_log_wiring.py` 는 목록이 아니라 **실제 요청을 태워** 한 줄이 나가는지·PII 가 섞이지 않는지 확인한다(소스에 `_log.begin(` 이 있어도 새 분기가 `_send` 를 거치지 않으면 그 요청은 조용히 사라진다).
- **`/api/health` 는 shallow 성공을 남기지 않는다**: 업타임 모니터·로드밸런서가 수십 초마다 치는 경로라 요청 1건=1줄이면 실제 트래픽이 그 줄들에 묻힌다(감사 스트림이 deep 점검만 기록하는 것과 같은 판단). **실패(4xx·5xx)와 deep 점검은 항상** 남고, 추적용 `X-Request-Id` 는 shallow 응답에도 붙는다. 전량 기록이 필요하면 `HEALTH_REQUEST_LOG=1`.
- **보조 필드는 아는 값만**: `rq.set()` 에 외부 입력을 그대로 담지 않는다. `voice` 의 `?op=`·이벤트 `type`, `wellbeing` 의 `?op=` 은 화이트리스트 밖이면 `other` 로 접는다 — 임의 문자열이 로그에 섞이면 집계 카디널리티가 터지고 PII 유입 경로가 된다.
- **응답 쓰기를 위임하는 핸들러**: `_errors.send/handle` 는 `rq` 를 넘기지 않아도 핸들러의 `self._rq` 를 승계한다. `speech.py` 처럼 응답을 다른 모듈(`_stt._send`·`_tts._respond`·`_vstudio._send`)에 맡기는 경우 `self._rq` 만 걸어 두면 `request_id` 헤더와 로그 한 줄이 따라온다.
- **코드가 두 칸이다**(2026-10-10 정리). 한 요청의 '코드'는 성질이 다른 둘이고, 겹쳐 쓰면 한쪽 집계가 반드시 틀어진다.
  - `code` — **응답 봉투(`_errors`)가 내려준 값과 같은 문자열**. 사용자·심사관이 신고하는 것은 이쪽이다("INVALID_REQUEST 가 떴어요"). 장애 조사는 이 값으로 로그를 찾는다.
  - `error_code` — 예외 **타입명** 유래(`ValueError`→`VALUE_ERROR`). 5xx 분류는 전부 `INTERNAL_ERROR` 라서, 이 칸이 없으면 무엇이 터졌는지 알 수 없다. **원인별** 집계용.
  - 예전에는 로그에 `error_code` 만 있었고 그 값이 봉투와 **달랐다**(봉투 `INVALID_REQUEST` ↔ 로그 `VALIDATION_ERROR`). 신고받은 코드로 grep 하면 아무것도 안 나왔고, 더 나쁜 쪽은 **거부(401/403/429)·404·413·405** — 예외 없이 `_errors.send` 로만 끝나는 경로라 **코드가 한 칸도 없었다**(거부 사유별 집계 불가). 호출부가 `rq.finish(_c, denied=True)` 로 먼저 닫아 버렸기 때문에 뒤따르는 `send` 가 코드를 실을 자리도 없었다.
  - 규약: **거부 응답의 로그는 `_errors.send` 가 닫는다**. 호출부는 `_guard.deny(...)` 만 부르고 미리 `rq.finish()` 하지 않는다(거부 표식 `extra.denied` 는 `deny` 가 단다). 2xx 에는 두 칸 모두 없다 — 성공이 오류 집계를 오염시키지 않는다. 회귀는 `tests/test_error_code_correlation.py` 가 **라우트 12개를 실제로 태워** 봉투 `code` == 로그 `code` 를 확인하고, 미리 닫는 구동작이 돌아오면 드리프트 검사가 잡는다.
- **통화 데이터는 로그로 내보내지 않는다**(2026-10-16). 발화 전사·통화 요약은 `print` 로도 로그 보조 필드로도 남기지 않는다 — 요약은 모델이 대화 전체를 자유 문장으로 옮긴 것이라 성명·주소·주문번호가 섞이고, 파싱 실패 시에는 모델 원문이 그대로 담긴다. 남기는 것은 **사실만**: 요약 유무(`result`)·턴 수(`turn_count`)·통화ID **지문**(`call`, `_call_metrics.digest` 와 같은 값 — 원문 통화ID 는 외부가 주는 값이라 번호가 섞여 들어올 수 있다). 요약 본문은 응답으로만 돌려준다.
- **감사 결과는 응답이 정한다**(2026-10-30). 감사 스트림(`kind=audit`)의 `result` 는 `_audit.result_for(status)` **한 곳**에서 나온다 — `2xx·3xx=allow · 4xx=deny · 5xx=error`(해석 불가는 `error` — 모르는 것을 성공으로 적지 않는다). 요청 로그의 레벨 규약(위 「레벨 규약」)과 같은 가름이다: 사용자 오타(400)가 `error` 칸에 쌓이면 `/health.audit` 와 알림에서 **진짜 5xx 가 그 안에 묻힌다**.
  - **성공은 응답을 보낸 뒤에 적는다.** 그 전에는 `wellbeing`·`voice` 가 접근 가드를 통과한 **직후** `allow/200` 을 적어, 실발신 차단(501)·콜백 전송 실패(502)·입력오류(400)·과대본문(413)·미보관 조회(404)·내부오류(500)가 감사에서 **전부 성공**으로 보였다(5차가 `ops_stats` 에서 고친 결함이 두 라우트에 남아 있었다). 거부(`deny`)만 가드 자리에서 적는다 — 그 상태코드는 가드 판정 자체다.
  - **무엇을 했는지도 적는다**: 같은 경로가 여러 동작을 맡으면 액션명만으로 가려지지 않는다. `wellbeing` 은 `op`(안부 실행·최근 이력·**판정 근거 열람**)를, `voice` 는 캠페인 발신 시도(`op=campaign`)를 남긴다. 값은 요청 로그와 같은 화이트리스트를 거친다(`_audit.FIELDS` 등록부).
  - 재발 방지는 릴리스 게이트 **21단계 `audit_outcome`** — 감사 호출이 응답 전송보다 **앞에서** 성공을 적으면, 실패 라벨을 손으로 적으면, 상태→라벨 판정이 어긋나면 통과하지 못한다(판정은 실제로 돌려 확인한다).
- 끄기: `CALLBOT_LOG=off` (로컬·테스트용, 기본은 켜짐).
- 오류 응답에는 `request_id`(+DSN 설정 시 `event_id`)를 함께 반환해 사용자 문의를 로그와 대조할 수 있다.

## 인스턴스 메모리 저장소 (영속 저장소 승인 전까지)

영속 저장소가 없으므로 통화 세션·감사 버퍼·요율 카운터·안부 결과는 **프로세스 메모리**에 있다.
웜 인스턴스는 수 시간을 살기 때문에, 상한이 없는 저장소는 (1) 메모리가 자라 **진행 중인 통화**를
죽이고 (2) 개인정보 보관 기간이 '인스턴스 수명'이 된다.

- 규약: 모듈·클래스 본문에서 **빈** dict/list 로 시작하는 저장소는 보관 상한을 **이름 있는 상수**로
  두고, 버린 건수를 집계·로그로 드러낸다(조용히 사라지게 하지 않는다).
- 게이트: `scripts/verify.py` 12단계 `mem_caps` — 저장소가 `MEM_STORES` 등록부에 없거나 등록된
  상한 기호가 사라지면 통과하지 못한다. 범위는 모듈·클래스 본문 + **모듈 전역 싱글턴**
  (`STORE = RecordingStore()` 처럼 import 시점에 하나만 만들어지는 인스턴스) 클래스의
  인스턴스 속성까지다(31차 확장). 요청마다 새로 만들어지는 클래스의 속성은 보지 않는다 —
  과차단하면 사람이 게이트를 끈다.
- 통화 세션(`voice._Session`): 상한 `SESSION_MAX`(기본 500, `_call_metrics.MAX_OPEN` 과 같은 눈금) ·
  만료 `SESSION_TTL`(기본 1800초). 환경변수 `CALLBOT_SESSION_MAX`·`CALLBOT_SESSION_TTL` 로
  **호출 시점**에 조정된다(잘못된 값은 기본값으로 — 상한이 사라지는 쪽으로 실패하지 않는다).
  현황은 `GET /api/voice.sessions`(`live`·`expired`·`evicted`·`max`·`ttl_sec`).
- 녹취 장부(`_recording_audit.STORE`, 31차): 상한 `MAX_RECORDS`(2000, `_call_metrics` 와 같은 눈금) ·
  감사 버퍼 `MAX_AUDIT`(2000, `_escalation` 과 같은 눈금). 초과 시 **파기된 묘비부터** 버린다 —
  아직 보관기간이 남은 레코드를 버리면 `purge_due()` 가 파기해야 할 것을 영영 찾지 못한다.
  현황은 `GET /api/ops_stats.recording`(`dropped`·`dropped_active`·`audit_dropped`·`scope`·`volatile`).
  **`dropped_active > 0` 이면 상한을 올릴 것이 아니라 영속 저장소를 봐야 한다**([승인 필요]).
  이 감사 버퍼는 **법정 보존용 원본이 아니다**(최근 2000줄, 재시작 시 소멸).
- 한 통화 **안쪽**의 대화 이력(`_engine._window`, 31차): 모델에 보내는 창은
  `HISTORY_MAX_MSGS`(40건)·`HISTORY_MAX_CHARS`(24000자). 환경변수
  `CALLBOT_HISTORY_MAX`·`CALLBOT_HISTORY_MAX_CHARS` 로 호출 시점에 조정된다.
  창을 넘어 잘린 건수는 응답 `log` 의 `{"turn":"context","dropped":n,"kept":m}` 한 줄로 드러난다.
  **줄어드는 것은 모델이 보는 창뿐이고 가드의 기억(`_mem`)은 전체 이력에서 계산한다** —
  견적 금액·정책 한도가 창 밖으로 밀려도 금액 재확인·한도 검사는 그대로 돈다.

## /health 계약 (업타임 모니터 설정 기준)

`GET /api/health` 는 무인증·no-store JSON. 모니터 알림 규칙은 아래 값에 의존한다.

- `ok`: 프로세스 생존 신호. 응답이 오면 항상 true (LB/업타임 호환).
- `status`: `healthy` | `degraded` | `unhealthy` — **required 의존성**만 반영.
  실패 시 알림은 `status != "healthy"` 로 거는 것을 권장(HTTP 코드는 200 고정).
- `dependencies[]`: `{name, kind, required, status, detail, checked, latency_ms}`.
  status 값은 `ok`·`not_configured`·`misconfigured`·`simulated`·`error`.
  `simulated`(CPaaS/음성/데모 주문)은 **장애가 아니라 승인 전 의도된 상태**이므로 알림 대상이 아니다.
- `version`: `commit`·`commit_short`·`branch`·`env`·`region` (Vercel 시스템 환경변수). 배포 추적용.
- 심층 점검: `HEALTH_DEEP=1` 환경변수 + `?deep=1` 쿼리가 **모두** 있을 때만 수행하며,
  자격증명 없이 TCP 연결만 시도한다(LLM·CPaaS 실호출 없음 = 과금 없음). 기본은 OFF.
- 요청 로그: shallow 성공은 남기지 않는다(모니터 폴링). 응답의 `X-Request-Id` 는 항상 있고,
  실패·deep 은 항상 기록된다. 전량 기록은 `HEALTH_REQUEST_LOG=1` (기본 OFF).
