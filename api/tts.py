import os
import json
import asyncio
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

VOICE = os.environ.get("CALLBOT_TTS_VOICE", "ko-KR-SunHiNeural")


def _alt_provider():
    """P0-1 후속: 프로바이더 팩토리 위임(옵트인).

    CALLBOT_TTS_PROVIDER 가 설정되고 'edge' 가 아니면 speech_providers 팩토리로 위임.
    - 미설정(기본): 기존 edge-tts 경로 그대로 (라이브 동작 불변).
    - sim: 오디오 미생성(빈 bytes) → JSON 메타 응답.
    - clova/google/aws: SPEECH_LIVE=1 게이트 전까지 sim 강제 폴백. [승인 필요]
    """
    want = (os.environ.get("CALLBOT_TTS_PROVIDER") or "").strip().lower()
    if not want or want == "edge":
        return None
    import sys as _s
    _s.path.insert(0, os.path.dirname(__file__))
    import speech_providers
    return speech_providers.get_tts()


def _provider_health():
    """speech_providers 의 TTS 프로바이더 health(읽기전용). 실패해도 200 유지."""
    try:
        import sys as _s
        _s.path.insert(0, os.path.dirname(__file__))
        import speech_providers
        h = speech_providers.health_report("tts")
        h["voice"] = VOICE
        return h
    except Exception:
        # 내부 예외 문구는 노출하지 않는다 — 상세는 구조화 로그·모니터링으로 본다
        return {"ok": False, "error": "provider health unavailable"}


def _synth(text):
    import edge_tts
    async def run():
        buf = bytearray()
        async for ch in edge_tts.Communicate(text, VOICE).stream():
            if ch.get("type") == "audio":
                buf.extend(ch["data"])
        return bytes(buf)
    return asyncio.run(run())


import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard
import _errors

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        try:
            qs = parse_qs(urlparse(self.path).query)
            if (qs.get("health", [""])[0] or "").strip() in ("1", "true", "yes"):
                # 운영 점검용: 오디오 합성 없음 · 과금 0 · 키 미노출
                b = json.dumps(_provider_health(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            # 입력검증: text 필수·1000자 상한. 빈 값은 본문 없는 400 대신 표준 봉투로.
            text = _errors.query_str(qs, "text", max_len=1000, required=True)
            alt = _alt_provider()
            if alt is not None:
                audio, meta = alt.synthesize(text, voice=VOICE)
                if not audio:
                    # sim 등 오디오 미생성 프로바이더 → 메타만 JSON 으로
                    b = json.dumps(meta, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
            else:
                audio = _synth(text)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
            self.end_headers()
            self.wfile.write(audio)
        except Exception as e:
            # 표준 에러 봉투 — 내부 예외 문구 대신 안정 코드로 응답
            _errors.handle(self, e, route="/api/tts", method="GET")
