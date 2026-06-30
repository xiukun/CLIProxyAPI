#!/usr/bin/env python3
"""Simple file upload server for syncing auth files."""

import os
from http.server import HTTPServer, BaseHTTPRequestHandler

# 文件路由：上传路径 -> 实际存储目录
FILE_ROUTES = {
    "state.vscdb": "/data/auth/state.vscdb",
}


class UploadHandler(BaseHTTPRequestHandler):
    def do_PUT(self):
        filename = self.path.lstrip("/")
        if not filename or filename not in FILE_ROUTES:
            self.send_error(400, f"Unknown file: {filename}. Allowed: {list(FILE_ROUTES.keys())}")
            return

        filepath = FILE_ROUTES[filename]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(body)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"OK: {filename} ({length} bytes)\n".encode())
        print(f"[upload] {filename} ({length} bytes)", flush=True)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("UPLOAD_PORT", "9100"))
    server = HTTPServer(("0.0.0.0", port), UploadHandler)
    print(f"[upload] Listening on :{port}", flush=True)
    server.serve_forever()
