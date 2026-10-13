# horizon

Long-running research agents on your own Macs. You give it a hypothesis, or a `PLAN.md` to start from. An open model works on it for hours, one shell command at a time, inside a throwaway Linux VM with no internet. It ticks off a plan checklist as it goes. A reviewer checks the work every 10 steps and kills the run if the agent drifts off course. Agent commits are pushed to a GitHub branch for you to review.

```
 terminal: horizon ─┐                       ┌─ VM per run (Apple container, no internet)
                    ├─ ~/.horizon/runs/ID ──┤   /work: PLAN.md, notes.md, RESULT.md, repo/
 Horizon.app ───────┘   meta.json, log.txt  └─ host pushes repo commits → branch horizon/ID
        │
        └─ horizon serve ─ mlx_lm.server (batches every run) ─ mlx.launch --backend jaccl
                           tensor-parallel over Thunderbolt 5 across Mac Studio 1 + Mac Studio 2
```

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/horizon/main/install.sh | sh
```

This installs the `horizon` command and builds `~/Applications/Horizon.app` (requires Xcode Command Line Tools). You also need Apple's VM runtime (`brew install container && container system start`, macOS 26) and `pip install mlx-lm` on every Mac, at the same path on each.

## Engine: two Mac Studios

```sh
horizon setup studio1.local,studio2.local   # once: RDMA over Thunderbolt (ssh + password-less sudo between the Macs)
horizon serve                               # Ornith-1.5-397B 4-bit, split across both Macs
```

Without `setup`, `serve` runs on one Mac. Both commands are also in the app, under **Engine**. A 397B model at 4 bits needs about 220 GB of memory, roughly 110 GB per 256 GB Studio. The rest is left for the KV cache that concurrent runs share. The server batches all runs together, so ten runs cost far less than ten times one.

## Runs

```sh
horizon                                     # asks for the hypothesis, then runs here with live output
horizon run PLAN.md --repo git@github.com:you/proj.git --detach
horizon ls | log ID | stop ID
horizon app                                 # same dashboard in a browser
```

The app and the browser dashboard both show live tokens per second, total tokens, steps per minute, and a progress ring for each run. The ring fills as `PLAN.md` boxes get ticked. From either you can start or stop runs, start or stop the engine, and pair the Macs.

## Security

- Each run gets its own VM. It sees only its own `work/` folder and is attached to an `--internal` network, so it can't reach the internet.
- GitHub pushes happen on the Mac, using your existing git credentials, which never enter the VM. The VM packs the agent's commits into a git bundle and the Mac fetches from that file, so no hook or config the agent writes into `.git` ever runs on your Mac. Pushes only go to `horizon/<run id>` branches.
- The dashboard listens only on 127.0.0.1. Requests that change things must carry a custom header, which blocks other web pages from starting or stopping runs.

Test: `python3 -m unittest test_horizon`
