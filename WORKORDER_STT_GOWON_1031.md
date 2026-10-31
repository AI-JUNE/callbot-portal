# 작업지시서: 자체 학습 전화 STT(GOWON)를 AICC 포털 음성 인식에 연결 (2026-10-31, 대표 요청)

> 대표 요청 원문: 「AICC 포털 STT 음성인식에 지금까지 작업한 것들 반영해줘」
> 이 지시서의 [ ] 를 위에서부터 처리하고 끝나면 [x] 로 바꾼다. 항목마다 pytest 전체 + `python scripts/verify.py` 통과 후 커밋.
> 원칙은 그대로: build now, activate on approval. 실호출은 `SPEECH_LIVE=1` + 설정이 모두 있을 때만.

## 배경 (무엇을 연결하나)

창업진흥원 지원 GPU(B200 MIG)에서 AI Hub 「저음질 전화망 음성인식」 데이터로 Whisper 를 LoRA 미세조정했다.
근거 문서: `D:\Desktop\GOWON\Dev\98. GPU\STT_phone\STT_학습결과_정리_1031.md`

| 모델 | 글자 오류율(CER) | 문장당 처리(p50 / p95) |
|---|---|---|
| Whisper turbo 학습 전 | 6.83% | — |
| **GOWON large v6 (권장)** | **5.59%** | 0.18초 / 0.48초 |
| GOWON turbo v4 (경량) | 6.02% | 0.08초 / 0.15초 |

측정 조건: AI Hub 전화망 시험 문장 2,000개(4개 분야 각 500), 띄어쓰기·문장부호 제외 CER, B200 MIG 1g.23gb, 한 문장씩 처리. 네트워크·발화 끝 감지 시간 제외.

운영 구조(대표 결정, 10-31): **모델은 로컬 PC 에 두고 GPU 없이(CPU, faster-whisper) 추론한다. GPU 는 재학습할 때만 임시로 빌린다.** 상세 `D:\Desktop\GOWON\Dev\98. GPU\STT_운영구조.md`.
그래서 STT 서버는 꺼져 있을 수 있다(노트북 종료 등) — **대체 경로(레거시 Gemini 전사)는 선택이 아니라 필수**다. 서버 불가 시 통화가 멈추지 않아야 한다.

모델 서버: `D:\Desktop\GOWON\Dev\98. GPU\STT_phone\serve_stt.py` (이미 작성됨, 저장소 밖. faster-whisper, 기본 CPU)
- `POST /v1/stt` 본문 `{"audio": "<base64>", "mime": "...", "lang": "ko"}` → `{"ok": true, "text": "...", "model": "large_v6", "ms": 180}`
- 헤더 `X-Timestamp`, `X-Signature = hex(HMAC-SHA256(secret, ts + "\n" + body))` — `_vstudio.sign` 과 **같은 함수**를 쓴다
- `GET /v1/health` → `{"ok": true, "model": "...", "gpu": "..."}`
- 오류: 401 signature, 400 bad_audio, 413 body_size/audio_too_long, 500 transcribe_failed

## 할 일

- [x] **`_speech_providers.py` 에 `GowonSTT`(name `gowon`) 추가, `_STT` 등록**
  - 설정: `GOWON_STT_URL`(서버 주소, `/v1/stt` 까지), `GOWON_STT_SECRET`(HMAC), 선택 `GOWON_STT_HOSTS`(허용 호스트), `GOWON_STT_ALLOW_INSECURE=1`(로컬 개발 전용)
  - 게이트: `SPEECH_LIVE=1` 이고 URL·SECRET 이 모두 있을 때만 실호출. 하나라도 없으면 지금 골격들과 같은 `PermissionError("[승인 필요] gowon …")` — `tests/test_speech_gate.py` 의 「등록된 골격 전부가 거부한다」가 미설정 상태에서 그대로 통과해야 한다.
  - 아웃바운드는 `_vstudio` 와 같은 수준: `_urlguard.check(label="GOWON_STT_URL")`, **리다이렉트 따라가지 않기**, 타임아웃 10초, 응답 상한 64KiB, 비밀값·발화 원문을 로그·응답·예외 문구에 싣지 않기.
  - 반환 모양은 기존 계약(`{"text", "provider", "sim", "lang"}`)에 `model`, `ms` 를 더한다.
  - 실패 분류: 타임아웃 504, 서버 5xx·깨진 JSON 502, 401 은 설정 오류(운영 경보). 실패 시 **레거시 Gemini 전사로 대체할지**는 QUALITY_BAR 의 폴백 원칙에 맞춰 결정하고 `docs`/DECISIONS 에 적는다(권장: Gemini 키가 있으면 대체하고 응답에 `fallback: true`, 구조화 로그에 사유 코드).
  - 결정(10-31): 권장안대로 **대체한다.** 서버에 닿지 못했거나 서버가 앓는 경우(`timeout`·`offline`·`upstream`·`bad_response`)만 Gemini 키가 있을 때 대체 전사하고, 응답에 `fallback: true`·`fallback_reason`·실제 전사한 모델 이름을 싣는다. **설정 오류는 대체로 가리지 않는다**(서명 거부 401·주소 거부) — 가리면 아무도 고치지 않는다. 근거·사유 코드는 `COMMERCIAL_READINESS.md` 「자체 학습 전화 STT」 절에 적었다.
