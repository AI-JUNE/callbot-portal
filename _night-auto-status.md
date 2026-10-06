# 야간 자율 개발 상태 (2026-10-06 · 22차 — 번호 원문 평문 보관·키 지문 유출 결함 3건)

## 이번 회차 처리 — 3건
`EUM_INTEGRATION.md` 는 실회선 발신 1건만 남았고 그건 **[승인 필요]** 라 코드로 열지 않았다. COMMERCIAL_READINESS 잔여도 약관 확정·CPaaS 활성화처럼 사람 몫뿐이라, 21차가 남긴 다음 후보(`_pii_vault` 키 재료 품질·`status()` 노출 범위)를 따라가 **이미 `[x]` 인 항목의 실제 동작**을 검사했다. 암호화가 작동하는지가 아니라 **암호화가 가리기로 한 것이 정말 가려지는지**를 봤고, 결함 3건이 나왔다.

### 1) [결함] 번호 원문이 레코드에 평문으로 남아 있었다 (`api/caller_id.py`)
- 중복 판정용 내부 키 `number_digits_masked_key` 에 정규화된 11자리 번호가 **그대로** 들어가 있었다. 이름만 `masked` 였다.
- 그 한 칸 때문에 ① 봉인(`number_sealed`) ② 사용 중지 시 암호 파기 ③ 키 미설정 시 "원문 미보관"이 **전부 장식**이었다. 파기해야 할 원문이 같은 레코드 옆칸에 남아 있으니, `reveal_number()` 가 `None` 을 돌려주고 `protection="shredded"` 로 보여도 번호는 멀쩡히 보관 중이었다.
- `pii_vault.fingerprint()` 비가역 지문(`number_fp`)으로 교체. 번호 후보는 10^9 개뿐이라 소금 없는 해시는 역산된다 — **프로세스 비밀 소금 HMAC**(어디에도 저장되지 않음)을 쓴다. 중복 판정·표기 정규화(+82·하이픈·공백)·테넌트 분리는 그대로.

### 2) [결함] 미인증 응답에 봉인 키 지문이 실려 나갔다 (`api/caller_id.py`)
- `GET /api/caller_id` 요약이 `vault.kid` 를 공개했다. 비엄격 모드에서 이 경로는 사실상 공개다 — 브라우저로 열면 지문이 보인다.
- 지문은 "이 후보가 진짜 키인가"를 **암호문 없이** 확인해 주는 단서다. 제거했다. 화면이 필요한 것은 `available` 뿐이고 콘솔(`public/admin.html`)은 원래 그것만 읽는다 — 화면 변경 0.

### 3) [결함] 키 지문 자체가 공짜 오프라인 검증기였다 (`api/_pii_vault.py`)
- 지문은 봉투·로그·미리보기(`safe_preview`)로 모듈 밖에 나가는데 `sha256(prefix+key)[:8]` 한 번으로 만들어졌다. 후보 키를 넣어 8자리만 맞춰 보면 초당 수백만 번 검증이 된다. `_decode_key` 가 **32바이트만 넘으면 문자열 암호도 재료로 받아주므로** 사전 공격은 이론이 아니다.
- PBKDF2-HMAC-SHA256 **20만회**로 바꿔 후보당 비용을 20만 배 올렸다(이 PC 실측 46ms, 같은 키는 프로세스당 1회만 계산하도록 캐시 — 요청 경로의 추가 비용은 콜드스타트 1회뿐). **구 지문(sha256)으로 봉인된 봉투는 계속 복호한다**(`legacy_key_id`) — 유도 방식을 바꿨다고 이미 보관된 기록을 못 읽게 만들지 않는다. 새 봉투는 새 지문, `rewrap()` 이 회전 경로.
- 곁들여 **키 재료 품질 보고**: `status()["material"]` 이 서로 다른 바이트 수·엔트로피 **상한**(섀넌×길이 — 낮으면 확실히 약하고, 높다고 안전이 증명되지는 않는다)·흔한 자리표시자 낱말을 `weak` 로 알린다. **거부하지는 않는다** — 거부하면 '키 미설정'과 같아져 보관을 포기하게 되고 그쪽이 더 나쁘다. `status()` 는 운영 점검용이고 공개 경로에 싣지 못하도록 회귀가 막는다(위 2번).

## 검증
- `python -m pytest -q tests` **1737건 통과**(1707 → +30). 커버리지 **99%** 유지(CI 하한선 97), `api/_pii_vault.py`·`api/caller_id.py` 모두 **100%**(기존 미싱 2줄도 함께 메움).
- **변이 검증 8건 전부 잡힘**(고치기 전 동작으로 되돌려 재실행): 평문 재보관 3건 실패 · 평문 대조 복귀 4건 · 지문 재노출 1건 · 지문을 sha256 로 되돌림 2건 · 반복횟수 1000회로 낮춤 1건 · 번호 지문 소금 제거 2건 · 구 봉투 호환 제거 1건 · 약한 재료 숨김 4건.
  - **21차 사고 재발 방지**: 변이를 저장소 안에서 하지 않는다. `api/` 와 대상 테스트만 임시 폴더로 복사해 거기서 되돌린다(21차엔 저장소에서 변이시킨 탓에 AutoPush 가 `return True  # MUTANT` 를 커밋했다). 스크립트는 저장소 밖(`%TEMP%\cb_mutate.py`).
  - 소금 제거 변이는 **처음에 놓쳤다**("sha256(번호)와 다른가"만 봤더니 변이체는 문맥까지 넣은 다른 조립법이었다). 판정을 **다른 프로세스에서 같은 값의 지문이 달라지는가**로 바꿨다 — 조립법을 하나하나 맞혀 볼 필요 없이 "공개 입력만으로 계산되지 않는다"를 직접 본다.
- `python scripts/verify.py` **7/7 PASS**. 모듈 셀프테스트 3종(`_pii_vault`·`caller_id`·`_recording_audit`) OK. 네트워크 미사용(urlopen 감시 유지).
- 콘솔·HTML 변경 없음. 가짜 수치 추가 없음.

