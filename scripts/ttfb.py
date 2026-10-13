#!/usr/bin/env python3
"""TTFB test: how fast the first byte of the answer arrives through each proxy.
Uses curl timings (connect / TLS / first byte / total). Same target and countries as the Infra test.

  python3 ttfb.py run --server us --n 10      (about 5-8 minutes, a few MB of traffic)
  python3 ttfb.py summary
  python3 ttfb.py summary --provider floppydata --countries
"""
import argparse, csv, datetime, os, random, statistics, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

import bench

TARGET = os.environ.get("TTFB_TARGET", bench.DEFAULT_TARGET)
FIELDS = ["run_id", "ts_utc", "server", "provider", "country_req", "ok", "http", "ip",
          "t_connect", "t_tls", "ttfb", "t_total", "err"]
MARK = "TTFBTIMING:"
WFMT = MARK + "%{http_code}|%{time_connect}|%{time_appconnect}|%{time_starttransfer}|%{time_total}"


def q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def one(entry, cc, provider, run_id, server, timeout):
    user = entry["user"].format(login=entry.get("login", ""), cc=cc)
    pw = (entry.get("pass", "{password}").replace("{cc}", cc).replace("{CC}", cc.upper())
          .replace("{password}", entry["password"]))
    cfg = "proxy = %s\nproxy-user = %s\n" % (q("http://" + entry["host"]), q(user + ":" + pw))
    row = dict(run_id=run_id, ts_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), server=server,
               provider=provider, country_req=cc, ok=0, http="", ip="", t_connect="", t_tls="",
               ttfb="", t_total="", err="")
    try:
        p = subprocess.run(["curl", "-K", "-", "-s", "-S", "--http1.1", "-m", str(timeout),
                            "-A", "Mozilla/5.0", "-w", WFMT, TARGET],
                           input=cfg, capture_output=True, text=True, timeout=timeout + 10)
    except Exception as e:  # noqa: BLE001
        row["err"] = "runner_" + type(e).__name__
        return row
    out = p.stdout
    body, _, tim = out.partition(MARK)
    parts = tim.split("|") if tim else []
    if len(parts) == 5:
        row.update(http=parts[0], t_connect=parts[1], t_tls=parts[2], ttfb=parts[3], t_total=parts[4])
    ip = ""
    for line in body.splitlines():
        if line.startswith("ip="):
            ip = line[3:]
    if p.returncode == 0 and row["http"] == "200" and ip:
        row.update(ok=1, ip=ip)
    else:
        e = (p.stderr or "").strip().lower()
        if "response 4" in e or "response 5" in e:
            row["err"] = "tunnel_failed_" + e.split("response ")[-1][:3]
        elif p.returncode == 28:
            row["err"] = "timeout"
        elif p.returncode == 0:
            row["err"] = "http_" + (row["http"] or "unknown") if row["http"] != "200" else "unexpected_response"
        else:
            row["err"] = "curl_%d" % p.returncode
    return row


def cmd_run(args):
    cfg = bench.load_config()["providers"]
    provs = [p for p in cfg if not args.providers or p in args.providers]
    run_id = args.run_id or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
    jobs = []
    rnd = random.Random(run_id + args.server)
    for cc in bench.COUNTRIES:
        order = provs[:]
        rnd.shuffle(order)
        for p in order:
            jobs += [(p, cc)] * args.n
    print("TTFB run %s on %s: %d providers, %d countries, %d requests each, %d requests in total" %
          (run_id, args.server, len(provs), len(bench.COUNTRIES), args.n, len(jobs)))
    os.makedirs(bench.DATA, exist_ok=True)
    path = os.path.join(bench.DATA, "ttfb_%s_%s.csv" % (args.server, run_id))
    done = 0
    with open(path, "w", newline="") as f, ThreadPoolExecutor(max_workers=args.workers) as ex:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
        futs = [ex.submit(one, cfg[p], cc, p, run_id, args.server, args.timeout) for p, cc in jobs]
        for fu in futs:
            w.writerow(fu.result()); done += 1
            if done % 200 == 0:
                f.flush(); print("[%d/%d]" % (done, len(jobs)))
    print("Saved %s" % path)


def load(server=None):
    rows = []
    for fn in sorted(os.listdir(bench.DATA)):
        if fn.startswith("ttfb_") and fn.endswith(".csv"):
            with open(os.path.join(bench.DATA, fn), newline="") as f:
                rows += list(csv.DictReader(f))
    return [r for r in rows if not server or r["server"] == server]


def pc(v, q_):
    return v[int(q_ * (len(v) - 1))] if v else 0.0


def line(label, rs):
    ok = [r for r in rs if r["ok"] == "1"]
    tt = sorted(float(r["ttfb"]) for r in ok)
    wait = sorted(float(r["ttfb"]) - float(r["t_tls"]) for r in ok if r["t_tls"])
    tot = sorted(float(r["t_total"]) for r in ok)
    return "%-24s| %5d | %-22s| %8.2f | %8.2f | %9.2f | %9.2f" % (
        label, len(rs), bench.fmt_rate(len(ok), len(rs)), statistics.median(tt) if tt else 0,
        pc(tt, 0.9), statistics.median(wait) if wait else 0, statistics.median(tot) if tot else 0)


HEAD = "%-24s| %5s | %-22s| %8s | %8s | %9s | %9s" % (
    "", "N", "Success Rate (95% CI)", "TTFB med", "TTFB P90", "Wait med", "Total med")


def cmd_summary(args):
    rows = load(args.server)
    if not rows:
        sys.exit("No TTFB data yet. Run:  python3 ttfb.py run --server us")
    if args.provider:
        rows = [r for r in rows if r["provider"] == args.provider]
    print("TTFB = seconds from sending the request until the first byte of the answer (includes proxy connect, tunnel and TLS).")
    print("Wait = TTFB minus the time the TLS connection was ready (the pure wait for the first byte).")
    print(HEAD); print("-" * len(HEAD))
    if args.countries:
        by = defaultdict(list)
        for r in rows: by[(r["server"], r["provider"], r["country_req"])].append(r)
        for k in sorted(by): print(line("%s %s %s" % k, by[k]))
        return
    by = defaultdict(list)
    for r in rows: by[(r["server"], r["provider"])].append(r)
    for srv in sorted(set(k[0] for k in by)):
        for k in sorted([k for k in by if k[0] == srv],
                        key=lambda k: statistics.median([float(r["ttfb"]) for r in by[k] if r["ok"] == "1"] or [99])):
            print(line("%s %s" % k, by[k]))
        print()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--server", required=True)
    r.add_argument("--n", type=int, default=10); r.add_argument("--workers", type=int, default=5)
    r.add_argument("--timeout", type=int, default=30); r.add_argument("--providers", nargs="*")
    r.add_argument("--run-id"); r.set_defaults(f=cmd_run)
    s = sub.add_parser("summary"); s.add_argument("--server"); s.add_argument("--provider")
    s.add_argument("--countries", action="store_true"); s.set_defaults(f=cmd_summary)
    a = ap.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
