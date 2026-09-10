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

# 시나리오 화이트리스트 — engine.run_turn 의 분기와 같은 목록을 유지한다.
# (드리프트는 tests/test_chat_assist.py 가 감시: 각 값이 서로 다른 프롬프트를 고르는지 확인)
SCENARIOS = ("refund", "integrity", "overdue", "welfare", "trio", "wellbeing", "안부")

# 메시지 role 화이트리스트 — engine._to_contents 가 해석하는 값만 받는다.
ROLES = ("user", "assistant", "model", "system", "tool")

MAX_CONTENT = 8000      # 항목당 본문 길이 상한(100건 × 8000자 < 본문 1MiB 상한)
MAX_TOOL_CALLS = 20


def validate_messages(msgs):
    """대화 이력 항목을 검증한다.

    이 검증이 없으면 잘못된 항목(예: content 없는 user)이 engine 안에서
    KeyError/TypeError 로 터져 **사용자 입력 오류가 500 으로 보고**된다.
    (모니터링에도 내부 오류로 잡혀 알림 노이즈가 된다.)
    실패는 어느 항목이 왜 틀렸는지 details[].field 로 돌려준다.
    """
    def bad(i, key, reason):
        return _errors.ValidationError.field("messages[%d].%s" % (i, key), reason)

    for i, m in enumerate(msgs):
        role = m.get("role")
        if role is None:
            raise bad(i, "role", "필수 항목입니다")
        if not isinstance(role, str):
            raise bad(i, "role", "문자열이어야 합니다")
        role = role.strip()
        if role not in ROLES:
            raise bad(i, "role", "허용값: %s" % ", ".join(ROLES))
        c = m.get("content")
        if role == "user" and c is None:
            raise bad(i, "content", "필수 항목입니다")
        if c is not None:
            if not isinstance(c, str):
                raise bad(i, "content", "문자열이어야 합니다")
            if len(c) > MAX_CONTENT:
                raise bad(i, "content", "최대 %d자" % MAX_CONTENT)
        if role == "tool":
            n = m.get("name")
            if not isinstance(n, str) or not n.strip():
                raise bad(i, "name", "tool 항목은 이름이 필요합니다")
        tcs = m.get("tool_calls")
        if tcs is not None:
            if not isinstance(tcs, list):
                raise bad(i, "tool_calls", "배열이어야 합니다")
            if len(tcs) > MAX_TOOL_CALLS:
                raise bad(i, "tool_calls", "최대 %d개" % MAX_TOOL_CALLS)
            for j, tc in enumerate(tcs):
                if not isinstance(tc, dict) or not isinstance(tc.get("name"), str):
                    raise bad(i, "tool_calls[%d].name" % j, "형식이 올바르지 않습니다")
    return msgs

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
        self._send(200,{"ok":True,"google_key_present":_key(),
                        "model":os.environ.get("CALLBOT_GEMINI_MODEL","gemini-2.5-flash"),
                        "scenarios":list(SCENARIOS)},rq)
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
            validate_messages(msgs)
            phone = _errors.as_str(body, "phone", default="01012345678", max_len=32)
            # 시나리오는 화이트리스트. 과거엔 이 값을 받고도 engine 에 넘기지 않아
            # 안부·연체 등 대본이 항상 기본(주문/환불) 프롬프트로 응답했다.
            scenario = _errors.as_choice(body, "scenario", SCENARIOS, default="refund")
            # 건수·시나리오만 기록 — 대화 내용·전화번호는 로그에 남기지 않는다
            rq.set(msg_count=len(msgs), scenario=scenario)
            self._send(200, run_turn(msgs, phone, scenario=scenario), rq)
            rq.finish(200)
        except Exception as e:
            # 표준 에러 봉투 + 모니터링(5xx만) + 구조화 로그를 한 번에 처리
            _errors.handle(self, e, route="/api/chat", method="POST", rq=rq)
