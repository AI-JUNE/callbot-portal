import os
import json
import urllib.request
from http.server import BaseHTTPRequestHandler

MODEL = os.environ.get("CALLBOT_GEMINI_MODEL", "gemini-2.5-flash")

# 브라우저 녹음이 실제로 보내는 컨테이너만 허용 — 임의 mime 로 업스트림을 찌르지 않는다.
# MediaRecorder 는 브라우저마다 codecs 파라미터를 붙이므로(예: audio/webm;codecs=opus)
# 파라미터를 떼고 기본 타입만 대조한다. 값 자체는 원문 그대로 넘긴다.
MIME_BASES = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg",
              "audio/wav", "audio/x-wav", "audio/aac", "audio/l16", "audio/3gpp")


def _check_mime(v):
    """허용 컨테이너면 원문 반환, 아니면 ValidationError(400)."""
    raw = (v or "").strip() or "audio/webm"
    if raw.split(";", 1)[0].strip().lower() in MIME_BASES:
        return raw
    raise _errors.ValidationError.field(
        "mime", "허용 컨테이너: %s" % ", ".join(MIME_BASES))


def _key():
    return (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()


def _alt_provider():
    """P0-1 후속: 프로바이더 팩토리 위임(옵트인).

    CALLBOT_STT_PROVIDER 가 설정되고 'gemini' 가 아니면 speech_providers 팩토리로 위임.
    - 미설정(기본): 기존 Gemini 경로 그대로 (라이브 동작 불변).
    - sim: 키·네트워크 호출 없는 결정적 응답.
    - clova/google/aws: SPEECH_LIVE=1 게이트 전까지 sim 강제 폴백. [승인 필요]
    """
    want = (os.environ.get("CALLBOT_STT_PROVIDER") or "").strip().lower()
    if not want or want == "gemini":
        return None
    import sys as _s
    _s.path.insert(0, os.path.dirname(__file__))
    import speech_providers
    return speech_providers.get_stt()


def _provider_health():
    """speech_providers 의 STT 프로바이더 health(읽기전용). 실패해도 GET 은 유지."""
    try:
        import sys as _s
        _s.path.insert(0, os.path.dirname(__file__))
        import speech_providers
        return speech_providers.health_report("stt")
    except Exception:  # 모듈 부재·임포트 실패 시에도 엔드포인트 정상 응답
        # 내부 예외 문구는 노출하지 않는다 — 상세는 구조화 로그·모니터링으로 본다
        return {"ok": False, "error": "provider health unavailable"}


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


import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard
import _errors

class handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        prov = (os.environ.get("CALLBOT_STT_PROVIDER") or "gemini").strip().lower()
        out = {"ok": True, "engine": "Gemini STT", "model": MODEL,
               "key_present": bool(_key()), "provider": prov}
        out["health"] = _provider_health()
        self._send(out)

    def do_POST(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        try:
            # 입력검증: base64 오디오 상한(8MiB)·mime 화이트리스트. 위반시 400/413.
            body = _errors.read_json(self, max_bytes=_errors.MAX_BODY_AUDIO)
            audio = _errors.as_str(body, "audio", required=True, max_len=_errors.MAX_BODY_AUDIO,
                                   allow_empty=False)
            mime = _check_mime(_errors.as_str(body, "mime", default="audio/webm", max_len=120))
            alt = _alt_provider()
            if alt is not None:
                r = alt.transcribe(audio, mime)
                self._send(r)
                return
            self._send({"text": transcribe(audio, mime), "model": MODEL})
        except Exception as e:
            # 표준 에러 봉투 — 내부 예외 문구 대신 안정 코드로 응답
            _errors.handle(self, e, route="/api/stt", method="POST")
