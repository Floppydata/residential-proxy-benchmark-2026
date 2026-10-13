#!/usr/bin/env python3
"""Residential proxy benchmark - infrastructure test (CDN target).

Commands:
  setup     save provider credentials (file is readable only by you)
  check     send 1 test request per provider/country to verify credentials
  run       run the benchmark and save raw results to data/*.csv
  summary   print tables (Success Rate with 95% CI, latency, Unique IPs, Geo Accuracy)
  schedule  print or install cron lines for repeated runs

Only the Python standard library is used.
"""
import argparse
import csv
import datetime
import getpass
import glob
import json
import math
import os
import random
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

HOME = os.environ.get("BENCH_HOME") or os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HOME, "providers.json")
DATA = os.path.join(HOME, "data")
LOGS = os.path.join(HOME, "logs")

DEFAULT_TARGET = "https://www.cloudflare.com/cdn-cgi/trace"
COUNTRIES = ["us", "de", "gb", "fr", "es", "br", "in", "pl", "ua", "tr", "vn", "id"]

TEMPLATES = {
    "dataimpulse": dict(label="DataImpulse", host="gw.dataimpulse.com:823",
                        user="{login}__cr.{cc}"),
    "floppydata": dict(label="Floppydata", host="geo.g-w.info:10080",
                       user="user-{login}-type-residential-country-{cc}"),
    "decodo": dict(label="Decodo", host="gate.decodo.com:7000",
                   user="user-{login}-country-{cc}"),
    "oxylabs": dict(label="Oxylabs", host="pr.oxylabs.io:7777",
                    user="customer-{login}-cc-{cc}"),
    "webshare": dict(label="Webshare", host="p.webshare.io:80",
                     user="{login}-{cc}-rotate"),
    "nodemaven": dict(label="NodeMaven", host="gate.nodemaven.com:8080",
                      user="{login}-country-{cc}"),
    "proxyempire": dict(label="ProxyEmpire", host="v2.proxyempire.io:5000",
                        user="{login}-country-{cc}"),
    "iproyal": dict(label="IPRoyal", host="geo.iproyal.com:12321",
                    user="{login}", **{"pass": "{password}_country-{cc}"}),
    "rayobyte": dict(label="Rayobyte", host="la.residential.rayobyte.com:8000",
                     user="{login}", **{"pass": "{password}-country-{CC}"}),
    "soax": dict(label="SOAX", host="proxy.soax.com:1337",
                 user="country-{cc}"),
    "evomi": dict(label="Evomi", host="rp.evomi.com:1000",
                  user="{login}", **{"pass": "{password}_country-{CC}"}),
    "geonode": dict(label="Geonode", host="proxy.geonode.io:9000",
                    user="{login}-type-residential-country-{cc}"),
}

FIELDS = ["run_id", "ts_utc", "server", "provider", "test", "target",
          "country_req", "ok", "sec", "ip", "geo_cf", "colo", "bytes", "err"]


# ----------------------------------------------------------------- config
def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("No providers.json yet. Run:  python3 bench.py setup")
    with open(CONFIG) as f:
        return json.load(f)


def save_config(cfg):
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(CONFIG, 0o600)


def cmd_setup(args):
    cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {"providers": {}}
    print("Enter provider credentials. Press Enter to keep the saved value or skip.")
    for key, tpl in TEMPLATES.items():
        if args.providers and key not in args.providers:
            continue
        cur = cfg["providers"].get(key, {})
        print("\n%s" % tpl["label"])
        hint = " (the Subuser value)" if key == "floppydata" else ""
        if "{login}" not in tpl["user"]:
            hint = " (not needed - just press Enter)"
        state = "saved" if cur.get("login") else "empty"
        login = input("  Login%s [%s]: " % (hint, state)).strip()
        password = getpass.getpass("  Password (hidden while typing): ").strip()
        login = login or cur.get("login", "")
        password = password or cur.get("password", "")
        if password and (login or "{login}" not in tpl["user"]):
            entry = dict(tpl)
            entry.update(login=login, password=password)
            cfg["providers"][key] = entry
    save_config(cfg)
    print("\nSaved providers: %s" % (", ".join(sorted(cfg["providers"])) or "none"))
    print("File %s is readable only by the current user." % CONFIG)


# ---------------------------------------------------------------- network
def make_opener(entry, cc):
    user = entry["user"].format(login=entry["login"], cc=cc)
    pw = (entry.get("pass", "{password}").replace("{cc}", cc).replace("{CC}", cc.upper())
          .replace("{password}", entry["password"]))
    proxy = "http://%s:%s@%s" % (urllib.parse.quote(user, safe=""),
                                 urllib.parse.quote(pw, safe=""),
                                 entry["host"])
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def classify_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return "http_%d" % e.code
    s = str(e).lower()
    if "407" in s:
        return "proxy_auth_407"
    if isinstance(e, socket.timeout) or "timed out" in s:
        return "timeout"
    if "tunnel connection failed" in s:
        code = s.split(":")[-1].strip().split(" ")[0]
        return "tunnel_failed_" + code if code.isdigit() else "tunnel_failed"
    if "reset" in s:
        return "conn_reset"
    if "refused" in s:
        return "conn_refused"
    if "ssl" in s or "certificate" in s:
        return "ssl_error"
    return "other: " + str(e)[:60]


