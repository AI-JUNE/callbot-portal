"""실시간 어시스트 API — 상담 요약 / TA(텍스트분석) / QA(품질평가) / KMS(RAG).
Gemini(engine._call) 재사용. JSON 강제 출력."""
import os, sys, json, re
from http.server import BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(__file__))
from engine import _call

MODEL = os.environ.get("CALLBOT_GEMINI_MODEL", "gemini-2.5-flash")

# 지원 태스크 화이트리스트 — 입력검증과 GET 응답이 같은 출처를 쓴다
TASKS = ("summary", "ta", "qa", "kms")

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
    return parse_json(txt)


def parse_json(txt):
    """모델 출력에서 JSON 을 뽑는다. 실패해도 예외를 던지지 않는다.

    과거엔 본문 속 `{...}` 를 다시 파싱하다 실패하면 JSONDecodeError 가 그대로
    올라가 **업스트림 형식 문제가 500(내부 오류)** 로 보고됐다(모니터링 노이즈).
    이제 마지막 수단으로 원문을 `raw` 로 돌려주고, 소비자가 판단하게 한다.
    """
    txt = (txt or "").strip()
    if not txt:
        return {"raw": ""}
    # ```json ... ``` 코드펜스 제거
    if txt.startswith("```"):
        body = txt.split("\n", 1)[1] if "\n" in txt else ""
        txt = body.rsplit("```", 1)[0].strip() if "```" in body else body.strip()
    for cand in (txt, _braces(txt)):
        if not cand:
            continue
        try:
            v = json.loads(cand)
        except Exception:
            continue
        # 최상위가 객체가 아니면(배열·숫자) 소비자 계약이 깨지므로 감싸서 돌려준다
        return v if isinstance(v, dict) else {"raw": v}
    return {"raw": txt}


def _braces(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    return m.group(0) if m else ""


import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard
import _log
import _errors
import monitoring

class handler(BaseHTTPRequestHandler):
    # 기본 접근로그는 쿼리스트링(PII 가능)을 그대로 찍으므로 침묵 — 구조화 로그가 대체
    log_message = _log.suppress_access_log

    def _send(self, code, obj, rq=None):
        d = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if rq is not None:
            _log.attach(self, rq)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.end_headers()
        self.wfile.write(d)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key, X-Webhook-Token")
        self.send_header("Access-Control-Expose-Headers", "X-Request-Id")
        self.end_headers()

    def do_GET(self):
        rq = _log.begin(self.headers, "/api/assist", "GET", self.path)
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            rq.finish(_c, denied=True)
            return _guard.deny(self, _c, _m, rq)
        self._send(200, {"ok": True, "tasks": list(TASKS)}, rq)
        rq.finish(200)

    def do_POST(self):
        rq = _log.begin(self.headers, "/api/assist", "POST", self.path)
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            rq.finish(_c, denied=True)
            return _guard.deny(self, _c, _m, rq)
        try:
            # 입력검증: task 는 화이트리스트, 원문·지식은 길이 상한. 위반시 400.
            b = _errors.read_json(self)
            task = _errors.as_choice(b, "task", TASKS, default="summary")
            text = _errors.as_str(b, "text", default="", max_len=20000)
            kb = _errors.as_str(b, "kb", default="", max_len=20000)
            if task == "kms" and not text:
                raise _errors.ValidationError.field("text", "kms 는 질문이 필요합니다")
            # task 종류와 입력 길이만 기록 — 상담 원문은 로그에 남기지 않는다
            rq.set(task=task, text_len=len(text))
            out = run_assist(task, text, kb)
            self._send(200, {"task": task, "result": out}, rq)
            rq.finish(200)
        except Exception as e:
            # 표준 에러 봉투 + 모니터링(5xx만) + 구조화 로그를 한 번에 처리
            _errors.handle(self, e, route="/api/assist", method="POST", rq=rq)
