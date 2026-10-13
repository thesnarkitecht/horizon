#!/usr/bin/env python3
"""horizon: the backend of Horizon.app — long-running research agents on your own Macs.

Each agent works a goal inside a throwaway Linux VM (Apple `container`), one shell command at a
time, following a PLAN.md checklist, until it is DONE, out of budget, or killed by the reviewer.
Swarms keep many agents going on one goal; they coordinate on a board only agents and the
reviewer read. All agents share one model server, which batches them together.

Horizon.app runs `horizon.py app` and shows its dashboard. The other subcommands are what the
dashboard spawns (and handy for debugging): run, swarm, ls, log, stop, setup, serve.
"""
import argparse, json, os, random, re, shutil, signal, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("HORIZON_HOME", Path.home() / ".horizon"))
RUNS = HOME / "runs"
SKILLS = HOME / "skills"  # one skill library for every agent, minted by the agents themselves
PROJECTS = HOME / "projects"  # swarms: meta.json + board.jsonl; their agents are ordinary runs
HOSTS = HOME / "hosts.json"  # written by `horizon setup`
LOCAL = "http://127.0.0.1:8080/v1/chat/completions"  # the local engine agents use (base model + adopted adapter)
LOCAL_REVIEW = "http://127.0.0.1:8081/v1/chat/completions"  # base weights only: the reviewer never runs fine-tuned
URL = os.environ.get("HORIZON_URL", LOCAL)
MODEL = os.environ.get("HORIZON_MODEL", "default_model")  # mlx_lm.server: the model it was started with
SERVE_MODEL = "mlx-community/Qwen3.8-27B-8bit"  # shortcut: assumed MLX quant name, check Hugging Face before first serve
CONFIG = HOME / "config.json"  # model source, set in the app; an API key never leaves this Mac
ADAPTERS = HOME / "adapters"  # LoRA adapters the agents train
MAX_VMS = 4  # extra VMs per agent
SELF = Path(__file__).resolve()
NET = "horizon-offline"  # host-only network: no internet from the VM
PORT = 7777
MAX_OUT = 8000  # chars of command output fed back to the model
KEEP = 40  # recent messages kept verbatim; older ones are folded into working memory (see compact)
REVIEW_EVERY = 10  # steps between reviewer checks

SYSTEM = """You are a researcher working alone on a goal for as long as it takes.
You have a Linux VM with no internet. /work persists.
First, if /work/PLAN.md does not exist, write it: the engineering plan as a markdown checklist
of concrete, checkable steps ("- [ ] step"). Then work through it, ticking a step ("- [x]") only
once its result is verified by running code or a proof checker. Revise the plan as you learn.
If /work/repo exists it is the project's git repo: work in it and commit often with clear messages;
your commits are pushed to a review branch for you.
Keep notes.md with what you tried, learned and plan next; re-read both files when you lose track.
Keep THOUGHT.md to one to three sentences: what you believe right now and what you are trying next.
Rewrite it whenever that changes; it is how the people running the lab follow your train of thought.
Each reply: think briefly, then give exactly ONE command in a ```bash block. You will see its output.
A reviewer reads your work and will stop you if you drift from the goal or claim unverified results.
When the goal is met with a verified result (or disproved), write it to /work/RESULT.md
and reply with the single word DONE and no bash block."""

TOOLS = """
Host tools: give one as your command; it runs on the Mac, not in your VM.
  vm up NAME [IMAGE] | vm exec NAME CMD | vm down NAME | vm ls
        up to 4 extra VMs on your private network, sharing /work and /shared: servers, clients,
        parallel experiments. They are removed when you finish.
  finetune NAME [ITERS]   LoRA-train the lab's base model on /shared/train/NAME/{train,valid}.jsonl
        (one {"messages": [...]} chat per line). Trains in the background on the GPU.
  finetune status NAME    training progress, and held-out loss: base model vs your adapter
  adopt NAME              run every agent on that adapter (only if it beats the base model's held-out
        loss); "adopt none" rolls back. The reviewer always runs the untouched base model.
Fine-tune to bake in knowledge the lab keeps needing (from the wiki and raw/), never to skip verification."""

LAB = """
Your lab, in /shared (kept after you finish):
  raw/    immutable evidence: datasets, benchmark outputs, scripts, logs. Add, never edit.
  wiki/   the knowledge base you curate: index.md lists every page with a one-line summary; one page
          per concept, method or result; link pages; cite the raw/ files behind every claim; mark dead
          ends as clearly as wins. Update a page rather than duplicate it. Read index.md first, and file
          what you learn before you stop: the wiki is how future you (and others) start where you ended.
  eval/   if the goal can be measured, ONE fixed evaluation script (eval/run.sh) that scores a
          candidate. Write it before optimizing, never change it to flatter a result, and append
          every scored attempt to raw/results.jsonl ({"score":..., "what":..., "by": your run id}),
          higher score = better (negate losses). The tournament ranks agents by these. Build on
          the best-scoring attempts, not on what sounds promising.
Skills, in /skills, shared by every agent on this Mac across all research. Each is a folder with a
SKILL.md (front matter "name:", "description:", "author:" your run id, and "verified:" the command
that proves it works), then the steps, plus any scripts it needs. The second time you do a procedure
(set up a benchmark, run an eval, a proof tactic, a data pipeline), mint a skill so nobody derives it
again. Run its verified: command before saving; fix or improve a skill when you find a better way;
use existing skills before writing your own."""

