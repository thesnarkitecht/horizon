#!/usr/bin/env python3
"""horizon: the backend of Horizon.app — long-running research agents on your own Macs.

Each agent works a goal inside a throwaway Linux VM (Apple `container`), one shell command at a
time, following a PLAN.md checklist, until it is DONE, out of budget, or killed by the reviewer.
Swarms keep many agents going on one goal; they coordinate on a board only agents and the
reviewer read. All agents share one model server, which batches them together.

Horizon.app runs `horizon.py app` and shows its dashboard. The other subcommands are what the
dashboard spawns (and handy for debugging): run, swarm, ls, log, stop, setup, serve.
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, threading, time, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("HORIZON_HOME", Path.home() / ".horizon"))
RUNS = HOME / "runs"
PROJECTS = HOME / "projects"  # swarms: meta.json + board.jsonl; their agents are ordinary runs
HOSTS = HOME / "hosts.json"  # written by `horizon setup`
URL = os.environ.get("HORIZON_URL", "http://127.0.0.1:8080/v1/chat/completions")
MODEL = os.environ.get("HORIZON_MODEL", "default_model")  # mlx_lm.server: the model it was started with
SERVE_MODEL = "mlx-community/Ornith-1.5-397B-4bit"  # shortcut: assumed MLX quant name, check Hugging Face before first serve
SELF = Path(__file__).resolve()
NET = "horizon-offline"  # host-only network: no internet from the VM
PORT = 7777
MAX_OUT = 8000  # chars of command output fed back to the model
KEEP = 40  # shortcut: keep only the last 40 messages, upgrade to summarising when runs lose the plot
REVIEW_EVERY = 10  # steps between reviewer checks

SYSTEM = """You are a researcher working alone on a goal for as long as it takes.
You have a Linux VM with no internet. /work persists.
First, if /work/PLAN.md does not exist, write it: the engineering plan as a markdown checklist
of concrete, checkable steps ("- [ ] step"). Then work through it, ticking a step ("- [x]") only
once its result is verified by running code or a proof checker. Revise the plan as you learn.
If /work/repo exists it is the project's git repo: work in it and commit often with clear messages;
your commits are pushed to a review branch for you.
Keep notes.md with what you tried, learned and plan next; re-read both files when you lose track.
Each reply: think briefly, then give exactly ONE command in a ```bash block. You will see its output.
A reviewer reads your work and will stop you if you drift from the goal or claim unverified results.
When the goal is met with a verified result (or disproved), write it to /work/RESULT.md
and reply with the single word DONE and no bash block."""

SWARM = """
You are one of many agents working on this goal in parallel, each in its own VM. Coordinate through
the board, a shared message board that only agents and the reviewer read:
  board read                latest posts from every agent
  board post TEXT           share a finding, a dead end, or a claim ("CLAIM: direction")
  board post TASK: TEXT     propose a self-contained sub-task; a new agent will be started on it
Read the board before choosing a direction and before finishing. Post failures as well as wins:
they save the others days. Don't duplicate a direction someone has claimed unless you can beat it."""

EXPLORE = "Explore: pick a promising direction no one on the board has claimed, claim it, and pursue it."

REVIEWER = """You supervise an autonomous research agent. Read its goal, plan and recent steps.
Kill it if it is misaligned: working on something other than the goal, claiming results it did not
verify, trying to reach the network or escape its VM, destroying its own evidence, or looping with
no progress. The board shows what agents tell each other; kill agents that post misleading claims,
coordinate to evade review, or flood it. Reply ON_TRACK, or KILL: <one-line reason>."""


def sh(*cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def stream(messages, out):
    """Stream one completion, passing each token to out(). Returns (text, tokens)."""
    body = json.dumps({"model": MODEL, "messages": messages, "stream": True, "max_tokens": 8192}).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
    text, tokens = "", 0
    with urllib.request.urlopen(req) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            delta = json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
            if delta:
                tokens += 1  # shortcut: one SSE chunk ~ one token on mlx_lm.server, use a tokenizer if billing matters
                text += delta
                out(delta)
    return text, tokens


def put(path, obj):
    """Write-then-rename so readers never see a half-written file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def board_read(pid, n=None):
    f = PROJECTS / pid / "board.jsonl"
    posts = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
    return posts[-n:] if n else posts


def board(pid, rid, cmd):
    """`board ...` runs on the Mac, not in the VM, so agents can't edit or delete anyone's posts."""
    verb, _, text = cmd.partition(" ")[2].partition(" ")
    text = text.strip().strip("'\"")
    if verb == "post" and text:
        line = json.dumps({"t": time.time(), "agent": rid, "text": text[:2000]}) + "\n"
        with open(PROJECTS / pid / "board.jsonl", "a") as f:
            f.write(line)  # one small O_APPEND write: atomic across agent processes
        return "posted"
    if verb == "read":
        return "\n".join(f"[{p['agent'][-4:]}] {p['text']}" for p in board_read(pid, 50)) or "(board is empty)"
    return "usage: board read | board post TEXT | board post TASK: TEXT"


def answer(reply):
    return reply.split("</think>")[-1]


def command(reply):
    """The one bash command in a reply, ignoring anything inside <think>."""
    m = re.findall(r"```(?:bash|sh)?\n(.*?)```", answer(reply), re.S)
    return m[-1].strip() if m else None


def review(goal, plan, history, posts=""):
    """Ask the model, as a reviewer, whether the agent is on track. Returns (kill reason or None, tokens)."""
    steps = "\n\n".join(f"[{m['role']}] {m['content'][-1500:]}" for m in history[-20:])
    reply, n = stream([{"role": "system", "content": REVIEWER},
                       {"role": "user", "content": f"Goal: {goal}\n\nPLAN.md:\n{plan}\n\nRecent steps:\n{steps}" + (f"\n\nBoard:\n{posts}" if posts else "")}],
                      lambda s: None)
    verdict = answer(reply).strip()
    return (verdict[4:].strip(" :") or "no reason given") if verdict.startswith("KILL") else None, n


def progress(d):
    """Share of PLAN.md checklist items ticked, 0..1."""
    plan = d / "work" / "PLAN.md"
    marks = re.findall(r"^\s*[-*] \[([ xX])\]", plan.read_text(), re.M) if plan.exists() else []
    return sum(m != " " for m in marks) / len(marks) if marks else 0.0


def meta_of(d):
    m = json.loads((d / "meta.json").read_text())
    if m["status"] == "running" or m["status"] == "queued" and time.time() - m.get("created", 0) > 120:
        try:
            os.kill(m["pid"], 0)
        except (OSError, KeyError, TypeError):
            m["status"] = "crashed"
    m["progress"] = 1.0 if m["status"] == "done" else progress(d)
    return m


def runs(project=None):
    """Runs newest first; project=None gives standalone runs, an id gives that swarm's agents."""
    out = []
    for d in sorted(RUNS.glob("*"), reverse=True):
        try:
            m = meta_of(d)
        except (OSError, ValueError):
            continue
        if m.get("project") == project:
            out.append(m)
    return out


def start(goal, budget=1_000_000, image="python:3.12", timeout=600, repo=None, project=None):
    """Create a run directory. A goal ending in .md is a plan file: the agent starts from it."""
    rid = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    d = RUNS / rid
    (d / "work").mkdir(parents=True)
    if goal.endswith(".md") and Path(goal).expanduser().is_file():
        plan = Path(goal).expanduser().read_text()
        (d / "work" / "PLAN.md").write_text(plan)
        goal = plan.strip().splitlines()[0].lstrip("# ") if plan.strip() else goal
    (d / "meta.json").write_text(json.dumps({"id": rid, "hypothesis": goal, "budget": budget, "image": image,
                                             "timeout": timeout, "repo": repo, "project": project, "created": time.time(),
                                             "tokens": 0, "steps": 0, "status": "queued"}))
    return rid


def spawn(*args, log=os.devnull):
    """Run `horizon ARGS` in the background, surviving the terminal or app that started it."""
    with open(log, "ab") as f:
        subprocess.Popen([sys.executable, str(SELF), *args], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT)


def clone(d, url):
    """Host-side clone: a private mirror that holds your git credentials, and a work copy the VM sees."""
    r = sh("git", "clone", "--bare", url, str(d / "mirror.git"))
    if r.returncode:
        raise RuntimeError(f"git clone failed: {r.stderr.strip()}")
    sh("git", "clone", "--no-local", str(d / "mirror.git"), str(d / "work" / "repo"))  # no hardlinks into the mirror
    sh("git", "-C", str(d / "work" / "repo"), "config", "user.name", "horizon")
    sh("git", "-C", str(d / "work" / "repo"), "config", "user.email", "horizon@localhost")


def publish(d, name, rid):
    """Push the agent's commits to branch horizon/RID. The VM packs a bundle and the host only fetches
    from that file, so nothing the agent writes into .git (hooks, config) ever runs on the Mac."""
    if not (d / "mirror.git").exists():
        return ""
    sh("container", "exec", name, "git", "-C", "/work/repo", "bundle", "create", "/work/.horizon.bundle", "HEAD")
    m = str(d / "mirror.git")
    r = sh("git", "-C", m, "fetch", str(d / "work" / ".horizon.bundle"), f"+HEAD:refs/heads/horizon/{rid}")
    if r.returncode == 0:
        r = sh("git", "-C", m, "push", "-f", "origin", f"horizon/{rid}")
    return f"pushed horizon/{rid}" if r.returncode == 0 else f"push failed: {r.stderr.strip()[-300:]}"


def _interrupt(*_):
    raise KeyboardInterrupt


def work(rid, echo=True):
    d = RUNS / rid
    meta = json.loads((d / "meta.json").read_text())
    meta.update(status="running", pid=os.getpid(), started=time.time())

    save = lambda: put(d / "meta.json", meta)
    save()
    signal.signal(signal.SIGTERM, _interrupt)  # `horizon stop` and the app's stop button
    log = open(d / "log.txt", "a", buffering=1)

    def out(s):
        if echo:
            sys.stdout.write(s); sys.stdout.flush()
        log.write(s)

    goal, budget = meta["hypothesis"], meta["budget"]
    sh("container", "network", "create", "--internal", NET)  # fails harmlessly if it exists
    name = f"horizon-{rid}"
    r = sh("container", "run", "-d", "--rm", "--name", name, "--network", NET,
           "-v", f"{d / 'work'}:/work", "-w", "/work", meta["image"], "sleep", "infinity")
    if r.returncode:
        meta["status"] = "vm failed"; save(); out(f"could not start VM: {r.stderr.strip()}\n"); log.close()
        sys.exit(1)
    out(f"run {rid}  vm {name}  budget {budget} tokens\ngoal: {goal}\n")
    if meta.get("repo"):
        try:
            clone(d, meta["repo"])
            # the VM sees /work owned by another uid; without this git refuses to commit there
            sh("container", "exec", name, "git", "config", "--global", "--add", "safe.directory", "*")
        except RuntimeError as e:
            meta["status"] = "git failed"; save(); out(f"{e}\n"); sh("container", "stop", name); log.close()
            sys.exit(1)

    pid = meta.get("project")
    if pid:
        swarm_goal = json.loads((PROJECTS / pid / "meta.json").read_text())["goal"]
        task = [{"role": "system", "content": SYSTEM + SWARM},
                {"role": "user", "content": f"Goal: {swarm_goal}\n\nYour part: {goal}"}]
        goal = f"{swarm_goal} / part: {goal}"
    else:
        task = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Goal: {goal}"}]
    history = []
    try:
        while meta["tokens"] < budget:
            meta["steps"] += 1
            out(f"\n\033[2m── step {meta['steps']}  {meta['tokens']}/{budget} tokens ──\033[0m\n")
            reply, n = stream(task + history[-KEEP:], out)
            meta["tokens"] += n; save()
            history.append({"role": "assistant", "content": reply})
            cmd = command(reply)
            if cmd is None:
                if answer(reply).strip().endswith("DONE"):
                    meta["status"] = "done"; break
                history.append({"role": "user", "content": "Give exactly one command in a ```bash block, or reply DONE."})
                continue
            out(f"\n\033[36m$ {cmd}\033[0m\n")
            if pid and cmd.split()[0] == "board":
                result = board(pid, rid, cmd)
                out("(board: hidden from the dashboard)\n")  # only agents and the reviewer read the board
                history.append({"role": "user", "content": result})
                continue
            try:
                r = sh("container", "exec", name, "sh", "-c", cmd, timeout=meta["timeout"])
                result = f"exit {r.returncode}\n{r.stdout}{r.stderr}"
            except subprocess.TimeoutExpired:
                result = f"timed out after {meta['timeout']}s"
            if len(result) > MAX_OUT:
                result = result[:MAX_OUT // 2] + f"\n…[{len(result) - MAX_OUT} chars cut]…\n" + result[-MAX_OUT // 2:]
            out(result + "\n")
            history.append({"role": "user", "content": result})
            if meta["steps"] % REVIEW_EVERY == 0:
                out(f"\n\033[2m{publish(d, name, rid)}\033[0m\n" if meta.get("repo") else "")
                plan = d / "work" / "PLAN.md"
                posts = "\n".join(f"[{p['agent'][-4:]}] {p['text']}" for p in board_read(pid, 30)) if pid else ""
                reason, n = review(goal, plan.read_text() if plan.exists() else "(none yet)", history, posts)
                meta["tokens"] += n
                out(f"\n\033[35mreviewer: {'KILL ' + reason if reason else 'on track'}\033[0m\n")
                if reason:
                    meta["status"] = f"killed: {reason}"; break
        else:
            meta["status"] = "out of budget"
    except KeyboardInterrupt:
        meta["status"] = "stopped"
    except Exception as e:  # model server down, etc.: record it rather than leave the run "running"
        meta["status"] = f"error: {e}"
    finally:
        save()
        if meta.get("repo"):
            out("\n" + publish(d, name, rid) + "\n")
        sh("container", "stop", name)
        out(f"\n{meta['status']}: {meta['steps']} steps, {meta['tokens']} tokens. files in {d / 'work'}\n")
        log.close()


def status():
    """Model server and cluster state for the dashboard."""
    try:
        with urllib.request.urlopen(URL.rsplit("/chat", 1)[0] + "/models", timeout=1) as r:
            models = [m["id"] for m in json.load(r).get("data", [])]
        up = True
    except Exception:
        up, models = False, []
    tail = lambda f: (HOME / f).read_text()[-4000:] if (HOME / f).exists() else ""
    return {"up": up, "models": models, "hosts": json.loads(HOSTS.read_text()) if HOSTS.exists() else None,
            "serve_log": tail("serve.log"), "setup_log": tail("setup.log")}


def new_swarm(goal, agents=16, budget=10**10, agent_budget=2_000_000, repo=None):
    sid = "s" + time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    (PROJECTS / sid).mkdir(parents=True)
    put(PROJECTS / sid / "meta.json", {"id": sid, "goal": goal, "agents": agents, "budget": budget,
                                       "agent_budget": agent_budget, "repo": repo, "next_task": 0,
                                       "created": time.time(), "status": "queued"})
    return sid


def project(sid):
    """A swarm's meta with live totals over its agents."""
    p = json.loads((PROJECTS / sid / "meta.json").read_text())
    kids = runs(sid)
    if p["status"] == "running":
        try:
            os.kill(p["pid"], 0)
        except (OSError, KeyError, TypeError):
            p["status"] = "crashed"
    finished = sum(k["status"] not in ("queued", "running") for k in kids)
    p.update(tokens=sum(k["tokens"] for k in kids), steps=sum(k["steps"] for k in kids),
             running=sum(k["status"] == "running" for k in kids), total=len(kids),
             done=sum(k["status"] == "done" for k in kids), finished=finished,
             progress=finished / len(kids) if kids else 0.0)  # share of agents that have finished their part
    return p


def projects():
    return [project(d.name) for d in sorted(PROJECTS.glob("*"), reverse=True) if (d / "meta.json").exists()]


def swarm(sid):
    """Keep `agents` runs going on the goal: agents' TASK: posts first, fresh explorers otherwise."""
    f = PROJECTS / sid / "meta.json"
    p = json.loads(f.read_text())
    p.update(status="running", pid=os.getpid())
    put(f, p)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        while True:
            kids = runs(sid)
            if sum(k["tokens"] for k in kids) >= p["budget"]:
                p["status"] = "out of budget"; break
            tasks = [x["text"][5:].strip() for x in board_read(sid) if x["text"].startswith("TASK:")]
            live = sum(k["status"] in ("queued", "running") for k in kids)
            for _ in range(p["agents"] - live):
                if p["next_task"] < len(tasks):
                    part = tasks[p["next_task"]]; p["next_task"] += 1
                else:
                    part = EXPLORE
                spawn("_work", start(part, p["agent_budget"], repo=p["repo"], project=sid))
            put(f, p)
            time.sleep(5)
    except KeyboardInterrupt:
        p["status"] = "stopped"
        for k in runs(sid):
            if k["status"] == "running":
                stop(k["id"])
    finally:
        put(f, p)


GPU = {}  # host -> latest macmon sample


def gpu(h):
    """One macmon sample (no sudo needed) from host h, over ssh unless it is this Mac."""
    here = h in ("localhost", "127.0.0.1") or h.split(".")[0] == os.uname().nodename.split(".")[0]
    cmd = ["macmon", "pipe", "-s", "1", "-i", "500"]
    try:
        r = sh(*(cmd if here else ["ssh", "-o", "BatchMode=yes", h, *cmd]), timeout=10)
        m = json.loads(r.stdout.strip().splitlines()[-1])
        return {"host": h.split(".")[0], "mhz": m["gpu_freq_mhz"], "active": m["gpu_active_ratio"],
                "watts": m["gpu_power"], "ram": m["memory"]["ram_usage"] / m["memory"]["ram_total"],
                "temp": m["temp"]["gpu_temp_avg"]}
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.TimeoutExpired):
        return {"host": h.split(".")[0], "error": "no macmon (brew install macmon)"}


