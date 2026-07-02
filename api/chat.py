import os, sys, json
from http.server import BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(__file__))
from engine import run_turn

def _key():
    return bool((os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip())

class handler(BaseHTTPRequestHandler):
    def _send(self,code,obj):
        d=json.dumps(obj,ensure_ascii=False).encode("utf-8")
        self.send_response(code); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(d)
    def do_OPTIONS(self):
        self.send_response(204); self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Headers","Content-Type"); self.end_headers()
    def do_GET(self):
        self._send(200,{"ok":True,"google_key_present":_key(),"model":os.environ.get("CALLBOT_GEMINI_MODEL","gemini-2.5-flash")})
    def do_POST(self):
        try:
            n=int(self.headers.get("content-length",0)); body=json.loads(self.rfile.read(n) or "{}")
            self._send(200, run_turn(body.get("messages",[]), body.get("phone","01012345678")))
        except Exception as e:
            self._send(500,{"error":str(e)})
