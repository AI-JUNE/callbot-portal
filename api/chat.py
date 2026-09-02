import os, sys, json
from http.server import BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(__file__))
from engine import run_turn

def _key():
    return bool((os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip())

import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard
import _log
import monitoring

class handler(BaseHTTPRequestHandler):
    # 기본 접근로그는 쿼리스트링(PII 가능)을 그대로 찍으므로 침묵 — 구조화 로그가 대체
    log_message = _log.suppress_access_log

    def _send(self,code,obj,rq=None):
        d=json.dumps(obj,ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("Content-Type","application/json; charset=utf-8")
        if rq is not None: _log.attach(self, rq)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers)); self.end_headers(); self.wfile.write(d)
    def do_OPTIONS(self):
        self.send_response(204); self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Webhook-Token")
        self.send_header("Access-Control-Expose-Headers", "X-Request-Id"); self.end_headers()
    def do_GET(self):
        rq = _log.begin(self.headers, "/api/chat", "GET", self.path)
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            rq.finish(_c, denied=True)
            return _guard.deny(self, _c, _m)
        self._send(200,{"ok":True,"google_key_present":_key(),"model":os.environ.get("CALLBOT_GEMINI_MODEL","gemini-2.5-flash")},rq)
        rq.finish(200)
    def do_POST(self):
        rq = _log.begin(self.headers, "/api/chat", "POST", self.path)
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            rq.finish(_c, denied=True)
            return _guard.deny(self, _c, _m)
        try:
            n=int(self.headers.get("content-length",0)); body=json.loads(self.rfile.read(n) or "{}")
            msgs = body.get("messages",[])
            # 건수만 기록 — 대화 내용·전화번호는 로그에 남기지 않는다
            rq.set(msg_count=len(msgs) if isinstance(msgs,list) else 0)
            self._send(200, run_turn(msgs, body.get("phone","01012345678")), rq)
            rq.finish(200)
        except Exception as e:
            # 오류 모니터링: DSN 미설정이면 no-op, 전송 실패해도 응답에 영향 없음
            _eid = monitoring.capture_error(e, route="/api/chat", method="POST", request_id=rq.request_id)
            rq.fail(e, 500, event_id=_eid)
            _out = {"error": str(e), "request_id": rq.request_id}
            if _eid:
                _out["event_id"] = _eid
            self._send(500, _out, rq)
