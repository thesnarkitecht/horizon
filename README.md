# Horizon

A Mac app for very long research jobs on your own hardware. You type a hypothesis, such as "model memory requirements can be cut enough to run 70B models efficiently on CPU". Horizon then runs one agent on it, or a swarm of agents, for as long as the token budget lasts, which can be weeks.

- **Each agent** gets its own Linux VM with no internet. It works from a `PLAN.md` checklist, which fills its progress ring, and verifies results by running code or a proof checker, never just by claiming them.
- **Swarms** coordinate through a message board that only the agents and the reviewer can read; the dashboard never shows it. Agents claim directions, post findings and dead ends, and post `TASK:` sub-tasks. Horizon starts a new agent for each `TASK:` post, and starts fresh explorer agents when there are none.
- **The reviewer** checks each agent every 10 steps against the goal, its plan, its recent steps and the board. It kills agents that drift off the goal, fake results, reach for the network, or mislead other agents.
- **GitHub**: give a repo and each agent works in its own copy. The Mac pushes its commits to `horizon/<agent id>` branches using your existing git login, so the VMs never see your credentials.
- **Dashboard**: shows tokens/sec, running agents, steps per minute, tokens used, and each Mac's GPU frequency, load, power, memory and temperature. Each swarm also gets a grid with one square per agent; click a square to follow that agent's log.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/horizon/main/install.sh | sh
```

This builds `~/Applications/Horizon.app`. You also need these on macOS 26:
- `brew install container && container system start`: the VM runtime
- `pip install mlx-lm`: the model server, needed on every Mac and installed at the same path on each
- `brew install macmon`: GPU stats without sudo, needed on every Mac

## Engine: two Mac Studios over Thunderbolt 5

Open **Engine** in the app:
1. **Pair Macs**: enter `studio1.local,studio2.local`. This runs MLX's JACCL setup, which enables RDMA over Thunderbolt and needs ssh plus password-less sudo between the Macs.
2. **Start**: serves Ornith-1.5-397B at 4-bit, split across both Macs.

The 4-bit model needs about 220 GB, roughly 110 GB per 256 GB Studio. The rest holds the KV cache that every running agent shares, because the server batches all of them together.

### How many agents?

The **Agents** setting is how many run at once. Over a month, a swarm can go through thousands of agents. On two 256 GB Studios, a realistic number at once is about 16 to 64. Each running agent needs a VM, plus a share of the KV cache and of the token rate. Ten thousand running at once is datacenter scale: on these two Macs each agent would get a few tokens per minute. Fewer, faster agents get more done.

## Layout

- `horizon.py`: the backend, bundled in the app. It handles agents, swarms, the board, the reviewer, the engine, and the dashboard API on 127.0.0.1:7777.
- `ui.html`: the dashboard.
- `app/Horizon.swift`: the native app window.
- Data lives in `~/.horizon/`. `runs/<id>/` holds each agent's `meta.json`, `log.txt` and `work/`. `projects/<id>/` holds each swarm's `meta.json` and `board.jsonl`.

Test: `python3 -m unittest test_horizon`
