"""실시간 어시스트 API — 상담 요약 / TA(텍스트분석) / QA(품질평가) / KMS(RAG).
Gemini(engine._call) 재사용. JSON 강제 출력."""
import os, sys, json, re
from http.server import BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(__file__))
from engine import _call

MODEL = os.environ.get("CALLBOT_GEMINI_MODEL", "gemini-2.5-flash")

PROMPTS = {
 "summary": '다음 상담 대화를 요약하라. JSON만 출력: {"summary":"3줄 이내 요약","points":["핵심1","핵심2"],"action":"다음 조치"}',
 "ta": '다음 상담을 텍스트 분석하라. JSON만: {"emotion":"긍정|중립|불만","keywords":["키워드"],"intent":"고객 의도","compliance":"준수|미흡"}',
 "qa": '다음 상담을 품질평가(100점)하라. JSON만: {"score":0,"items":[{"name":"친절도","score":0},{"name":"정확성","score":0},{"name":"절차준수","score":0},{"name":"정보보안","score":0}],"comment":"한줄 코멘트"}',
}


def run_assist(task, text, kb=""):
    if task == "kms":
        prompt = ('다음 지식만 근거로 질문에 간결히 답하고 근거 문서 제목을 알려라. '
                  'JSON만: {"answer":"답변","source":"근거 문서명"}\n[지식]\n' + (kb or "") + "\n[질문] " + text)
    else:
        prompt = PROMPTS[task] + "\n\n[대화]\n" + text
    payload = {
        "systemInstruction": {"parts": [{"text": "너는 상담 분석 도우미다. 반드시 유효한 JSON만 출력한다. 한국어."}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 400, "responseMimeType": "application/json"},
    }
    resp = _call(MODEL, payload)
    parts = (resp.get("candidates") or [{}])[0].get("content", {}).get("parts", []) or []
    txt = "".join(p.get("text", "") for p in parts).strip()
    try:
        return json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {"raw": txt}


import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard

class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        d = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.end_headers()
        self.wfile.write(d)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Webhook-Token")
        self.end_headers()

    def do_GET(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        self._send(200, {"ok": True, "tasks": ["summary", "ta", "qa", "kms"]})

    def do_POST(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        try:
            n = int(self.headers.get("content-length", 0))
            b = json.loads(self.rfile.read(n) or "{}")
            task = b.get("task", "summary")
            out = run_assist(task, b.get("text", ""), b.get("kb", ""))
            self._send(200, {"task": task, "result": out})
        except Exception as e:
            self._send(500, {"error": str(e)})