def utcnow():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def one_request(opener, run_id, server, provider, cc, target, timeout):
    t = time.time()
    row = dict(run_id=run_id, ts_utc=utcnow(), server=server, provider=provider,
               test="cdn", target=target, country_req=cc, ok=0, sec=0.0, ip="",
               geo_cf="", colo="", bytes=0, err="")
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode(errors="replace")
        d = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
        row["bytes"] = len(body)
        if d.get("ip"):
            row.update(ok=1, ip=d["ip"], geo_cf=d.get("loc", "").lower(),
                       colo=d.get("colo", ""))
        else:
            row["err"] = "unexpected_response"
    except Exception as e:  # noqa: BLE001 - every failure is a data point
        row["err"] = classify_error(e)
    row["sec"] = round(time.time() - t, 3)
    return row


# ------------------------------------------------------------------ stats
def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d


def pctl(sorted_vals, q):
    n = len(sorted_vals)
    return sorted_vals[min(n - 1, max(0, math.ceil(q * n) - 1))]


def fmt_rate(k, n):
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return "%.1f%% (%.1f-%.1f)" % (100.0 * k / n, 100 * lo, 100 * hi)


def stats(rs):
    good = [r for r in rs if r["ok"]]
    out = dict(n=len(rs), k=len(good))
    if good:
        t = sorted(r["sec"] for r in good)
        out["med"] = statistics.median(t)
        out["p90"] = pctl(t, 0.9)
        out["uniq"] = len(set(r["ip"] for r in good))
        out["geo"] = sum(1 for r in good if r["geo_cf"] == r["country_req"])
    return out


def print_table(rows):
    by = defaultdict(list)
    for r in rows:
        by[r["country_req"]].append(r)
    order = [c for c in COUNTRIES if c in by] + sorted(c for c in by if c not in COUNTRIES)
    head = "%-8s| %-9s| %-24s| %-10s| %-8s| %-13s| %-11s| %s" % (
        "Country", "Requests", "Success Rate (95% CI)", "Median, s", "P90, s",
        "Unique IPs", "Duplicates", "Geo Accuracy (Cloudflare)")
    print(head)
    print("-" * len(head))

    def line(label, s):
        if not s["k"]:
            print("%-8s| %-9d| %-24s| no successful requests" % (label, s["n"], fmt_rate(0, s["n"])))
            return
        print("%-8s| %-9d| %-24s| %-10.2f| %-8.2f| %-13s| %-11s| %s" % (
            label, s["n"], fmt_rate(s["k"], s["n"]), s["med"], s["p90"],
            "%d/%d" % (s["uniq"], s["k"]),
            "%.1f%%" % (100.0 * (1 - s["uniq"] / s["k"])),
            fmt_rate(s["geo"], s["k"])))

    for c in order:
        line(c.upper(), stats(by[c]))
    print("-" * len(head))
    line("TOTAL", stats(rows))
    errs = Counter(r["err"] for r in rows if not r["ok"])
    if errs:
        print("Errors: " + ", ".join("%s x%d" % kv for kv in errs.most_common()))


def print_report(rows, title):
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)
    print_table(rows)


# --------------------------------------------------------------- commands
def cmd_check(args):
    cfg = load_config()
    for key, entry in sorted(cfg["providers"].items()):
        if args.providers and key not in args.providers.split(","):
            continue
        for cc in args.countries.split(","):
            row = one_request(make_opener(entry, cc), "check", "check", key, cc,
                              args.target, args.timeout)
            print("%-12s %-3s ok=%d  %.2fs  ip=%s  cloudflare_country=%s  %s" % (
                key, cc, row["ok"], row["sec"], row["ip"] or "-", row["geo_cf"] or "-",
                row["err"]))


