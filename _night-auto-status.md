# 야간 자율 개발 상태 (2026-08-19 · 5회차)

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