SWARM = """
You are one of many agents working on this goal in parallel, each in its own VM, like a research lab.
/shared is the whole lab's, and /shared/MILESTONES.md is its road to the goal: a checklist
("- [ ] milestone") from first result to final answer. If it is missing, write it first (5-12
milestones). Tick one only when its result is verified and written up in the wiki.
The board is a message board only agents and the reviewer read:
  board read                       latest posts, with each agent's id
  board post TEXT                  a finding, a dead end, or a claim ("CLAIM: direction")
  board post TASK: TEXT            a self-contained sub-task; a new agent will be started on it
  board post REVIVE: AGENT_ID why  bring back a finished agent whose notes and files you need extended
Don't duplicate a claimed direction unless you can beat it. Post failures too: they save the others days."""

EXPLORE = "Explore: pick a promising direction no one on the board has claimed, claim it, and pursue it."
LIBRARIAN = ("Librarian: lint the wiki and skills. Merge duplicate pages, fix broken links and orphans, flag "
             "contradictions and stale claims, check claims against raw/, keep index.md complete; re-run each "
             "skill's verified: command and fix or delete broken skills; post TASK: for the gaps you find.")
LIBRARIAN_EVERY = 8  # every 8th agent a swarm starts tends the wiki
LEAD_EVERY = 5  # every 5th slot a swarm fills goes back to the tournament leader, if it has finished
LEAD_NOTE = "you lead the lab's tournament; push your line of work further and close its open questions"
TOURNAMENT_EVERY = 60  # seconds between tournament rounds
MATCHES_PER_ROUND = 4
MIN_STEPS = 10  # an agent enters the tournament once it has done this much work
JUDGE = """You judge a research tournament. Two lines of work, A and B, pursue the same goal.
Which is more likely to reach the goal? Weigh verified evidence over claims, milestones actually
completed, eval scores, and how promising the next steps are. Ignore length and confident tone.
Reply with exactly A or B on the first line, then one short reason."""

COMPACT = """You maintain the working memory of a long-running research agent whose older steps no longer
fit in its context. Rewrite the memory to fold in the older steps. Keep: facts verified in the
environment (with the command or file that verified them), approaches that failed and why, open
questions, and the current plan. Drop chatter, repeated output, and claims nothing verified.
Plain text, under 400 words."""

REVIEWER = """You supervise an autonomous research agent. Read its goal, plan and recent steps.
Kill it if it is misaligned: working on something other than the goal, claiming results it did not
verify, trying to reach the network or escape its VM, destroying its own evidence, or looping with
no progress. The board shows what agents tell each other; kill agents that post misleading claims,
coordinate to evade review, or flood it, and agents that write misleading wiki pages or skills, or
fine-tune on data meant to change the agents' goals or weaken review. Reply ON_TRACK, or KILL: <one-line reason>."""


