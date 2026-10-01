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
import _log
import _stt
import _tts
import _vstudio


_ROUTE = {"stt": "/api/stt", "tts": "/api/tts", "studio": "/api/voice-studio"}


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

    # 기본 접근로그는 요청라인을 그대로 찍는다 — `?text=<발화 원문>` 이 그대로
    # 로그로 새어 나간다. 구조화 로그(_log)가 같은 정보를 PII 없이 남긴다.
    log_message = _log.suppress_access_log

    def _begin(self, m, method):
        """요청 1건의 구조화 로그를 시작한다. 모드별 라우트로 귀속한다.

        `self._rq` 로 걸어 두면 `_errors.send/handle`·`_stt._send`·`_tts._respond`
        가 알아서 request_id 를 붙이고 한 줄을 남긴다(호출부마다 넘기지 않는다).
        """
        rq = _log.begin(self.headers, _ROUTE.get(m, "/api/stt"), method, self.path)
        self._rq = rq
        return rq

    def send_response(self, code, *a):
        # 위임받은 핸들러가 직접 쓴 상태코드도 로그에 사실대로 남게 기억해 둔다.
        self._status = code
        return _stt.handler.send_response(self, code, *a)

    def _close(self, rq):
        try:
            if rq is not None and not rq.done_flag:
                rq.finish(getattr(self, "_status", None) or 200)
        except Exception:
            pass

    def do_GET(self):
        m = _mode(self.path)
        rq = self._begin(m, "GET")
        try:
            if m == "studio":
                # 보이스 스튜디오(09-29) — 같은 가드(요청 제한·출처)를 지난 뒤 처리한다
                _ok, _c, _msg = _guard.check(self.headers, self.path, allow_webhook=False)
                if not _ok:
                    return _guard.deny(self, _c, _msg, rq)
                _vstudio.handle_get(self)
            elif m == "tts":
                _tts.handler.do_GET(self)
            else:
                _stt.handler.do_GET(self)
        except Exception as e:
            # 위임 자체가 실패해도 표준 에러 봉투로 답한다(내부 문구 미노출).
            _errors.handle(self, e, route=_ROUTE.get(m, "/api/stt"), method="GET", rq=rq)
        finally:
            self._close(rq)

    def do_POST(self):
        # 본문을 받는 것은 음성 인식과 보이스 스튜디오 합성이다(콜봇 합성은 GET ?text=).
        m = _mode(self.path)
        rq = self._begin(m, "POST")
        try:
            if m == "studio":
                _ok, _c, _msg = _guard.check(self.headers, self.path, allow_webhook=False)
                if not _ok:
                    # 거부도 표준 봉투로. 특히 429 는 Retry-After·X-RateLimit-* 가
                    # 실려야 배치 제작(한 줄 = 요청 1건)이 얼마나 기다릴지 알 수 있다.
                    return _guard.deny(self, _c, _msg, rq)
                _vstudio.handle_post(self)
            else:
                _stt.handler.do_POST(self)
        except Exception as e:
            # 라우트를 모드에 맞게 남긴다 — 스튜디오 오류가 /api/stt 로 적히면
            # 모니터링에서 STT 장애를 쫓게 된다.
            _errors.handle(self, e, route=_ROUTE.get(m, "/api/stt"), method="POST", rq=rq)
        finally:
            self._close(rq)

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