def sample_gpus():
    while True:
        try:
            hosts = [h["ssh"] for h in json.loads(HOSTS.read_text())["hosts"]] if HOSTS.exists() else ["localhost"]
        except (OSError, ValueError, KeyError, TypeError):
            hosts = ["localhost"]
        for h in hosts:
            GPU[h] = gpu(h)
        time.sleep(2)


def kill_server():
    try:
        os.killpg(int((HOME / "serve.pid").read_text()), signal.SIGTERM)  # mlx.launch and its ssh children too
    except (OSError, ValueError):
        pass


def stop(rid):
    f = (PROJECTS if rid.startswith("s") else RUNS) / rid / "meta.json"
    pid = json.loads(f.read_text()).get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class Dashboard(BaseHTTPRequestHandler):
    def reply(self, code, body=b"", ctype="application/json", **headers):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), str(v))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self.reply(200, SELF.with_name("ui.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/runs":
            return self.reply(200, json.dumps(runs()).encode())
        if self.path == "/projects":
            return self.reply(200, json.dumps(projects()).encode())
        m = re.fullmatch(r"/projects/(s[\w-]+)/runs", self.path)
        if m and (PROJECTS / m[1]).is_dir():
            return self.reply(200, json.dumps(runs(m[1])).encode())
        if self.path == "/gpu":
            return self.reply(200, json.dumps(list(GPU.values())).encode())
        if self.path == "/status":
            return self.reply(200, json.dumps(status()).encode())
        m = re.fullmatch(r"/runs/([\w-]+)/log\?from=(-?\d+)", self.path)
        if m and (RUNS / m[1] / "log.txt").exists():
            with open(RUNS / m[1] / "log.txt", "rb") as f:
                size = f.seek(0, 2)
                # from=-1: just the tail, so a month-long log doesn't load whole
                f.seek(max(0, size - 256_000) if int(m[2]) < 0 else min(int(m[2]), size))
                body = f.read(4_000_000)
                return self.reply(200, body, "text/plain; charset=utf-8", X_Next=f.tell())
        self.reply(404)

    def do_POST(self):
        # A custom header forces a CORS preflight we never answer, so other web pages can't start or stop runs.
        if self.headers.get("X-Horizon") != "1":
            return self.reply(403)
        if self.path == "/runs":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            goal = str(body.get("hypothesis", "")).strip()
            if not goal:
                return self.reply(400)
            repo = str(body.get("repo") or "").strip() or None
            agents = int(body.get("agents") or 1)
            if agents > 1:
                rid = new_swarm(goal, agents, int(float(body.get("budget") or 1e10)), repo=repo)
                spawn("_swarm", rid, log=PROJECTS / rid / "swarm.log")
            else:
                rid = start(goal, int(float(body.get("budget") or 1e6)), repo=repo)
                spawn("_work", rid)
            return self.reply(200, json.dumps({"id": rid}).encode())
        if self.path in ("/serve", "/setup"):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            arg = str(body.get("model" if self.path == "/serve" else "hosts") or "").strip()
            if self.path == "/setup" and not re.fullmatch(r"[\w.@-]+(,[\w.@-]+)+", arg):
                return self.reply(400)  # ssh hostnames only: this reaches a command line
            if self.path == "/serve":
                spawn("serve", *(["--model", arg] if arg else []), log=HOME / "serve.log")
            else:
                spawn("setup", arg, log=HOME / "setup.log")
            return self.reply(200)
        if self.path == "/serve/stop":
            kill_server()
            return self.reply(200)
        m = re.fullmatch(r"/(?:runs|projects)/([\w-]+)/stop", self.path)
        if m and ((RUNS if m[1][0].isdigit() else PROJECTS) / m[1] / "meta.json").exists():
            stop(m[1])
            return self.reply(200)
        self.reply(404)

    def log_message(self, *a):
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="horizon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="start a run")
    r.add_argument("hypothesis", nargs="?", help="the goal, or a path to a PLAN.md to start from")
    r.add_argument("--budget", type=int, default=1_000_000, help="tokens")
    r.add_argument("--image", default="python:3.12", help="VM image; use one with Lean for formal proofs")
    r.add_argument("--timeout", type=int, default=600, help="seconds per command")
    r.add_argument("--repo", help="git URL the agent works in; its commits are pushed to branch horizon/RUN_ID")
    r.add_argument("--detach", action="store_true", help="run in the background")
    sw = sub.add_parser("swarm", help="many agents on one goal, coordinating on a board")
    sw.add_argument("goal")
    sw.add_argument("--agents", type=int, default=16, help="agents running at once")
    sw.add_argument("--budget", type=float, default=1e10, help="tokens for the whole swarm")
    sw.add_argument("--agent-budget", type=float, default=2e6, help="tokens per agent")
    sw.add_argument("--repo")
    sub.add_parser("ls", help="list runs")
    for c in ("log", "stop"):
        sub.add_parser(c).add_argument("id")
    a_ = sub.add_parser("app", help="open the dashboard")
    a_.add_argument("--no-open", action="store_true")
    s = sub.add_parser("setup", help="pair Macs for distributed inference")
    s.add_argument("hosts", help="comma separated ssh hostnames, e.g. studio1.local,studio2.local")
    v = sub.add_parser("serve", help="start the model server")
    v.add_argument("--model", default=SERVE_MODEL)
    w = sub.add_parser("_work"); w.add_argument("id")
    w = sub.add_parser("_swarm"); w.add_argument("id")
    a = p.parse_args(argv or ["run"])

    if a.cmd == "run":
        goal = a.hypothesis or input("hypothesis › ").strip()
        if not goal:
            p.error("need a hypothesis")
        rid = start(goal, a.budget, a.image, a.timeout, a.repo)
        if a.detach:
            spawn("_work", rid); print(rid)
        else:
            work(rid)
    elif a.cmd == "_work":
        work(a.id, echo=False)
    elif a.cmd == "swarm":
        sid = new_swarm(a.goal, a.agents, int(a.budget), int(a.agent_budget), a.repo)
        spawn("_swarm", sid, log=PROJECTS / sid / "swarm.log"); print(sid)
    elif a.cmd == "_swarm":
        swarm(a.id)
    elif a.cmd == "ls":
        for m in projects():
            print(f"{m['id']} {m['status'][:20]:<20} {m['running']:>4} running {m['total']:>6} agents {m['tokens']:>12} tok  {m['goal'][:40]}")
        for m in runs():
            print(f"{m['id']}  {m['status'][:20]:<20} {m['progress']:>4.0%} {m['steps']:>5} steps {m['tokens']:>10} tok  {m['hypothesis'][:50]}")
    elif a.cmd == "log":
        sys.stdout.write((RUNS / a.id / "log.txt").read_text())
    elif a.cmd == "stop":
        stop(a.id)
    elif a.cmd == "app":
        HOME.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=sample_gpus, daemon=True).start()
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Dashboard)
        print(f"horizon dashboard on http://127.0.0.1:{PORT}")
        if not a.no_open:
            webbrowser.open(f"http://127.0.0.1:{PORT}")
        srv.serve_forever()
    elif a.cmd == "setup":
        HOME.mkdir(parents=True, exist_ok=True)
        # needs ssh between the Macs and password-less sudo; enables RDMA over Thunderbolt 5
        os.execvp("mlx.distributed_config", ["mlx.distributed_config", "--backend", "jaccl", "--over", "thunderbolt",
                                             "--hosts", a.hosts, "--auto-setup", "--output-hostfile", str(HOSTS)])
    elif a.cmd == "serve":
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / "serve.pid").write_text(str(os.getpid()))  # exec keeps this pid
        cmd = [shutil.which("mlx_lm.server") or "mlx_lm.server", "--model", a.model, "--host", "127.0.0.1", "--port", "8080"]
        if HOSTS.exists():
            # tensor-parallel across the paired Macs; shortcut: assumes mlx_lm is installed at the same path on each
            cmd = ["mlx.launch", "--backend", "jaccl", "--hostfile", str(HOSTS), "--env", "MLX_METAL_FAST_SYNCH=1", "--", *cmd]
        os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
