# CallBot 자동 고도화 — 실행 상태

- 실행 시각: 2026-07-05 (주말 낮 자동 실행, callbot-weekend-dev)
- 상태: ✅ 정상 완료 (변경 있음)
- 이번 회차 개선(접근성):
  - 아이콘 전용 버튼 6개에 aria-label 부여(순수 추가 속성, 시각·동작 변화 없음):
    긍정/부정 피드백(👍👎), 코칭 이전/다음(‹›), 공지 닫기·재생 닫기(✕).
    스크린리더가 아이콘 버튼의 용도를 음성 안내 가능.
- 빌드 스탬프: B69 → B70 (2026-07-05). 푸터 + 콘솔 로그 동기화.
- 검증(전부 통과): node --check(구문 OK), view↔titles 39/39 정합,
  중복 id 없음, 금지어 0, 신규 aria-label 6종 반영 확인.
- 동기화: public/index.html, outputs/tts-upgrade/full/index.html (md5 동일),
  callbot-voicebot-v8.zip 재패키징(무결성 OK, 21파일, index·public/index 모두 B70).
- 참고: 이번엔 zip을 /tmp에서 빌드 후 mount로 cp — 마운트 rename 제약 회피,
  임시 파일 잔존 없음(이전 회차 _v8_new.zip 이슈 해소).
- 다음 실행: 정상 계속.
