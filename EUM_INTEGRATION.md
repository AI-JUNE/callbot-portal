# 이음 2R 연동 과제 — 안부 콜봇 (가이드 §6-1)

이음이 쓰는 시나리오는 **「안부」 1개**뿐. 콘솔 메뉴는 이음과 무관하며 심사에 노출하지 않는다.

## 흐름
담당자 현황판 「안부 전화」 → `POST /api/wellbeing/call {senior_id, callback_url}` → 기존 `api/voice.py` (시나리오=안부) → 통화 종료 시 `callback_url` 로 웹훅 POST:
```json
{ "senior_id": "...", "answered": true, "mood_score": 1~5, "risk_level": "low|mid|high", "transcript_summary": "...", "raw_ref": "..." }
```
- risk_level=high 또는 2회 연속 미응답 → 이음이 follow_ups 생성(이음 쪽 책임)
- **실전화 없이 동작** — 「웹에서 바로 테스트」 경로(전화망 미경유)로 시연. 실회선은 3R.

### 규격 보충 (구현 확정분)
- 미응답 시: `answered=false`, `mood_score=null`, `risk_level="unknown"`. 연속 미응답 집계는 이음 쪽 책임(우리는 건별 사실만 보고).
- 부가 키(무시 가능): `schema`("wellbeing.result.v1")·`scenario`·`mode`·`dimensions`(4항목 점수·라벨)·`flags`·`ts`.
  `dimensions` 는 담당자가 점수 근거를 확인할 수 있게 함께 보낸다.
- 서명 헤더: `X-Callbot-Timestamp`, `X-Callbot-Signature: sha256=<hex>`, `X-Callbot-Event: wellbeing.result`.
  서명 대상은 `<timestamp>.<body>` (본문 재전송 방지). 검증 예시는 `wellbeing.verify_signature()`.
- 요청 옵션: `profile`(ok|watch|risk|no_answer, 시연 대본) 또는 `answers`(4문항 직접 주입).
  `callback_url` 생략 시 전송 없이 페이로드만 반환(dry-run).

## 과제
- [x] `api/wellbeing.py` — 안부 시나리오 전용 엔드포인트. 시뮬레이션 모드에서 30초 내 결과 웹훅 발송
      · `POST /api/wellbeing/call`(vercel.json rewrite 로 하위경로 → `?op=`), `GET /api/wellbeing` 은 문항·서명규약·샘플 페이로드 안내, `?op=recent` 최근 실행.
      · 전화망 미경유(통신비 0원). 동기 처리라 콜백까지 1초 내(로컬 E2E 실측) — 30초 예산 안. 웹훅 타임아웃 8초.
      · **실발신 차단**: `mode=live` 또는 `CPAAS_LIVE=1` 이면 501 「[승인 필요]」. 실회선은 3R.
- [x] 안부 시나리오 프롬프트 — 기분·식사·수면·통증 4문항, 결과를 mood_score(1~5)·risk_level 로 구조화
      · 프롬프트: `engine.PROMPT_WELLBEING`(scenario=`wellbeing`/`안부`). 의료조언·개인정보 수집·판매 권유 금지 가드레일 포함. 대본은 `sim_call.SCRIPTS["wellbeing"]`.
      · **판정은 LLM 문장이 아니라 규칙**(`score_answers`): 항목별 키워드 → 1~5, 평균 반올림이 `mood_score`.
        한 항목이라도 1점이거나 위험신호(자해 암시·거동 곤란·결식·어지럼)가 있으면 평균과 무관하게 `high`.
        같은 답변이면 같은 점수가 나오고 근거(`dimensions`)가 남아 담당자가 검증할 수 있다.
- [x] 웹훅 서명(HMAC) — `CALLBACK_SECRET` 환경변수, 미설정 시 서명 생략(로컬)
      · HMAC-SHA256(`<ts>.<body>`), 허용 시차 300초. 본문 변조·키 불일치·시각 이탈 모두 거부(회귀 9건).
      · 덤으로 **SSRF 가드**: 콜백은 https 만(개발 시 `WELLBEING_ALLOW_INSECURE=1`), 루프백·사설·링크로컬·메타데이터 주소 차단, `WELLBEING_CALLBACK_HOSTS` 화이트리스트 지원. 한계(DNS rebinding)는 코드 주석에 명시.
- [x] 개인정보 — 통화 요약에 성명·연락처 미포함. maskPii 경유
      · `transcript_summary` 는 원문 발화가 아니라 4항목 판정 라벨로 조립하고 `monitoring.scrub()` 을 한 번 더 통과.
      · 페이로드·최근목록 어디에도 원문 발화가 실리지 않는다(성명·휴대전화·주민번호 주입 회귀 3건).
      · 전송 실패 사유에도 콜백 URL·토큰이 새지 않는다(예외 타입명만 기록).
