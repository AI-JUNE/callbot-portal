# CallBot 자동 고도화 — 실행 상태

- 실행 시각: 2026-07-15 (callbot-night-auto-dev)
- 상태: ✅ 정상 완료 (변경 있음 — **public/admin.html**)
- 빌드 스탬프: **B91 → B92**
- 이번 회차 항목: **콘솔 표 행 필터/검색 도입** (지난 회차 "다음 실행 후보" 1순위)

## 이번 회차 구현
### public/admin.html — 표 행 필터/검색 (신규)
- B91에서 정렬을 넣었지만, 행이 많은 표는 정렬만으로 원하는 값 탐색이 어려움.
- `tblFilterInit()` + `tfApply()` 추가.
  - 적용 조건: 이미 정렬 적용된 표(`data-ts="1"`, 즉 구조가 안전한 표) **AND 데이터행 6개 이상**.
    → 작은 표엔 검색창이 안 붙어 UI가 지저분해지지 않음. 구조 복잡한 표는 애초에 제외됨.
  - `data-nofilter="1"` 로 개별 표 opt-out 가능.
- 표 위에 검색창(`type=search`) + "지우기" 버튼 + "N / M 행" 카운트 삽입.
  - 행 `textContent` 부분일치(대소문자 무시), 120ms 디바운스.
  - 매칭 안 되는 행은 `display:none` (노드 유지 → 기존 inline 핸들러·정렬과 충돌 없음).
  - 결과 0건이면 "검색 결과가 없습니다." 행 표시.
- 접근성: 검색창 `aria-label`, 카운트 `aria-live="polite"`, **Esc 로 검색어 초기화**, 지우기 버튼 `aria-label`.
- 동적 렌더 표 대응: `show()` 안에서 재호출 + 기존 디바운스 `MutationObserver`(250ms)에 함께 연결 → 재렌더 후에도 필터 재적용.

## 검증
- `node --check` (admin.html / index.html 인라인 script 전체): 모두 OK.
- nav ↔ view ↔ titles 정합: **39 / 39 / 39 완전 일치**, 차집합 0.
- 중복 id: 0 (양쪽 파일)
- 금지어(농협·라피치·IBK·날리지큐브·보이스봇·신세계·하나은행): 0
- 파일 완전성: host Read 로 `</body></html>` 까지 확인 (admin.html 2,184줄).
- 백업: `outputs/bak/admin.pre92.html`
- CPaaS/발신/과금 관련 변경 없음. 커밋·푸시·배포 안 함.

## 사람이 할 일
- 리뷰 후 커밋·배포 (이 태스크는 커밋/푸시/배포하지 않음).
- 브라우저 /admin 에서 육안 확인 권장: 행 많은 표(대화 이력·운영 통계·계정)에 검색창이 뜨는지, 정렬과 같이 써도 깨지지 않는지.
- 미승인 대기: `proposals/api_auth.py`, `proposals/confirm_refund_guard.py`, `ORDER_BACKEND=http` 실연동.

## 다음 실행 후보
- 콘솔: 필터/정렬 상태를 뷰 전환 후에도 유지할지 결정(현재는 재렌더 시 필터는 유지, 정렬은 초기화 — 불일치).
- 콘솔: 필터 적용 중인 표에 CSV 내보내기(보이는 행만) 검토.
- confirm_refund 2단계 확인 가드를 engine.py `_guard` 에 반영 (**[사람 승인 필요]** — 자동 적용 금지).
