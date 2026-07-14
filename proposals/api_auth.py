"""
[사람 승인 필요 — 미적용 제안]
api/ 엔드포인트 공통 인증 가드.

현재 api/*.py (assist, chat, engine, sim_call, stt, tts, voice) 의 handler 는
어떤 인증도 없이 do_GET/do_POST 를 처리한다. 공개 배포 시 누구나 호출 가능하며
STT/TTS/LLM 처럼 과금이 발생하는 업스트림을 그대로 태울 수 있다.

이 모듈은 그 구멍을 막기 위한 최소 가드다. 아직 어떤 핸들러에도 연결하지 않았다.
활성화하려면 사람이 (1) 환경변수 PORTAL_API_KEY 설정 (2) 각 핸들러에 3줄 추가
(3) 프런트(public/admin.html)에서 헤더 동봉 — 세 가지를 직접 승인·수행해야 한다.

--- 적용 방법 (승인 시) ---
  from _auth import require_key            # api/_auth.py 로 배치했을 때
  class handler(BaseHTTPRequestHandler):
      def do_POST(self):
          if not require_key(self): return  # 401 응답까지 여기서 끝냄
          ...기존 로직...

--- 정책 ---
  * PORTAL_API_KEY 미설정 = 개발 모드로 간주하고 통과(현행 동작 유지).
    운영에서는 반드시 설정할 것. 미설정 시 응답 헤더에 경고를 남긴다.
  * 키 비교는 hmac.compare_digest 로 타이밍 공격 방지.
  * 키는 Authorization: Bearer <key> 또는 X-Portal-Key 헤더로 받는다.
    쿼리스트링(?key=)은 로그·리퍼러에 남으므로 받지 않는다.
  * 브라우저 프리플라이트(OPTIONS)는 통과시킨다.
  * 실패는 이유를 노출하지 않고 401 + 최소 정보만.

--- 남은 한계 (인증만으로는 못 막음) ---
  * 단일 공유키는 프런트에 넣는 순간 사실상 공개된다. 콘솔이 브라우저에서
    직접 api/ 를 호출하는 구조라면, 공유키가 아니라 세션 로그인 + 서버측
    프록시 또는 짧은 수명의 서명 토큰이 정공법이다. 이 파일은 그 전 단계의
    최소 방어선(봇·무작위 스캐너 차단)으로만 쓸 것.
  * 레이트리밋 없음. 과금 폭주 방어는 별도 필요(아래 스텁 참고).
"""
import os
import hmac
import json


def _client_key(handler):
    """요청에서 키를 뽑는다. 없으면 None."""
    h = handler.headers
    auth = h.get('Authorization') or ''
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return (h.get('X-Portal-Key') or '').strip() or None


def _deny(handler):
    body = json.dumps({'error': 'unauthorized'}).encode('utf-8')
    handler.send_response(401)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('WWW-Authenticate', 'Bearer realm="callbot-api"')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def require_key(handler):
    """
    통과하면 True. 막았으면 401 응답을 이미 보냈고 False.
    호출부는 False 일 때 즉시 return 해야 한다.
    """
    if handler.command == 'OPTIONS':
        return True

    expected = os.environ.get('PORTAL_API_KEY', '').strip()
    if not expected:
        # 개발 모드: 현행 동작 유지. 운영 배포 전 반드시 키를 설정할 것.
        return True

    got = _client_key(handler)
    if not got:
        _deny(handler)
        return False
    if not hmac.compare_digest(got, expected):
        _deny(handler)
        return False
    return True


# --- 레이트리밋 스텁 (미구현 · 별도 승인 필요) ---------------------------
# 서버리스(Vercel)는 인스턴스가 살아있지 않으므로 프로세스 메모리 카운터는
# 신뢰할 수 없다. 실제로 하려면 외부 저장소(Upstash Redis 등)가 필요하다.
# def check_rate(handler, limit_per_min=60): ...
