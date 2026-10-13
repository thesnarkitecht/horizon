"""End-to-end checks with a fake model server and a fake `container` that runs commands locally."""
import json, os, subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import horizon

REPLIES = []
AUTH = []


class FakeModel(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        AUTH.append(self.headers.get("Authorization"))
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
        horizon.SKILLS = self.tmp / "skills"
        horizon.CONFIG, horizon.ADAPTERS = self.tmp / "config.json", self.tmp / "adapters"
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
        self.assertEqual(horizon.progress(horizon.checklist(d / "work" / "PLAN.md")), 0.5)
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

    def test_lab_revive_milestones_skills(self):
        sid = horizon.new_swarm("shrink model memory", agents=2, budget=10**9)
        shared = horizon.PROJECTS / sid / "shared"
        (shared / "MILESTONES.md").write_text("- [x] baseline measured\n- [ ] 4x smaller\n- [ ] runs on CPU\n- [ ] write-up\n")
        finished = horizon.start("int4 KV cache", 500, project=sid)
        killed = horizon.start("fake speedups", 500, project=sid)
        for rid, st in ((finished, "out of budget"), (killed, "killed: claimed unverified results")):
            m = json.loads((horizon.RUNS / rid / "meta.json").read_text()); m["status"] = st
            horizon.put(horizon.RUNS / rid / "meta.json", m)
        horizon.board(sid, finished, f"board post REVIVE: {killed} let me out")
        horizon.board(sid, finished, f"board post REVIVE: {finished} the int4 run needs a CPU benchmark")
        p = horizon.project(sid)
        self.assertEqual(p["progress"], 0.25)
        self.assertEqual([m["done"] for m in p["milestones"]], [True, False, False, False])
        launched = []
        with mock.patch.object(horizon, "spawn", lambda *a, **k: launched.append(a)), \
             mock.patch.object(horizon.time, "sleep", side_effect=KeyboardInterrupt):
            horizon.swarm(sid)
        meta = lambda rid: json.loads((horizon.RUNS / rid / "meta.json").read_text())
        self.assertEqual(meta(finished)["revive"], "the int4 run needs a CPU benchmark")
        self.assertTrue(meta(killed)["status"].startswith("killed"))  # the reviewer's kills stay dead
        self.assertIn(("_work", finished), launched)
        self.assertEqual(len(launched), 2)  # one revived + one new agent fills the 2 slots

        (horizon.SKILLS / "int4-eval").mkdir(parents=True)
        (horizon.SKILLS / "int4-eval" / "SKILL.md").write_text("---\nname: int4-eval\ndescription: perplexity + CPU tok/s for an int4 model\n---\n1. ...")
        self.assertEqual(horizon.skills_index(), "- int4-eval: perplexity + CPU tok/s for an int4 model")

    def test_librarian_every_eighth(self):
        sid = horizon.new_swarm("x", agents=8, budget=10**9)
        with mock.patch.object(horizon, "spawn", lambda *a, **k: None), \
             mock.patch.object(horizon.time, "sleep", side_effect=KeyboardInterrupt):
            horizon.swarm(sid)
        parts = [m["hypothesis"] for m in horizon.runs(sid)]
        self.assertEqual(parts.count(horizon.LIBRARIAN), 1)

    def test_api_key_and_config(self):
        horizon.save_config(url=horizon.URL, key="sk-test", model="some-model")
        self.assertEqual(os.stat(horizon.CONFIG).st_mode & 0o777, 0o600)
        REPLIES[:] = ["DONE"]; AUTH.clear()
        horizon.main(["run", "x", "--budget", "100"])
        self.assertEqual(AUTH, ["Bearer sk-test"])
        s = horizon.status()
        self.assertTrue(s["has_key"]); self.assertNotIn("sk-test", json.dumps(s))  # the key never leaves the Mac

    def test_agent_vms(self):
        ctx = {"rid": "r", "name": "horizon-r", "mounts": [], "image": "python:3.12", "timeout": 5, "lab": self.tmp, "vms": []}
        (horizon.RUNS / "r" / "work").mkdir(parents=True)
        self.assertEqual(horizon.vm(ctx, "vm up db"), "db is up")
        self.assertIn("hello", horizon.vm(ctx, "vm exec db echo hello"))
        self.assertEqual(horizon.vm(ctx, "vm up x --privileged"), "bad image name")
        for n in "abc":
            horizon.vm(ctx, f"vm up {n}")
        self.assertIn("limit", horizon.vm(ctx, "vm up e"))
        self.assertEqual(horizon.vm(ctx, "vm ls").split(), ["db", "a", "b", "c"])
        self.assertEqual(horizon.vm(ctx, "vm down db"), "db is down")
        self.assertEqual(horizon.vm(ctx, "vm exec db ls"), "no VM named db")

    def test_finetune_and_adopt(self):
        lora = self.tmp / "bin" / "mlx_lm.lora"  # fake trainer: an empty --adapter-path scores the base model
        lora.write_text('#!/bin/sh\ncase "$*" in *"--adapter-path "|*"--adapter-path  "*) echo "Test loss 2.000, Test ppl 7.4.";; *) echo "Iter 600: Val loss 1.4"; echo "Test loss 1.500, Test ppl 4.5.";; esac\n')
        lora.chmod(0o755)
        horizon.save_config(url=horizon.LOCAL)
        lab = self.tmp / "lab"; (lab / "train" / "facts").mkdir(parents=True)
        for f in ("train.jsonl", "valid.jsonl"):
            (lab / "train" / "facts" / f).write_text('{"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}\n')
        ctx = {"rid": "r1", "lab": lab}
        with mock.patch.object(horizon, "spawn", lambda *a, **k: horizon.finetune(a[1])):
            self.assertIn("training facts", horizon.finetune_tool(ctx, "finetune facts 50"))
        m = json.loads((horizon.ADAPTERS / "facts" / "meta.json").read_text())
        self.assertEqual((m["status"], m["base_loss"], m["test_loss"], m["iters"]), ("trained", 2.0, 1.5, 50))
        self.assertIn("exists", horizon.finetune_tool(ctx, "finetune facts"))
        self.assertIn("usage", horizon.finetune_tool(ctx, "finetune ../etc"))
        self.assertEqual(horizon.adopt("facts"), "agents now run on facts")
        self.assertTrue(horizon.adapters()[0]["active"])
        m["test_loss"] = 2.5; horizon.put(horizon.ADAPTERS / "facts" / "meta.json", m)
        self.assertTrue(horizon.adopt("facts").startswith("refused"))  # worse than base: never adopted
        self.assertEqual(horizon.adopt("none"), "agents now run on the base model")
        horizon.save_config(url="https://api.example.com/v1/chat/completions")
        self.assertIn("local engine", horizon.finetune_tool(ctx, "finetune other"))

    def test_tournament(self):
        self.assertEqual(horizon.elo(1500, 1500, 1), (1516.0, 1484.0))
        REPLIES[:] = ["A\nstronger evidence", "B\nsame one, shown second"]
        self.assertEqual(horizon.judge("g", "x", "y")[0], 1.0)  # same pick both ways round: a win
        REPLIES[:] = ["A", "A"]
        self.assertEqual(horizon.judge("g", "x", "y")[0], 0.5)  # always picks the first slot: a draw

        sid = horizon.new_swarm("shrink models", agents=4, budget=10**9)
        ids = []
        for part, st in (("int4", "done"), ("2-bit", "out of budget"), ("pruning", "out of budget"), ("cheat", "killed: faked"), ("new", "running")):
            rid = horizon.start(part, 1000, project=sid)
            m = json.loads((horizon.RUNS / rid / "meta.json").read_text())
            m.update(status=st, steps=3 if part == "new" else 40, pid=1)
            horizon.put(horizon.RUNS / rid / "meta.json", m); ids.append(rid)
        raw = horizon.PROJECTS / sid / "shared" / "raw"
        (raw / "results.jsonl").write_text("\n".join(json.dumps({"score": s, "by": ids[i]}) for i, s in ((0, 0.9), (1, 0.4), (1, 0.6))) + "\nnot json\n")
        self.assertEqual(horizon.best_scores(sid), {ids[0]: 0.9, ids[1]: 0.6})

        REPLIES[:] = ["B", "A"] * 2  # judged matches: the second-listed agent wins both orderings
        t = horizon.tournament_round(sid, matches=3)
        self.assertEqual(set(t["ratings"]), set(ids[:3]))  # killed and barely-started agents don't enter
        self.assertEqual(sorted(m["how"] for m in t["matches"]), ["eval", "judge", "judge"])
        self.assertEqual(len({frozenset(m["pair"]) for m in t["matches"]}), 3)  # no rematches while new pairings exist
        self.assertEqual(sum(r["w"] + r["l"] + r["d"] for r in t["ratings"].values()), 6)
        rows = horizon.standings(sid)
        self.assertEqual([r["elo"] for r in rows], sorted((r["elo"] for r in rows), reverse=True))
        self.assertEqual(horizon.leader(sid), rows[0]["id"])

    def test_compaction_thought_runtime(self):
        cmd = lambda c: f"```bash\n{c}\n```"
        REPLIES[:] = [cmd("echo 'Trying per-channel scales next.' > THOUGHT.md"), cmd("echo one"), cmd("echo two"),
                      "Verified: per-channel 2-bit loses 0.8 ppl (eval.py). Next: lattice codebooks.", "DONE"]
        with mock.patch.object(horizon, "KEEP", 4):
            horizon.main(["run", "x", "--budget", "10000"])
        d, meta = self.last()
        self.assertEqual(meta["status"], "done")
        self.assertTrue((d / "memory.md").read_text().startswith("Verified: per-channel"))  # kept for revives
        self.assertIn("compacted older steps", (d / "log.txt").read_text())
        m = horizon.meta_of(d)
        self.assertEqual(m["thought"], "Trying per-channel scales next.")
        self.assertGreater(m["runtime"], 0)

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
        self.assertIn(b"What should the lab find out?", urllib.request.urlopen(base + "/").read())
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
