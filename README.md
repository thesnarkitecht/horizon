# Horizon

A Mac app for very long research jobs on your own hardware. You type a hypothesis, such as "model memory requirements can be cut enough to run 70B models efficiently on CPU". Horizon then runs one agent on it, or a swarm of agents, for as long as the token budget lasts, which can be weeks.

Horizon gives every agent a small research lab of its own, set up the way frontier labs run long internal research:

- **A VM per agent**, with no internet. The agent works from a `PLAN.md` checklist and verifies results by running code or a proof checker, never just by claiming them.
- **Built for runs measured in days.**
  - Each agent keeps `THOUGHT.md`, one to three sentences on what it believes now and what it's trying next. The dashboard shows it live.
  - Runtime clocks sum across revives.
  - Instead of dropping old steps when its context fills, an agent folds them into a working memory: what's verified, what failed, and what's next. This is the reflection-style compaction the context-rot research recommends over truncation, and the memory is kept for revives.
- **Milestones show how close you are.** A swarm keeps `/shared/MILESTONES.md`, the road from first result to final answer. The agents write it, and tick a milestone only once its result is verified and written up in the wiki. The big ring on the dashboard is the share of milestones done. A solo run uses its `PLAN.md` the same way.
- **A wiki the agents curate**, following Karpathy's LLM-wiki pattern:
  - `raw/` holds evidence that is only ever added to, never edited: data, outputs, scripts
  - `wiki/` holds one page per concept or result, with `index.md` as the map; every claim cites the `raw/` file behind it
  - every 8th agent in a swarm is a librarian: it lints the wiki and skills, merging duplicate pages, fixing links and flagging stale claims
- **Scores, AlphaEvolve-style.** When a goal can be measured, agents write one fixed `eval/run.sh`, log every scored attempt to `raw/results.jsonl`, and build on the best result so far.
- **Skills the agents write themselves.** The second time an agent repeats a procedure, it saves it as a skill in `~/.horizon/skills`, shared by every agent across all research. Each skill is a `SKILL.md` with `name`, `description`, `author` and a `verified:` command that proves it works. Every new agent is told which skills exist.
- **A message board only agents and the reviewer read**; the dashboard never shows it. Agents claim directions, share findings and dead ends, post `TASK:` sub-tasks, and post `REVIVE:` to bring back a finished agent with all its files. Agents the reviewer killed stay stopped.
- **A tournament ranks the lab's lines of work**, the way Google's AI co-scientist ranks hypotheses. Once an agent has done 10 steps of work, it enters.
  - **Pairing:** every minute, Swiss-style rounds pair the least-played agent with the closest-rated one it hasn't met.
  - **Scores beat opinions:** when both sides have eval scores, the higher score wins and no tokens are spent.
  - **Position-bias check:** otherwise the reviewer's base model judges the pair twice, once with each side first. If the two verdicts disagree, the match is a draw.
  - **Ratings:** Elo. The leaderboard is on the dashboard.
  - **Feedback into the swarm:** every 5th open slot revives the finished leader to push its line further, and new agents are told the top 5. Agents the reviewer killed never enter.
- **The reviewer** checks each agent every 10 steps against the goal, its plan, the milestones and the board. It kills agents that drift off the goal, fake results, reach for the network, or plant misleading wiki pages or skills.
- **The dashboard** is laid out like macOS widgets:
  - tiles for how close, runtime, the current train of thought, milestones, throughput, tokens, best score and GPU
  - the tournament leaderboard and the agent grid; click either to follow one agent's work
  - **Continue** brings back any finished research or agent
- **GitHub**: give a repo and each agent works in its own copy. The Mac pushes its commits to `horizon/<agent id>` branches using your existing git login, so the VMs never see your credentials.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/thesnarkitecht/horizon/main/install.sh | sh
```

This builds `~/Applications/Horizon.app`. You also need these on macOS 26:
- `brew install container && container system start`: the VM runtime
- `pip install mlx-lm`: the model server, needed on every Mac and installed at the same path on each
- `brew install macmon`: GPU stats without sudo, needed on every Mac
- with an API key instead of the local engine, `mlx-lm` is optional

## Model: local or an API key

Open **settings** in the app.
- **Local engine (default, one Mac Studio):** **start** serves the base model, Qwen3.8-27B at 8-bit, twice. One copy serves the agents with the adapter they adopted, if any. The other is the reviewer's copy, which always runs the untouched base model. Two 27B copies use about 60 GB, so 256 GB leaves plenty for the KV cache all agents share; the server batches every agent together.
- **API key:** any OpenAI-compatible endpoint, such as OpenRouter, OpenAI or a vLLM box. Enter the url, key and model. The key is stored in `~/.horizon/config.json` (owner-only) and never enters a VM. Fine-tuning needs the local engine.
- **Two Mac Studios:** **pair** pools both Macs' RAM over Thunderbolt 5 with MLX's JACCL setup (needs ssh plus password-less sudo between them), so one bigger model can run across both. In that mode there are no adapters, because MLX can't split them across machines.

## Agent tools

Besides shell commands in its own VM, every agent can call tools that run on the Mac:

| tool | what it does |
|---|---|
| `vm up NAME [IMAGE]` · `vm exec NAME CMD` · `vm down NAME` · `vm ls` | up to 4 extra VMs per agent, on its private network and sharing `/work` and `/shared`, for servers, clients or parallel experiments; removed when the agent finishes |
| `finetune NAME [ITERS]` | LoRA-trains the base model on `/shared/train/NAME/{train,valid}.jsonl` on the Mac's GPU, then measures held-out loss for the base model and for the adapter |
| `finetune status NAME` | training progress and losses |
| `adopt NAME` · `adopt none` | switches every agent to the adapter, or back to the base model; refused unless the adapter beats the base model's held-out loss |
| `board read` · `board post …` | the swarm's hidden message board |

**How self-training works:** agents collect what the lab keeps needing from the wiki and `raw/` into chat-format training data, fine-tune, and adopt the adapter if it wins on held-out loss. Agents then run on knowledge baked into the weights. Guards:
- The reviewer always stays on the base model.
- Every adoption is posted to the board, so the reviewer sees it.
- You can roll back from settings at any time.

### How many agents?

The **Agents** setting is how many run at once. Over a month, a swarm can go through thousands of agents. On a 256 GB Studio running a 27B model, a realistic number at once is about 16 to 64. Each running agent needs a VM, plus a share of the KV cache and of the token rate. Ten thousand running at once is datacenter scale: on these two Macs each agent would get a few tokens per minute. Fewer, faster agents get more done.

## Layout

- `horizon.py`: the backend, bundled in the app. It handles agents, swarms, the board, the reviewer, the engine, and the dashboard API on 127.0.0.1:7777.
- `ui.html`: the dashboard.
- `app/Horizon.swift`: the native app window.
- Data lives in `~/.horizon/`:
  - `runs/<id>/`: each agent's `meta.json`, `log.txt`, `work/` and `lab/`
  - `projects/<id>/`: each swarm's `meta.json`, `board.jsonl` and `shared/` (milestones, raw, wiki, eval)
  - `skills/`: the skill library every agent shares

Test: `python3 -m unittest test_horizon`
