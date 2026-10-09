"""Demo mode: replay the real gpt-4 -> gpt-5.6-sol migration from cache, paced like the real run, fully offline.

  python demo.py --demo                  # narrated replay (~2 min) + dashboard at http://localhost:4173/#savings
  python demo.py --demo --speed 2        # twice as fast
  python demo.py --demo --no-server      # terminal only

Nothing here can call a model: it imports no API client, reads only results/*.jsonl and reports/*.json, and
refuses to start if any cached file is missing. Row pacing follows the recorded timestamps (`ts`), squeezed
into a fixed time per stage. The dashboard server (dashboard/server.mjs) is started unless one is already up.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
R = HERE / "results"
BOLD, DIM, GREEN, ORANGE, RESET = (("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m")
                                   if sys.stdout.isatty() else ("",) * 5)
# Seconds per stage at --speed 1; together about two minutes with the pauses.
BUDGET = {"switch": 22, "rescue": 14, "lines": 8, "heldout": 12}


def rows(name):
    out = [json.loads(l) for l in open(R / name)]
    return sorted(out, key=lambda r: r["ts"])


def paced(rs, budget, speed):
    """Yield rows with the recorded gaps between them, scaled so the whole list takes `budget` seconds."""
    t = [datetime.fromisoformat(r["ts"]).timestamp() for r in rs]
    span = max(t[-1] - t[0], 1e-6)
    scale = budget / speed / span
    for i, r in enumerate(rs):
        if i:
            time.sleep(min((t[i] - t[i - 1]) * scale, 1.5 / speed))
        yield r


def say(n, text, speed, pause=1.2):
    print(f"\n{BOLD}[{n}/5]{RESET} {text}", flush=True)
    time.sleep(pause / speed)


def mark(r):
    if r.get("error"):
        return f"{ORANGE}error{RESET}"
    if r.get("passed_all"):
        return f"{GREEN}✓{RESET}"
    failed = [c for c, ok in (r.get("checks") or {}).items() if not ok]
    return f"{ORANGE}✗ {', '.join(failed)}{RESET}"


def ticker(rs, budget, speed, label):
    for r in paced(rs, budget, speed):
        d = r.get("decision") or {}
        print(f"   {DIM}{label}{RESET} {r['input_id']} run {r['run']}  {r.get('category')}/{d.get('action')}  {mark(r)}", flush=True)


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="replay the cached real run (required; there is no live mode here)")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--port", type=int, default=4173)
    ap.add_argument("--no-server", action="store_true")
    args = ap.parse_args()
    if not args.demo:
        sys.exit("Run with --demo to replay the cached real run. (Live runs: model_only.py / assign.py.)")

    mo = json.loads((HERE / "reports/model_only/result_gpt-5.6-sol.json").read_text())
    demo = json.loads((HERE / "reports/demo/results.json").read_text())["demo"]
    tag = mo["heldout_after"]["file"].split("__patch-")[1].split("__")[0]
    step = mo["stepfinder"]["guilty_step"]
    files = {"new_dev": "agent__gpt-5.6-sol__noplant.jsonl",
             "rescue": f"agent__gpt-5.6-sol-np__{step}-gpt-4-np.jsonl",
             "line": f"agent__gpt-5.6-sol-np__ablate-{step}-L{mo['patch']['edits'][0]['line_index']}.jsonl",
             "fixed_ho": f"agent__gpt-5.6-sol-np__patch-{tag}__hard.jsonl"}
    missing = [f for f in files.values() if not (R / f).exists()]
    if missing:
        sys.exit(f"Cached run files missing (pull the repo again): {missing}")

    server = None
    if not args.no_server:
        if port_open(args.port):
            print(f"{DIM}Dashboard already running on :{args.port}{RESET}")
        else:
            server = subprocess.Popen(["node", str(HERE / "dashboard" / "server.mjs")],
                                      env={**os.environ, "PORT": str(args.port)},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(30):
                if port_open(args.port):
                    break
                time.sleep(0.1)
        print(f"Dashboard: {BOLD}http://localhost:{args.port}/#savings{RESET}  (offline replay; no model calls)")

    s = args.speed
    c = demo["confidence"]
    try:
        print(f"\n{BOLD}upshift{RESET} · support agent · gpt-4 → gpt-5.6-sol · replaying a real recorded run "
              f"{DIM}(hand-written tickets, real model outputs){RESET}")
        say(1, f"Only the model changes. {c['dev_tickets']} tickets × {c['runs_per_ticket']} runs on gpt-5.6-sol, "
               f"same prompts, tools and tickets as gpt-4.", s)
        ticker(rows(files["new_dev"]), BUDGET["switch"], s, "sol")
        reg = [x["input_id"] for x in mo["dev"]["regressed"]]
        print(f"   → {ORANGE}{len(reg)} tickets got worse{RESET} than gpt-4: {', '.join(reg)}. gpt-4 itself was stable on "
              f"{c['dev_tickets'] - c['old_model_unstable_tickets']}/{c['dev_tickets']}, so this isn't noise. "
              f"Failing check: the reply drops “5–7 business days”.")

        say(2, "Which step? Put gpt-4 back at one step at a time, 3 runs each.", s)
        ticker(rows(files["rescue"]), BUDGET["rescue"], s, f"gpt-4 at {step}")
        for st, v in mo["stepfinder"]["per_step"].items():
            hit = st == step
            print(f"   {'→' if hit else ' '} {st:<9} repair {v['repair']}/{v['of']}" +
                  (f"   reproduce {v['reproduce']}/{v['of']}  {ORANGE}← cause{RESET}" if hit else ""))

        say(3, f"Which line in `{step}`? Remove one line at a time.", s)
        ticker(rows(files["line"]), BUDGET["lines"], s, "without line")
        for r in mo["lines"]["results"]:
            flag = f"  {ORANGE}← this line{RESET}" if r["repairs"] else ""
            print(f"   L{r['line_index'] + 1} {r['repairs']}/{r['of']}  {DIM}{r['line'][:70]}{RESET}{flag}")

        p = mo["patch"]
        say(4, f"Fix: {mo['fix']['fixer_model']} rewrites at most 2 lines, seeing only these {len(reg)} tickets.", s)
        for e in p["edits"]:
            print(f"   {ORANGE}- {e['old']}{RESET}\n   {GREEN}+ {e['new']}{RESET}")
        print(f"   dev re-run, 24 tickets × 3: {p['dev_verdict_counts']['REGRESSED']} got worse")

        say(5, f"Held-out: {c['heldout_tickets']} tickets the fixer never saw.", s)
        ticker(rows(files["fixed_ho"]), BUDGET["heldout"], s, "sol + fix")
        print(f"   → got worse: {ORANGE}{mo['heldout_before']['verdict_counts']['REGRESSED']}{RESET} before the fix, "
              f"{GREEN}{mo['heldout_after']['verdict_counts']['REGRESSED']}{RESET} after.")
        t = demo["totals_usd_per_1k"]
        rec = demo["recommended"]
        print(f"\n{BOLD}Result:{RESET} model cost per 1,000 tickets ${t['all_old']:.2f} → ${rec['usd_per_1k']:.2f} "
              f"({rec['savings_vs_all_old_pct']:.0f}% lower), 0 of 36 tickets worse. Open the dashboard for savings, evidence and the PR.")
        if server:
            print(f"{DIM}Dashboard stays up. Ctrl+C to stop.{RESET}")
            server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.terminate()


if __name__ == "__main__":
    main()
