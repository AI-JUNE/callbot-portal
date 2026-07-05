# CallBot 자동 고도화 — 실행 상태

- 실행 시각: 2026-07-06 (야간 자동 실행, callbot-night-auto-dev)
- 상태: ✅ 정상 완료 (변경 있음)
- 이번 회차 개선(접근성):
  - 검색 성격 input 4종에 aria-label 부여(순수 속성 추가, 시각·동작 변화 없음):
    scriptQ(스크립트 검색), kmsq3(지식 검색), cmdIn(화면·기능 검색),
    세션ID·번호 검색(고객 통화 이력 세션 검색, id 없던 입력).
    스크린리더가 각 검색창 용도를 음성 안내 가능. aria-label 총 32→36.
- 빌드 스탬프: B71 → B72 (2026-07-06). 푸터 + 콘솔 로그(BUILD) 동기화.
- 검증(전부 통과): node --check(구문 OK), titles↔view 39/39 정합,
  중복 id 없음, 금지어 0, 신규 aria-label 4종 반영 확인.
- 동기화: public/index.html(md5 동일), outputs/tts-upgrade/full/index.html(md5 동일),
  callbot-voicebot-v8.zip 재패키징(무결성 OK, 21항목=파일19+디렉터리2, index·public/index 모두 B72, 금지어 0).
- 참고: zip은 mktemp 샌드박스 로컬에서 빌드 후 mount로 cp(mount에서 zip 직접 생성 시 I/O 제약 있어 우회). CPaaS는 sim/dry-run 유지, git 미실행(배포는 작업 스케줄러 담당).
- 다음 실행: 정상 계속. 남은 저위험 후보: placeholder만 있고 aria-label 없는 입력 14종(폼 입력류) 추가 라벨링.
