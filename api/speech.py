# -*- coding: utf-8 -*-
"""음성 입출력 단일 진입점 — /api/stt · /api/tts 를 함수 하나로 받는다.

왜 합쳤는가: Vercel 은 `api/` 아래 밑줄로 시작하지 않는 .py 를 **파일 1개 = 서버리스
함수 1개**로 배포한다. Hobby 플랜 상한이 배포당 12개인데 이 저장소는 그 위에 있었다.
로직은 그대로 두고 진입점만 묶는다 — 실제 처리는 `_stt` · `_tts` 가 예전과 똑같이 한다.

URL 은 하나도 바뀌지 않는다. `vercel.json` 의 rewrites 가
    /api/stt  ->  /api/speech?mode=stt
    /api/tts  ->  /api/speech?mode=tts
로 넘기고, 원래 쿼리스트링은 그대로 합쳐져 온다.

요청 제한 등급은 `_ratelimit.ROUTE_CLASS` 에 "speech" 를 넣어 예전 등급(speech)을 유지한다.

**왜 `_stt.handler` 를 상속하는가**: 핸들러들은 `_send()` 같은 자기 메서드를 쓴다.
상속하지 않고 `_stt.handler.do_GET(self)` 로만 위임하면 그 메서드가 없어
`AttributeError` 가 난다(실제로 `?health=1` 경로가 이렇게 깨졌다).
상속하면 STT 쪽 보조 메서드가 전부 따라오고, TTS 는 보조 메서드가 없어
위임만으로 충분하다. 새 보조 메서드가 `_tts` 에 생기면 여기도 상속을 늘려야 한다.
"""
import os
import sys
from urllib.parse import urlparse, parse_qs

_d = os.path.dirname(__file__)
if _d not in sys.path:
    sys.path.insert(0, _d)

import _errors
import _guard
import _stt
import _tts
import _vstudio


def _mode(path):
    """?mode=tts 면 tts, 그 외에는 stt. 경로에 /tts 가 직접 오는 경우도 받는다."""
    try:
        u = urlparse(path or "")
        m = (parse_qs(u.query).get("mode", [""])[0] or "").strip().lower()
        if m in ("stt", "tts", "studio"):
            return m
        seg = (u.path or "").rstrip("/").rsplit("/", 1)[-1].lower()
        if seg in ("stt", "tts"):
            return seg
        if seg == "voice-studio":
            return "studio"
    except Exception:
        pass
    return "stt"


class handler(_stt.handler):
    """STT 핸들러를 그대로 물려받고, tts 모드일 때만 TTS 쪽으로 넘긴다."""

    def do_GET(self):
        m = "stt"
        try:
            m = _mode(self.path)
            if m == "studio":
                # 보이스 스튜디오(09-29) — 같은 가드(요청 제한·출처)를 지난 뒤 처리한다
                _ok, _c, _msg = _guard.check(self.headers, self.path, allow_webhook=False)
                if not _ok:
                    return _vstudio._send(self, _c, {"ok": False, "error": "요청이 너무 많거나 허용되지 않은 곳에서 왔습니다."})
                return _vstudio.handle_get(self)
            if m == "tts":
                return _tts.handler.do_GET(self)
            return _stt.handler.do_GET(self)
        except Exception as e:
            # 위임 자체가 실패해도 표준 에러 봉투로 답한다(내부 문구 미노출).
            _errors.handle(self, e, route="/api/" + m, method="GET")

    def do_POST(self):
        # 본문을 받는 것은 음성 인식과 보이스 스튜디오 합성이다(콜봇 합성은 GET ?text=).
        try:
            if _mode(self.path) == "studio":
                _ok, _c, _msg = _guard.check(self.headers, self.path, allow_webhook=False)
                if not _ok:
                    return _vstudio._send(self, _c, {"ok": False, "error": "요청이 너무 많거나 허용되지 않은 곳에서 왔습니다."})
                return _vstudio.handle_post(self)
            return _stt.handler.do_POST(self)
        except Exception as e:
            _errors.handle(self, e, route="/api/stt", method="POST")

    def do_OPTIONS(self):
        fn = getattr(_stt.handler, "do_OPTIONS", None)
        if fn is not None:
            return fn(self)
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type, x-api-key")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass
