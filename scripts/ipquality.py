#!/usr/bin/env python3
"""IP quality check of the IPs collected by the benchmark.
Scamalytics (fraud score, datacenter/VPN/proxy flags) + IPinfo Lite (ASN, country).

  python3 ipquality.py setup                   (enter keys once, hidden)
  python3 ipquality.py run --per-provider 20   (pilot; later 300)
  python3 ipquality.py summary
"""
import argparse, csv, getpass, glob, json, os, random, re, statistics, sys, time
import urllib.parse, urllib.request
from collections import defaultdict

import bench

HOME = bench.HOME
CONF = os.path.join(HOME, "ipquality.json")
CACHE = os.path.join(HOME, "ipcache.json")
SAMPLE = os.path.join(HOME, "ipsample.csv")
SCAM_BASE = os.environ.get("SCAM_BASE", "https://api11.scamalytics.com/v3")
IPINFO_BASE = os.environ.get("IPINFO_BASE", "https://api.ipinfo.io/lite")
HOSTING_WORDS = ("hosting", "cloud", "datacenter", "data center", "data-center", "server", "vps", "colo",
                 "amazon", "aws", "google", "microsoft", "azure", "digitalocean", "ovh", "hetzner",
                 "leaseweb", "m247", "cloudflare", "linode", "akamai", "vultr", "contabo", "choopa",
                 "oracle", "alibaba", "tencent", "scaleway", "packet", "datacamp", "cdn77", "zenlayer",
                 "ipxo", "psychz", "quadranet", "colocrossing", "serverius", "worldstream")


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, obj, private=False):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)


def cmd_setup(args):
    c = load_json(CONF, {})
    c["scam_user"] = input("Scamalytics API username [%s]: " % ("saved" if c.get("scam_user") else "empty")).strip() or c.get("scam_user", "")
    k = getpass.getpass("Scamalytics API key (hidden): ").strip()
    c["scam_key"] = k or c.get("scam_key", "")
    t = getpass.getpass("IPinfo Lite token (hidden): ").strip()
    c["ipinfo_token"] = t or c.get("ipinfo_token", "")
    save_json(CONF, c, private=True)
    print("Saved to %s (readable only by you)." % CONF)


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def lookup(ip, conf):
    out = {"ip": ip}
    try:
        q = urllib.parse.urlencode({"key": conf["scam_key"], "ip": ip})
        out["scam"] = http_json("%s/%s?%s" % (SCAM_BASE, urllib.parse.quote(conf["scam_user"]), q))
    except Exception as e:  # noqa: BLE001
        out["scam_err"] = str(e)[:100]
    try:
        out["ipinfo"] = http_json("%s/%s?token=%s" % (IPINFO_BASE, urllib.parse.quote(ip), conf["ipinfo_token"]))
    except Exception as e:  # noqa: BLE001
        out["ipinfo_err"] = str(e)[:100]
    return out


def collect():
    """provider -> country -> {ip: server}"""
    by = defaultdict(lambda: defaultdict(dict))
    files = glob.glob(os.path.join(bench.DATA, "infra_*.csv")) + glob.glob(os.path.join(bench.DATA, "remote", "infra_*.csv"))
    for fn in sorted(files):
        with open(fn, newline="") as f:
            for r in csv.DictReader(f):
                if r["ok"] == "1" and r["ip"]:
                    by[r["provider"]][r["country_req"]].setdefault(r["ip"], r["server"])
    return by


