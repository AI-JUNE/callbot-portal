# 야간 자율 개발 상태 (2026-08-31 · 8회차)

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
