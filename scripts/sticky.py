#!/usr/bin/env python3
"""Sticky Session Stability test. Needs bench.py and providers.json in the same folder.

  python3 sticky.py check    --server us          (find the working sticky format of each provider, ~1 min)
  python3 sticky.py run      --server us          (32-minute test: 8 sessions per provider)
  python3 sticky.py summary                       (table of Sticky Session Stability)
  python3 sticky.py schedule --server us --install
"""
import argparse, csv, json, os, random, re, string, subprocess, statistics, sys, threading, time
import urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import bench

CHOICE_FILE = os.path.join(bench.HOME, "sticky_choice.json")
TARGET = bench.DEFAULT_TARGET
FIELDS = ["run_id", "ts_utc", "server", "provider", "session", "country", "t_sec", "ok", "ip", "err"]
MIN = 30  # requested session length, minutes

# Candidate sticky formats. {sid}=8 letters/digits, {n}=number, {m}=minutes, {s}=seconds.
# 'host' overrides the rotating host. 'user' and 'pass' are templates. The first one that works is used.
CAND = {
    "dataimpulse": [dict(user="{login}__cr.{cc};sessid.{sid}")],
    "floppydata": [
        dict(user="user-{login}-type-residential-country-{cc}-session-{sid}-sessionduration-{m}"),
        dict(user="user-{login}-type-residential-country-{cc}-session-{sid}-lifetime-{m}"),
        dict(user="user-{login}-type-residential-country-{cc}-sessid-{sid}-sesstime-{m}"),
        dict(user="user-{login}-type-residential-country-{cc}-session-{sid}"),
        dict(user="user-{login}-type-residential-country-{cc}-sid-{sid}"),
    ],
    "decodo": [
        dict(host="gate.decodo.com:10000", user="user-{login}-country-{cc}-session-{sid}-sessionduration-{m}"),
        dict(host="gate.decodo.com:7000", user="user-{login}-country-{cc}-session-{sid}-sessionduration-{m}"),
    ],
    "oxylabs": [dict(user="customer-{login}-cc-{cc}-sessid-{sid}-sesstime-{m}")],
    "webshare": [dict(user="{login}-{cc}-{n}"), dict(user="{login}-{cc}-session-{sid}")],
    "nodemaven": [dict(user="{login}-country-{cc}-sid-{sid}-ttl-{m}m"), dict(user="{login}-country-{cc}-sid-{sid}")],
    "proxyempire": [dict(user="{login}-country-{cc}-sid-{sid}"), dict(user="{login}-country-{cc}-sid-{n}")],
    "iproyal": [dict(user="{login}", **{"pass": "{password}_country-{cc}_session-{sid}_lifetime-{m}m"})],
    "rayobyte": [
        dict(user="{login}", **{"pass": "{password}-country-{CC}-session-{sid}"}),
        dict(user="{login}", **{"pass": "{password}-country-{CC}-session-{sid}-lifetime-{m}"}),
    ],
    "soax": [
        dict(host="proxy.soax.com:1337", user="country-{cc}-sessionid-{sid}-sessionlength-{s}"),
        dict(host="proxy.soax.com:5000", user="country-{cc}-sessionid-{sid}-sessionlength-{s}"),
    ],
    "evomi": [dict(user="{login}", **{"pass": "{password}_country-{CC}_session-{sid}_lifetime-{m}"})],
    "geonode": [
        dict(host="proxy.geonode.io:10000", user="{login}-type-residential-country-{cc}-session-{sid}-lifetime-{m}"),
        dict(host="proxy.geonode.io:10000", user="{login}-type-residential-country-{cc}-session-{sid}"),
        dict(host="proxy.geonode.io:9000", user="{login}-type-residential-country-{cc}-session-{sid}"),
    ],
}


def new_sid():
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))


def build_opener(entry, cand, cc, sid, minutes=MIN):
    f = dict(login=entry.get("login", ""), cc=cc, CC=cc.upper(), sid=sid,
             n=int(sid, 36) % 90000 + 10000, m=minutes, s=minutes * 60)
    user = cand["user"]
    pw = cand.get("pass", entry.get("pass", "{password}"))
    for k, v in f.items():
        user = user.replace("{%s}" % k, str(v))
        pw = pw.replace("{%s}" % k, str(v))
    pw = pw.replace("{password}", entry["password"])
    host = cand.get("host", entry["host"])
    proxy = "http://%s:%s@%s" % (urllib.parse.quote(user, safe=""), urllib.parse.quote(pw, safe=""), host)
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def fetch_ip(opener, target=None, timeout=25):
    """Returns (ip, err)."""
    try:
        with opener.open(urllib.request.Request(target or TARGET, headers={"User-Agent": "Mozilla/5.0"}),
                         timeout=timeout) as r:
            text = r.read(4096).decode("utf-8", "replace")
        m = re.search(r"^ip=(\S+)", text, re.M)
        return (m.group(1), "") if m else ("", "no_ip_in_reply")
    except Exception as e:
        return "", bench.classify_error(e)


