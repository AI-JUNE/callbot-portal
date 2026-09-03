# 상용 출시 잔여 과제 (COMMERCIAL READINESS)

작성 2026-09-01. **이 문서는 자동 개발의 최우선 백로그다.** 위에서부터 소진한다.

## 원칙
- `[ ]` 미완, `[x]` 완료. 완료 시 근거(파일·테스트)를 한 줄로 남긴다.
- **build now, activate on approval**: 코드는 끝까지 만들되 실인증·실결제·실개인정보·실발신 **활성화는 사람 승인**. 스위치는 환경변수로 분리하고 기본 OFF.
- 임의 성과·KPI 수치를 화면·문서에 넣지 않는다. 실측 전에는 기능 서술로 쓴다.
- 모든 변경은 테스트·빌드 검증 통과 후 커밋한다.

## 공통 상용 필수 (전 제품)
- [x] **에러 모니터링** — `api/monitoring.py`(의존성 0, Sentry envelope). chat·assist·ops_stats 핸들러 except 배선, `/api/health.monitoring` 상태 노출. `tests/test_monitoring.py` 16건 통과(no-op·PII 마스킹·DSN 미하드코딩·전송실패 격리). DSN은 Vercel 환경변수 `SENTRY_DSN` **[승인 필요: 사람이 등록]**
- [x] **구조화 로깅** — `api/_log.py`(의존성 0, JSON 1줄/요청). request_id(인바운드 x-request-id·x-vercel-id 승계)·duration_ms·error_code, 응답 헤더 `X-Request-Id`. PII 미기록: 쿼리스트링 제거·예외 메시지 미기록·필드 마스킹, 기본 접근로그(쿼리 PII 유출) 침묵. chat·assist·ops_stats 배선. `tests/test_logging.py` 21건 + E2E 스모크 통과
- [x] **/health 확장** — `api/health.py`에 `version`(build·commit·branch·env·region)과 `dependencies[]`(llm·order_backend·speech·cpaas·monitoring·storage: status/required/detail/latency_ms) 추가. required 기준 `status`=healthy|degraded|unhealthy 산출, 하위호환 키 유지. 기본 shallow(네트워크 무접속)·deep 도달성 점검은 `HEALTH_DEEP=1`+`?deep=1` 동시 충족시 자격증명 없이 TCP만. `tests/test_health.py` 26건 통과(비밀값 미노출·부작용 없음·예외 격리·상태 산출) + E2E GET/HEAD 200
- [x] **표준 에러 응답 + 입력검증** — `api/_errors.py`(의존성 0). 전 라우트 공통 봉투 `{ok:false, error(문자열·하위호환), code, status, request_id, details?, event_id?}`. 내부 예외 문구(`str(e)`) 응답 제거 — 진단은 `CALLBOT_DEBUG_ERRORS=1` 일 때만 scrub 경유. 예외 분류(400/413/502/504/500), 4xx 는 모니터링 미전송(알림 노이즈 차단). 입력검증: 본문 상한(기본 1MiB·STT 8MiB)·타입·길이·화이트리스트(chat.messages, assist.task, ops_stats.period, stt.mime, tts.text, voice 본문). `_guard.deny` 도 같은 봉투. `tests/test_errors.py` 39건 + 전체 103건 통과, 로컬 E2E 400/403/200 확인
- [x] **rate limit** 공개 API 적용 — `api/_ratelimit.py`(의존성 0). 경로 등급별 한도: llm(chat·assist) 20/분·speech(stt·tts) 12/분·webhook(voice) 120/분·default 40/분. IP 한도만으로는 X-Forwarded-For 위조로 우회되므로 **등급별 전역 한도**(과금 상한, 기본 240/120/1200/600)를 함께 둔다. 전역 차단시 IP 카운터를 소모하지 않아 정상 사용자가 이중 벌점을 받지 않는다. 429는 표준 봉투 + `Retry-After`·`X-RateLimit-*`(`_errors.send(extra_headers=)`), 키 사전 상한 5000으로 메모리 방어, 제한 로직 장애는 허용 쪽으로 폴백(가용성 우선). `/health.ratelimit` + dependencies 노출(IP 원문 미포함). 한계: 서버리스 인스턴스 로컬 카운터라 클러스터 정확 쿼터가 아님 — 정밀 쿼터 필요시 공유 저장소 배선 **[승인 필요]**. `tests/test_ratelimit.py` 31건 + 전체 134건 통과, 로컬 E2E 200×3→429(헤더 포함) 확인
- [x] **접근·감사 로그** — `api/_audit.py`(의존성 0). 관리 기능(`/api/ops_stats`·`/api/voice`·`/api/health?deep`) 접근만 별도 스트림(`kind=audit`)으로 분리 기록: 액션·결과(allow/deny/error)·상태코드·actor(자격 종류 + 지문)·client(IP 솔트 해시)·ua 대분류·request_id. **거부도 기록**(403/401/429) — 침해 조사에서 실패 시도가 더 중요. PII·비밀값 미기록: 원문 IP·API 키·웹훅 토큰·쿼리스트링·본문 제외(키는 sha256 앞 8자 지문만, 틀린 키 반복 시도는 지문으로 묶임). append-only(수정·삭제 API 없음), 버퍼 유실은 `evicted` 카운터로 드러냄, 감사 실패는 요청을 죽이지 않음(가용성 우선). `/health.audit` + dependencies 에 **개수 요약만** 노출. 한계: 인스턴스 메모리 버퍼는 휘발 — 영속 보관은 플랫폼 로그 수집 의존, 변조방지(WORM) 저장소·HTTP 조회 화면·고정 솔트(`CALLBOT_AUDIT_SALT`)는 **[승인 필요]**. `tests/test_audit.py` 29건 + 전체 163건 통과, 로컬 E2E 200(allow)/403(deny) 2건 기록·IP 원문 미유출 확인
- [x] **백업·복구 절차** — `RUNBOOK.md`. 백업 대상 인벤토리(무상태 구조상 '데이터 복구'가 아니라 '서비스 재구성'임을 명시)·RTO/RPO 목표·정기 오프라인 번들 절차·복구 시나리오 5종(배포 롤백/저장소 손상/Vercel 유실/시크릿 유출/의존 서비스 장애). 리허설은 문서가 아니라 실행으로 검증: `scripts/restore_drill.py`(의존성 0, 읽기 전용·네트워크 미사용) 가 백업→번들 무결성→빈 폴더 복원→HEAD 일치→핵심파일→py_compile→HTML 파싱 7단계를 실제 수행. 2026-09-03 리허설 **7/7 성공·26.4초**(번들 0.9MiB, api 20개, public 7개) 기록. 영속 스토리지 스냅샷·번들 자동화·Vercel 설정 백업은 **[승인 필요]**
- [ ] **약관·개인정보 처리방침 확정본 반영** (현재 초안, 문안은 사람이 확정)
- [ ] **테스트** 핵심 로직 커버리지 확보, CI에서 실행 — *CI 부분 완료*: `.github/workflows/ci.yml`(push·PR·수동, 시크릿 불요, `permissions: contents:read`)에서 pytest 163건 + 커버리지 보고 + `scripts/verify.py` 릴리스 게이트 + `scripts/restore_drill.py` 복구 리허설 실행. 게이트는 로컬·CI가 **같은 스크립트**를 쓴다(문법·HTML 파싱·중복 id·금지어 §13-1·복지 잔재 §13-5). **남은 일 = 커버리지**: 2026-09-03 실측 `api` 전체 **37%**(2376문 중 1507 미실행). 횡단 관심사는 높으나(`_ratelimit` 94·`monitoring` 89·`_log` 87·`_errors` 85·`_audit` 69·`health` 67·`_guard` 52%) **핵심 도메인 로직이 0%**: `voice`(253문)·`engine`(130)·`ops_stats`(133)·`order_backend`(91)·`sip_adapter`(86)·`escalation`(81)·`recording_audit`(85)·`speech_providers`(99)·`tts`(77)·`assist`(70)·`chat`(48)·`sim_call`(42), `stt` 29%. 다음 순서 제안: `escalation`(상담사 전환 — 아래 백로그 항목과 겹침) → `engine` → `voice`(웹훅 분기)

