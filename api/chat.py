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
import _errors
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
            return _guard.deny(self, _c, _m, rq)
        self._send(200,{"ok":True,"google_key_present":_key(),"model":os.environ.get("CALLBOT_GEMINI_MODEL","gemini-2.5-flash")},rq)
        rq.finish(200)
    def do_POST(self):
        rq = _log.begin(self.headers, "/api/chat", "POST", self.path)
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            rq.finish(_c, denied=True)
            return _guard.deny(self, _c, _m, rq)
        try:
            # 입력검증: 본문 상한·타입·길이. 위반은 400(details 포함)으로 즉시 거부.
            body = _errors.read_json(self)
            msgs = _errors.as_list(body, "messages", required=True, max_items=100, item_type=dict)
            if not msgs:
                raise _errors.ValidationError.field("messages", "최소 1개 필요합니다")
            phone = _errors.as_str(body, "phone", default="01012345678", max_len=32)
            # 건수만 기록 — 대화 내용·전화번호는 로그에 남기지 않는다
            rq.set(msg_count=len(msgs))
            self._send(200, run_turn(msgs, phone), rq)
            rq.finish(200)
        except Exception as e:
            # 표준 에러 봉투 + 모니터링(5xx만) + 구조화 로그를 한 번에 처리
            _errors.handle(self, e, route="/api/chat", method="POST", rq=rq)