def cmd_run(args):
    cfg = load_config()
    keys = args.providers.split(",") if args.providers else sorted(cfg["providers"])
    for k in keys:
        if k not in cfg["providers"]:
            sys.exit("Unknown provider: %s (saved: %s)" % (k, ", ".join(sorted(cfg["providers"]))))
    countries = args.countries.split(",")
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M", time.gmtime())
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "infra_%s_%s.csv" % (args.server, run_id))
    total = args.n * len(countries) * len(keys)
    print("[%s] run %s on server '%s': %d providers x %d countries x %d requests = %d requests"
          % (utcnow(), run_id, args.server, len(keys), len(countries), args.n, total))
    rows = []
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        # countries are the outer loop and providers the inner one, so both
        # providers are measured at nearly the same time in every country
        for cc in countries:
            order = keys[:]
            random.shuffle(order)
            for key in order:
                opener = make_opener(cfg["providers"][key], cc)
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    batch = list(ex.map(
                        lambda i: one_request(opener, run_id, args.server, key, cc,
                                              args.target, args.timeout), range(args.n)))
                w.writerows(batch)
                f.flush()
                rows += batch
                ok = sum(r["ok"] for r in batch)
                print("[%s] %-12s %-3s %d/%d ok" % (utcnow(), key, cc.upper(), ok, args.n))
    print("[%s] finished, raw data in %s" % (utcnow(), path))
    for key in keys:
        print_report([r for r in rows if r["provider"] == key],
                     "Provider: %s | Server: %s | Run: %s | Target: %s | Rotation: every request"
                     % (key, args.server, run_id, args.target))


def load_rows(server=None, run=None, provider=None):
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA, "infra_*.csv"))):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                r["ok"] = int(r["ok"])
                r["sec"] = float(r["sec"])
                r["bytes"] = int(r["bytes"] or 0)
                if server and r["server"] != server:
                    continue
                if run and r["run_id"] != run:
                    continue
                if provider and r["provider"] != provider:
                    continue
                rows.append(r)
    return rows


def cmd_summary(args):
    rows = load_rows(args.server, args.run, args.provider)
    if not rows:
        sys.exit("No data found in %s" % DATA)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["server"], r["provider"])].append(r)
    for (server, prov), rs in sorted(groups.items()):
        runs = sorted(set(r["run_id"] for r in rs))
        print_report(rs, "Provider: %s | Server: %s | Runs: %d (%s .. %s) | Target: %s | Rotation: every request"
                     % (prov, server, len(runs), runs[0], runs[-1], rs[0]["target"]))


def cmd_schedule(args):
    tag = "# bench-auto"
    hours = [int(h) for h in args.hours.split(",")]
    today = datetime.datetime.now(datetime.timezone.utc).date()
    lines = []
    for d in range(args.start_in_days, args.start_in_days + args.days):
        day = today + datetime.timedelta(days=d)
        for h in hours:
            at_utc = datetime.datetime(day.year, day.month, day.day, h, 0,
                                       tzinfo=datetime.timezone.utc)
            if at_utc <= datetime.datetime.now(datetime.timezone.utc):
                continue  # skip times that are already in the past
            at = at_utc.astimezone()
            lines.append("%d %d %d %d * cd %s && %s bench.py run --server %s --n %d >> %s 2>&1 %s"
                         % (at.minute, at.hour, at.day, at.month, HOME, sys.executable,
                            args.server, args.n, os.path.join(LOGS, "cron.log"), tag))
    print("Runs at %s UTC on %d day(s), starting %s:" % (
        ", ".join("%02d:00" % h for h in hours), args.days,
        today + datetime.timedelta(days=args.start_in_days)))
    for ln in lines:
        print("  " + ln)
    if not args.install:
        print("\nNothing installed. Add --install to write these into cron.")
        return
    os.makedirs(LOGS, exist_ok=True)
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    keep = [ln for ln in cur.stdout.splitlines() if tag not in ln] if cur.returncode == 0 else []
    new = "\n".join(keep + lines) + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    print("\nInstalled %d runs into cron. Check with:  crontab -l" % len(lines))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup")
    s.add_argument("providers", nargs="*", help="only these providers, e.g. proxyempire nodemaven")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("check")
    s.add_argument("--countries", default="us,de")
    s.add_argument("--providers", default="")
    s.add_argument("--target", default=DEFAULT_TARGET)
    s.add_argument("--timeout", type=float, default=30)
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("run")
    s.add_argument("--server", required=True, help="label of this test server, e.g. de, us, sg")
    s.add_argument("--n", type=int, default=100, help="requests per provider and country")
    s.add_argument("--workers", type=int, default=5)
    s.add_argument("--countries", default=",".join(COUNTRIES))
    s.add_argument("--providers", default="")
    s.add_argument("--target", default=DEFAULT_TARGET)
    s.add_argument("--timeout", type=float, default=30)
    s.add_argument("--run-id", default="")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("summary")
    s.add_argument("--server", default="")
    s.add_argument("--run", default="")
    s.add_argument("--provider", default="")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("schedule")
    s.add_argument("--server", required=True)
    s.add_argument("--days", type=int, default=3)
    s.add_argument("--hours", default="8,20", help="UTC hours, comma separated")
    s.add_argument("--start-in-days", type=int, default=1)
    s.add_argument("--n", type=int, default=100)
    s.add_argument("--install", action="store_true")
    s.set_defaults(fn=cmd_schedule)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
