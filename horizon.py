#!/usr/bin/env python3
"""horizon: a long-running research agent for Apple Silicon.

The model (served locally by mlx_lm.server) works on one hypothesis inside a
throwaway Linux VM (Apple `container`), one shell command at a time, until it
says DONE or the token budget runs out.

  horizon run "hypothesis" [--budget TOKENS] [--image IMAGE]
  horizon ls
  horizon log RUN_ID
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

URL = os.environ.get("HORIZON_URL", "http://127.0.0.1:8080/v1/chat/completions")
MODEL = os.environ.get("HORIZON_MODEL", "default_model")  # mlx_lm.server: the model it was started with
RUNS = Path(os.environ.get("HORIZON_HOME", Path.home() / ".horizon")) / "runs"
NET = "horizon-offline"  # host-only network: no internet from the VM
MAX_OUT = 8000  # chars of command output fed back to the model
KEEP = 40  # shortcut: keep only the last 40 messages, upgrade to summarising when runs lose the plot

SYSTEM = """You are a researcher working alone on a hypothesis for as long as it takes.
You have a Linux VM with no internet. /work persists; keep notes.md there with what you
have tried, learned, and plan next. Re-read it whenever you lose track.
Each reply: think briefly, then give exactly ONE command in a ```bash block. You will see its output.
Verify results by running code or a proof checker, never by assertion.
When you have a verified result (or have disproved the hypothesis), write it to /work/RESULT.md
and reply with the single word DONE and no bash block."""


def sh(*cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def stream(messages, out):
    """Stream one completion, echoing tokens to the terminal and log. Returns (text, tokens)."""
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


def command(reply):
    """The one bash command in a reply, ignoring anything inside <think>."""
    m = re.findall(r"```(?:bash|sh)?\n(.*?)```", reply.split("</think>")[-1], re.S)
    return m[-1].strip() if m else None


def run(args):
    rid = time.strftime("%Y%m%d-%H%M%S")
    d = RUNS / rid
    (d / "work").mkdir(parents=True)
    meta = {"id": rid, "hypothesis": args.hypothesis, "budget": args.budget, "tokens": 0, "steps": 0, "status": "running"}
    save = lambda: (d / "meta.json").write_text(json.dumps(meta, indent=1))
    save()
    log = open(d / "log.txt", "a", buffering=1)

    def out(s):
        sys.stdout.write(s); sys.stdout.flush(); log.write(s)

    sh("container", "network", "create", "--internal", NET)  # fails harmlessly if it exists
    name = f"horizon-{rid}"
    r = sh("container", "run", "-d", "--rm", "--name", name, "--network", NET,
           "-v", f"{d / 'work'}:/work", "-w", "/work", args.image, "sleep", "infinity")
    if r.returncode:
        meta["status"] = "vm failed"; save()
        sys.exit(f"could not start VM: {r.stderr.strip()}")
    out(f"run {rid}  vm {name}  budget {args.budget} tokens\n")

    task = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Hypothesis: {args.hypothesis}"}]
    history = []
    try:
        while meta["tokens"] < args.budget:
            meta["steps"] += 1
            out(f"\n\033[2m── step {meta['steps']}  {meta['tokens']}/{args.budget} tokens ──\033[0m\n")
            reply, n = stream(task + history[-KEEP:], out)
            meta["tokens"] += n; save()
            history.append({"role": "assistant", "content": reply})
            cmd = command(reply)
            if cmd is None:
                if reply.split("</think>")[-1].strip().endswith("DONE"):
                    meta["status"] = "done"; break
                history.append({"role": "user", "content": "Give exactly one command in a ```bash block, or reply DONE."})
                continue
            out(f"\n\033[36m$ {cmd}\033[0m\n")
            try:
                r = sh("container", "exec", name, "sh", "-c", cmd, timeout=args.timeout)
                result = f"exit {r.returncode}\n{r.stdout}{r.stderr}"
            except subprocess.TimeoutExpired:
                result = f"timed out after {args.timeout}s"
            if len(result) > MAX_OUT:
                result = result[:MAX_OUT // 2] + f"\n…[{len(result) - MAX_OUT} chars cut]…\n" + result[-MAX_OUT // 2:]
            out(result + "\n")
            history.append({"role": "user", "content": result})
        else:
            meta["status"] = "out of budget"
    except KeyboardInterrupt:
        meta["status"] = "stopped"
    finally:
        save()
        sh("container", "stop", name)
        out(f"\n{meta['status']}: {meta['steps']} steps, {meta['tokens']} tokens. files in {d / 'work'}\n")
        log.close()


def ls(_):
    for m in sorted(RUNS.glob("*/meta.json")):
        m = json.loads(m.read_text())
        print(f"{m['id']}  {m['status']:<14} {m['steps']:>5} steps {m['tokens']:>10} tok  {m['hypothesis'][:60]}")


def log(args):
    sys.stdout.write((RUNS / args.id / "log.txt").read_text())


def main(argv=None):
    p = argparse.ArgumentParser(prog="horizon")
    sub = p.add_subparsers(required=True)
    r = sub.add_parser("run"); r.set_defaults(f=run)
    r.add_argument("hypothesis")
    r.add_argument("--budget", type=int, default=1_000_000)
    r.add_argument("--image", default="python:3.12")
    r.add_argument("--timeout", type=int, default=600, help="seconds per command")
    sub.add_parser("ls").set_defaults(f=ls)
    g = sub.add_parser("log"); g.add_argument("id"); g.set_defaults(f=log)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
