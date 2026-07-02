import os
import json
import urllib.request
from http.server import BaseHTTPRequestHandler

MODEL = os.environ.get("CALLBOT_GEMINI_MODEL", "gemini-2.5-flash")


def _key():
    return (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()


def transcribe(audio_b64, mime):
    key = _key()
    if not key:
        raise RuntimeError("GOOGLE_API_KEY 환경변수가 없습니다.")
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (MODEL, key)
    prompt = (
        "한국어 상담 발화를 들리는 그대로 정확히 전사. "
        "짧은 답변(네/아니요/예)도 전사. 설명·따옴표 없이 전사문만. 무음이면 빈 문자열."
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": mime or "audio/webm", "data": audio_b64}},
            ],
        }],
        # temperature 0 + thinkingBudget 0 (추론 비활성화)로 전사 지연 최소화
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 80,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    try:
        txt = d["candidates"][0]["content"]["parts"][0]["text"]
        return (txt or "").strip().strip('"').strip()
    except Exception:
        return ""


class handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send({"ok": True, "engine": "Gemini STT", "model": MODEL, "key_present": bool(_key())})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or "{}")
            text = transcribe(body.get("audio", ""), body.get("mime", "audio/webm"))
            self._send({"text": text, "model": MODEL})
        except Exception as e:
            self._send({"error": str(e)})
