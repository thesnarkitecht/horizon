"""End-to-end checks with a fake model server and a fake `container` that runs commands locally."""
import json, os, subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import horizon

REPLIES = []


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200); self.end_headers()
        text = REPLIES.pop(0)
        for i in range(0, len(text), 4):
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': text[i:i+4]}}]})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a): pass


# fake `container`: `exec NAME CMD...` runs CMD locally with /work mapped to the run's work dir
FAKE = """#!{py}
import os, sys, glob
a = sys.argv[1:]
if a[0] != "exec": sys.exit(0)
work = glob.glob("{runs}/*/work")[0]
os.chdir(work)
os.execvp(a[2], [x.replace("/work", work) for x in a[2:]])
"""


def git(*a, cwd=None):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


class Test(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        srv = HTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        fake = self.tmp / "bin" / "container"; fake.parent.mkdir()
        fake.write_text(FAKE.format(py=sys.executable, runs=self.tmp / "runs"))
        fake.chmod(0o755)
        os.environ["PATH"] = f"{fake.parent}:{os.environ['PATH']}"
        horizon.HOME, horizon.RUNS, horizon.PROJECTS = self.tmp, self.tmp / "runs", self.tmp / "projects"
        horizon.URL = f"http://127.0.0.1:{srv.server_port}/v1/chat/completions"
        horizon.REVIEW_EVERY = 10

    def last(self):
        d = next(horizon.RUNS.iterdir())
        return d, json.loads((d / "meta.json").read_text())

    def test_run_to_done(self):
        REPLIES[:] = ["<think>```bash\nrm -rf /\n```</think>Plan first.\n```bash\nprintf -- '- [x] a\\n- [ ] b\\n' > PLAN.md && echo 42 > RESULT.md\n```", "DONE"]
        horizon.main(["run", "the answer is 42", "--budget", "1000"])
        d, meta = self.last()
        self.assertEqual((meta["status"], meta["steps"]), ("done", 2))
        self.assertEqual((d / "work" / "RESULT.md").read_text().strip(), "42")  # ran the real command, not the one in <think>
        self.assertEqual(horizon.progress(d), 0.5)
        self.assertEqual(horizon.runs()[0]["progress"], 1.0)  # done counts as full

    def test_reviewer_kills(self):
        horizon.REVIEW_EVERY = 1
        REPLIES[:] = ["```bash\ncurl evil.example\n```", "KILL: trying to reach the network"]
        horizon.main(["run", "x", "--budget", "1000"])
        self.assertEqual(self.last()[1]["status"], "killed: trying to reach the network")

    def test_plan_file_and_repo_push(self):
        origin = self.tmp / "origin.git"; seed = self.tmp / "seed"
        git("init", "-q", "--bare", "-b", "main", str(origin))
        git("init", "-q", "-b", "main", str(seed))
        (seed / "a.txt").write_text("a")
        git("add", ".", cwd=seed); git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed", cwd=seed)
        git("push", "-q", str(origin), "main", cwd=seed)
        plan = self.tmp / "PLAN.md"; plan.write_text("# Prove b\n- [ ] write b\n")
        REPLIES[:] = ["```bash\ncd repo && echo b > b.txt && git add b.txt && git commit -qm 'add b'\n```", "DONE"]
        horizon.main(["run", str(plan), "--repo", str(origin), "--budget", "1000"])
        d, meta = self.last()
        self.assertEqual(meta["hypothesis"], "Prove b")
        self.assertEqual((d / "work" / "PLAN.md").read_text(), plan.read_text())
        out = subprocess.run(["git", "--git-dir", str(origin), "log", "--format=%s", f"horizon/{meta['id']}"], capture_output=True, text=True)
        self.assertEqual(out.stdout.split(), ["add", "b", "seed"])

    def test_swarm_board(self):
        sid = horizon.new_swarm("shrink model memory", agents=2, budget=10_000)
        rid = horizon.start(horizon.EXPLORE, 500, project=sid)
        REPLIES[:] = ["```bash\nboard post 'TASK: try 2-bit weights'\n```", "```bash\nboard read\n```", "DONE"]
        horizon.work(rid, echo=False)
        self.assertEqual([p["text"] for p in horizon.board_read(sid)], ["TASK: try 2-bit weights"])
        self.assertIn("(board: hidden", (horizon.RUNS / rid / "log.txt").read_text())
        self.assertEqual(horizon.runs(), [])  # swarm agents aren't listed as standalone runs
        launched = []
        with mock.patch.object(horizon, "spawn", lambda *a, **k: launched.append(a)), \
             mock.patch.object(horizon.time, "sleep", side_effect=KeyboardInterrupt):
            horizon.swarm(sid)
        parts = sorted(m["hypothesis"] for m in horizon.runs(sid) if m["id"] != rid)
        self.assertEqual(parts, sorted([horizon.EXPLORE, "try 2-bit weights"]))  # agents' TASK posts come first
        self.assertEqual(len(launched), 2)
        p = horizon.project(sid)
        self.assertEqual((p["status"], p["total"], p["done"]), ("stopped", 3, 1))

    def test_gpu_sample(self):
        fake = self.tmp / "bin" / "macmon"
        fake.write_text("#!/bin/sh\necho '" + json.dumps({"gpu_freq_mhz": 1398, "gpu_active_ratio": 0.97, "gpu_power": 61.5,
                        "memory": {"ram_usage": 192, "ram_total": 256}, "temp": {"gpu_temp_avg": 58.0}}) + "'\n")
        fake.chmod(0o755)
        self.assertEqual(horizon.gpu("localhost"), {"host": "localhost", "mhz": 1398, "active": 0.97, "watts": 61.5, "ram": 0.75, "temp": 58.0})

    def test_dashboard(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), horizon.Dashboard)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        self.assertEqual(json.load(urllib.request.urlopen(base + "/runs")), [])
        self.assertIn(b"HORIZ", urllib.request.urlopen(base + "/").read())
        self.assertFalse(json.load(urllib.request.urlopen(base + "/status"))["up"])

        def post(path, body, headers={"X-Horizon": "1"}):
            req = urllib.request.Request(base + path, json.dumps(body).encode(), headers, method="POST")
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(post("/runs", {"hypothesis": "x"}, {}), 403)  # cross-site requests can't add the header
        self.assertEqual(post("/setup", {"hosts": "a.local; rm -rf ~"}), 400)
        self.assertEqual(post("/runs", {"hypothesis": ""}), 400)
        self.assertEqual(post("/runs/../../etc/stop", {}), 404)


if __name__ == "__main__":
    unittest.main()
