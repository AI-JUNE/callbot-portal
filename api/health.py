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
            "order_backend": (os.environ.get("ORDER_BACKEND") or "mock").strip(),
        },
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