- [x] 웹훅 재시도 — 콜백 5xx·타임아웃 시 지수 백오프 재시도와 실패 보관
      · `deliver_with_retry()`. 기본 3회 시도(`WELLBEING_WEBHOOK_RETRIES` 로 1~5 조절), 대기 0.5s→1.0s(상한 4s).
      · **재시도 대상은 타임아웃·연결오류·5xx·429·408 뿐.** 4xx 는 다시 보내도 같은 답이므로 1회로 끝낸다(상대 서버를 두드리지 않는다).
      · 전체 예산 24초를 넘길 것 같으면 재시도를 접고 `gave_up` 사유를 응답·보관 기록에 남긴다(서버리스 30초 한도 안 응답 보장).
      · 최종 실패는 `FAILED`(최근 50건)에 보관하고 `GET ?op=failures` 로 조회. **콜백 호스트만** 남기며 경로·쿼리(토큰)는 기록하지 않는다.
      · 실패는 여전히 502 로 드러난다 — 재시도했다고 200 으로 위장하지 않는다.
- [x] 안부 결과 이력 조회 — `raw_ref` 로 판정 근거를 되짚는 읽기 전용 경로
      · `GET /api/wellbeing?op=result&ref=<raw_ref>` → 페이로드(4항목 점수·라벨·flags)·문항·전송 결과. 없으면 404, ref 누락은 400.
      · 보관은 웹훅으로 이미 나간 페이로드 사본뿐(원문 발화·성명·연락처 없음), 인스턴스 메모리 최근 200건. 영속 보관은 이음 저장소 몫.
- [x] 테스트 — 시뮬레이션 호출 → 웹훅 페이로드 스키마 검증
      · `tests/test_wellbeing.py` **83건**(재시도 10·이력 10 추가). 전체 **399건 통과**.
      · 네트워크 미사용(urlopen 감시로 강제 — 테스트가 과금되지 않는다).
      · 실패 경로 동반: 전송실패 502(삼키지 않음)·입력오류 400·과대본문 413·실발신 501·외부오리진 403.
      · 로컬 E2E(실제 소켓, 외부망 미접속): GET 200 → POST 200(콜백 수신·서명 검증·변조 거부) → 502 → 400 → 501 전부 확인.

- [x] 「안부 전화」 시연 화면 — 콘솔에서 senior_id·프로필을 골라 호출하고 수신 페이로드를 그대로 보여주는 검증 패널
      · `public/admin.html` 좌측 「콜봇 데모 → 안부 전화 연동」(`#wellbeing`). 대상자 ID·응답 프로필·콜백 URL(선택) 입력 → **실제 배포 API** `POST /api/wellbeing/call` 호출.
      · 화면에 위험도·mood_score·응답여부·전송결과·`raw_ref` 칩, 4항목 점수·판정 근거 표, **웹훅 본문 원본 JSON** 을 그대로 노출. 심사관이 전화 없이 흐름을 눈으로 확인한다.
      · 곁들여 「문항·서명 규약 확인」(`GET /api/wellbeing`)·「최근 실행 이력」(`?op=recent`) 버튼.
      · 콜백 URL 은 비우면 발송 없이 페이로드만 생성(dry-run)이 기본. 채워 넣으면 서버가 SSRF 가드를 거쳐 실제 전송한다.
      · **가짜 수치를 추가하지 않았다** — 화면은 서버 응답만 그린다(하드코딩 페이로드 금지 회귀 포함).
      · 품질: 빈 상태 안내·로딩 표시(`role=status`)·오류 표시(`role=alert`)·필드별 인라인 검증(`details[].field/reason` 반영)·`aria-live` 결과 영역·라벨·Enter 키 실행.
        네트워크 실패와 HTTP 4xx/5xx 를 삼키지 않는다(401/403 은 새로고침 안내, 501 은 「[승인 필요]」 표기).
      · 회귀: `tests/test_console_wellbeing.py` **15건**(라우팅·엔드포인트·프로필 목록 드리프트·페이로드 필드 정합·이스케이프·빈/오류 상태·허위수치 금지). 전체 **414건 통과**.
      · 검증: HTMLParser 파싱(신규 오류 0, 기존 1건은 사전 존재분)·`py_compile`·금지어 스캔·로컬 E2E(4개 프로필 200, 입력오류 400, 실발신 501, 사설 콜백 400, 가드 401).

## 남은 항목
- [ ] 실회선 발신 경로 — `api/voice.py` 아웃바운드 + 안부 시나리오 결선 **[승인 필요: CPAAS_LIVE·실발신·실과금]**
      · 코드로 열지 않는다. 승인 전까지 엔드포인트는 `mode=live`·`CPAAS_LIVE=1` 을 501 로 막는다.
