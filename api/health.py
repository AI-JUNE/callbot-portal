# -*- coding: utf-8 -*-
# ==========================================================================
# /api/health — 무인증 헬스체크 엔드포인트
# --------------------------------------------------------------------------
# 목적: 업타임 모니터/로드밸런서/배포 파이프라인이 서비스 생존 여부를 확인할 수
#       있도록 표준 헬스체크를 제공한다. 외부 모니터는 Origin/Referer 헤더 없이
#       호출하므로 _guard.check(오리진 검사)를 적용하지 않는다.
# 원칙: 민감정보(키 값·비밀·PII) 미노출. 키의 "설정 여부"만 boolean으로 노출.
#       과금 유발 경로(engine.run_turn 등) 미호출 — 순수 상태 응답.
# ==========================================================================
import os, sys, json, time, platform
from http.server import BaseHTTPRequestHandler

BUILD = os.environ.get("CALLBOT_BUILD", "dev")


def _bool_env(*names):
    for n in names:
        if (os.environ.get(n) or "").strip():
            return True
    return False


def _speech():
    """STT/TTS 프로바이더 상태 요약 — 실호출·키 값 노출 없음 (P0-1 후속).

    - requested: env(CALLBOT_STT_PROVIDER/CALLBOT_TTS_PROVIDER) 기준 요청 값.
      미설정 시 기존 라이브 경로 기본값(stt=gemini, tts=edge).
    - delegated: speech_providers 팩토리로 위임되는지 여부.
    - active/forced_sim: 위임 시 실제 선택 결과. SPEECH_LIVE=0이면 비-sim 요청도
      sim 강제(실호출 원천 차단) → forced_sim=True 로 표시.
    """
    stt_want = (os.environ.get("CALLBOT_STT_PROVIDER") or "").strip().lower()
    tts_want = (os.environ.get("CALLBOT_TTS_PROVIDER") or "").strip().lower()
    info = {
        "speech_live": (os.environ.get("SPEECH_LIVE") or "0").strip() == "1",
        "stt": {"requested": stt_want or "gemini", "delegated": bool(stt_want and stt_want != "gemini")},
        "tts": {"requested": tts_want or "edge", "delegated": bool(tts_want and tts_want != "edge")},
    }
    if info["stt"]["delegated"] or info["tts"]["delegated"]:
        try:
            d = os.path.dirname(__file__)
            if d not in sys.path:
                sys.path.insert(0, d)
            import speech_providers as sp
            if info["stt"]["delegated"]:
                p, m = sp._pick(sp._STT, "CALLBOT_STT_PROVIDER")
                info["stt"]["active"] = p.name
                info["stt"]["forced_sim"] = bool(m.get("forced_sim"))
            if info["tts"]["delegated"]:
                p, m = sp._pick(sp._TTS, "CALLBOT_TTS_PROVIDER")
                info["tts"]["active"] = p.name
                info["tts"]["forced_sim"] = bool(m.get("forced_sim"))
        except Exception as e:  # 헬스는 절대 실패하지 않도록 best-effort
            info["note"] = "speech_providers unavailable: %s" % e
    return info


def _payload():
    return {
        "ok": True,
        "service": "callbot-portal",
        "status": "healthy",
        "ts": int(time.time()),
        "build": BUILD,
        "runtime": {
            "python": platform.python_version(),
        },
        # 설정 여부만 노출 (값 미노출)
        "config": {
            "google_key_present": _bool_env("GOOGLE_API_KEY", "GEMINI_API_KEY"),
            "api_key_gate": _bool_env("CALLBOT_API_KEY"),
            "strict_mode": (os.environ.get("CALLBOT_STRICT") or "").strip() == "1",
            "cpaas_webhook": _bool_env("CPAAS_WEBHOOK_TOKEN"),
            "order_backend": (os.environ.get("ORDER_BACKEND") or "demo").strip(),  # order_backend.get_backend() 기본값(demo)과 일치
        },
        "speech": _speech(),
    }


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        d = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(d)))
        self.end_headers()
        self.wfile.write(d)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            self._send(200, _payload())
        except Exception as e:
            self._send(500, {"ok": False, "status": "error", "error": str(e)})

    # HEAD 요청(일부 모니터)도 200으로 응답
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
