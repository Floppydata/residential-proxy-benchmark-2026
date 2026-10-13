#!/usr/bin/env python3
"""Residential proxy benchmark - target test (Wikipedia control + Amazon search).

Uses bench.py from the same folder for providers and proxy settings.

Commands:
  run       run the target test and save raw results to data/targets_*.csv
  summary   print Target Success Rate tables from all saved results
  schedule  print or install cron lines for repeated runs

Only the Python standard library is used.
"""
import argparse
import csv
import datetime
import glob
import os
import random
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench  # noqa: E402

WIKI_URL = "https://en.wikipedia.org/wiki/Proxy_server"
AMAZON_URLS = {
    "us": "https://www.amazon.com/s?k=laptop",
    "de": "https://www.amazon.de/s?k=laptop",
    "gb": "https://www.amazon.co.uk/s?k=laptop",
    "fr": "https://www.amazon.fr/s?k=laptop",
    "es": "https://www.amazon.es/s?k=laptop",
    "br": "https://www.amazon.com.br/s?k=laptop",
    "in": "https://www.amazon.in/s?k=laptop",
    "pl": "https://www.amazon.pl/s?k=laptop",
    "tr": "https://www.amazon.com.tr/s?k=laptop",
}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "Upgrade-Insecure-Requests": "1",
}
MAX_BYTES = 3000000
CAPTCHA_MARKERS = ("validateCaptcha", "Type the characters you see",
                   "Enter the characters you see", "api-services-support@amazon.com",
                   "To discuss automated access")
FIELDS = ["run_id", "ts_utc", "server", "provider", "target", "country_req", "url",
          "ok", "status", "sec", "bytes", "result"]
TARGET_ORDER = ["wikipedia", "amazon"]
TARGET_LABEL = {"wikipedia": "Wikipedia (control)", "amazon": "Amazon search"}


# ---------------------------------------------------------------- requests
def judge(target, status, text):
    """Turn a response into one result label. 'success' = usable page."""
    if target == "amazon" and any(m in text for m in CAPTCHA_MARKERS):
        return "captcha"
    if status == 200:
        if target == "wikipedia":
            return "success" if ("mw-parser-output" in text and "Wikipedia" in text) else "bad_content"
        return "success" if "s-search-result" in text else "bad_content"
    if status in (403, 429, 503):
        return "blocked_%d" % status
    if status == 404:
        return "not_found"
    return "http_%d" % status


