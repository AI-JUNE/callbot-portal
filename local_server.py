"""로컬에서 포털을 그대로 테스트(배포 전 확인용). 추가 설치 불필요.

    set GOOGLE_API_KEY=...        # Windows PowerShell: $env:GOOGLE_API_KEY="..."
    python local_server.py
    → http://localhost:8000
"""
import http.server, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "api"))
from engine import run_turn

PUBLIC = os.path.join(os.path.dirname(__file__), "public")


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=PUBLIC, **k)

    def do_POST(self):
        if self.path.startswith("/api/chat"):
            n = int(self.headers.get("content-length", 0))
            try:
                body = json.loads(self.rfile.read(n) or "{}")
                out = run_turn(body.get("messages", []), body.get("phone", "01012345678"))
                code = 200
            except Exception as e:
                out, code = {"error": str(e)}, 500
            data = json.dumps(out, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Callbot 포털 로컬 실행 → http://localhost:{port}")
    http.server.HTTPServer(("", port), H).serve_forever()