def now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ check
def cmd_check(args):
    cfg = bench.load_config()["providers"]
    choice = json.load(open(CHOICE_FILE)) if os.path.exists(CHOICE_FILE) else {}
    print("%-12s| %-3s | %-62s| %s" % ("Provider", "Try", "Sticky format tried", "Result"))
    print("-" * 110)
    for key, entry in cfg.items():
        if args.providers and key not in args.providers:
            continue
        cands = CAND.get(key, [])
        found = False
        for i, cand in enumerate(cands, 1):
            sid = new_sid()
            ips, errs = [], []
            for _ in range(3):
                ip, err = fetch_ip(build_opener(entry, cand, "us", sid), args.target)
                ips.append(ip); errs.append(err)
                time.sleep(3)
            other, _e = fetch_ip(build_opener(entry, cand, "us", new_sid()), args.target)
            good = all(ips) and len(set(ips)) == 1
            label = ("%s | %s" % (cand.get("host", entry["host"]), cand["user"]))[:60]
            if good:
                res = "OK same IP x3" + ("" if other != ips[0] else " (new session gave same IP, check!)")
            else:
                res = "FAIL " + (", ".join(sorted(set(e for e in errs if e))) or "IP changed within 9 s")
            print("%-12s| %-3d | %-62s| %s" % (entry["label"], i, label, res))
            if good and not found:
                choice[key] = i - 1
                found = True
        if not found:
            choice.pop(key, None)
            print("%-12s| --- | %-62s| NO WORKING FORMAT FOUND" % (entry["label"], ""))
    json.dump(choice, open(CHOICE_FILE, "w"))
    print("\nWorking: %d of %d providers. Saved to %s" % (len(choice), len(cfg), CHOICE_FILE))


# -------------------------------------------------------------------- run
lock = threading.Lock()


def session_worker(entry, key, cand, run_id, server, idx, cc, sid, minutes, interval, writer, target):
    opener = build_opener(entry, cand, cc, sid, minutes)
    start = time.time()
    end = start + minutes * 60
    k = 0
    while True:
        t = time.time() - start
        ip, err = fetch_ip(opener, target)
        with lock:
            writer.writerow(dict(run_id=run_id, ts_utc=now().strftime("%Y-%m-%dT%H:%M:%SZ"), server=server,
                                 provider=key, session=idx, country=cc, t_sec=int(t), ok=int(bool(ip)),
                                 ip=ip, err=err))
        k += 1
        nxt = start + k * interval
        if nxt > end:
            break
        time.sleep(max(0, nxt - time.time()))


def cmd_run(args):
    cfg = bench.load_config()["providers"]
    if not os.path.exists(CHOICE_FILE):
        sys.exit("Run 'check' first.")
    choice = json.load(open(CHOICE_FILE))
    run_id = args.run_id or now().strftime("%Y%m%d-%H%M")
    os.makedirs(bench.DATA, exist_ok=True)
    path = os.path.join(bench.DATA, "sticky_%s_%s.csv" % (args.server, run_id))
    rnd = random.Random(run_id + args.server)
    countries = [rnd.choice(bench.COUNTRIES) for _ in range(args.sessions)]
    jobs = []
    for key, entry in cfg.items():
        if key not in choice or (args.providers and key not in args.providers):
            continue
        for i in range(args.sessions):
            jobs.append((entry, key, CAND[key][choice[key]], run_id, args.server, i + 1, countries[i], new_sid(),
                         args.minutes, args.interval))
    print("Sticky run %s on %s: %d sessions, %d min, request every %d s" %
          (run_id, args.server, len(jobs), args.minutes, args.interval))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = []
            for j in jobs:
                futs.append(ex.submit(session_worker, *j, w, args.target))
                time.sleep(0.2)
            for fu in futs:
                fu.result()
    print("Saved %s" % path)


# ---------------------------------------------------------------- summary
def load_rows(server=None):
    rows = []
    for fn in sorted(os.listdir(bench.DATA)):
        if fn.startswith("sticky_") and fn.endswith(".csv"):
            if server and ("_%s_" % server) not in fn:
                continue
            with open(os.path.join(bench.DATA, fn)) as f:
                rows += list(csv.DictReader(f))
    return rows


