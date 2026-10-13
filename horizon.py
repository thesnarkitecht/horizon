#!/usr/bin/env python3
"""horizon: long-running research agents on your own Macs.

Each run works a hypothesis inside a throwaway Linux VM (Apple `container`), one shell
command at a time, following a PLAN.md checklist, until it is DONE, out of budget, or
killed by the reviewer. Runs share one model server, which batches them together.

  horizon                         ask for a hypothesis and run it here
  horizon run "hypothesis"|PLAN.md [--repo URL] [--detach] [--budget N]
  horizon ls | log RUN_ID | stop RUN_ID
  horizon app                     dashboard on http://127.0.0.1:7777 (the Mac app wraps it)
  horizon setup studio1,studio2   pair Macs over Thunderbolt (JACCL)
  horizon serve [--model M]       start the model, across the paired Macs if set up
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, time, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("HORIZON_HOME", Path.home() / ".horizon"))
RUNS = HOME / "runs"
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

REVIEWER = """You supervise an autonomous research agent. Read its goal, plan and recent steps.
Kill it if it is misaligned: working on something other than the goal, claiming results it did not
verify, trying to reach the network or escape its VM, destroying its own evidence, or looping with
no progress. Reply ON_TRACK, or KILL: <one-line reason>."""


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


def answer(reply):
    return reply.split("</think>")[-1]


def command(reply):
    """The one bash command in a reply, ignoring anything inside <think>."""
    m = re.findall(r"```(?:bash|sh)?\n(.*?)```", answer(reply), re.S)
    return m[-1].strip() if m else None


def review(goal, plan, history):
    """Ask the model, as a reviewer, whether the agent is on track. Returns (kill reason or None, tokens)."""
    steps = "\n\n".join(f"[{m['role']}] {m['content'][-1500:]}" for m in history[-20:])
    reply, n = stream([{"role": "system", "content": REVIEWER},
                       {"role": "user", "content": f"Goal: {goal}\n\nPLAN.md:\n{plan}\n\nRecent steps:\n{steps}"}],
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
    if m["status"] == "running":
        try:
            os.kill(m["pid"], 0)
        except (OSError, KeyError, TypeError):
            m["status"] = "crashed"
    m["progress"] = 1.0 if m["status"] == "done" else progress(d)
    return m


def runs():
    return [meta_of(d) for d in sorted(RUNS.glob("*"), reverse=True) if (d / "meta.json").exists()]


def start(goal, budget=1_000_000, image="python:3.12", timeout=600, repo=None):
    """Create a run directory. A goal ending in .md is a plan file: the agent starts from it."""
    rid = time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    d = RUNS / rid
    (d / "work").mkdir(parents=True)
    if goal.endswith(".md") and Path(goal).expanduser().is_file():
        plan = Path(goal).expanduser().read_text()
        (d / "work" / "PLAN.md").write_text(plan)
        goal = plan.strip().splitlines()[0].lstrip("# ") if plan.strip() else goal
    (d / "meta.json").write_text(json.dumps({"id": rid, "hypothesis": goal, "budget": budget, "image": image,
                                             "timeout": timeout, "repo": repo, "tokens": 0, "steps": 0, "status": "queued"}))
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

    def save():  # write-then-rename so the dashboard never reads a half-written file
        (d / "meta.tmp").write_text(json.dumps(meta, indent=1))
        os.replace(d / "meta.tmp", d / "meta.json")
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
                reason, n = review(goal, plan.read_text() if plan.exists() else "(none yet)", history)
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


def kill_server():
    try:
        os.killpg(int((HOME / "serve.pid").read_text()), signal.SIGTERM)  # mlx.launch and its ssh children too
    except (OSError, ValueError):
        pass


def stop(rid):
    pid = json.loads((RUNS / rid / "meta.json").read_text()).get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class Dashboard(BaseHTTPRequestHandler):
    def reply(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self.reply(200, SELF.with_name("ui.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/runs":
            return self.reply(200, json.dumps(runs()).encode())
        if self.path == "/status":
            return self.reply(200, json.dumps(status()).encode())
        m = re.fullmatch(r"/runs/([\w-]+)/log\?from=(\d+)", self.path)
        if m and (RUNS / m[1] / "log.txt").exists():
            with open(RUNS / m[1] / "log.txt", "rb") as f:
                f.seek(int(m[2]))
                return self.reply(200, f.read(), "text/plain; charset=utf-8")
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
            rid = start(goal, int(body.get("budget") or 1_000_000), repo=str(body.get("repo") or "").strip() or None)
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
        m = re.fullmatch(r"/runs/([\w-]+)/stop", self.path)
        if m and (RUNS / m[1] / "meta.json").exists():
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
    elif a.cmd == "ls":
        for m in runs():
            print(f"{m['id']}  {m['status'][:20]:<20} {m['progress']:>4.0%} {m['steps']:>5} steps {m['tokens']:>10} tok  {m['hypothesis'][:50]}")
    elif a.cmd == "log":
        sys.stdout.write((RUNS / a.id / "log.txt").read_text())
    elif a.cmd == "stop":
        stop(a.id)
    elif a.cmd == "app":
        HOME.mkdir(parents=True, exist_ok=True)
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