- [x] **`scripts/verify.py` 등록부 갱신**: `_speech_providers.py` 를 `OUTBOUND`(사유: 자체 STT 서버 — GOWON_STT_URL, _urlguard 필수)와 `URLGUARD_REQUIRED` 에 추가. 게이트 표(SPEECH_LIVE) 규약 유지.
- [x] **health 반영**: `_kind_health` / `health.py _speech()` 에서 gowon 이 「설정됨 + 게이트 ON」이면 `ready`, 아니면 `pending_approval`. 리포트에 모델 이름(`GOWON_STT_MODEL_LABEL`, 기본 `large_v6`)만 싣고 주소·비밀값은 싣지 않는다(`test_report_carries_no_secret` 유지). 위 테스트 docstring 의 「모든 비-sim 은 pending」 문장을 「설정이 없으면 pending」으로 정확히 고친다.
- [x] **테스트 `tests/test_gowon_stt.py`** (네트워크 0, urlopen 대역): 미설정 거부 · 게이트 OFF 면 sim · 정상 전사 · 서명 헤더가 `_vstudio.sign` 과 같은 값 · http/사설 IP/메타데이터 주소 거부 · 리다이렉트 미추종 · 401/500/타임아웃/깨진 JSON 분류 · 응답 상한 · 발화와 비밀값이 로그에 없음 · 폴백 경로.
- [x] **관리 콘솔(admin.html) 표기** (「시스템 응답 현황」 화면 맨 위 카드. 배지·안내 문장은 `/api/stt` 응답만 그린다): STT 엔진을 고르는 자리(서비스 설정 또는 시스템 응답현황)에 「GOWON 전화 STT (자체 학습)」를 추가하고, 위 표의 수치를 **측정 조건과 함께** 적는다. 설정 전에는 「승인 후 활성화」 배지. 문구 기준: 가운뎃점·대시·화살표 축약 금지, 사람이 쓴 전문 문장. 「타사보다 정확」 같은 비교 표현 금지(비교 측정을 하지 않았다).
- [x] **문서**: `COMMERCIAL_READINESS.md` 에 항목 추가(코드 완료 / 활성화는 [승인 필요]), 환경변수 목록이 있는 문서(DEPLOY 등)에 네 변수 추가, `_night-auto-status.md` 기록. (DEPLOY.md 3-1 절 · 43차 기록)

## 하지 않는 것

- Vercel 환경변수 등록, `SPEECH_LIVE` 켜기, GPU 서버 공개 — 대표 승인 사항(아래).
- 랜딩(index.html) 공개 수치 게재 — 대표가 문구를 정한 뒤에.

## 대표 승인 대기 (자동개발이 하지 않는다)

1. 로컬 STT 서버를 외부에 여는 방법: Cloudflare Tunnel 같은 터널(https 주소) 또는 상시 CPU 서버. NHN GPU 는 쓰지 않는다(영구 자원 아님).
2. 결정 후 Vercel 에 `GOWON_STT_URL`, `GOWON_STT_SECRET`, `CALLBOT_STT_PROVIDER=gowon`, `SPEECH_LIVE=1` 등록.
