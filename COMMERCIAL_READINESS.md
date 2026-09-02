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
- [ ] **표준 에러 응답** 전 API 통일 + 입력검증
- [ ] **rate limit** 공개 API 적용
- [ ] **접근·감사 로그** — 관리 기능 접근 이력
- [ ] **백업·복구 절차** RUNBOOK.md 문서화 + 복구 리허설 기록
- [ ] **약관·개인정보 처리방침 확정본 반영** (현재 초안, 문안은 사람이 확정)
- [ ] **테스트** 핵심 로직 커버리지 확보, CI에서 실행

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

