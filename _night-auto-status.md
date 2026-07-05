# CallBot 자동 고도화 — 실행 상태

- 실행 시각: 2026-07-05 (주말 낮 자동 실행, callbot-weekend-dev)
- 상태: ✅ 정상 완료 (변경 있음)
- 이번 회차 개선(접근성):
  - 사이드바 내 8개 `<nav>` 랜드마크에 aria-label 부여
    (즐겨찾기/콜봇 데모/콜봇 플랫폼/지식·운영/콜봇 통합통계/운영 관리/멀티모달/상담지원 AI).
    스크린리더가 중복 "navigation" 랜드마크를 구분 가능. 시각·동작 변화 없음(순수 추가 속성).
- 빌드 스탬프: B68 → B69 (2026-07-05). 푸터 + 콘솔 로그(구 B63) 동기화.
- 검증(전부 통과): node --check(구문 OK), nav↔view↔titles 39/39/39 정합,
  중복 id 없음, 금지어 0.
- 동기화: public/index.html, outputs/tts-upgrade/full/index.html (md5 동일),
  callbot-voicebot-v8.zip 재패키징(무결성 OK, 21파일).
- 주의(경미): outputs/_v8_new.zip 임시 파일이 마운트 삭제 불가로 잔존
  (최종 zip과 동일 내용, 무해). 다음 회차엔 임시 파일을 /tmp에 생성 예정.
- 다음 실행: 정상 계속.
