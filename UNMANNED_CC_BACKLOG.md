# 소규모 무인 콜센터 — 개발 백로그 (Callbot 중심)

목표: 소규모 도급사용 무인 상담센터 MVP. 원칙 **build now, activate on approval** — 실발신·실과금·실개인정보는 코드/스캐폴딩까지만, sim/dry-run·플래그 OFF 유지.
정본: C:\Users\sukju\OneDrive\Desktop\Callbot\v2\callbot-portal (public/*.html, api/*.py). 검증: node --check(스크립트), 파일 완전성 host Read.

## P0 (이 순서로, 회당 1~2건)
1. **STT/TTS 실연동 인터페이스 추상화** ✅(2026-08-19 B144 — `speech_providers.health_report()` 추가, `/api/stt` GET 응답 `health` 필드·`/api/tts?health=1` 로 프로바이더 상태 노출(실호출·키 노출 없음). 2026-07-29 api/speech_providers.py — 골격 완료, 엔드포인트 위임은 다음 회차) — engine에 provider 인터페이스(clova/google/aws)만 정의, 기본 sim 구현. 키·실호출 없음(플래그 CPAAS_LIVE=off 유지).
2. **SIP/회선 연동 어댑터(스텁)** ✅(2026-07-29 api/sip_adapter.py — sim 어댑터·이중방어 완료) — 인바운드/아웃바운드 콜 이벤트 수신·발신 인터페이스 골격. 실발신 금지, 시뮬레이터로 검증.
3. **시나리오 빌더(간이)** ✅(2026-08-19 B144 — 기대 키워드 오버라이드를 시나리오 JSON 내보내기/가져오기에 포함(`batchKeywords`, 저장본 있을 때만 동봉·구포맷 하위호환·형식 검사·시나리오 단위 교체). 2026-08-18 B143 — 배치 케이스별 기대 키워드 인라인 편집(추론값 시드·localStorage `cb_batchkw`·`엄격` 체크 시에만 실패 판정)·키워드 초기화·배치 결과 CSV 내보내기(클라이언트 전용). 2026-08-10 검증 강화 B139 — bValidate 검사 13종·오류/경고 분리·결과 패널(aria-live). 2026-07-30 확인 — public/admin.html view-builder에 노드 추가·편집·삭제, 대본 단계 편집, 검증·JSON 내보내기·버전 저장, 배치 테스트 기 구현. 데이터 로컬/데모) — 관리자 콘솔(admin.html)에서 인텐트→응답→분기 편집 UI. 데이터는 로컬/데모.
4. **상담원 에스컬레이션(폴백)** ✅(2026-07-30 완료 — api/escalation.py 모듈 + engine.py 연동: escalate_to_agent 디스패치(가드 전환·LLM 직접 호출 모두)에서 escalation.QUEUE 티켓 기록. best-effort로 통화 흐름 영향 없음, 기본 동작 불변 검증) — 신뢰도 낮음/키워드/요청 시 상담원 연결 큐로 전환하는 흐름 + 상태표시.
5. **녹취·감사로그 설계·스텁** ✅(2026-07-30 api/recording_audit.py — 메타 스키마·sim 저장소(원문 저장 없음, RECORDING_LIVE off 이중 방어)·접근 목적 화이트리스트+감사로그·보관 90일/파기 스텁. 실녹취 활성·실스토리지 파기 연동은 [승인 필요]) — 통화 메타·전사 저장 스키마, 접근·감사로그, 보관/파기 정책(코드 제안, 실데이터 OFF).
6. **운영 대시보드(간이)** ✅(2026-08-31 B146 — ops_stats 응답의 `escalation`/`recording` 고정 스키마 정규화(`source` 표기)·`gates`(RECORDING_LIVE/CPAAS_LIVE/SPEECH_LIVE) 읽기전용 노출 + admin.html view-monitor 에 상담원 연결 큐·녹취/감사 sim 현황 카드. 2026-08-25 B145 — view-monitor 음성엔진 STT/TTS health 카드. 2026-07-31 api/ops_stats.py — 콜/자동처리율/연결감축/대기/SLA 읽기전용 집계 엔드포인트(_guard·sim) + admin.html view-monitor 연결감축 KPI 카드. 실통계·CTI 연동은 [승인 필요]) — 콜/자동처리율·연결감축·대기·SLA 카드(데모 집계). PMS 대시보드 패턴 참고.

7. **PII 저장 암호화·파기 설계(코드 제안)** ✅(2026-08-07 proposals/pii_crypto.py, 커밋 cd12ee3 — envelope encryption(DEK+KEK 랩·AES-256-GCM)·crypto-shredding 파기·로그 가명화(HMAC). 미배선·PII_CRYPTO_LIVE OFF. cryptography 의존성 추가·KEK 발급·배선·파기 배치는 [승인 필요]) — 상용 전환 전제조건(평문 저장 금지·파기 증명).

## 활성화 게이트(사람 승인 필수)
CPAAS_LIVE 켜기·실발신·실과금·실개인정보(녹취) 수집·통신 회선 계약·PG. → 절대 자동 실행 금지, "[승인 필요]" 표시.

## 규칙
- 저·중위험(UI·시나리오·대시보드·인터페이스 골격·테스트)은 구현 후 커밋. Callbot 자체 auto-deploy가 배포.
- 실발신/과금/개인정보/보안설정은 코드 제안만. sim/dry-run 유지.