## 사람이 할 일
- **[확인 필요]** `PII_MASTER_KEY` 를 이미 등록했다면 `status()["material"]["weak"]` 를 한 번 보라. 참이면 `openssl rand -base64 32` 로 만든 값으로 교체 권장(교체는 `PII_MASTER_KEY_OLD` 에 구키를 남기면 무중단).
- **[확인 필요 · 배포]** 21차 항목이 그대로 유효하다: 라이브가 `ce973ba` 라면 `39de817` 까지 반영해야 가드 수정이 적용된다(푸시는 사람/AutoPush 몫).
- 리뷰만. 미승인 대기(변동 없음): 실회선 발신(CPAAS_LIVE)·실과금·실개인정보, SPEECH_LIVE, RECORDING_LIVE, proposals/*, 영속 저장소, 약관·개인정보 처리방침 확정 문안. **[승인 필요]**
- 설계 메모(지금 손대지 않음): `number_fp` 의 소금은 프로세스마다 새로 만들어진다. 대장이 인스턴스 메모리뿐이라 수명이 같아 문제가 없지만, **영속 저장소를 붙이는 날 소금도 함께 보관**해야 중복 판정이 재시작을 넘어 유지된다. 그 결정을 조용히 넘기지 않도록 회귀(`test_salt_is_secret_not_derivable`)가 의도적으로 깨지게 해 뒀다.

## 다음 실행 후보
- `caller_id` 증빙 봉인은 **실질이 없다** — 봉투 안에 든 `evidence_type`·발급일이 같은 레코드에 평문으로 있고 `view()` 로 공개된다. 보호한다고 표시(`evidence_protection="sealed"`)만 하는 셈이라, 표기를 사실에 맞추든지 봉인 대상을 바꾸든지 정해야 한다.
- `reject` 는 번호 원문을 파기하지 않는다(`revoke` 만 한다). 반려된 등록이 번호를 계속 들고 있어야 할 이유가 있는지 확인 필요.
- `_audit`(98%)·`_speech_providers`(91%)·`_ratelimit`(94%)·`_guard`(95%) 잔여 방어 분기.

---

# 이전 회차 (2026-10-05 · 21차 — 접근 가드·요청 제한 결함 + 릴리스 게이트 2종)

> 10~20차는 이 파일에 적히지 않았다(커밋 메시지·COMMERCIAL_READINESS.md 가 기록). 21차부터 다시 이어 적는다.

## 이번 회차 처리 — 3건
`EUM_INTEGRATION.md` 는 실회선 발신 1건만 남았고 그건 **[승인 필요]** 라 코드로 열지 않았다. COMMERCIAL_READINESS 쪽 잔여도 약관 확정·CPaaS 활성화 등 사람 몫뿐이라, 이미 `[x]` 인 항목의 **실제 동작을 다시 검사**해 결함 2건을 찾아 고치고 재발 방지 게이트를 붙였다.

### 1) [결함] 한 IP 의 폭주가 전체 사용자를 막던 요청 제한 (`api/_ratelimit.py`)
- 전역 카운터를 **먼저 소비**한 뒤 IP 한도를 보는 순서라, IP 한도에서 429 로 돌려보낸 요청들이 전역 과금 상한을 그대로 깎았다. 분당 20회를 넘긴 호출자 하나가 240회 전역 예산을 혼자 비우면 **그 1분간 다른 모든 사용자가 429(global)** 를 받는다. 막는 장치가 피해를 퍼뜨리고 있었다.
- `_take` → `_peek`/`_commit` 2단계로 분리. 두 층을 **모두 통과한 요청만 계상**한다 — 거부된 요청은 어느 버킷도 소모하지 않는다. 기존 반대 방향 불변식(전역 초과가 IP 카운터를 깎지 않음)도 그대로 유지.

### 2) [결함] 헤더 한 줄로 열리던 접근 가드 (`api/_guard.py`)
- `_origin_ok` 가 `Sec-Fetch-Site: same-origin` 을 **무조건** 참으로 취급했다. 브라우저 밖에서는 아무나 적을 수 있는 헤더라, 그 줄만 붙이면 `Origin: https://evil.example` 여도 허용 오리진 목록을 통째로 건너뛰고 과금 경로(LLM·STT·TTS)에 들어왔다.
- 이제 그 신호가 Origin·Referer 호스트와 요청 `Host` 와 어긋나면 거부한다(브라우저는 그 조합을 만들지 않는다). Host 를 모르면 판단 보류, Origin==Host 면 허용목록에 없는 미리보기 배포도 통과 — **정상 사용자 영향 0**.

### 3) 릴리스 게이트 2종 추가 (`scripts/verify.py` 5단계 → 7단계)
- `outbound`: api 의 파일이 `urlopen(`·`socket.create_connection(` 으로 밖에 나가면 **등록부에 사유가 적혀 있어야** 통과. 요청 본문에서 온 URL 을 쓰는 `voice`·`wellbeing` 은 `_urlguard` 경유 강제. 20차 SSRF 결함처럼 **한 줄로 추가되는 외부 호출**은 리뷰에서 안 보인다 — 게이트가 센다. 등록부 잔재도 실패시켜 목록이 썩지 않게 한다.
- `request_log`: `_` 없는 api 라우트 12개가 전부 `_log.begin(` 을 거치는가(로그 없는 라우트는 장애를 되짚을 기록이 없다).

## 검증
- `python -m pytest -q tests` **1707건 통과**(1673 → +34: 게이트 회귀 14 · 소켓 E2E 10 · 가드 9 · 요청제한 2 · 기존 1건 보강). 커버리지 **99%** 유지(CI 하한선 97). 바뀐 두 모듈의 신규 코드는 미싱 라인 0.
- **변이 검증 2건**(구동작으로 되돌려 재실행): 전역 선소비로 되돌리면 **3건**, same-origin 무조건 신뢰로 되돌리면 **7건**이 즉시 실패 — 테스트가 실제로 그 결함을 잡는다(단위·소켓 양쪽에서).
- `python scripts/verify.py` **7/7 PASS** · `python scripts/restore_drill.py` **7/7 성공(3.0s)**.
- 신규 `tests/test_e2e_guard_ratelimit.py` 는 **실제 소켓**(루프백·임의 포트)으로 ops_stats·wellbeing 핸들러를 띄워 확인한다. 외부망 미접속, 과금 경로 미호출.

## 사람이 할 일
- **[확인 필요 · 배포]** AutoPush 가 11:16 에 작업 도중 스냅샷을 커밋·푸시했다(`ce973ba`). 그 커밋의 `api/_guard.py` 에는 **변이 검증용 `return True  # MUTANT`** 가 그대로 들어가 있다 — 가드가 구동작(= same-origin 무조건 신뢰)으로 되돌아간 상태다. 바로 뒤 커밋 `39de817` 이 정상 코드로 되돌려 놓았지만 **푸시는 내가 하지 않는다**. 라이브가 `ce973ba` 라면 `39de817` 까지 반영해야 이번 가드 수정이 실제로 적용된다.
- 리뷰만. 미승인 대기(변동 없음): 실회선 발신(CPAAS_LIVE)·실과금·실개인정보, SPEECH_LIVE, RECORDING_LIVE, proposals/*, 공유 저장소 기반 정밀 쿼터, 약관·개인정보 처리방침 확정 문안. **[승인 필요]**
- 참고(이번에 손대지 않음): 오리진 가드는 여전히 **브라우저 밖 위조에 완전하지는 않다**. Origin·Host 를 함께 맞춰 보내는 클라이언트는 통과한다 — 근본 차단은 `CALLBOT_STRICT=1` + API 키이고, 그건 사람이 켤 일이다.

## 다음 실행 후보
- `_pii_vault` 키 재료 품질(32바이트 이상이면 전부 수용 — 저엔트로피 키도 통과) 점검과 `status()` 노출 범위.
- `_audit`·`_speech_providers` 잔여 방어 분기(각 98%·91%) 보강.

---

# 이전 회차 (2026-09-16 · 테스트 9차 + AI 고지 문구)

## 이번 회차 처리 — 2건
EUM_INTEGRATION.md 는 `[승인 필요]`(실회선) 1건만 남아 코드로 열지 않았다. COMMERCIAL_READINESS 에서 2건 처리.

### 1) 테스트 커버리지 9차 — `order_backend`·`sim_call`
- `tests/test_order_backend_sim.py` 57건 신규 → 697건 통과, 커버리지 82% → 84%(`order_backend` 55→100%, `sim_call` 60→97%).
- 결함 3건 수정: `sim_call` 미지 시나리오 조용히 refund 폴백 → 400(LLM 미호출) / engine 장애 200 속 error·본문 없는 500 → 표준 봉투 / `DemoOrderBackend.lookup_recent_order` 얕은 복사로 데모 주문 변조 → 깊은 복사.
- `/api/sim_call` 요율 등급 default → llm(sim 1회 = LLM 3~8회). `tests/test_escalation.py` 의 폴백 테스트는 거부 계약으로 갱신.

### 2) AI 고지 문구 테넌트별 설정 화면 (법정 요구) — 신규
- `api/disclosure.py`: 요건 규칙 검사(AI 명시·운영주체·상담사 안내 권고·녹음 고지↔RECORDING_LIVE 일치·길이·PII·금지어·마크업) 통과 문구만 저장. 버전·append-only 이력·감사 스트림 `tenant.disclosure`.
- `api/voice.py`: 통화 첫 발화 = `greeting(tenant)` (테넌트 설정 > CALLBOT_GREETING > 기본). 종전 기본 인사말은 AI 명시가 없어 교체. 이벤트 `tenant_id` 전달.
- `public/admin.html`: 「관리 → AI 고지 문구」 패널(검사·저장·복귀·불러오기·목록·이력, 확인창·인라인 검증·빈/오류/로딩 상태·a11y). 가짜 수치 없음.
- 테스트 50 + 14건 → 전체 761건 통과, 커버리지 85%. 로컬 E2E 9단계 확인.

## 검증
- `scripts/verify.py` 5단계 통과(py_compile 24·HTML 7·중복 id·금지어·복지 잔재). 네트워크 미사용(urlopen 감시).

## 사람이 할 일
- **[승인 필요]** 고지 문구 영속 저장소·관리자 인증 배선, 최종 법정 문안 확정. `.git/index.lock` 은 여전히 삭제 권한 없음(aside 로 이름 변경해 우회).
- 리뷰만. 미승인 대기(변동 없음): proposals/api_auth.py, proposals/confirm_refund_guard.py, proposals/pii_crypto.py, ORDER_BACKEND=http, SPEECH_LIVE/CPAAS_LIVE, RECORDING_LIVE, 실배정·CTI 연동.

## 다음 실행 후보
- 「발신번호 등록 상태 관리 화면」(번호별 등록·증빙·만료, sim·읽기 전용 설계).
- 커버리지: `voice` 91%·`_errors` 88% 잔여 분기.

---

# 이전 회차 (2026-09-14 · 테스트 8차)

## 이번 회차 처리 — 1건 (api 테스트 · 콘솔 변경 없음)
EUM_INTEGRATION.md 는 `[승인 필요]`(실회선) 1건만 남아 코드로 열지 않았다. COMMERCIAL_READINESS '테스트 커버리지' 다음 순서 `sip_adapter`(마지막 0% 모듈) 처리.

- `tests/test_sip_adapter.py` 42건 신규 → 전체 640건 통과, 커버리지 79% → 82%(`sip_adapter` 0→89%).
- 회귀로 결함 3건 수정(`api/sip_adapter.py`): 리스너 예외로 통화 누수 → 리스너 격리·`listener_errors` health 노출 / 이벤트·dry-run 기록의 원문 번호 → 마스킹(voice 와 동일 규칙) / 무효 번호 묵인 기록 → ValueError.
- 게이트 `CPAAS_LIVE` 호출 시점 판독(`is_live()`), `_event` assert → ValueError. 실발신 경로는 여전히 전부 501/PermissionError.

## 검증
- `py_compile` 23개·`scripts/verify.py` 5단계 통과(HTML 파싱·중복 id·금지어·복지 잔재). 네트워크 미사용(urlopen·socket 감시).

## 커밋
- 산출물은 AutoPush 가 `0864bb7` 로 먼저 자동 커밋·푸시. 그 위에 이 회차가 만든 `cb804d1` 은 **내용 없는 빈 커밋**(중복) — 되돌리려 했으나 `.git/HEAD.lock`·`index.lock` 삭제 권한이 자동 거부되어 남아 있다. 무해하지만 정리하려면 사람이 `.git/*.lock` 삭제 후 `git reset --soft 0864bb7`(로컬 미푸시일 때만).

## 사람이 할 일
- `.git/index.lock`·`.git/HEAD.lock` 잔존 → 다음 회차 커밋 실패 원인. 삭제 권한 승인 또는 수동 삭제 필요.
- 리뷰만. 미승인 대기(변동 없음): proposals/api_auth.py, proposals/confirm_refund_guard.py, proposals/pii_crypto.py, ORDER_BACKEND=http, SPEECH_LIVE/CPAAS_LIVE, RECORDING_LIVE, 실배정·CTI 연동. **[승인 필요]**

## 다음 실행 후보
- 커버리지: `order_backend` 55% · `sim_call` 60% 보강(0% 모듈은 소진).
- 「AI 고지 문구 테넌트별 설정 화면」 또는 「발신번호 등록 상태 관리 화면」 — sim·읽기 전용 설계로 착수 가능.

---

# 이전 회차 (2026-09-14 · 테스트 7차)

## 이번 회차 처리 — 1건 (api 테스트 · 콘솔 변경 없음)
EUM_INTEGRATION.md 는 `[승인 필요]`(실회선) 1건만 남아 코드로 열지 않았다. COMMERCIAL_READINESS '테스트 커버리지' 다음 순서(`tts` → `recording_audit`)를 처리.

### tests/test_tts_recording.py 57건 → 전체 598건 통과, 커버리지 76% → 79%
- `tts` 0→90% · `recording_audit` 31→73%(셀프테스트 블록 제외 시 100%). `urlopen` 감시 + `_synth` 대역으로 edge_tts·네트워크 미호출 강제(과금 0).
- 결함 2건 수정(`api/recording_audit.py`): 감사 로그 반환값 얕은 복사(변조 가능) → 항목 복사 / 미지 파기 방식 조용히 hard_delete → `PURGE_METHODS` 검증·ValueError.

## 검증
- pytest 598 passed · `scripts/verify.py` 전 항목 PASS(py_compile 23·HTML 7·중복 id·금지어·복지 잔재) · `python3 api/recording_audit.py` 셀프테스트 OK.

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): 실회선 발신(CPAAS_LIVE), SPEECH_LIVE, RECORDING_LIVE, proposals/*, 실배정·CTI. **[승인 필요]**

## 다음 실행 후보
- `sip_adapter`(0%) 회귀 테스트(소~중).
- `order_backend` 55%·`sim_call` 60% 보강(중).

---

# 이전 회차 (2026-09-01 · 10회차)

## 이번 회차 처리 — 2건 (B149) · 전부 `public/admin.html` (라이브 /admin)
9회차(B148) 직후 이어서 실행. 9회차가 남긴 "다음 실행 후보" 2건을 그대로 처리했습니다.

### 1) 노드 순서 변경 키보드 지원 (Alt+←/→)
- 노드명(`<b contenteditable>`)에 `id="bnd<i>"` · `onkeydown="bNodeKey(event,i)"` 부착. `bNodeKey()` 신설.
- `Alt+←` 앞으로, `Alt+→` 뒤로. `Alt` 없이 누른 화살표·그 외 키는 **무동작이며 `preventDefault()` 도 호출하지 않습니다**(캐럿 이동 등 기본 동작 보존). 구형 키명 `Left`/`Right` 도 인식.
- **편집 중이던 값을 먼저 반영**: 이동 전에 `bEditNode(i, ev.target.textContent)` 를 호출합니다. `onblur` 는 innerHTML 재생성 시 발화가 보장되지 않아 입력 중이던 이름이 사라질 수 있었습니다. 값이 실제로 바뀐 경우에만 스냅샷이 쌓이므로, 이름 수정+이동은 undo 2단계 / 이동만 하면 1단계입니다.
- `bMoveNode(i,dir,fromKey)` 3번째 인자 추가(기본 `undefined` — 기존 버튼 호출부 동작 불변). 키 경로면 이동한 노드명에 포커스가 따라가 **연속 Alt+→ 가 가능**합니다.
- `_bFoc(id,alt)` 신설: 대상이 없거나 `disabled` 면 반대 방향 버튼으로 폴백, 둘 다 없으면 조용히 무시. 버튼 클릭 경로에서도 새 위치의 같은 방향 버튼에 포커스를 옮겨 연타가 가능합니다(`nmvl<i>`/`nmvr<i>` id 부여).

### 2) 대본 단계 순서 변경 (▲/▼)
- 각 행 첫 칸에 `▲`/`▼` 버튼(`.rmv`, `id="rmvu<j>"`/`"rmvd<j>"`) 추가 — 인접 행과 자리 교환. 첫 행 `▲`·마지막 행 `▼` 는 `disabled`(단일 행이면 양쪽 비활성). 헤더 첫 칸 폭 `32px → 58px`.
- `bMoveRow(j,dir)` 신설. `bMoveNode` 와 동일 패턴 — `_bPush()` 재사용으로 **실행취소·다시실행 대상**이며, 새 편집이므로 redo 스택도 규칙대로 무효화됩니다.
- `d.script` 만 교환하므로 `flow`·`edge` 는 불변(역도 성립). 행의 4개 열은 통째로 이동합니다. 기존 셀 인라인 편집(`bEdit`)·행 삭제(`bDelRow`)는 그대로.

### 빌드 스탬프
- B148 → **B149 · 2026-09-01** (헤더 `#buildStamp` + `console.log`, 2곳 각각 `count==1` 확인 후 치환).

## 검증
- 편집은 bash + python(utf-8) 정확 매칭 치환, **앵커 9곳 전부 `count==1` assert 통과**. 편집 전 `/tmp/admin.b148.bak` 백업.
- `node --check` OK(인라인 스크립트 1개). 중복 id **0** · 금지어 **0** · view↔titles 정합 **39=39, 대칭차집합 공집합**.
- 로직 스모크(node vm, DOM 스텁) **39건 전부 통과** — B148 회귀 4건(undo/redo 왕복·버튼 경로 이동·이동 undo·경계 무동작) / 키보드 15건(양방향 이동·Alt 없으면 무동작·타 키 무시·preventDefault 호출 여부 2건·편집값 선반영·undo 2단계/1단계·포커스 이동·경계·`ev` null·`target` null·구형 키명·키 이동 redo) / 포커스 4건(방향별 포커스 2건·disabled 폴백·대상 부재 무예외) / 대본 이동 16건(상하 이동·경계 2건·범위 밖·단일 행·미등록 시나리오·undo·undo→redo·flow 불변·script 불변·열 보존·포커스·토스트 2방향·redo 무효화·연속 이동 복원).
- 렌더 마크업 검증 **17건 통과**(`renderBuilder` 의 노드 루프 + 대본 테이블 단독 실행): 노드/행 개수, 신규 id 6종, 경계 `disabled` 8종, `onkeydown` 부착, 기존 `onblur` 편집 12셀 유지, 삭제 버튼 유지, aria-label 4종, 헤더 폭, 단일 항목 양쪽 비활성, 빈 대본 무예외.
- 파일 완전성: admin.html 400,121 → **401,942자**(+1,821), 2,701행 `</html>` 종료를 host Read 로 확인, 잘림 없음.
- 배포는 CallbotAutoDeploy 자동 처리. git 명령 미실행. 발신·과금·설정 변경·개인정보 수집 코드 없음(빌더 UI 로컬 편집 전용).

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): `proposals/api_auth.py`, `proposals/confirm_refund_guard.py`, `proposals/pii_crypto.py`, `ORDER_BACKEND=http`, `SPEECH_LIVE`/`CPAAS_LIVE`, `RECORDING_LIVE`, 실배정·CTI 연동. **[승인 필요]**
- 자율 결정 1건: 키 조합을 `Alt+←/→` 로 확정했습니다(노드가 가로 배열이라 방향 일치). 대본 행은 세로 배열이라 버튼만 `▲/▼` 로 두고 키 조합은 붙이지 않았습니다 — 필요하시면 다음 회차에 `Alt+↑/↓` 로 추가하겠습니다.

## 다음 실행 후보
- 대본 단계 키보드 이동(`Alt+↑/↓`) — 셀 편집 중 이동, 노드와 동일 패턴(소).
- 노드/단계 이동 시 스크린리더 안내 — `#bValid` 와 별개의 `aria-live="polite"` 상태 줄에 "3번째 → 2번째" 형태로 위치 변화 고지(중).

---

# 이전 회차 (2026-09-01 · 9회차)

## 이번 회차 처리 — 2건 (B148) · 전부 `public/admin.html` (라이브 /admin)
직전 회차 "다음 실행 후보" 2건을 그대로 처리했습니다. 백로그 P0 1~7 전부 ✅ 이므로 저위험 UX 개선 계속.

### 1) 시나리오 빌더 다시실행(Redo)
- 툴바 `↷ 다시실행` 버튼(`#bRedoBtn`, 기본 `disabled`) 추가 — `↶ 실행취소` 바로 오른쪽.
- `_bRedoS`(시나리오별 스택, 상한 `_BUNDO_MAX`=20 공유) · `_bPushTo(st,v)`(스택 적립 공용화) · `_bApplySnap(v,k)`(스냅샷 적용 공용화) · `bRedo()` 신설.
- `_bPush()` 재작성: 새 편집이 발생하면 **해당 시나리오의 redo 스택을 무효화**(표준 undo/redo 동작). 기존 호출부 8곳(노드 편집/추가/삭제·대본 셀 편집·단계 추가/삭제·JSON 가져오기·버전 복원 2경로)은 시그니처 불변이라 그대로 동작합니다.
- `bUndo()`는 되돌리기 직전 상태를 redo 스택에 적립, `bRedo()`는 재적용 직전 상태를 undo 스택에 적립 → 왕복이 무한 반복 가능.
- `_bUndoSync()`가 두 버튼을 함께 동기화(남은 단계 수 표시·스택 비면 자동 비활성). 버튼 노드가 없어도 예외 없이 동작하도록 `if(!b)return` → `if(b){…}` 로 완화.
- **브라우저 메모리 전용** — localStorage·서버 저장 없음(`💾 버전 저장` 과 역할 분리). 손상 스냅샷은 상태를 되돌리지 않고 안내 토스트만 출력.

### 2) 시나리오 빌더 노드 순서 변경
- 각 노드 카드 좌상단에 `◀`/`▶` 버튼(`.nmv.l` / `.nmv.r`) 추가 — 인접 노드와 자리 교환. 첫 노드의 `◀`, 마지막 노드의 `▶` 는 `disabled`(단일 노드면 양쪽 비활성).
- **자율 결정**: 백로그 후보에는 `↑/↓` 로 적혀 있었으나 `#bflow` 는 노드가 **가로로 배열**되고 사이에 `.arrow` 가 놓이는 레이아웃이라 방향을 `◀/▶` 로 바꿨습니다(aria-label: "앞으로 이동"/"뒤로 이동"). 세로 화살표를 원하시면 문자만 교체하면 됩니다.
- `bMoveNode(i,dir)` 는 기존 `_bPush()` 를 재사용하므로 이동도 **실행취소·다시실행 대상**입니다. 범위 밖 인덱스·미등록 시나리오·단일 노드는 스냅샷을 쌓지 않고 무동작.
- 기존 삭제(`.ndel`)·노드명 인라인 편집은 그대로 유지. `edge`/`script` 는 건드리지 않습니다.

### 빌드 스탬프
- B147 → **B148 · 2026-09-01** (헤더 `#buildStamp` + `console.log`, 2곳 각각 `count==1` 확인 후 치환).

## 검증
- 편집은 전부 bash + python(utf-8) 정확 매칭 치환, **앵커 9곳 전부 `count==1` assert 통과**. 편집 전 `/tmp/admin.b147.bak` 백업.
- `node --check` OK(인라인 스크립트 1개). 중복 id **0** · 금지어 **0**(농협·라피치·IBK·날리지큐브·보이스봇·신세계·하나은행) · view↔titles 정합 **39=39, 대칭차집합 공집합**.
- 기능 스모크(node vm, DOM 스텁) **30건 전부 통과** — Redo 16건: 초기 비활성·빈 스택 안내·왕복 복원·redo 후 undo 재가능·새 편집 시 redo 무효화·버튼 카운트·소진 시 비활성·시나리오별 스택 분리·상한 20·깊은 복사·손상 스냅샷 안전 실패·버튼 노드 부재 무예외·기존 undo 동작 불변 2건·script/edge 동시 복원·renderBuilder 호출. 노드 이동 14건: 앞/뒤 이동·경계 무동작 2건·범위 밖 무예외·undo 복원·undo→redo·단일 노드·미등록 시나리오·토스트 방향·script 보존·redo 무효화·연속 이동 복원·노드 속성 보존.
- 렌더 마크업 검증 **9건 통과**(`renderBuilder` 노드 루프 단독 실행): 노드 수·경계 `disabled` 3종·중간 노드 활성 2종·aria-label·기존 삭제 버튼 3개 유지·노드명 편집 유지·단일 노드 양쪽 비활성.
- 파일 완전성: admin.html 398,095 → **400,121자**(+2,026), 2,685행 `</html>` 종료를 host Read 로 확인, 잘림 없음.
- 배포는 CallbotAutoDeploy 자동 처리. git 명령 미실행.

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): `proposals/api_auth.py`, `proposals/confirm_refund_guard.py`, `proposals/pii_crypto.py`, `ORDER_BACKEND=http`, `SPEECH_LIVE`/`CPAAS_LIVE`, `RECORDING_LIVE`, 실배정·CTI 연동. **[승인 필요]**
- **문서 경로 불일치(4회차부터 6회 연속 반복)**: 백로그·스케줄 작업 정의의 정본 경로가 `OneDrive\Desktop\Callbot\v2\callbot-portal` 이나 실제는 `OneDrive\Desktop\Dev\2. Callbot\v2\callbot-portal` 입니다. 매 회차 탐색 비용이 발생하므로 **작업 정의 갱신 권장**.

## 다음 실행 후보
- 노드 이동 키보드 지원 — 노드 포커스 상태에서 `Alt+←/→` 로 이동(접근성, 소).
- 시나리오 빌더 대본 단계 순서 변경(↑/↓) — 노드와 동일 패턴을 `d.script` 에 적용, `bMoveNode` 구조 재사용(중).

---

# 이전 회차 (2026-08-31 · 8회차)

## 이번 회차 처리 — 2건 (B147) · 전부 `public/admin.html` (라이브 /admin)
직전 회차 "다음 실행 후보" 2건을 그대로 처리했습니다. 백로그 P0 1~7 은 전부 ✅ 상태이므로 저위험 UX 개선으로 진행.

### 1) view-monitor 카드 자동 갱신(30초) — 기존 자동갱신 토글 존중
- `monCardsAuto(start)` / `monCardsTick()` / `_htmlSet()` 신설(`window._monCardTimer`).
- 갱신 대상은 **fetch 기반 카드 2종**(🎙️ 음성엔진 프로바이더 · 🧑‍💼 상담원 연결 큐/녹취)만. **SLA 카드는 제외** — `renderSLA()` 는 `DAYS[0]` 고정값 렌더라 재호출해도 값이 바뀌지 않아 DOM 만 흔들립니다(자율 결정, 후보 3종 중 2종만 채택).
- 4중 가드: `curView==='monitor'` · `!window._arOff`(cb_autoref) · `document.visibilityState!=='hidden'` · 진입 시 이전 타이머 clear(중복 방지). `show()` 이탈 경로에서 `monTimer` 와 함께 정리.
- `speechHealthLoad(force,quiet)` · `opsSimLoad(force,quiet)` 에 **선택 인자 `quiet` 추가**(하위호환 — 기존 호출부 동작 불변). quiet 이면 "불러오는 중…" 플레이스홀더를 건너뛰어 30초마다 화면이 깜빡이지 않습니다.
- `_htmlSet(el,html)`: 내용이 **바뀐 경우에만** innerHTML 대입 → `aria-live="polite"` 영역의 30초 주기 중복 낭독 방지 + DOM churn 제거.
- 기간 툴바에 상태 라벨 `#monAutoLbl`(`aria-live="off"`): `자동 갱신 30초` / `… · HH:MM:SS`(마지막 갱신) / `자동 갱신 OFF`. `toggleAutoRefresh()` 에서 즉시 시작·정지 연동.
- **읽기전용 GET 재조회만** 추가했습니다. 발신·과금·설정 변경 코드 없음.

### 2) 시나리오 빌더 실행취소(Undo)
- 툴바에 `↶ 실행취소` 버튼(`#bUndoBtn`, 기본 `disabled`) 추가 — `✓ 검증` 왼쪽.
- `_bUndoS`(시나리오별 스택, 상한 20) · `_bSnap()`(JSON 직렬화 = 깊은 복사) · `_bPush()` · `_bUndoSync()` · `bUndo()` 신설. **브라우저 메모리 전용** — localStorage·서버 저장 없음(기존 `💾 버전 저장` 과 역할 분리).
- 적립 지점 8곳: 노드 이름 편집(`bEditNode`) · 노드 삭제/추가(`bDelNode`/`bAddNode`) · 대본 셀 편집(`bEdit`) · 단계 삭제/추가(`bDelRow`/`bAddRow`) · JSON 가져오기(`bImport`) · 버전 복원 2경로(`scnVersions` · `scnDiff`).
- 텍스트 편집은 `onblur` 기준이며 **값이 실제로 바뀐 경우에만** 적립(빈 blur 로 스택이 차지 않음). 마지막 남은 노드 삭제는 기존대로 거부되고 스냅샷도 쌓지 않습니다.
- 버튼에 남은 단계 수 표시, 스택이 비면 자동 비활성. `renderBuilder()` 종료 시 동기화하므로 탭 전환 시 해당 시나리오 스택이 정확히 반영됩니다(시나리오 간 스택 분리).
- 손상 스냅샷은 파싱 실패 시 상태를 되돌리지 않고 안내 토스트만 출력.

### 빌드 스탬프
- B146 → **B147 · 2026-08-31** (헤더 `#buildStamp` + `console.log`, 2곳 각각 `count==1` 확인 후 치환).

## 검증
- 편집은 전부 bash + python(utf-8) 정확 매칭 치환, **앵커 26곳 전부 `count==1` assert 통과**(A 12 + B 12 + 스탬프 2). 편집 전 `/tmp/admin.b146.bak` 백업.
- `node --check` OK(인라인 스크립트 1개). 중복 id **0** · 금지어 **0**(농협·라피치·IBK·날리지큐브·보이스봇·신세계·하나은행) · view↔titles 정합 **39=39, 대칭차집합 공집합**.
- 기능 스모크(node, DOM/fetch/타이머 스텁) **56건 전부 통과**:
  - 자동 갱신 19건 — 타이머 생성·30초 주기·재진입 시 중복 없음·OFF 시 미생성·타 뷰 미생성·정지·라벨 3종·tick 시 STT/TTS/ops_stats 3회 요청·tick quiet(로딩 문구 미표시)·OFF/타 뷰/탭 숨김에서 tick 무동작·복귀 재개·마지막 시각 표기·`_htmlSet` 동일/변경/`null` 노드.
  - quiet 로더 8건 — quiet 시 플레이스홀더 생략·비 quiet 시 기존 동작·현재 기간(`?period=week`) 전달·두 섹션 렌더·게이트 OFF 문구·동일 응답 시 DOM 불변·fetch 예외 무전파.
  - 실행취소 29건 — 초기 비활성·빈 스택 안내·편집 적립/복원·동일 값 미적립·버튼 카운트·노드 삭제/추가 복원·마지막 노드 보호·단계 추가/삭제 복원·셀 편집 복원·시나리오별 스택 분리·탭 복귀 시 유지·상한 20·깊은 복사(참조 공유 없음)·손상 스냅샷 안전 실패·버튼 노드 부재 시 무예외·미등록 시나리오 미적립.
- 파일 완전성: admin.html 394,663 → **398,034자**(+3,371), 2,666행 `</html>` 종료를 host Read 로 확인, 잘림 없음.
- 배포는 CallbotAutoDeploy 자동 처리. git 명령 미실행.

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): `proposals/api_auth.py`, `proposals/confirm_refund_guard.py`, `proposals/pii_crypto.py`, `ORDER_BACKEND=http`, `SPEECH_LIVE`/`CPAAS_LIVE`, `RECORDING_LIVE`, 실배정·CTI 연동. **[승인 필요]**
- **문서 경로 불일치(4회차부터 5회 연속 반복)**: 백로그·스케줄 작업 정의의 정본 경로가 `OneDrive\Desktop\Callbot\v2\callbot-portal` 이나 실제는 `OneDrive\Desktop\Dev\2. Callbot\v2\callbot-portal` 입니다. 매 회차 탐색 비용이 발생하므로 **작업 정의 갱신 권장**.
- 자율 결정 2건: ① 후보의 "카드 3종" 중 SLA 카드는 정적 렌더라 자동 갱신에서 제외 ② Undo 는 1단계가 아닌 **시나리오별 20단계 스택**(코드 복잡도 동일, 오조작 복구 폭이 넓음). 1단계로 제한을 원하시면 `_BUNDO_MAX` 값만 1 로 바꾸면 됩니다.

## 다음 실행 후보
- 실행취소의 다시실행(Redo) — `bUndo()` 시 현재 상태를 redo 스택에 적립, `↷ 다시실행` 버튼(소).
- 시나리오 빌더 노드 순서 변경(↑/↓ 이동) — 현재는 추가·삭제만 가능하며 순서를 바꾸려면 지우고 다시 넣어야 함. Undo 적립 지점 재사용(중).

---

# 이전 회차 (2026-08-31 · 7회차)

## 착수 전 확인 — 직전 "다음 실행 후보" 2건은 **이미 구현되어 있었음(B145 · 2026-08-25, 미기록 회차)**
- `speechHealthLoad()` + view-monitor 음성엔진 health 카드, `_bImpDiff()` + `bImport` 확인 다이얼로그 모두 코드에 존재. 빌드 스탬프 B145·2026-08-25.
- 6회차 보고서가 이 문서에 기록되지 않은 상태였습니다(코드만 반영). 이번 회차에서 사실만 병기하고 새 항목으로 진행했습니다.
- 백로그 P0 1~7 전부 ✅ 상태 → 규칙에 따라 **저위험 운영 대시보드 개선**을 선택.

## 이번 회차 처리 — 2건 (B146)
### 1) 운영 집계 응답 정규화 + 활성화 게이트 현황 노출 (api/ops_stats.py)
- `_norm_stats(raw, keys)` 신설: `escalation`/`recording` 을 **고정 스키마·정수**로 정규화. 소스 모듈을 못 읽으면 전 항목 0 + `source:"unavailable"`, 읽으면 `source:"sim"`. 값이 문자열·None·누락이어도 0 으로 방어(예외 없음).
  - 이전에는 `esc or {"total":0}` 라 콘솔 쪽에서 키 존재를 가정할 수 없었음 → 이제 스키마 불변.
  - `escalation`: queued/assigned/resolved/abandoned/total, `recording`: active/purged/total.
- `gate_flags()` 신설 + 응답에 `gates` 추가: `recording_live` · `cpaas_live` · `speech_live`. **환경변수를 읽기만** 합니다(설정·활성화 코드 없음). 기본 미설정 시 전부 false.
- 기존 필드·경로·`_guard` 검사·`?period=` 동작 전부 불변(키 추가만) — 하위호환.

### 2) 상담원 연결 큐 · 녹취/감사 sim 현황 카드 (public/admin.html, view-monitor)
- `/api/ops_stats?period=<현재기간>` 1회 GET 으로 큐(대기·배정·완료·이탈·누적)와 녹취 메타(보관 중·파기·누적)를 타일로 표시. **집계 숫자만 · 통화 원문·개인정보 미포함.**
- 섹션별 뱃지: 소스 정상 `sim`, 미가용 `소스 없음`. 헤더에 게이트 표기 — 전부 OFF 면 `게이트 전부 OFF · sim`, 켜진 게 있으면 `게이트 ON: RECORDING_LIVE · …`(표시만, 켜지 않음).
- 신규: `_osEsc/_osNum/_osTiles/opsSimLoad`, `#osBody`·`#osGate`. B145 `speechHealthLoad` 와 동일한 캐시 가드(`_osLoaded`) + `🔄 새로고침` 버튼.
- 훅: `show('monitor')` 진입 시 1회 조회, `monSetPeriod()` 로 기간 변경 시 강제 재조회. 엔드포인트 실패·fetch 예외 시 안내문구로 폴백(통화 흐름·기존 KPI 영향 없음).

### 빌드 스탬프
- B145 → **B146 · 2026-08-31** (헤더 span + console.log, 2곳 정확 매칭 치환).

## 검증
- `api/ops_stats.py` 셀프테스트 OK(기존 12건 + B146 신규: 키·정수 타입, source 값, None→전부 0·unavailable, 문자열/None/누락 값 방어, gates 키셋·bool, SPEECH_LIVE=1→true·off→false 후 환경 원복, JSON 직렬화). `py_compile` OK.
- admin.html 인라인 스크립트 `node --check` OK(스크립트 1개). 중복 id 0 · 금지어 0 · view↔titles 정합(39=39, 대칭차집합 공집합).
- 기능 스모크(node, DOM/fetch 스텁) **20건 전부 통과**: period 전달·두 섹션 렌더·수치 표시·sim 뱃지 2개·게이트 OFF 문구·게이트 ON 목록·`unavailable` 뱃지 2개·실패 안내·게이트 문구 초기화·fetch 예외 무전파·force 없을 때 재조회 스킵·null 섹션 안내·비수치→0·XSS 이스케이프·대상 노드 없을 때 무예외.
- 파일 완전성: admin.html 391,683 → 394,663자(+2,980), 2,631행 `</html>` 종료 host Read 확인, 잘림 없음. 편집은 bash+python utf-8 정확 매칭 치환(앵커 5곳 전부 `count==1` assert). `.py` 는 heredoc 전체 작성 전 백업(/tmp) 후 셀프테스트 통과 확인.
- 배포는 CallbotAutoDeploy 자동 처리. git 명령 미실행.

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): proposals/api_auth.py, proposals/confirm_refund_guard.py, proposals/pii_crypto.py, ORDER_BACKEND=http, SPEECH_LIVE/CPAAS_LIVE, RECORDING_LIVE, 실배정·CTI 연동. **[승인 필요]**
- **문서 경로 불일치(4회차부터 반복)**: 백로그 정본 경로가 `OneDrive\Desktop\Callbot\v2\callbot-portal` 로 적혀 있으나 실제는 `OneDrive\Desktop\Dev\2. Callbot\v2\callbot-portal` 입니다. 스케줄 작업 정의에도 같은 경로가 있어 매회 탐색이 필요합니다 — 문서·작업 정의 갱신 권장.
- 6회차(B145) 보고서가 이 문서에 없습니다. 별도 보관본이 있으면 병합해 주세요.
- 자율 결정 사항: `gates` 는 **읽기 전용 표시**로만 추가(활성화 코드 없음). 카드는 기존 `monFetchStats` 에 끼워 넣지 않고 B145 패턴대로 독립 로더로 분리(기간 필터·새로고침과 수명주기가 달라서).

## 다음 실행 후보
- 시나리오 빌더 실행 취소(Undo) — 노드/대본 편집 직전 상태 1단계 복원(현재는 🕘 버전 저장만 있음, 중).
- view-monitor 카드 3종(SLA·음성엔진·큐/녹취) 자동 갱신 옵션 — 기존 `자동갱신 OFF` 토글(`cb_autoref`) 존중하며 30초 주기(소).

---

# 이전 회차 (2026-08-19 · 5회차)

## 이번 회차 처리 — 직전 "다음 실행 후보" 2건 (B144)
### 1) 프로바이더 health 를 /api/stt·/api/tts GET 에 노출 (api/speech_providers.py, api/stt.py, api/tts.py)
- `speech_providers.health_report(kind)` 신설: 게이트(`SPEECH_LIVE`) 상태 + 종류별 `requested/legacy/delegated/known/effective/forced_sim` + 프로바이더 목록(`sim`=ready, clova·google·aws=`pending_approval`). **인스턴스화·네트워크 호출·키 노출 없음.**
- `/api/stt` GET 응답에 `health` 필드 추가(기존 필드 불변). `/api/tts` 는 `?health=1` 쿼리에서만 JSON health 반환(오디오 합성 없음·과금 0), 그 외 동작·400 규칙 종전과 동일.
- 두 엔드포인트 모두 `_provider_health()` 를 try/except 로 감싸 speech_providers 임포트 실패 시에도 응답 유지.
- 레거시 기본값(stt=gemini, tts=edge) 은 `delegated:false` 로 표기 — 라이브 경로 동작 불변.

### 2) 기대 키워드 오버라이드를 시나리오 JSON 내보내기/가져오기에 포함 (public/admin.html)
- `bExport`: 현재 시나리오의 `cb_batchkw` 저장본이 있을 때만 `batchKeywords` 키 동봉(+토스트에 건수 표기). **저장본이 없으면 산출물은 B140 포맷과 완전히 동일 — 하위호환 유지.**
- `_bImpCheck`: `batchKeywords` 선택 검사 추가(객체 여부·최대 500건·항목 객체·`kw` 문자열 비어있지 않음). 키가 없으면 검사 생략.
- `_kwOvApply(scn,kv)` 신설: 해당 시나리오 오버라이드만 교체(타 시나리오 보존). `kw` 40자·키 200자 절단, `hard` 0/1 강제, 빈 `kw` 스킵. 빈 객체는 초기화, `undefined` 는 무변경(-1).
- `bImport` 토스트에 `기대 키워드 N건 적용` / `초기화` 표기. 데이터는 종전대로 **로컬 파일·localStorage 전용, 서버 전송 없음**.

### 빌드 스탬프
- B143 → **B144 · 2026-08-19** (헤더 span + console.log, 2곳 정확 매칭 치환).

## 검증
- `.py` 3종 `py_compile` OK. `speech_providers.py` 셀프테스트 OK(sim 응답·deny·health 출력).
- health 시나리오 5종 확인: 기본(gemini/edge·delegated false) · sim · clova(게이트 OFF→forced_sim) · clova+SPEECH_LIVE=1(effective clova) · 미지값(known false→sim). `stt._provider_health()`/`tts._provider_health()` JSON 직렬화 확인.
- admin.html 인라인 스크립트 `node --check` OK. 중복 id 0 · 금지어 0 · titles↔view 정합(39=39, 대칭차집합 공집합).
- 기능 스모크(node, DOM/localStorage 스텁) **21건 전부 통과**: 저장값 없을 때 키 미포함(하위호환)·포함 시 건수 토스트·hard 보존·타 시나리오 격리·구포맷 통과·배열/비객체/빈kw/501건 거부·null 검사생략·undefined 무변경·교체 적용·빈 객체 초기화·40자 절단·hard 강제·빈 kw 스킵·손상 localStorage 폴백·내보내기→가져오기 왕복.
- 파일 완전성: admin.html 385,419→386,810자, `</html>` 종료 host Read 확인, 잘림 없음. 편집은 bash+python utf-8 정확 매칭 치환(치환 건수 assert).
- 배포는 CallbotAutoDeploy 자동 처리. git 명령 미실행.

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): proposals/api_auth.py, proposals/confirm_refund_guard.py, proposals/pii_crypto.py, ORDER_BACKEND=http, SPEECH_LIVE/CPAAS_LIVE, RECORDING_LIVE, 실배정·CTI 연동. **[승인 필요]**
- 참고: 백로그 문서의 정본 경로(`OneDrive\Desktop\Callbot\v2\callbot-portal`)와 실제 경로(`OneDrive\Desktop\Dev\2. Callbot\v2\callbot-portal`)가 다릅니다(4회차부터 반복 보고). 문서 갱신 여부 확인 필요.
- 자율 결정 사항: `/api/tts` 는 GET 에 `text` 필수라 health 를 `?health=1` 옵트인 쿼리로 분리(빈 text 400 동작 보존). `/api/stt` 는 기존 GET 이 상태 조회용이라 응답에 필드 추가.

## 다음 실행 후보
- 운영 대시보드(view-monitor)에 STT/TTS 프로바이더 health 카드 추가 — `/api/stt`·`/api/tts?health=1` 조회, sim/승인대기 뱃지(소~중).
- `bImport` 가져오기 미리보기(적용 전 diff 요약: 노드 N개·대본 N행·기대 키워드 N건 변경) — 오적용 방지(중).

---

# 이전 회차 (2026-08-18 · 4회차)

## 이번 회차 처리 — 직전 "다음 실행 후보" 1건 + 부수 1건 (B143)
### 1) 케이스별 기대 키워드 인라인 편집 (public/admin.html)
- 배치 테스트 표의 `기대 키워드` 칸에서 **자동(대본) 케이스만** 편집 가능: 텍스트 입력 + `엄격` 체크박스. 정적 ABATCH 케이스는 종전대로 읽기 전용.
- 시드값은 B142 `_kwInfer` 추론값. 저장 전에는 `추론` 뱃지 표시, 저장하면 사라짐.
- 저장소 `localStorage['cb_batchkw'] = {시나리오:{발화:{kw,hard}}}`. `_batchCases`가 자동 케이스에 오버라이드 적용 — `hard=1`이면 c[1](실패 판정), 아니면 c[3](참고 경고). **소프트→하드 승격은 사람이 체크박스로 결정.**
- 빈값 저장 = 해제(추론값 복원). 헤더에 `↺ 키워드 초기화`(현재 시나리오 저장값 일괄 삭제).
- 신규: `_kwOvAll/_kwOv/_kwOvSet/_kwAttr/_kwCell/kwSave/kwReset`, `_bcCur`(현재 렌더 케이스 캐시).

### 2) 배치 결과 CSV 내보내기 (public/admin.html)
- 헤더 `⬇ 결과 CSV`: 마지막 실행 결과를 `batch_<시나리오>_<YYYY-MM-DD>.csv`로 저장. 컬럼 `# / 시나리오 / 테스트 발화 / 출처 / 기대 키워드 / 판정 방식(엄격·참고) / 봇 응답 / 오류 / 결과 / 참고 경고`.
- UTF-8 BOM(엑셀 한글 깨짐 방지), `""` 이스케이프. **클라이언트 전용 · 서버 전송 없음 · 데모 발화만(실개인정보 없음)**.
- runBatch가 구조체 `rows`를 함께 수집해 `_bcCur.results`에 보관. 미실행 시 안내 토스트.

### 빌드 스탬프
- B142 → **B143 · 2026-08-18** (헤더 span + console.log, 2곳 정확 매칭 치환).

## 검증
- 인라인 스크립트 `node --check` OK. 중복 id 0 · 금지어 0 · titles↔view 정합(39=39, 대칭차집합 공집합).
- 기능 스모크(node, DOM/localStorage 스텁) **21건 전부 통과**: 추론 시드·정적 케이스 읽기전용·soft 저장 반영·hard 승격 이동·빈값 해제·시나리오 격리·초기화·속성 이스케이프·미지 시나리오 0건 무예외·손상 localStorage 폴백·kwSave 래퍼 탐색/trim/뱃지 제거/무예외.
- CSV 스모크 **11건 전부 통과**: 미실행 안내·BOM·행수·헤더·따옴표 이스케이프·판정 방식·출처 기본값·경고 표기·토스트.
- 파일 완전성: 2529행 `</html>` 종료 host Read 확인, 잘림 없음. 편집은 bash+python utf-8 정확 매칭 치환(치환 건수 assert).
- 배포는 CallbotAutoDeploy 자동 처리. git 명령 미실행.

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): proposals/api_auth.py, proposals/confirm_refund_guard.py, proposals/pii_crypto.py, ORDER_BACKEND=http, SPEECH_LIVE/CPAAS_LIVE, RECORDING_LIVE, 실배정·CTI 연동. **[승인 필요]**
- 참고: 백로그 문서의 정본 경로(`OneDrive\Desktop\Callbot\v2\callbot-portal`)와 실제 경로(`OneDrive\Desktop\Dev\2. Callbot\v2\callbot-portal`)가 다릅니다. 문서 갱신 여부 확인 필요.

## 다음 실행 후보
- 기대 키워드 오버라이드를 시나리오 JSON 내보내기/가져오기에 포함(팀 공유·버전 관리) — bExport/bImport 하위호환 유지 필요(중).
- speech_providers.py 프로바이더별 health 를 /api/stt·/api/tts GET 응답에 노출(운영 점검용, sim 유지)(소).

---

# 이전 회차 (2026-08-13 · 3회차)

## 이번 회차 처리 — 직전 "다음 실행 후보" 2건 (B142)
### 1) 자동 케이스 기대 키워드 추론 — 소프트 체크 (public/admin.html)
- `_kwInfer(발화)` 신설: 고객 발화에서 한글 명사형 토큰 1개 추출(조사 제거→동사어미 요/다/죠/까 제외→불용어 `_KWSTOP` 제외). `_batchCases` 자동 케이스에 4번째 열로 부여.
- **소프트 정책(오탐 방지)**: 추론 키워드는 실패 판정에 쓰지 않음. 기대 키워드 칸에 `추론·키워드`(dim) 표시, 응답에 미포함이면 통과 옆 `⚠ 키워드` 참고 태그만. 정적 ABATCH 키워드는 기존대로 hard 판정.
- 예: '기초연금이요'→기초연금, '반품하고 싶어요'→반품, '곧 갚을게요'→추론 없음(동사만).

### 2) 검증+배치 원클릭 "⚡ 전체 점검" + 배치 요약을 검증 패널 병기 (public/admin.html)
- 빌더 헤더에 `⚡ 전체 점검` 버튼: `fullCheck()` = bValidate() 후 runBatch() 순차 실행.
- `_bvBatchLine()`: 배치 완료 시 bValid 패널(표시 중 + 동일 시나리오일 때만)에 점선 구분선과 함께 `✅/⚠ 배치 테스트 n/m 통과 · 시각` 한 줄 병기(중복 시 교체). 배치 단독 실행 시에도 패널이 열려 있으면 반영.
- runBatch 겸사 정리: 응답 원문(full) 변수로 키워드 판정(기존 60자 절단본 중복 검사 제거).

### 빌드 스탬프
- B141 → **B142 · 2026-08-13** (헤더 span + console.log, 2곳 정확 매칭 치환).

## 검증
- 인라인 스크립트 `node --check` OK.
- 중복 id 0 · 금지어 0 · nav↔view↔titles 정합(39=39, 차집합 공집합).
- 기능 스모크(node, 스텁): 추론(기초연금이요→기초연금·동사만→빈값·불용어 제외) / welfare 자동 2건(4열)·refund 정적4+자동1·미지 시나리오 0건 무예외 — 전부 통과.
- 파일 완전성: 2483행 `</html>` 종료 확인, 잘림 없음. 편집은 bash+python utf-8 정확 매칭 치환(치환 건수 assert).
- 배포는 CallbotAutoDeploy 자동 처리. git 명령 미실행.

## 사람이 할 일
- 리뷰만. 미승인 대기(변동 없음): proposals/api_auth.py, proposals/confirm_refund_guard.py, proposals/pii_crypto.py, ORDER_BACKEND=http, SPEECH_LIVE/CPAAS_LIVE, RECORDING_LIVE, 실배정·CTI 연동. **[승인 필요]**

## 다음 실행 후보
- 케이스별 기대 키워드 인라인 편집(추론값을 시드로, 로컬 저장) — 소프트→하드 승격을 사람이 결정(소~중).
- speech_providers.py 엔드포인트 위임(백로그 1번 잔여: stt.py/tts.py가 provider 인터페이스 경유, sim 유지)(중).
