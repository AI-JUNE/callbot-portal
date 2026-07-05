# CallBot 자동 고도화 — 실행 상태

- 실행 시각: 2026-07-05 (야간 자동 실행, callbot-night-auto-dev)
- 상태: ✅ 정상 완료 (변경 있음)
- 이번 회차 개선(접근성):
  - 데이터 필터 검색 input 5종에 aria-label 부여(순수 속성 추가, 시각·동작 변화 없음):
    reasonQ(상담 사유코드 검색), histQ(대화 이력 검색), kbq(지식 문서 검색),
    logQ(접속·녹취 로그 검색), mmQ(멀티모달 이력 검색).
    스크린리더가 각 검색창 용도를 음성 안내 가능.
- 빌드 스탬프: B70 → B71 (2026-07-05). 푸터 + 콘솔 로그 동기화.
- 검증(전부 통과): node --check(구문 OK), titles↔view 39/39 정합,
  중복 id 없음, 금지어 0, 신규 aria-label 5종 반영 확인.
- 동기화: public/index.html(md5 동일), outputs/tts-upgrade/full/index.html(md5 동일, 이번 세션 outputs 신규 생성),
  callbot-voicebot-v8.zip 재패키징(무결성 OK, 21파일, index·public/index 모두 B71).
- 참고: zip은 /tmp에서 빌드 후 mount로 cp(rename 제약 회피). 
  /tmp의 이전 잔존 callbot-voicebot-v8.zip(타 소유자, 삭제 불가)은 무해 — 새 빌드는 별도 파일명으로 생성 후 배포 대상에 덮어씀.
- 다음 실행: 정상 계속.
