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
    import _speech_providers as speech_providers
    return speech_providers.get_tts()


def _provider_health():
    """speech_providers 의 TTS 프로바이더 health(읽기전용). 실패해도 200 유지."""
    try:
        import sys as _s
        _s.path.insert(0, os.path.dirname(__file__))
        import _speech_providers as speech_providers
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
    data = asyncio.run(run())
    if not data:
        # 0바이트를 200 으로 내보내면 호출자는 '무음'을 재생하고 운영자는 아무것도
        # 보지 못한다. 실패는 실패로 드러낸다(_vstudio.synth_standard 와 같은 규약).
        raise RuntimeError("empty audio")
    return data


import os as _os_g, sys as _sys_g
_sys_g.path.insert(0, _os_g.path.dirname(__file__))
import _guard
import _errors


def _respond(h, body, ctype):
    """성공 응답 한 곳 — 오류 응답(_errors.send)과 같은 헤더 계약을 쓴다.

    보조 '메서드' 가 아니라 모듈 함수인 이유: `speech.handler` 는 `_tts.handler`
    를 상속하지 않고 위임만 한다(회귀 test_speech_dispatch 가 메서드 추가를 막는다).
    """
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Cache-Control", "no-store")
    h.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(h.headers))
    h.send_header("Access-Control-Expose-Headers", _errors.EXPOSE_HEADERS)
    rq = getattr(h, "_rq", None)
    if rq is not None:
        h.send_header("X-Request-Id", rq.request_id)
    # 오디오 응답에도 길이를 알려준다 — 받는 쪽이 중간에 끊긴 것과 구분할 수 있다.
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)
    try:                          # 요청 1건 = 구조화 로그 1줄(성공 경로)
        if rq is not None:
            rq.finish(200)
    except Exception:
        pass

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
                _respond(self, b, "application/json; charset=utf-8")
                return
            # 입력검증: text 필수·1000자 상한. 빈 값은 본문 없는 400 대신 표준 봉투로.
            text = _errors.query_str(qs, "text", max_len=1000, required=True)
            alt = _alt_provider()
            if alt is not None:
                audio, meta = alt.synthesize(text, voice=VOICE)
                if not audio:
                    # sim 등 오디오 미생성 프로바이더 → 메타만 JSON 으로
                    b = json.dumps(meta, ensure_ascii=False).encode("utf-8")
                    _respond(self, b, "application/json; charset=utf-8")
                    return
            else:
                audio = _synth(text)
            if not audio:
                # 합성 경로가 어떤 이유로든 0바이트를 돌려줬다. 200 으로 내보내면
                # 호출자는 무음을 재생하고 운영자는 아무것도 보지 못한다 —
                # 5xx 로 분류해 모니터링에 남긴다(오디오 미생성 sim 은 위에서 끝난다).
                raise RuntimeError("empty audio")
            _respond(self, audio, "audio/mpeg")
        except Exception as e:
            # 표준 에러 봉투 — 내부 예외 문구 대신 안정 코드로 응답
            _errors.handle(self, e, route="/api/tts", method="GET")