def unzip(raw, encoding):
    if "gzip" in (encoding or "").lower():
        try:
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw).decode(errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    return raw.decode(errors="replace")


def fetch(opener, url, timeout):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw, enc = r.status, r.read(MAX_BYTES), r.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as e:
        status, raw, enc = e.code, e.read(MAX_BYTES), e.headers.get("Content-Encoding", "")
    return status, len(raw), unzip(raw, enc)


def one_target(opener, run_id, server, provider, target, cc, url, timeout):
    t = time.time()
    row = dict(run_id=run_id, ts_utc=bench.utcnow(), server=server, provider=provider,
               target=target, country_req=cc, url=url, ok=0, status=0, sec=0.0,
               bytes=0, result="")
    try:
        status, nbytes, text = fetch(opener, url, timeout)
        row.update(status=status, bytes=nbytes, result=judge(target, status, text))
        row["ok"] = int(row["result"] == "success")
    except Exception as e:  # noqa: BLE001 - every failure is a data point
        row["result"] = bench.classify_error(e)
    row["sec"] = round(time.time() - t, 3)
    return row


# ------------------------------------------------------------------ tables
def table(rows):
    by = defaultdict(list)
    for r in rows:
        by[(r["target"], r["country_req"])].append(r)
    head = "%-20s| %-8s| %-9s| %-30s| %-10s| %-8s| %-8s| %-8s| %-6s| %s" % (
        "Target", "Country", "Requests", "Target Success Rate (95% CI)", "Median, s",
        "P90, s", "Captcha", "Blocked", "Other", "Avg KB/request")
    print(head)
    print("-" * len(head))

    def line(tname, cname, rs):
        n, k = len(rs), sum(r["ok"] for r in rs)
        good = sorted(r["sec"] for r in rs if r["ok"])
        med = "%.2f" % statistics.median(good) if good else "-"
        p90 = "%.2f" % bench.pctl(good, 0.9) if good else "-"
        cap = sum(1 for r in rs if r["result"] == "captcha")
        blk = sum(1 for r in rs if r["result"].startswith("blocked_"))
        kb = sum(r["bytes"] for r in rs) / n / 1024.0
        print("%-20s| %-8s| %-9d| %-30s| %-10s| %-8s| %-8d| %-8d| %-6d| %.1f" % (
            tname, cname, n, bench.fmt_rate(k, n), med, p90, cap, blk, n - k - cap - blk, kb))

    for tg in [t for t in TARGET_ORDER if any(k[0] == t for k in by)]:
        ccs = [c for c in bench.COUNTRIES if (tg, c) in by] + \
              sorted(c for (t, c) in by if t == tg and c not in bench.COUNTRIES)
        for c in ccs:
            line(TARGET_LABEL[tg], c.upper(), by[(tg, c)])
        line(TARGET_LABEL[tg], "ALL", [r for r in rows if r["target"] == tg])
        print("-" * len(head))
    other = Counter(r["result"] for r in rows if not r["ok"])
    if other:
        print("Results other than success: " + ", ".join("%s x%d" % kv for kv in other.most_common()))


def report(rows, title):
    print()
    print("=" * 130)
    print(title)
    print("=" * 130)
    table(rows)


# ---------------------------------------------------------------- commands
def cmd_run(args):
    cfg = bench.load_config()
    keys = args.providers.split(",") if args.providers else sorted(cfg["providers"])
    for k in keys:
        if k not in cfg["providers"]:
            sys.exit("Unknown provider: %s (saved: %s)" % (k, ", ".join(sorted(cfg["providers"]))))
    countries = args.countries.split(",")
    amazon_cc = set(c for c in args.amazon_countries.split(",") if c)
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M", time.gmtime())
    os.makedirs(bench.DATA, exist_ok=True)
    path = os.path.join(bench.DATA, "targets_%s_%s.csv" % (args.server, run_id))

    def jobs(cc):
        out = []
        if args.n_wiki:
            out.append(("wikipedia", args.wiki_url or WIKI_URL, args.n_wiki))
        if args.n_amazon and cc in amazon_cc:
            url = args.amazon_url or AMAZON_URLS.get(cc)
            if url:
                out.append(("amazon", url, args.n_amazon))
        return out

    total = sum(n for cc in countries for (_, _, n) in jobs(cc)) * len(keys)
    print("[%s] target run %s on server '%s': %d providers, %d requests in total"
          % (bench.utcnow(), run_id, args.server, len(keys), total))
    rows = []
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for cc in countries:
            order = keys[:]
            random.shuffle(order)
            for key in order:
                opener = bench.make_opener(cfg["providers"][key], cc)
                for target, url, n in jobs(cc):
                    with ThreadPoolExecutor(max_workers=args.workers) as ex:
                        batch = list(ex.map(
                            lambda i: one_target(opener, run_id, args.server, key, target,
                                                 cc, url, args.timeout), range(n)))
                    w.writerows(batch)
                    f.flush()
                    rows += batch
                    print("[%s] %-12s %-3s %-10s %d/%d ok" % (
                        bench.utcnow(), key, cc.upper(), target, sum(r["ok"] for r in batch), n))
    print("[%s] finished, raw data in %s" % (bench.utcnow(), path))
    for key in keys:
        report([r for r in rows if r["provider"] == key],
               "Provider: %s | Server: %s | Run: %s | Targets: Wikipedia (control), Amazon search | Rotation: every request"
               % (key, args.server, run_id))


def load_rows(server="", run="", provider=""):
    rows = []
    for p in sorted(glob.glob(os.path.join(bench.DATA, "targets_*.csv"))):
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                r["ok"] = int(r["ok"])
                r["sec"] = float(r["sec"])
                r["bytes"] = int(r["bytes"] or 0)
                if (server and r["server"] != server) or (run and r["run_id"] != run) \
                        or (provider and r["provider"] != provider):
                    continue
                rows.append(r)
    return rows


def cmd_summary(args):
    rows = load_rows(args.server, args.run, args.provider)
    if not rows:
        sys.exit("No target data found in %s" % bench.DATA)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["server"], r["provider"])].append(r)
    for (server, prov), rs in sorted(groups.items()):
        runs = sorted(set(r["run_id"] for r in rs))
        report(rs, "Provider: %s | Server: %s | Runs: %d (%s .. %s) | Targets: Wikipedia (control), Amazon search | Rotation: every request"
               % (prov, server, len(runs), runs[0], runs[-1]))


