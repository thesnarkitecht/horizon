"""End-to-end check with a fake model server and a fake `container` that runs commands locally."""
import json, os, stat, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPLIES = ["<think>```bash\nrm -rf /\n```</think>Let me check.\n```bash\necho 42 > RESULT.md\n```", "DONE"]


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200); self.end_headers()
        text = REPLIES.pop(0)
        for i in range(0, len(text), 4):
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': text[i:i+4]}}]})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a): pass


class Test(unittest.TestCase):
    def test_run(self):
        tmp = Path(tempfile.mkdtemp())
        srv = HTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # fake `container`: `exec NAME sh -c CMD` runs CMD in the run's work dir, everything else succeeds
        fake = tmp / "bin" / "container"; fake.parent.mkdir()
        fake.write_text(f'#!/bin/sh\n[ "$1" = exec ] && cd "$(ls -d {tmp}/runs/*/work)" && shift 2 && exec "$@"\nexit 0\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ.update(PATH=f"{fake.parent}:{os.environ['PATH']}", HORIZON_HOME=str(tmp),
                          HORIZON_URL=f"http://127.0.0.1:{srv.server_port}/v1/chat/completions")
        import horizon
        horizon.RUNS = tmp / "runs"
        horizon.URL = os.environ["HORIZON_URL"]
        horizon.main(["run", "the answer is 42", "--budget", "1000"])
        d = next(horizon.RUNS.iterdir())
        meta = json.loads((d / "meta.json").read_text())
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["steps"], 2)
        self.assertGreater(meta["tokens"], 0)
        self.assertEqual((d / "work" / "RESULT.md").read_text().strip(), "42")  # ran the real command, not the one in <think>
        self.assertIn("$ echo 42", (d / "log.txt").read_text())


if __name__ == "__main__":
    unittest.main()
