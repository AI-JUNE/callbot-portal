# Callbot SaaS 포털 (Vercel 배포용)

ECP 같은 **웹 포털 형식**의 콜봇 테스트 콘솔. 로컬이 아니라 **Vercel에 올려 URL로** 누구나 접속해 테스트.

## 로컬 / Vercel 차이 (왜 이 구조인가)
- **키 보안**: API 키를 코드/화면에 안 넣고 **Vercel 환경변수(서버)** 에만 둠 → 노출 위험 제거.
- **무상태 서버**: serverless라 대화 상태를 서버가 안 들고, **브라우저가 대화기록을 보관**해 매 요청에 보냄.
- **음성**: 서버에서 무거운 STT/TTS(whisper/edge-tts)를 못 돌리므로 **브라우저 내장 음성(Web Speech API)** 사용 → 설치 0, 무료. (Chrome 권장)

## 구조
```
callbot-portal/
├─ api/chat.py       # Vercel Python 서버리스 (POST /api/chat)
├─ api/engine.py     # 무상태 엔진: Gemini REST + 툴콜 + 가드레일 + 모의백엔드 (stdlib만)
├─ public/index.html # 포털 프론트(사이드바·KPI·채팅·토큰비용·로그·브라우저음성)
├─ vercel.json
├─ requirements.txt  # (비어있음 — 의존성 없음)
└─ local_server.py   # 배포 전 로컬 확인용
```

## 1) 로컬에서 먼저 확인 (선택)
```powershell
$env:GOOGLE_API_KEY = "발급한_Gemini_키"
$env:CALLBOT_GEMINI_MODEL = "gemini-2.5-flash"
python local_server.py
# → http://localhost:8000
```

## 2) Vercel 배포 (URL 생성)
방법 A — CLI:
```bash
npm i -g vercel
cd callbot-portal
vercel            # 첫 배포(프리뷰)
vercel --prod     # 운영 URL
```
방법 B — GitHub: 이 폴더를 깃 저장소로 올리고 vercel.com에서 Import.

배포 중/후 **Project → Settings → Environment Variables** 에 추가:
- `GOOGLE_API_KEY` = (Gemini 키)
- `CALLBOT_GEMINI_MODEL` = `gemini-2.5-flash` (선택)

설정 후 재배포하면 `https://<프로젝트>.vercel.app` 으로 어디서나 접속해 테스트.

## 테스트
"방금 받은 거에 콜라가 안 왔어요" → "네 환불해주세요" → "네".
🎤 버튼으로 말해도 되고, 봇 답은 음성으로 나옵니다(브라우저 음성).

## 주의
- 무료 Web Speech는 브라우저(특히 Chrome) 의존 — 일부 환경/HTTPS에서만 마이크 허용.
- `api/engine.py`의 모의 백엔드를 실제 주문/결제 API로 바꾸면 진짜 데이터로 동작.
- 모델 라우팅·토큰예산 등 고급 기능은 로컬 풀버전(callbot-poc)에 더 있음.
