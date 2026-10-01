# 오류 모니터링 도입 가이드 (상용 필수)

참조 구현: `Dev\3. Chatbot\src\lib\monitoring.ts` (의존성 0, 검증 완료)

## 원칙
- **npm 설치 금지** — OneDrive에서 설치가 실패하고 빌드 리스크가 있다. 공식 SDK 대신 위 참조 구현을 복사·이식한다.
- **DSN 미설정 시 no-op** — `process.env.SENTRY_DSN` 이 없으면 아무 동작도 하지 않아야 한다.
- **DSN 하드코딩 절대 금지** — 환경변수로만 주입(Vercel Environment Variables).
- **전송 전 PII 마스킹** — 주민등록번호·카드·휴대전화·이메일·계좌. 참조 구현의 `scrub()` 그대로 사용.
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

- 출력: 요청당 JSON 1줄(stdout) — `request_id·route·method·path·status·duration_ms·error_code`.
- 요청 ID: 인바운드 `x-request-id`/`x-vercel-id` 승계, 없으면 생성. 응답 헤더 `X-Request-Id` 로 반환. 교차출처에서도 읽히도록 **실제 응답**에 `Access-Control-Expose-Headers: X-Request-Id, Retry-After, X-RateLimit-*`(`_errors.EXPOSE_HEADERS`)를 함께 내보낸다 — 프리플라이트(OPTIONS)의 `Access-Control-Allow-Headers` 는 *요청* 헤더용이라 이 역할을 하지 못한다(2026-10-01 정정).
- **PII 미기록**: 경로에서 쿼리스트링 제거, 예외 *메시지*는 남기지 않고 에러코드만(`ValueError`→`VALUE_ERROR`), 보조 필드는 `monitoring.scrub()` 통과.
- **기본 접근로그 침묵**: `BaseHTTPRequestHandler` 기본 로그는 쿼리스트링을 그대로 stderr 에 찍어 `?phone=010-…`·`?text=<발화 원문>`·`?t=<웹훅 토큰>` 이 유출된다. 핸들러에 `log_message = _log.suppress_access_log` 배선으로 차단. **신규 핸들러 추가 시 반드시 동일 배선할 것** — 이제 말로만 있는 규칙이 아니라 `tests/test_logging.py::test_every_handler_silences_default_access_log` 가 `api/` 의 모든 핸들러 파일을 훑어 강제한다(2026-10-01: `speech`·`voice`·`wellbeing`·`health` 4개가 빠져 있던 것을 이 회귀로 잡았다).
- **배선 현황**: 요청 1건 = 로그 1줄(`_log.begin`)까지 배선된 핸들러는 `assist·caller_id·chat·disclosure·ops_stats·partners·settlement·sim_call·speech`. `health·voice·wellbeing` 은 접근로그 침묵만 된 상태이며, 이 '미배선' 칸이 커지는 것은 `test_structured_logging_does_not_regress` 가 막는다.
- **응답 쓰기를 위임하는 핸들러**: `_errors.send/handle` 는 `rq` 를 넘기지 않아도 핸들러의 `self._rq` 를 승계한다. `speech.py` 처럼 응답을 다른 모듈(`_stt._send`·`_tts._respond`·`_vstudio._send`)에 맡기는 경우 `self._rq` 만 걸어 두면 `request_id` 헤더와 로그 한 줄이 따라온다.
- 끄기: `CALLBOT_LOG=off` (로컬·테스트용, 기본은 켜짐).
- 오류 응답에는 `request_id`(+DSN 설정 시 `event_id`)를 함께 반환해 사용자 문의를 로그와 대조할 수 있다.

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
