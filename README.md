# horizon

A long-running research agent for Apple Silicon, used entirely from the terminal. You give it a hypothesis. A local model works on it one shell command at a time inside a throwaway Linux VM with no internet, until it reaches a verified result or runs out of its token budget.

## Setup (macOS 26 on Apple Silicon)

```sh
brew install container && container system start   # Apple's lightweight-VM containers
pip install mlx-lm
mlx_lm.server --model <an MLX quant of Qwen3.8-27B, e.g. 4-bit>   # serves on 127.0.0.1:8080
```

## Use

```sh
./horizon.py run "every even number > 2 is a sum of two primes below 10^7" --budget 5000000
./horizon.py ls            # all runs: status, steps, tokens
./horizon.py log RUN_ID    # replay a run's transcript
```

`run` streams the model's tokens and each command's output live. Press Ctrl-C to stop a run. Each run keeps its files in `~/.horizon/runs/RUN_ID/` (`work/` is mounted at `/work` in the VM, plus `log.txt` and `meta.json`).

Options: `--image` (default `python:3.12`; use an image with Lean installed for formal proofs), `--timeout` (seconds per command). Settings: `HORIZON_URL`, `HORIZON_MODEL`, `HORIZON_HOME`.

## Security

Each run gets its own VM through Apple's `container`. It sees only its own `work/` folder and is attached to an `--internal` (host-only) network, so it can't reach the internet. The model server listens on the Mac's 127.0.0.1, which the VM can't reach.

Test: `python3 -m unittest test_horizon`
