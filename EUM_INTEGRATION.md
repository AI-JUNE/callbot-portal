# 이음 2R 연동 과제 — 안부 콜봇 (가이드 §6-1)

이음이 쓰는 시나리오는 **「안부」 1개**뿐. 콘솔 메뉴는 이음과 무관하며 심사에 노출하지 않는다.

## 흐름
담당자 현황판 「안부 전화」 → `POST /api/wellbeing/call {senior_id, callback_url}` → 기존 `api/voice.py` (시나리오=안부) → 통화 종료 시 `callback_url` 로 웹훅 POST:
```json
{ "senior_id": "...", "answered": true, "mood_score": 1~5, "risk_level": "low|mid|high", "transcript_summary": "...", "raw_ref": "..." }
```
- risk_level=high 또는 2회 연속 미응답 → 이음이 follow_ups 생성(이음 쪽 책임)
- **실전화 없이 동작** — 「웹에서 바로 테스트」 경로(전화망 미경유)로 시연. 실회선은 3R.

## 과제
- [ ] `api/wellbeing.py` — 안부 시나리오 전용 엔드포인트. 시뮬레이션 모드에서 30초 내 결과 웹훅 발송
- [ ] 안부 시나리오 프롬프트 — 기분·식사·수면·통증 4문항, 결과를 mood_score(1~5)·risk_level 로 구조화
- [ ] 웹훅 서명(HMAC) — `CALLBACK_SECRET` 환경변수, 미설정 시 서명 생략(로컬)
- [ ] 개인정보 — 통화 요약에 성명·연락처 미포함. maskPii 경유
- [ ] 테스트 — 시뮬레이션 호출 → 웹훅 페이로드 스키마 검증
