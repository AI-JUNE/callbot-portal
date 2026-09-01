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
