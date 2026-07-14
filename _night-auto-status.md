# CallBot 자동 고도화 — 실행 상태

- 실행 시각: 2026-07-14 (callbot-night-auto-dev)
- 상태: ✅ 정상 완료 (변경 있음 — **public/admin.html** 콘솔)
- 빌드 스탬프: **B89 → B90**
- 이번 회차 항목: **사이드 내비 키보드 접근성 복구 (키보드만으로 화면 전환 불가 버그)**

## 이번 회차 구현 (public/admin.html)
1. **[버그] 사이드 내비가 키보드로 조작 불가** — 좌측 메뉴 39개 항목이 `<a data-v="…">` 형태인데
   `href`·`tabindex`·`role`이 전혀 없음 → 브라우저 기본 규칙상 **탭 포커스 대상이 아님**.
   즉 마우스 없이는 콘솔의 어떤 화면으로도 이동할 수 없었음(전체 내비게이션 차단 수준의 a11y 결함).
   - `navA11y()` 추가: `href` 없는 `.nav a` 에 `role="link"` + `tabindex="0"` 부여.
   - 위임 keydown 핸들러: `.nav a` 에 포커스된 상태에서 **Enter / Space** 로 활성화(스크롤 방지 포함).
   - 즐겨찾기 내비(`#favNav`)는 `renderFavs()` 로 동적 재생성되므로 `show()` 안에서 `navA11y()` 재적용.
2. 포커스 링은 기존 전역 `[tabindex]:focus-visible` 규칙이 이미 커버 → CSS 추가 없음.
3. 기존 role="button" 전용 keydown 핸들러는 그대로 유지(간섭 없음).

## 검증
- `node --check` (인라인 script 추출 1개): OK.
- nav ↔ view ↔ titles 정합: 39개 완전 일치.
- 중복 id 0, 금지어(농협·라피치·IBK·날리지큐브·보이스봇·신세계·하나은행) 0.
- 파일 완전성: host Read 로 `</body>` / `</html>` 까지 확인 (336,597 bytes).
- 백업: `outputs/bak/admin.pre.html`.

## 사람이 할 일
- 리뷰 후 커밋·배포 (이 태스크는 커밋/푸시하지 않음).
- 미승인 대기: `proposals/api_auth.py`, `proposals/confirm_refund_guard.py`, `ORDER_BACKEND=http` 실연동.

## 다음 실행 후보
- 콘솔: 테이블에 정렬 기능이 아예 없음(aria-sort 이슈 아님) — 대화 이력/운영 통계 표에 정렬 도입 검토.
- 랜딩: 후기(테스티모니얼)가 예시임을 명시하는 캡션 추가(통계엔 ※ 주석 있으나 후기엔 없음).
- confirm_refund 2단계 확인 가드를 engine.py `_guard` 에 반영 (**사람 승인 필요** — 자동 적용 금지).
