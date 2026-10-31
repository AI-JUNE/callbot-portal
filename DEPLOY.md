# Vercel 배포 가이드 (로컬 → SaaS URL)

목표: 로컬이 아니라 `https://<프로젝트>.vercel.app` 처럼 **인터넷 URL**로 누구나 접속.

---

## 0. 준비
- Node.js 설치 (https://nodejs.org, LTS).
- Vercel 계정 (https://vercel.com, 깃허브/구글로 무료 가입).
- 새로 발급한 **Gemini 키** (노출된 키는 폐기).

## 1. Vercel CLI 설치 & 로그인
PowerShell에서:
```powershell
npm i -g vercel
vercel login
```
(이메일/깃허브로 로그인)

## 2. 이 폴더에서 배포
```powershell
cd "callbot-portal 가 풀린 경로"
vercel
```
질문이 나오면 대부분 Enter(기본값):
- Set up and deploy? → **Y**
- Which scope? → 본인 계정
- Link to existing project? → **N**
- Project name? → 엔터(기본) 또는 callbot-portal
- In which directory is your code? → **.**(현재 폴더, 엔터)
- 빌드 설정 자동 감지 → 엔터

→ 끝나면 **프리뷰 URL** 이 출력됩니다.

## 3. API 키를 환경변수로 등록 (중요)
키는 코드에 안 넣고 Vercel에 보관합니다:
```powershell
vercel env add GOOGLE_API_KEY
```
- 값: (새 Gemini 키 붙여넣기)
- 환경: Production, Preview, Development 모두 선택(스페이스로 토글 후 Enter)

(선택) 모델 지정:
```powershell
vercel env add CALLBOT_GEMINI_MODEL
# 값: gemini-2.5-flash
```

## 3-1. (선택) 자체 학습 전화 STT 연결 — **[승인 필요]**
전화망 음성으로 미세조정한 자체 모델(`gowon`)로 전사하려면 아래 변수를 등록합니다.
코드는 이미 들어가 있고 **등록 전에는 어떤 요청도 나가지 않습니다**(미설정이면 거부).

| 변수 | 뜻 | 비고 |
|---|---|---|
| `GOWON_STT_URL` | 자체 STT 서버 주소. `/v1/stt` 까지 적습니다 | **https 만** 허용(평문 http·사설망·클라우드 메타데이터 주소는 가드가 거부) |
| `GOWON_STT_SECRET` | 서버와 나눠 갖는 HMAC 공유 비밀 | 서버의 값과 같아야 합니다. 다르면 401 이고 포털은 설정 오류로 경보를 올립니다 |
| `GOWON_STT_HOSTS` | (선택) 허용 호스트 목록(콤마) | 가장 엄격한 설정입니다. 적어 두면 그 호스트로만 나갑니다 |
| `GOWON_STT_ALLOW_INSECURE` | (선택) `1` 이면 평문 http·사설 주소 허용 | **로컬 개발 전용.** 운영에는 쓰지 않습니다 |
| `GOWON_STT_MODEL_LABEL` | (선택) 리포트에 적을 모델 이름 | 기본값 `large_v6` |

위 변수를 등록해도 **그것만으로는 켜지지 않습니다.** 실제로 전사에 쓰려면
`CALLBOT_STT_PROVIDER=gowon` 과 실호출 게이트 `SPEECH_LIVE=1` 이 함께 필요하고,
둘 다 대표 승인 사항입니다. 상태는 `/api/health` 의 `speech.stt.gowon` 과
관리 콘솔 「시스템 응답 현황」 화면에서 확인합니다(주소·비밀값은 응답에 싣지 않습니다).

서버가 꺼져 있거나 응답하지 않으면 통화를 멈추지 않고 기존 Gemini 전사로 이어 가며,
그 사실을 응답(`fallback`)과 구조화 로그에 남깁니다. 서명 거부(401)와 주소 거부는
설정 오류이므로 대체로 가리지 않고 오류로 드러냅니다.

## 4. 운영 배포
```powershell
vercel --prod
```
→ 출력된 `https://<프로젝트>.vercel.app` 로 접속하면 ECP식 포털이 SaaS로 뜹니다.

---

## 대안: 깃허브 연동 (자동배포)
1. 이 폴더를 GitHub 저장소로 push.
2. vercel.com → **Add New → Project → Import** 해당 저장소.
3. **Settings → Environment Variables** 에 `GOOGLE_API_KEY` 추가.
4. 이후 git push 할 때마다 자동 재배포.

## 자주 막히는 곳
- **흰 화면/대시보드 안 뜸**: 차트는 인터넷 CDN(jsdelivr)에서 받습니다. 사내망 차단 시 그 도메인 허용.
- **api 500 / GOOGLE_API_KEY 없음**: 3번 환경변수 등록 후 `vercel --prod` 다시.
- **마이크 안 됨**: 브라우저 음성은 HTTPS(=vercel.app)에서 동작. Chrome 권장, 사이트 마이크 권한 허용.
- **모델 404**: `CALLBOT_GEMINI_MODEL` 을 본인 키에서 되는 모델명으로.

## 배포 전 로컬 미리보기 (선택)
```powershell
$env:GOOGLE_API_KEY="새 키"
python local_server.py   # → http://localhost:8000
```
