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
