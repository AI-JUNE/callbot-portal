# DESIGN_LOG

## 2026-11-19 문구 정리 (웹 개편 기준서 4절)
- 바꾼 화면: `public/index.html`, `public/lp.html`, `public/console.html` (소개 화면). 색, 레이아웃, 사진은 그대로.
- 전: 「24시간 멈추지 않는」, 「상담의 절반을 AI가」, 처리율 78%, 응답 0.9초, CSAT 4.6, 별점 후기, 「15년 경험」, 화살표와 가운뎃점.
- 후: 업무 장면 중심 문장, 측정하지 않은 수치와 후기 삭제, 목업 KPI는 「샘플」 표기, 「데모」는 「체험」으로 변경.
- 메모: `public/console.html` 파일 끝에 NUL 바이트가 있다(HEAD에도 있던 것, 이번 변경과 무관). 별도 정리 필요.

## 2026-11-25 용어 표준 적용 (문구 용어표준 4절)
- 바꾼 화면: `public/index.html`, `public/lp.html`, `public/console.html`. 색, 레이아웃, 기능 id는 그대로.
- 전: 상담원, 상담원 연결, QA 채점, 「조회해 드릴게요」. 후: 상담사, 상담사 전환, QA 평가, 「바로 조회합니다」.
- 미진행: DS 1~2단계(토큰, 골격). 토큰 원본 `gowon-ds.css`와 `시안.html`이 이 실행의 작업 폴더 밖이라 읽지 못했다. 다음 실행에서 접근 가능할 때 진행.
- 미진행: `admin.html`, `react.html`, `privacy.html`, `terms.html` 문구 대조(다음 묶음).

## 2026-11-26 문구 정리 2차 (admin 제외 묶음)
- 바꾼 화면: `public/terms.html`, `public/privacy.html`, `public/react.html`. 색, 레이아웃, 기능은 그대로.
- 전: 경고 이모지, 가운뎃점 구분, 「대시보드」, 「채팅 데모」. 후: 이모지와 가운뎃점 제거, 「운영 현황」, 「채팅 샘플」, 날짜 2026.07.15 표기.
- 미진행: `admin.html`(금지 후보 다수, 다음 실행), DS 1~2단계(토큰 원본 폴더가 작업 허용 범위 밖이라 읽지 못함). 약관과 개인정보처리방침 본문은 법무 검토 대상이라 손대지 않음.

## 2026-11-24 감리 지적 (디자인 시스템 감리, 직접 고치지 않음)
- 감리 지적: 이 정적 포털 `public/` 은 DS v1.0 토큰(`--gw-*`, #0C66E4) 도입 0건. `admin.html`, `console-theme.css`, `console-pages.css` 가 옛 기준이다. 플랫폼의 콜봇 앱(DS 2단계 2차)과 구분해서 기록할 것.
- 감리 지적: §2 금지 후보 검출은 `admin.html` 97, `index.html` 25, `lp.html` 27, `react.html` 16, `console-theme.css` 12, `console-pages.css` 7, `privacy.html`, `terms.html` 각 2.
- 감리 지적: `public/console.html` 파일 끝 NUL 바이트 아직 정리 전. → 2026-11-24 정리함(367,867B → 35,404B, 본문 변경 없음).

## 2026-11-26 문구 정리 3차 (admin.html)
- 바꾼 화면: public/admin.html. 색, 레이아웃, 기능 id는 그대로.
- 전: 상담원, 내려받기, 데모 데이터, 이모지, 가운뎃점 구분. 후: 상담사, 다운로드, 샘플 데이터, 이모지 제거, 쉼표 구분.
- 정산 CSV 비고는 서버 표식(데모 데이터)과 일치시키려고 그대로 둠. 테스트 기대값 2건(test_escalation_store, test_settlement_lines)은 새 문구로 수정.
- 미진행: DS 1~2단계(토큰 원본 폴더가 작업 범위 밖), console-theme.css와 console-pages.css 의 금지 후보, admin.html 의 줄표와 화살표.