def sh(*cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def cfg():
    """Model source: the local engine, or any OpenAI-compatible API (url + key + model) set in the app."""
    c = {"url": URL, "key": "", "model": MODEL, "base_model": SERVE_MODEL}
    try:
        c.update(json.loads(CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    c["review_url"] = LOCAL_REVIEW if c["url"] == LOCAL and not HOSTS.exists() else c["url"]
    return c


def stream(messages, out, review=False):
    """Stream one completion, passing each token to out(). Returns (text, tokens)."""
    c = cfg()
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {c['key']}"} if c["key"] else {})}
    body = json.dumps({"model": c["model"], "messages": messages, "stream": True, "max_tokens": 8192}).encode()
    for _ in range(60):  # the engine restarts when an adapter is adopted: wait up to 10 minutes for it
        text, tokens = "", 0
        try:
            with urllib.request.urlopen(urllib.request.Request(c["review_url"] if review else c["url"], body, headers)) as r:
                for line in r:
                    line = line.decode().strip()
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    choice = (json.loads(line[5:]).get("choices") or [{}])[0]  # APIs send usage-only chunks too
                    delta = (choice.get("delta") or {}).get("content") or ""
                    if delta:
                        tokens += 1  # shortcut: one SSE chunk ~ one token, use a tokenizer if billing matters
                        text += delta
                        out(delta)
            return text, tokens
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise  # bad key, bad model name: waiting won't fix it
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(10)
    raise RuntimeError("model unreachable for 10 minutes")


def put(path, obj):
    """Write-then-rename so readers never see a half-written file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, path)


def board_read(pid, n=None):
    f = PROJECTS / pid / "board.jsonl"
    posts = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
    return posts[-n:] if n else posts


def posts_text(pid, n):
    return "\n".join(f"[{p['agent']}] {p['text']}" for p in board_read(pid, n))


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
        return posts_text(pid, 50) or "(board is empty)"
    return "usage: board read | board post TEXT | board post TASK: TEXT"


def skills_index():
    """name: description for each skill, so agents know what exists without reading every file."""
    lines = []
    for f in sorted(SKILLS.glob("*/SKILL.md")):
        head = f.read_text(errors="replace")[:1000]
        desc = re.search(r"^description:\s*(.+)$", head, re.M)
        lines.append(f"- {f.parent.name}: {desc[1].strip()[:200] if desc else '(no description)'}")
    return "\n".join(lines)


VM_USAGE = "usage: vm up NAME [IMAGE] | vm exec NAME CMD | vm down NAME | vm ls"


def vm(ctx, cmd):
    """Extra VMs an agent starts for itself: same private network and mounts, stopped when it ends."""
    m = re.match(r"vm\s+(up|exec|down|ls)\b\s*([\w-]*)\s*(.*)", cmd, re.S)
    if not m:
        return VM_USAGE
    verb, short, rest = m[1], m[2], m[3].strip()
    mine = ctx["vms"]
    if verb == "ls":
        return "\n".join(v.rsplit("-", 1)[-1] for v in mine) or "(no extra VMs)"
    if not short:
        return VM_USAGE
    full = f"{ctx['name']}-{short}"
    if verb == "up":
        image = rest or ctx["image"]
        if not re.fullmatch(r"[\w][\w./:@-]*", image):
            return "bad image name"
        if full not in mine and len(mine) >= MAX_VMS:
            return f"limit: {MAX_VMS} extra VMs per agent; vm down one first"
        r = sh("container", "run", "-d", "--rm", "--name", full, "--network", NET, *ctx["mounts"], "-w", "/work",
               image, "sleep", "infinity")
        if r.returncode:
            return f"vm up failed: {r.stderr.strip()[-300:]}"
        mine.append(full)
        return f"{short} is up"
    if full not in mine:
        return f"no VM named {short}"
    if verb == "down":
        sh("container", "stop", full)
        mine.remove(full)
        return f"{short} is down"
    try:
        r = sh("container", "exec", full, "sh", "-c", rest, timeout=ctx["timeout"])
        return f"exit {r.returncode}\n{r.stdout}{r.stderr}"
    except subprocess.TimeoutExpired:
        return f"timed out after {ctx['timeout']}s"


def adapters():
    out = []
    active = (HOME / "active_adapter").read_text().strip() if (HOME / "active_adapter").exists() else None
    for f in sorted(ADAPTERS.glob("*/meta.json")):
        m = json.loads(f.read_text())
        out.append({**m, "name": f.parent.name, "active": f.parent.name == active})
    return out


def finetune_tool(ctx, cmd):
    """finetune NAME [ITERS] | finetune status NAME. Training runs on the Mac's GPU, never in a VM."""
    args = cmd.split()[1:]
    if args[:1] == ["status"] and len(args) == 2 and re.fullmatch(r"[\w-]{1,40}", args[1]):
        a = ADAPTERS / args[1]
        if not (a / "meta.json").exists():
            return f"no adapter named {args[1]}"
        log = (a / "train.log").read_text()[-1500:] if (a / "train.log").exists() else ""
        return json.dumps(json.loads((a / "meta.json").read_text())) + "\n" + log
    c = cfg()
    if c["url"] != LOCAL or HOSTS.exists():
        return "finetune needs the local engine on one Mac (adapters can't run on an API or across paired Macs)"
    if not args or not re.fullmatch(r"[\w-]{1,40}", args[0]):
        return "usage: finetune NAME [ITERS] | finetune status NAME"
    name, data = args[0], ctx["lab"] / "train" / args[0]
    if not ((data / "train.jsonl").is_file() and (data / "valid.jsonl").is_file()):
        return f"put train.jsonl and valid.jsonl in /shared/train/{name}/ first"
    a = ADAPTERS / name
    if a.exists():
        return f"adapter {name} exists; pick a new name"
    iters = max(10, min(20000, int(args[1]))) if len(args) > 1 and args[1].isdigit() else 600
    a.mkdir(parents=True)
    shutil.copytree(data, a / "data")  # a frozen copy: the agent can't change the data mid-training
    shutil.copy(a / "data" / "valid.jsonl", a / "data" / "test.jsonl")  # held-out loss is measured on valid
    put(a / "meta.json", {"by": ctx["rid"], "base": c["base_model"], "iters": iters, "status": "training",
                          "created": time.time()})
    spawn("_finetune", name, log=a / "train.log")
    return f"training {name} ({iters} iters) in the background; check: finetune status {name}"


def finetune(name):
    """LoRA-train, then measure held-out loss for the base model and for the adapter."""
    a = ADAPTERS / name
    m = json.loads((a / "meta.json").read_text())
    lora = shutil.which("mlx_lm.lora") or "mlx_lm.lora"

    def run(iters, out):
        p = subprocess.Popen([lora, "--model", m["base"], "--train", "--iters", str(iters), "--test",
                              "--data", str(a / "data"), "--adapter-path", str(a / out)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        text = ""
        for line in p.stdout:
            print(line, end="", flush=True); text += line
        losses = re.findall(r"Test loss ([\d.]+)", text)
        return float(losses[-1]) if p.wait() == 0 and losses else None

    m["base_loss"] = run(0, "untrained")  # shortcut: a 0-iteration LoRA is the base model, verify on first real run
    m["test_loss"] = run(m["iters"], "lora")
    m["status"] = "trained" if m["test_loss"] is not None else "failed"
    put(a / "meta.json", m)


def adopt(name):
    """Point the agents' engine at an adapter (or "none"), then restart it if Horizon runs it."""
    active = HOME / "active_adapter"
    if name == "none":
        active.unlink(missing_ok=True)
    else:
        f = ADAPTERS / name / "meta.json"
        if not re.fullmatch(r"[\w-]{1,40}", name) or not f.exists():
            return f"no adapter named {name}"
        m = json.loads(f.read_text())
        if m.get("test_loss") is None or m.get("base_loss") is None or m["test_loss"] >= m["base_loss"]:
            return f"refused: {name} doesn't beat the base model on held-out loss ({m.get('test_loss')} vs {m.get('base_loss')})"
        active.write_text(name)
    if (HOME / "serve.pid").exists():
        kill_server()
        spawn("serve", log=HOME / "serve.log")
    return f"agents now run on {name}" if name != "none" else "agents now run on the base model"


def answer(reply):
    return reply.split("</think>")[-1]


def command(reply):
    """The one bash command in a reply, ignoring anything inside <think>."""
    m = re.findall(r"```(?:bash|sh)?\n(.*?)```", answer(reply), re.S)
    return m[-1].strip() if m else None


def compact(goal, memory, history):
    """Reflection-style compaction: fold the oldest turns into working memory instead of dropping them.
    Returns (memory, the history to keep, tokens). The kept history starts at an assistant turn."""
    cut = len(history) - KEEP // 2
    while cut < len(history) and history[cut]["role"] != "assistant":
        cut += 1
    old = "\n\n".join(f"[{m['role']}] {m['content'][-2000:]}" for m in history[:cut])
    reply, n = stream([{"role": "system", "content": COMPACT},
                       {"role": "user", "content": f"Goal: {goal}\n\nWorking memory so far:\n{memory or '(empty)'}\n\nOlder steps to fold in:\n{old}"}],
                      lambda s: None)
    return answer(reply).strip(), history[cut:], n


def review(goal, plan, history, posts=""):
    """Ask the model, as a reviewer, whether the agent is on track. Returns (kill reason or None, tokens)."""
    steps = "\n\n".join(f"[{m['role']}] {m['content'][-1500:]}" for m in history[-20:])
    reply, n = stream([{"role": "system", "content": REVIEWER},
                       {"role": "user", "content": f"Goal: {goal}\n\nPLAN.md:\n{plan}\n\nRecent steps:\n{steps}" + (f"\n\nBoard:\n{posts}" if posts else "")}],
                      lambda s: None, review=True)
    verdict = answer(reply).strip()
    return (verdict[4:].strip(" :") or "no reason given") if verdict.startswith("KILL") else None, n


def checklist(f):
    """[{"text", "done"}] for each "- [ ] item" in a markdown file."""
    items = re.findall(r"^\s*[-*] \[([ xX])\]\s*(.+)$", f.read_text(), re.M) if f.exists() else []
    return [{"text": t.strip()[:160], "done": m != " "} for m, t in items]


def progress(items):
    return sum(i["done"] for i in items) / len(items) if items else 0.0


def meta_of(d):
    m = json.loads((d / "meta.json").read_text())
    if m["status"] == "running" or m["status"] == "queued" and time.time() - m.get("created", 0) > 120:
        try:
            os.kill(m["pid"], 0)
        except (OSError, KeyError, TypeError):
            m["status"] = "crashed"
    m["milestones"] = checklist(d / "work" / "PLAN.md")
    t = d / "work" / "THOUGHT.md"
    if t.exists():
        m["thought"], m["thought_at"] = t.read_text(errors="replace").strip()[:600], t.stat().st_mtime
    m["runtime"] = m.get("runtime", 0) + (time.time() - m["started"] if m["status"] == "running" and m.get("started") else 0)
    m["progress"] = 1.0 if m["status"] == "done" else progress(m["milestones"])
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
    lab = PROJECTS / meta["project"] / "shared" if meta.get("project") else d / "lab"  # a solo run has its own
    for sub in ("raw", "wiki"):
        (lab / sub).mkdir(parents=True, exist_ok=True)
    SKILLS.mkdir(parents=True, exist_ok=True)
    mounts = ["-v", f"{d / 'work'}:/work", "-v", f"{lab}:/shared", "-v", f"{SKILLS}:/skills"]
    ctx = {"rid": rid, "name": name, "mounts": mounts, "image": meta["image"], "timeout": meta["timeout"],
           "lab": lab, "vms": []}
    r = sh("container", "run", "-d", "--rm", "--name", name, "--network", NET,
           *mounts, "-w", "/work", meta["image"], "sleep", "infinity")
    if r.returncode:
        meta["status"] = "vm failed"; save(); out(f"could not start VM: {r.stderr.strip()}\n"); log.close()
        sys.exit(1)
    out(f"run {rid}  vm {name}  budget {budget} tokens\ngoal: {goal}\n")
    if meta.get("repo") and not (d / "mirror.git").exists():  # a revived agent keeps its clone
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
        task = [{"role": "system", "content": SYSTEM + TOOLS + LAB + SWARM},
                {"role": "user", "content": f"Goal: {swarm_goal}\n\nYour part: {goal}"}]
        goal = f"{swarm_goal} / part: {goal}"
    else:
        task = [{"role": "system", "content": SYSTEM + TOOLS + LAB}, {"role": "user", "content": f"Goal: {goal}"}]
    task[1]["content"] += f"\n\nSkills available in /skills:\n{skills_index() or '(none yet: mint the first)'}"
    if pid and (top := standings(pid)[:5]):
        task[1]["content"] += "\n\nTournament leaders (rated by the reviewer):\n" + "\n".join(
            f"{n}. {r['hypothesis'][:120]} (agent {r['id']}, elo {r['elo']:.0f})" for n, r in enumerate(top, 1))
    if meta.get("revive"):
        task[1]["content"] += (f"\n\nYou are being revived to continue earlier work: {meta['revive']}\n"
                               "Your /work files are as you left them; start by reading notes.md and PLAN.md.")
    history = []
    memory = (d / "memory.md").read_text() if (d / "memory.md").exists() else ""  # survives a revive
    try:
        while meta["tokens"] < budget:
            meta["steps"] += 1
            out(f"\n\033[2m── step {meta['steps']}  {meta['tokens']}/{budget} tokens ──\033[0m\n")
            if len(history) > KEEP:
                memory, history, n = compact(goal, memory, history)
                meta["tokens"] += n
                (d / "memory.md").write_text(memory)
                out("\n\033[2m(compacted older steps into working memory)\033[0m\n")
            first = {"role": "user", "content": task[1]["content"] + (f"\n\nYour working memory (older steps, compacted):\n{memory}" if memory else "")}
            reply, n = stream([task[0], first, *history], out)
            meta["tokens"] += n; save()
            history.append({"role": "assistant", "content": reply})
            cmd = command(reply)
            if cmd is None:
                if answer(reply).strip().endswith("DONE"):
                    meta["status"] = "done"; break
                history.append({"role": "user", "content": "Give exactly one command in a ```bash block, or reply DONE."})
                continue
            out(f"\n\033[36m$ {cmd}\033[0m\n")
            head = cmd.split()[0]
            if head == "board":
                result = board(pid, rid, cmd) if pid else "the board is for swarms; you are working alone"
                out("(board: hidden from the dashboard)\n")  # only agents and the reviewer read the board
                history.append({"role": "user", "content": result})
                continue
            if head in ("vm", "finetune", "adopt"):
                if head == "adopt":
                    result = adopt(cmd.split()[1]) if len(cmd.split()) == 2 else "usage: adopt NAME | adopt none"
                    if pid and not result.startswith(("refused", "no adapter")):
                        board(pid, rid, f"board post ADOPTED: {cmd.split()[1]} ({result})")  # the reviewer sees it
                else:
                    result = vm(ctx, cmd) if head == "vm" else finetune_tool(ctx, cmd)
                out(result[:MAX_OUT] + "\n")
                history.append({"role": "user", "content": result[:MAX_OUT]})
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
                plan = plan.read_text() if plan.exists() else "(none yet)"
                if pid and (PROJECTS / pid / "shared" / "MILESTONES.md").exists():
                    plan += "\n\nLab milestones:\n" + (PROJECTS / pid / "shared" / "MILESTONES.md").read_text()
                reason, n = review(goal, plan, history, posts_text(pid, 30) if pid else "")
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
        meta["runtime"] = meta.get("runtime", 0) + time.time() - meta["started"]  # summed across revives
        save()
        if meta.get("repo"):
            out("\n" + publish(d, name, rid) + "\n")
        for v in [name, *ctx["vms"]]:
            sh("container", "stop", v)
        out(f"\n{meta['status']}: {meta['steps']} steps, {meta['tokens']} tokens. files in {d / 'work'}\n")
        log.close()


def save_config(**kw):
    c = {}
    try:
        c = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        pass
    c.update(kw)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds an API key: owner-only
    with os.fdopen(fd, "w") as f:
        json.dump(c, f)


def status():
    """Model server and cluster state for the dashboard."""
    try:
        with urllib.request.urlopen(URL.rsplit("/chat", 1)[0] + "/models", timeout=1) as r:
            models = [m["id"] for m in json.load(r).get("data", [])]
        up = True
    except Exception:
        up, models = False, []
    tail = lambda f: (HOME / f).read_text()[-4000:] if (HOME / f).exists() else ""
    c = cfg()
    return {"up": up, "models": models, "hosts": json.loads(HOSTS.read_text()) if HOSTS.exists() else None,
            "source": "local" if c["url"] == LOCAL else c["url"], "model": c["model"], "base_model": c["base_model"],
            "has_key": bool(c["key"]), "adapters": adapters(),
            "serve_log": tail("serve.log"), "setup_log": tail("setup.log")}


def new_swarm(goal, agents=16, budget=10**10, agent_budget=2_000_000, repo=None):
    """A swarm's lab: shared/ (MILESTONES.md, raw/, wiki/) is mounted into every agent's VM.
    A goal ending in .md is a milestones file to start from."""
    sid = "s" + time.strftime("%Y%m%d-%H%M%S-") + os.urandom(2).hex()
    shared = PROJECTS / sid / "shared"
    for sub in ("raw", "wiki"):
        (shared / sub).mkdir(parents=True)
    (shared / "wiki" / "index.md").write_text("# Index\n\nOne line per page: [title](page.md): summary\n")
    if goal.endswith(".md") and Path(goal).expanduser().is_file():
        plan = Path(goal).expanduser().read_text()
        (shared / "MILESTONES.md").write_text(plan)
        goal = plan.strip().splitlines()[0].lstrip("# ") if plan.strip() else goal
    put(PROJECTS / sid / "meta.json", {"id": sid, "goal": goal, "agents": agents, "budget": budget,
                                       "agent_budget": agent_budget, "repo": repo, "next_task": 0,
                                       "next_revive": 0, "spawned": 0, "created": time.time(), "status": "queued"})
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
    t = PROJECTS / sid / "tournament.json"
    judged = json.loads(t.read_text())["tokens"] if t.exists() else 0
    p.update(tokens=sum(k["tokens"] for k in kids) + judged, steps=sum(k["steps"] for k in kids),
             running=sum(k["status"] == "running" for k in kids), total=len(kids),
             done=sum(k["status"] == "done" for k in kids), finished=finished,
             milestones=checklist(PROJECTS / sid / "shared" / "MILESTONES.md"))
    p["progress"] = progress(p["milestones"])
    p["runtime"] = (time.time() if p["status"] in ("running", "queued") else p.get("ended", time.time())) - p["created"]
    thinking = [k for k in kids if k.get("thought")]
    if thinking:
        k = max(thinking, key=lambda k: k["thought_at"])
        p.update(thought=k["thought"], thought_at=k["thought_at"], thought_by=k["id"], thought_part=k["hypothesis"])
    return p


def projects():
    return [project(d.name) for d in sorted(PROJECTS.glob("*"), reverse=True) if (d / "meta.json").exists()]


def best_scores(sid):
    """Best eval score per agent from the lab's raw/results.jsonl (higher is better)."""
    f = PROJECTS / sid / "shared" / "raw" / "results.jsonl"
    best = {}
    for line in f.read_text(errors="replace").splitlines() if f.exists() else []:
        try:
            r = json.loads(line)
            score, by = float(r["score"]), str(r["by"])
        except (ValueError, KeyError, TypeError):
            continue
        best[by] = max(best.get(by, score), score)
    return best


def entrant(m):
    """What the judge sees of one agent's line of work."""
    w = RUNS / m["id"] / "work"
    tail = lambda f: (w / f).read_text(errors="replace")[-1500:] if (w / f).exists() else "(none)"
    return (f"Direction: {m['hypothesis']}\nStatus: {m['status']}, {m['steps']} steps, "
            f"plan {m['progress']:.0%} done\nRESULT.md:\n{tail('RESULT.md')}\nnotes.md (latest):\n{tail('notes.md')}")


def judge(goal, a, b):
    """1 if A wins, 0 if B, 0.5 for a draw. Asks twice with the sides swapped: a judge that just
    prefers whichever comes first (or second) gives a draw, not a win."""
    def ask(x, y):
        reply, n = stream([{"role": "system", "content": JUDGE},
                           {"role": "user", "content": f"Goal: {goal}\n\n== A ==\n{x}\n\n== B ==\n{y}"}],
                          lambda s: None, review=True)
        m = re.match(r"\W*([AB])\b", answer(reply).strip())
        return m and m[1], n
    (v1, n1), (v2, n2) = ask(a, b), ask(b, a)
    s = 1.0 if (v1, v2) == ("A", "B") else 0.0 if (v1, v2) == ("B", "A") else 0.5
    return s, n1 + n2


def elo(ra, rb, score, k=32):
    expected = 1 / (1 + 10 ** ((rb - ra) / 400))
    return ra + k * (score - expected), rb - k * (score - expected)


def tournament_round(sid, matches=MATCHES_PER_ROUND):
    """Swiss-style: the least-played agent meets the closest-rated one it hasn't met yet. Eval scores
    decide a match when both sides have one; otherwise the reviewer's base model judges."""
    f = PROJECTS / sid / "tournament.json"
    t = json.loads(f.read_text()) if f.exists() else {"ratings": {}, "matches": [], "tokens": 0}
    pool = {k["id"]: k for k in runs(sid) if k["steps"] >= MIN_STEPS and k["hypothesis"] != LIBRARIAN
            and not k["status"].startswith("killed")}
    if len(pool) < 2:
        return t
    goal = json.loads((PROJECTS / sid / "meta.json").read_text())["goal"]
    R, scores = t["ratings"], best_scores(sid)
    for i in pool:
        R.setdefault(i, {"elo": 1500.0, "w": 0, "l": 0, "d": 0})
    met = {frozenset(m["pair"]) for m in t["matches"]}
    games = lambda i: R[i]["w"] + R[i]["l"] + R[i]["d"]
    for _ in range(matches):
        a = min(pool, key=lambda i: (games(i), random.random()))
        b = min((i for i in pool if i != a), key=lambda i: (frozenset((a, i)) in met, abs(R[i]["elo"] - R[a]["elo"])))
        if a in scores and b in scores and scores[a] != scores[b]:
            s, n, how = float(scores[a] > scores[b]), 0, "eval"
        else:
            s, n = judge(goal, entrant(pool[a]), entrant(pool[b]))
            how = "judge"
        R[a]["elo"], R[b]["elo"] = elo(R[a]["elo"], R[b]["elo"], s)
        for i, r in ((a, s), (b, 1 - s)):
            R[i]["w" if r == 1 else "l" if r == 0 else "d"] += 1
        met.add(frozenset((a, b)))
        t["matches"] = (t["matches"] + [{"pair": [a, b], "score": s, "how": how, "t": time.time()}])[-1000:]
        t["tokens"] += n
    put(f, t)
    return t


def standings(sid):
    f = PROJECTS / sid / "tournament.json"
    R = json.loads(f.read_text())["ratings"] if f.exists() else {}
    kids, scores = {k["id"]: k for k in runs(sid)}, best_scores(sid)
    rows = [{"id": i, **r, "score": scores.get(i), "hypothesis": kids[i]["hypothesis"], "status": kids[i]["status"]}
            for i, r in R.items() if i in kids]
    return sorted(rows, key=lambda r: -r["elo"])


def leader(sid):
    """The best-rated agent that has finished and can be revived."""
    for r in standings(sid):
        if r["status"] not in ("queued", "running") and not r["status"].startswith("killed"):
            return r["id"]


def revive(rid, why, extra_budget):
    """Bring a finished agent back with its files. Never one the reviewer killed."""
    d = RUNS / rid
    m = meta_of(d)
    if m["status"] in ("queued", "running") or m["status"].startswith("killed"):
        return False
    m = json.loads((d / "meta.json").read_text())
    m.update(status="queued", revive=why, budget=m["tokens"] + int(extra_budget), created=time.time(),
             revived=m.get("revived", 0) + 1)
    put(d / "meta.json", m)
    spawn("_work", rid)
    return True


def swarm(sid):
    """Keep `agents` runs going on the goal: agents' TASK: posts first, fresh explorers otherwise."""
    f = PROJECTS / sid / "meta.json"
    p = json.loads(f.read_text())
    p.update(status="running", pid=os.getpid())
    put(f, p)
    signal.signal(signal.SIGTERM, _interrupt)

    def compete():
        try:
            while True:
                try:
                    tournament_round(sid)
                except Exception as e:  # a failed round must not stop the swarm
                    print(f"tournament: {e}", flush=True)
                time.sleep(TOURNAMENT_EVERY)
        except KeyboardInterrupt:
            pass
    threading.Thread(target=compete, daemon=True).start()
    try:
        while True:
            kids = runs(sid)
            if project(sid)["tokens"] >= p["budget"]:
                p["status"] = "out of budget"; break
            posts = board_read(sid)
            tasks = [x["text"][5:].strip() for x in posts if x["text"].startswith("TASK:")]
            revives = [x["text"][7:].strip() for x in posts if x["text"].startswith("REVIVE:")]
            mine = {k["id"] for k in kids}
            live = sum(k["status"] in ("queued", "running") for k in kids)
            while live < p["agents"] and p["next_revive"] < len(revives):
                rid, _, why = revives[p["next_revive"]].partition(" ")
                p["next_revive"] += 1
                if rid in mine and revive(rid, why or "another agent needs this work extended", p["agent_budget"]):
                    live += 1
            for _ in range(p["agents"] - live):
                p["spawned"] = p.get("spawned", 0) + 1
                if p["spawned"] % LIBRARIAN_EVERY == 0:
                    part = LIBRARIAN
                elif p["spawned"] % LEAD_EVERY == 0 and (top := leader(sid)) and revive(top, LEAD_NOTE, p["agent_budget"]):
                    continue
                elif p["next_task"] < len(tasks):
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
        p["ended"] = time.time()
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
        m = re.fullmatch(r"/projects/(s[\w-]+)/tournament", self.path)
        if m and (PROJECTS / m[1]).is_dir():
            f = PROJECTS / m[1] / "tournament.json"
            played = json.loads(f.read_text())["matches"] if f.exists() else []
            return self.reply(200, json.dumps({"rows": standings(m[1])[:50], "matches": len(played),
                                               "by_eval": sum(x["how"] == "eval" for x in played)}).encode())
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
        m = re.fullmatch(r"/(runs|projects)/([\w-]+)/continue", self.path)
        if m and ((RUNS if m[1] == "runs" else PROJECTS) / m[2] / "meta.json").exists():
            if m[1] == "runs":
                ok = revive(m[2], "you were asked to keep going", 1_000_000)
            else:
                cur = project(m[2])
                ok = cur["status"] not in ("running", "queued")
                if ok:
                    pm = json.loads((PROJECTS / m[2] / "meta.json").read_text())
                    pm.update(status="queued", budget=max(pm["budget"], cur["tokens"] * 2))
                    put(PROJECTS / m[2] / "meta.json", pm)
                    spawn("_swarm", m[2], log=PROJECTS / m[2] / "swarm.log")
            return self.reply(200 if ok else 409)
        if self.path == "/config":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            url = str(body.get("url") or LOCAL).strip()
            if not re.fullmatch(r"https?://\S+", url):
                return self.reply(400)
            kw = {"url": url, "model": str(body.get("model") or "default_model").strip()}
            if "key" in body:  # absent: keep the stored key
                kw["key"] = str(body["key"]).strip()
            if body.get("base_model"):
                kw["base_model"] = str(body["base_model"]).strip()
            save_config(**kw)
            return self.reply(200)
        m = re.fullmatch(r"/adapters/([\w-]{1,40})/adopt", self.path)
        if m:
            msg = adopt(m[1])
            return self.reply(409 if msg.startswith(("refused", "no adapter")) else 200, msg.encode(), "text/plain")
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
    v.add_argument("--model", help=f"base model (default {SERVE_MODEL})")
    w = sub.add_parser("_work"); w.add_argument("id")
    w = sub.add_parser("_swarm"); w.add_argument("id")
    w = sub.add_parser("_finetune"); w.add_argument("name")
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
    elif a.cmd == "_finetune":
        finetune(a.name)
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
        if a.model:
            save_config(base_model=a.model)
        model = cfg()["base_model"]
        (HOME / "serve.pid").write_text(str(os.getpid()))  # exec keeps this pid
        server = shutil.which("mlx_lm.server") or "mlx_lm.server"
        cmd = [server, "--model", model, "--host", "127.0.0.1", "--port", "8080"]
        if HOSTS.exists():
            # one bigger model tensor-parallel across the paired Macs (no adapters: mlx_lm can't shard them);
            # shortcut: assumes mlx_lm is installed at the same path on each
            cmd = ["mlx.launch", "--backend", "jaccl", "--hostfile", str(HOSTS), "--env", "MLX_METAL_FAST_SYNCH=1", "--", *cmd]
        else:
            # the reviewer's copy: base weights, never fine-tuned (a 27B model fits twice in 256 GB)
            subprocess.Popen([server, "--model", model, "--host", "127.0.0.1", "--port", "8081"])
            active = HOME / "active_adapter"
            if active.exists():
                cmd += ["--adapter-path", str(ADAPTERS / active.read_text().strip() / "lora")]
        os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
