import os
import json
import asyncio
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler

VOICE = os.environ.get("CALLBOT_TTS_VOICE", "ko-KR-SunHiNeural")


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

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        _ok, _c, _m = _guard.check(self.headers, self.path, allow_webhook=False)
        if not _ok:
            return _guard.deny(self, _c, _m)
        try:
            qs = parse_qs(urlparse(self.path).query)
            text = (qs.get("text", [""])[0]).strip()[:1000]
            if not text:
                self.send_response(400)
                self.end_headers()
                return
            audio = _synth(text)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", _guard.allow_origin_header(self.headers))
            self.end_headers()
            self.wfile.write(audio)
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