## Callbot(AICC Portal) 전용 (준비도 ~52%)
- [ ] 통화 개인정보 암호화·파기 배선 (설계는 완료, 미배선) **[승인 후 활성화]**
- [ ] 발신번호 등록 상태 관리 화면 — 번호별 등록·증빙·만료
- [ ] AI 고지 문구 테넌트별 설정 화면 (법정 요구)
- [ ] 상담사 에스컬레이션 실동작 검증 (sim 시나리오 회귀 테스트)
- [ ] 운영 대시보드 지표 실데이터 연결 준비 (현재 데모)
- [ ] CPaaS 활성화 전 체크리스트 이행 — CPAAS_ACTIVATION.md 참조 **[실발신은 승인]**

## 파트너 채널 (제이투모로우원 — 운영 대행 + 수익 배분)

계약·서비스 주체는 고원, 파트너는 영업·운영을 담당하고 수익을 배분한다.
**향후 리셀러(파트너 명의 계약)로 전환될 수 있으므로, 지금은 2계층으로 확장 가능한 형태로만 열어둔다.**

- [ ] **파트너(채널) 개념 도입** — 조직/계약에 `partner_id`(nullable) 추가. 없으면 직접 계약. 스키마만 준비하고 화면 노출은 최소
- [ ] **매출 귀속 근거** — 어떤 고객사가 어느 파트너를 통해 유입됐는지 기록(유입 경로·계약일·담당자). 정산 분쟁을 예방하는 핵심
- [ ] **파트너 역할 권한** — 파트너 담당자는 자기가 유치한 고객사만 조회. 기존 RBAC에 `partner_admin` 역할 추가(활성화는 승인)
- [ ] **정산 리포트** — 파트너별 계약·이용 실적·수수료 산출 근거를 조회·내보내기. 수수료율은 설정값으로 분리(하드코딩 금지)
- [ ] **2계층 확장 여지 확보** — 테넌트 조회 경로에 파트너 필터가 나중에 끼어들 수 있도록 쿼리 계층 정리. 지금 화이트라벨은 구현하지 않음

> 원칙: 파트너 관련 기능도 **코드는 만들되 활성화는 승인**. 실제 정산·청구는 계약서 확정 후.