def cmd_schedule(args):
    tag = "# bench-targets"
    hours = [int(h) for h in args.hours.split(",")]
    today = datetime.datetime.now(datetime.timezone.utc).date()
    now = datetime.datetime.now(datetime.timezone.utc)
    me = os.path.abspath(__file__)
    lines = []
    for d in range(args.start_in_days, args.start_in_days + args.days):
        day = today + datetime.timedelta(days=d)
        for h in hours:
            at_utc = datetime.datetime(day.year, day.month, day.day, h, args.minute,
                                       tzinfo=datetime.timezone.utc)
            if at_utc <= now:
                continue  # skip times that are already in the past
            at = at_utc.astimezone()
            lines.append("%d %d %d %d * cd %s && %s %s run --server %s >> %s 2>&1 %s" % (
                at.minute, at.hour, at.day, at.month, bench.HOME, sys.executable, me,
                args.server, os.path.join(bench.LOGS, "cron-targets.log"), tag))
    print("Target runs at %s UTC (minute %02d), starting %s:" % (
        ", ".join("%02d" % h for h in hours), args.minute,
        today + datetime.timedelta(days=args.start_in_days)))
    for ln in lines:
        print("  " + ln)
    if not args.install:
        print("\nNothing installed. Add --install to write these into cron.")
        return
    os.makedirs(bench.LOGS, exist_ok=True)
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    keep = [ln for ln in cur.stdout.splitlines() if tag not in ln] if cur.returncode == 0 else []
    subprocess.run(["crontab", "-"], input="\n".join(keep + lines) + "\n", text=True, check=True)
    print("\nInstalled %d target runs into cron. Check with:  crontab -l" % len(lines))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run")
    s.add_argument("--server", required=True, help="label of this test server, e.g. de, us, sg")
    s.add_argument("--n-wiki", type=int, default=20, help="Wikipedia requests per provider and country")
    s.add_argument("--n-amazon", type=int, default=30, help="Amazon requests per provider and country")
    s.add_argument("--countries", default=",".join(bench.COUNTRIES))
    s.add_argument("--amazon-countries", default="us,de,gb")
    s.add_argument("--providers", default="")
    s.add_argument("--workers", type=int, default=5)
    s.add_argument("--timeout", type=float, default=40)
    s.add_argument("--run-id", default="")
    s.add_argument("--wiki-url", default="", help="override (for tests only)")
    s.add_argument("--amazon-url", default="", help="override (for tests only)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("summary")
    s.add_argument("--server", default="")
    s.add_argument("--run", default="")
    s.add_argument("--provider", default="")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("schedule")
    s.add_argument("--server", required=True)
    s.add_argument("--days", type=int, default=4)
    s.add_argument("--hours", default="11,23", help="UTC hours, comma separated")
    s.add_argument("--minute", type=int, default=0)
    s.add_argument("--start-in-days", type=int, default=0)
    s.add_argument("--install", action="store_true")
    s.set_defaults(fn=cmd_schedule)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