def cmd_run(args):
    conf = load_json(CONF, {})
    if not (conf.get("scam_user") and conf.get("scam_key") and conf.get("ipinfo_token")):
        sys.exit("Run 'setup' first.")
    cache = load_json(CACHE, {})
    by = collect()
    rnd = random.Random(args.seed)
    rows = []
    for prov in sorted(by):
        per_cc = {}
        for cc, ips in by[prov].items():
            lst = sorted(ips)
            rnd.shuffle(lst)
            per_cc[cc] = lst
        picked, i = [], 0
        while len(picked) < args.per_provider and any(i < len(v) for v in per_cc.values()):
            for cc in sorted(per_cc):
                if i < len(per_cc[cc]) and len(picked) < args.per_provider:
                    ip = per_cc[cc][i]
                    picked.append((prov, ip, cc, by[prov][cc][ip]))
            i += 1
        rows += picked
    new = sorted(set(ip for _, ip, _, _ in rows if ip not in cache))
    print("Sample: %d IPs (%d providers). New lookups needed: %d (Scamalytics free limit: 5000 a month)." %
          (len(rows), len(by), len(new)))
    if len(new) > args.max_new:
        sys.exit("Too many new lookups (%d > --max-new %d). Lower --per-provider or raise --max-new." % (len(new), args.max_new))
    if not args.yes:
        if input("Continue? [y/N] ").strip().lower() != "y":
            return
    with open(SAMPLE, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["provider", "ip", "country_req", "server"]); w.writerows(rows)
    left = None
    for n, ip in enumerate(new, 1):
        res = lookup(ip, conf)
        cache[ip] = res
        cr = (res.get("scam") or {}).get("scamalytics", {}).get("credits", {})
        if cr:
            left = cr.get("remaining", left)
        if "scam_err" in res and ("401" in res["scam_err"] or "403" in res["scam_err"]):
            save_json(CACHE, cache); sys.exit("Scamalytics refused the key: %s" % res["scam_err"])
        if n % 25 == 0 or n == len(new):
            save_json(CACHE, cache)
            print("[%d/%d] done%s" % (n, len(new), ("  credits remaining: %s" % left) if left is not None else ""))
        time.sleep(0.15)
    save_json(CACHE, cache)
    print("Finished. Now run:  python3 ipquality.py summary")


def parse(res, cc):
    sc = (res.get("scam") or {}).get("scamalytics") or {}
    px = sc.get("scamalytics_proxy") or {}
    info = res.get("ipinfo") or {}
    asname = (info.get("as_name") or "").lower()
    dc = bool(px.get("is_datacenter"))
    d = dict(ok=bool(sc.get("scamalytics_score") is not None),
             score=sc.get("scamalytics_score"), risk=sc.get("scamalytics_risk"),
             dc=dc, vpn=bool(px.get("is_vpn")), resproxy=bool(px.get("is_resproxy")),
             other=bool(px.get("is_amazon_aws") or px.get("is_google") or px.get("is_apple_icloud_private_relay")),
             blacklisted=bool(sc.get("is_blacklisted_external")),
             host_asn=any(w in asname for w in HOSTING_WORDS),
             ipinfo_ok=bool(info.get("country_code")), geo=(info.get("country_code") or "").lower() == cc)
    d["proxy"] = d["dc"] or d["vpn"] or d["resproxy"] or d["other"]
    d["residential"] = not (d["dc"] or d["host_asn"])
    return d


def pct(k, n):
    return "%.1f%%" % (100.0 * k / n) if n else "-"


def cmd_summary(args):
    cache = load_json(CACHE, {})
    by = defaultdict(list)
    with open(SAMPLE, newline="") as f:
        for r in csv.DictReader(f):
            if r["ip"] in cache:
                by[r["provider"]].append(parse(cache[r["ip"]], r["country_req"]))
    head = "%-12s| %-5s| %-10s| %-10s| %-12s| %-12s| %-10s| %-10s| %-24s| %s" % (
        "Provider", "IPs", "Score avg", "Score med", "High risk", "Proxy Det.", "Datacenter", "VPN",
        "Residential/ISP (ASN)", "Geo (IPinfo)")
    print(head); print("-" * len(head))
    for prov in sorted(by):
        ds = [d for d in by[prov] if d["ok"]]
        n = len(ds)
        if not n:
            print("%-12s| no valid Scamalytics answers" % prov); continue
        sc = [d["score"] for d in ds]
        hi = sum(1 for d in ds if d["risk"] in ("high", "very high"))
        gi = [d for d in by[prov] if d["ipinfo_ok"]]
        print("%-12s| %-5d| %-10.1f| %-10.1f| %-12s| %-12s| %-10s| %-10s| %-24s| %s" % (
            prov, n, statistics.mean(sc), statistics.median(sc), pct(hi, n),
            pct(sum(d["proxy"] for d in ds), n), pct(sum(d["dc"] for d in ds), n),
            pct(sum(d["vpn"] for d in ds), n), pct(sum(d["residential"] for d in ds), n),
            pct(sum(d["geo"] for d in gi), len(gi))))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(f=cmd_setup)
    r = sub.add_parser("run"); r.add_argument("--per-provider", type=int, default=20)
    r.add_argument("--seed", type=int, default=1); r.add_argument("--max-new", type=int, default=4200)
    r.add_argument("--yes", action="store_true"); r.set_defaults(f=cmd_run)
    sub.add_parser("summary").set_defaults(f=cmd_summary)
    a = ap.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
