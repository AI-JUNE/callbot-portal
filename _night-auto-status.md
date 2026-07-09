# CallBot 자동 고도화 — 실행 상태

- 실행 시각: 2026-07-09 (야간 자동 실행, callbot-night-auto-dev)
- 상태: ✅ 정상 완료 (변경 있음)
- 이번 회차 개선(접근성 — 아이콘 전용 버튼 aria-label 보강):
  1) 아이콘(✕) 전용 삭제 버튼 7종(class="ndel")에 aria-label="삭제" 부여.
     대상: rowDel×4, kpiDel, bDelNode, bDelRow. → 스크린리더에서 '삭제'로 읽힘.
  2) 숨김 파일 입력(#kbFile, KMS 엑셀/CSV 업로드)에 aria-label="지식 파일 업로드" 부여.
  · 순수 속성 추가만, 시각·동작 변화 없음(치환 8건, bash+python). 빌드 스탬프 B77 유지.
- 검증(전부 통과): node --check(스크립트 블록 OK),
  nav↔titles 39/39 정합(고아 0), 중복 정적 id 0, 금지어 0, NUL 0,
  파일 끝(</body></html>) host Read 확인 정상.
- 동기화: public/index.html 를 root 와 md5 동일(5d6bb559...)로 맞춤.
- 미실행: git commit/push/deploy 없음(배포는 사람 승인). CPaaS sim/dry-run 유지.
- ⚠️ 자동배포 중지 권고: auto-deploy.bat(git add/commit/push origin main) 여전히 존재
  → Windows CallbotAutoDeploy 작업 스케줄러 중지 권장(사람 리뷰 전 자동 푸시 방지).
- 다음 실행 후보: (a) engine.py 하드코딩 데모데이터 → 연동 인터페이스 추상화[사람 승인],
  (b) confirm_refund 2단계 확인·감사로그[사람 승인], (c) api/ 엔드포인트 인증[사람 승인].
  저위험: tabindex/포커스 순서 점검, 대비(contrast) 점검, 빈 title 속성 정리.