def analyse(rows):
    """One verdict per session."""
    by = defaultdict(list)
    for r in rows:
        by[(r["run_id"], r["server"], r["provider"], r["session"])].append(r)
    out = defaultdict(list)
    for (run, srv, prov, ses), rs in by.items():
        rs.sort(key=lambda r: int(r["t_sec"]))
        oks = [r for r in rs if r["ok"] == "1"]
        planned = len(rs)
        if not oks:
            out[prov].append(dict(started=False, stable=False, hold=0.0, n=planned, ok=0, same=0))
            continue
        first = oks[0]["ip"]
        changed_at = None
        for r in oks:
            if r["ip"] != first:
                changed_at = int(r["t_sec"]); break
        last_t = int(oks[-1]["t_sec"])
        hold = (changed_at if changed_at is not None else last_t) / 60.0
        out[prov].append(dict(started=True, stable=(changed_at is None and len(oks) >= 0.9 * planned),
                              hold=hold, n=planned, ok=len(oks), same=sum(1 for r in oks if r["ip"] == first)))
    return out


def cmd_summary(args):
    rows = load_rows(args.server)
    if not rows:
        sys.exit("No sticky data yet.")
    res = analyse(rows)
    head = "%-12s| %-9s| %-30s| %-18s| %-22s| %-10s" % (
        "Provider", "Sessions", "Sticky Session Stability (95% CI)", "Median IP hold, min",
        "Request IP consistency", "Error Rate")
    print(head); print("-" * len(head))
    for prov in sorted(res, key=lambda p: -sum(s["stable"] for s in res[p]) / len(res[p])):
        ss = res[prov]
        n, k = len(ss), sum(s["stable"] for s in ss)
        med = statistics.median(s["hold"] for s in ss)
        okreq = sum(s["ok"] for s in ss); allreq = sum(s["n"] for s in ss)
        cons = sum(s["same"] for s in ss) / okreq * 100 if okreq else 0
        print("%-12s| %-9d| %-30s| %-18.1f| %-22s| %.1f%%" % (
            bench.TEMPLATES.get(prov, {}).get("label", prov), n, bench.fmt_rate(k, n), med,
            "%.1f%%" % cons, (allreq - okreq) / allreq * 100))


# --------------------------------------------------------------- schedule
def cmd_schedule(args):
    start = now() + timedelta(minutes=5)
    until = datetime.strptime(args.until, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    hours = [int(h) for h in args.hours.split(",")]
    runs, d = [], start.replace(hour=0, minute=0, second=0, microsecond=0)
    while d <= until:
        for h in hours:
            t = d.replace(hour=h, minute=args.minute)
            if start <= t <= until:
                runs.append(t)
        d += timedelta(days=1)
    here = os.path.dirname(os.path.abspath(__file__))
    lines = ["%d %d %d %d * cd %s && %s %s run --server %s >> %s/logs/cron-sticky.log 2>&1 # bench-sticky" %
             (t.minute, t.hour, t.day, t.month, here, sys.executable, os.path.join(here, "sticky.py"),
              args.server, here) for t in runs]
    print("Sticky runs (UTC), %d min each:" % MIN)
    for l in lines:
        print("  " + l)
    if not args.install:
        print("\nNothing installed. Add --install to put them into cron."); return
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    keep = [l for l in cur.splitlines() if "# bench-sticky" not in l]
    new = "\n".join(keep + lines) + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    print("\nInstalled %d sticky runs into cron. Check with:  crontab -l" % len(lines))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("--providers", nargs="*"); c.add_argument("--server", default="us")
    c.add_argument("--target", default=None); c.set_defaults(f=cmd_check)
    r = sub.add_parser("run"); r.add_argument("--server", required=True); r.add_argument("--sessions", type=int, default=8)
    r.add_argument("--minutes", type=int, default=MIN); r.add_argument("--interval", type=int, default=30)
    r.add_argument("--providers", nargs="*"); r.add_argument("--run-id"); r.add_argument("--target", default=None)
    r.set_defaults(f=cmd_run)
    s = sub.add_parser("summary"); s.add_argument("--server"); s.set_defaults(f=cmd_summary)
    sc = sub.add_parser("schedule"); sc.add_argument("--server", required=True)
    sc.add_argument("--until", default="2026-10-08 22:00"); sc.add_argument("--hours", default="3,9,15,21")
    sc.add_argument("--minute", type=int, default=30); sc.add_argument("--install", action="store_true")
    sc.set_defaults(f=cmd_schedule)
    a = ap.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
