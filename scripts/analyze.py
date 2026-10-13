#!/usr/bin/env python3
"""Residential proxy benchmark: merge CSVs of the three servers into one workbook.

  python3 -I analyze.py --raw RAWDIR --out results.xlsx [--ipq IPQDIR]

RAWDIR contains one folder per server (us, de, sg), each with the folder data/ from that server.
IPQDIR (optional) contains ipsample.csv and ipcache.json from the IP-quality run.
"""
import argparse, glob, json, math, os, statistics
from collections import defaultdict

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

START = "2026-10-05 19:00:00"          # nothing before the official start counts
STICKY_WINDOW_S = 25 * 60
FLOPPY_NEW_FORMAT_FROM = "2026-10-06 09:00:00"   # sticky format from the official docs
SERVERS = ["us", "de", "sg"]
SERVER_NAMES = {"us": "USA (Ashburn)", "de": "Germany", "sg": "Singapore"}

LABEL = {"dataimpulse": "DataImpulse", "floppydata": "Floppydata", "decodo": "Decodo", "oxylabs": "Oxylabs",
         "webshare": "Webshare", "nodemaven": "NodeMaven", "proxyempire": "ProxyEmpire", "iproyal": "IPRoyal",
         "rayobyte": "Rayobyte", "soax": "SOAX", "evomi": "Evomi", "geonode": "Geonode"}
ORDER = ["dataimpulse", "floppydata", "oxylabs", "rayobyte", "webshare", "proxyempire", "soax", "nodemaven",
         "decodo", "evomi", "geonode", "iproyal"]
# price per GB as actually paid (invoices of 5 Oct 2026; ProxyEmpire: 2 GB incl. tax), and the package
PRICE = {"geonode": (0.79, "10 GB, subscription"), "dataimpulse": (1.00, "50 GB"), "floppydata": (1.00, "50 GB, 30 days"),
         "evomi": (1.04, "Core tariff, EUR 0.93 ex-VAT"), "webshare": (2.70, "3 GB"), "rayobyte": (3.50, "2 GB"),
         "decodo": (3.75, "3 GB"), "nodemaven": (5.00, "2 GB"), "soax": (4.67, "effective price, 1 credit = $1"),
         "iproyal": (5.31, "2 GB, may include tax"), "oxylabs": (6.00, "5 GB"),
         "proxyempire": (8.47, "2 x 1 GB, $7.00 ex-tax, 8.47 incl. tax")}
# assumptions for the cost estimate, to be replaced after calibration with provider panel counters
INFRA_OVERHEAD_BYTES = 6000      # TLS handshake with Cloudflare + headers, per request through the tunnel
TARGET_OVERHEAD_BYTES = 8000     # TLS handshake and headers for Amazon / Wikipedia
TARGET_WIRE_FACTOR = 0.25        # compressed size / logged size (the logged size is after decompression)


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def fmt_rate(k, n):
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return "%.1f%% (%.1f-%.1f)" % (100.0 * k / n, max(0.0, 100 * lo), min(100.0, 100 * hi))


def norm_ts(s):
    return s.replace("T", " ").replace("Z", "")


def read_all(raw, prefix):
    frames = []
    for srv in SERVERS:
        for fn in sorted(glob.glob(os.path.join(raw, srv, "data", prefix + "_*.csv"))):
            df = pd.read_csv(fn, dtype=str, keep_default_na=False)
            if len(df):
                df["server"] = srv
                frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = df["ts_utc"].map(norm_ts)
    return df[df["ts"] >= START].copy()


def num(df, cols):
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ------------------------------------------------------------------ analyses
def load_infra(raw):
    df = read_all(raw, "infra")
    if df.empty:
        return df
    df = num(df, ["ok", "sec", "bytes"])
    df["geo_ok"] = (df["geo_cf"].str.lower() == df["country_req"].str.lower()) & (df["ok"] == 1)
    return df


def infra_stats(g):
    n = len(g)
    ok = g[g["ok"] == 1]
    s = sorted(ok["sec"].tolist())
    uniq = ok["ip"].nunique()
    return dict(n=n, ok=len(ok), success=len(ok) / n if n else float("nan"),
                lo=wilson(len(ok), n)[0], hi=wilson(len(ok), n)[1],
                median=statistics.median(s) if s else float("nan"),
                p90=s[int(0.9 * (len(s) - 1))] if s else float("nan"),
                unique=uniq, dup=(1 - uniq / len(ok)) if len(ok) else float("nan"),
                geo=(ok["geo_ok"].sum() / len(ok)) if len(ok) else float("nan"),
                geo_k=int(ok["geo_ok"].sum()))


def load_targets(raw):
    df = read_all(raw, "targets")
    if df.empty:
        return df
    return num(df, ["ok", "sec", "bytes"])


def sticky_sessions(raw):
    df = read_all(raw, "sticky")
    if df.empty:
        return []
    df = num(df, ["ok", "t_sec"])
    df = df[df["t_sec"] <= STICKY_WINDOW_S]
    # Floppydata: only the format from the official documentation
    df = df[~((df["provider"] == "floppydata") & (df["ts"] < FLOPPY_NEW_FORMAT_FROM))]
    out = []
    for (run, srv, prov, ses), g in df.groupby(["run_id", "server", "provider", "session"]):
        g = g.sort_values("t_sec")
        oks = g[g["ok"] == 1]
        planned = len(g)
        if oks.empty:
            out.append(dict(run=run, server=srv, provider=prov, stable=False, hold=0.0, n=planned, ok=0, same=0))
            continue
        first = oks["ip"].iloc[0]
        changed = oks[oks["ip"] != first]
        hold = (changed["t_sec"].iloc[0] if len(changed) else oks["t_sec"].iloc[-1]) / 60.0
        out.append(dict(run=run, server=srv, provider=prov, stable=(len(changed) == 0 and len(oks) >= 0.9 * planned),
                        hold=hold, n=planned, ok=len(oks), same=int((oks["ip"] == first).sum())))
    return out


def sticky_stats(ss):
    n = len(ss)
    k = sum(s["stable"] for s in ss)
    okreq = sum(s["ok"] for s in ss)
    allreq = sum(s["n"] for s in ss)
    return dict(n=n, k=k, rate=k / n if n else float("nan"), lo=wilson(k, n)[0], hi=wilson(k, n)[1],
                hold=statistics.median(s["hold"] for s in ss) if ss else float("nan"),
                cons=(sum(s["same"] for s in ss) / okreq) if okreq else float("nan"),
                err=((allreq - okreq) / allreq) if allreq else float("nan"))


def load_ttfb(raw):
    df = read_all(raw, "ttfb")
    if df.empty:
        return df
    return num(df, ["ok", "t_connect", "t_tls", "ttfb", "t_total"])


def ttfb_stats(g):
    n = len(g)
    ok = g[g["ok"] == 1]
    tt = sorted(ok["ttfb"].tolist())
    wait = (ok["ttfb"] - ok["t_tls"]).tolist()
    tun = (ok["t_tls"] - ok["t_connect"]).tolist()
    return dict(n=n, success=len(ok) / n if n else float("nan"),
                tcp=ok["t_connect"].median(), tunnel=statistics.median(tun) if tun else float("nan"),
                wait=statistics.median(wait) if wait else float("nan"),
                ttfb=statistics.median(tt) if tt else float("nan"),
                p90=tt[int(0.9 * (len(tt) - 1))] if tt else float("nan"),
                total=ok["t_total"].median())


HOSTING = ("hosting", "cloud", "datacenter", "data center", "data-center", "server", "vps", "colo", "amazon", "aws",
           "google", "microsoft", "azure", "digitalocean", "ovh", "hetzner", "leaseweb", "m247", "cloudflare",
           "linode", "akamai", "vultr", "contabo", "choopa", "oracle", "alibaba", "tencent", "scaleway", "packet",
           "datacamp", "cdn77", "zenlayer", "ipxo", "psychz", "quadranet", "colocrossing", "serverius", "worldstream")


def ipq_stats(ipq):
    import csv
    sample = os.path.join(ipq, "ipsample.csv")
    cache_p = os.path.join(ipq, "ipcache.json")
    if not (os.path.exists(sample) and os.path.exists(cache_p)):
        return {}
    cache = json.load(open(cache_p))
    by = defaultdict(list)
    for r in csv.DictReader(open(sample, newline="")):
        res = cache.get(r["ip"])
        if not res:
            continue
        sc = (res.get("scam") or {}).get("scamalytics") or {}
        px = sc.get("scamalytics_proxy") or {}
        info = res.get("ipinfo") or {}
        asn = (info.get("as_name") or "").lower()
        dc = bool(px.get("is_datacenter"))
        d = dict(ok=sc.get("scamalytics_score") is not None, score=sc.get("scamalytics_score"),
                 risk=sc.get("scamalytics_risk"), dc=dc, vpn=bool(px.get("is_vpn")),
                 proxy=dc or bool(px.get("is_vpn")) or bool(px.get("is_resproxy")) or bool(
                     px.get("is_amazon_aws") or px.get("is_google") or px.get("is_apple_icloud_private_relay")),
                 res=not (dc or any(w in asn for w in HOSTING)),
                 ipinfo_ok=bool(info.get("country_code")),
                 geo=(info.get("country_code") or "").lower() == r["country_req"])
        by[r["provider"]].append(d)
    out = {}
    for p, ds in by.items():
        v = [d for d in ds if d["ok"]]
        n = len(v)
        if not n:
            continue
        sc = [d["score"] for d in v]
        gi = [d for d in ds if d["ipinfo_ok"]]
        out[p] = dict(n=n, avg=statistics.mean(sc), med=statistics.median(sc),
                      high=sum(d["risk"] in ("high", "very high") for d in v), proxy=sum(d["proxy"] for d in v),
                      dc=sum(d["dc"] for d in v), vpn=sum(d["vpn"] for d in v), res=sum(d["res"] for d in v),
                      geo=sum(d["geo"] for d in gi), geo_n=len(gi))
    return out


# ------------------------------------------------------------------ workbook
HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(bold=True, color="FFFFFF")
KEY_FILL = PatternFill("solid", fgColor="E8F0FA")


def sheet(wb, title, header, rows, widths=None, pct_cols=(), num_cols=None, freeze="B2", note=None):
    ws = wb.create_sheet(title[:31])
    r0 = 1
    if note:
        ws.cell(1, 1, note).font = Font(italic=True, color="555555")
        r0 = 3
    for j, h in enumerate(header, 1):
        c = ws.cell(r0, j, h)
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[r0].height = 45
    for i, row in enumerate(rows, r0 + 1):
        for j, v in enumerate(row, 1):
            if isinstance(v, float) and math.isnan(v):
                v = None
            c = ws.cell(i, j, v)
            if j in pct_cols and isinstance(v, (int, float)):
                c.number_format = "0.0%"
            elif num_cols and j in num_cols and isinstance(v, (int, float)):
                c.number_format = num_cols[j]
    for j in range(1, len(header) + 1):
        ws.column_dimensions[get_column_letter(j)].width = (widths or {}).get(j, 16)
    ws.column_dimensions["A"].width = (widths or {}).get(1, 20)
    ws.freeze_panes = ws.cell(r0 + 1, 2) if freeze else None
    return ws


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", default="results.xlsx")
    ap.add_argument("--ipq")
    a = ap.parse_args()

    infra = load_infra(a.raw)
    targets = load_targets(a.raw)
    sticky = sticky_sessions(a.raw)
    ttfb = load_ttfb(a.raw)
    ipq = ipq_stats(a.ipq) if a.ipq else {}
    provs = [p for p in ORDER if p in set(infra["provider"])] if len(infra) else ORDER

    # per-provider pooled values
    P = {p: infra_stats(infra[infra["provider"] == p]) for p in provs}
    PS = {(p, s): infra_stats(infra[(infra["provider"] == p) & (infra["server"] == s)]) for p in provs for s in SERVERS}
    ST = {p: sticky_stats([s for s in sticky if s["provider"] == p]) for p in provs}
    STS = {(p, s): sticky_stats([x for x in sticky if x["provider"] == p and x["server"] == s]) for p in provs for s in SERVERS}
    TT = {p: ttfb_stats(ttfb[ttfb["provider"] == p]) for p in provs} if len(ttfb) else {}
    TTS = {(p, s): ttfb_stats(ttfb[(ttfb["provider"] == p) & (ttfb["server"] == s)]) for p in provs for s in SERVERS} if len(ttfb) else {}

    def tstat(p, tname, srv=None):
        g = targets[(targets["provider"] == p) & (targets["target"] == tname)]
        if srv:
            g = g[g["server"] == srv]
        n = len(g)
        k = int((g["ok"] == 1).sum())
        cap = int((g["result"] == "captcha").sum())
        ch = int((g["result"] == "challenge").sum())
        blk = int(g["result"].str.startswith("blocked_").sum())
        oth = n - k - cap - ch - blk
        okg = g[g["ok"] == 1]
        return dict(n=n, k=k, cap=cap, ch=ch, blk=blk, oth=oth, med=okg["sec"].median() if len(okg) else float("nan"),
                    p90=okg["sec"].quantile(0.9) if len(okg) else float("nan"), kb=g["bytes"].mean() / 1024 if n else float("nan"))

    TG = {(p, t): tstat(p, t) for p in provs for t in ("wikipedia", "amazon")} if len(targets) else {}

    wb = Workbook()
    wb.remove(wb.active)

    # ---------------- Summary (the layout of the original template)
    gp = infra.groupby(["provider", "country_req"]) if len(infra) else None
    weakest = {}
    for p in provs:
        g = infra[infra["provider"] == p]
        sr = g.groupby("country_req")["ok"].mean()
        if len(sr):
            worst = sr.idxmin()
            weakest[p] = "%s %.1f%%" % (worst.upper(), 100 * sr.min())
    header = ["Provider", "Success Rate", "Response Time / Latency (median, P90, s)", "Error Rate (CAPTCHA / Block Rate)",
              "Unique IPs", "Duplicate Rate", "Geo Accuracy", "IP Risk / Fraud Score", "Proxy Detection Rate",
              "Residential / ISP Classification", "Sticky Session Stability", "Target Success Rate",
              "Cost per Successful Request", "Performance by GEO",
              "TTFB (median, s) US / DE / SG", "Success Rate US / DE / SG", "Latency median US / DE / SG (s)"]
    rows = []
    for i, p in enumerate(provs, 2):
        s = P[p]
        row = [LABEL[p], fmt_rate(s["ok"], s["n"]), "%.2f / %.2f" % (s["median"], s["p90"]),
               "Infra %.1f%%" % (100 * (1 - s["success"]))]
        if TG:
            a_, w_ = TG[(p, "amazon")], TG[(p, "wikipedia")]
            if a_["n"]:
                row[3] += "; Amazon captcha %.1f%%, challenge %.1f%%, blocked %.1f%%" % (
                    100 * a_["cap"] / a_["n"], 100 * a_["ch"] / a_["n"], 100 * a_["blk"] / a_["n"])
        row += ["%d of %d" % (s["unique"], s["ok"]), s["dup"], fmt_rate(s["geo_k"], s["ok"])]
        q = ipq.get(p)
        if q:
            row += ["avg %.1f, median %.1f, high risk %s" % (q["avg"], q["med"], fmt_rate(q["high"], q["n"])),
                    fmt_rate(q["proxy"], q["n"]), fmt_rate(q["res"], q["n"])]
        else:
            row += ["pending", "pending", "pending"]
        st = ST[p]
        row += [fmt_rate(st["k"], st["n"]) + " (n=%d)" % st["n"] if st["n"] else "-"]
        if TG:
            row += ["Wikipedia %s; Amazon %s" % (fmt_rate(TG[(p, "wikipedia")]["k"], TG[(p, "wikipedia")]["n"]),
                                                 fmt_rate(TG[(p, "amazon")]["k"], TG[(p, "amazon")]["n"]))]
        else:
            row += ["-"]
        row += ["='Cost per Successful Request'!H%d" % i, weakest.get(p, "-")]
        if TT:
            row += [" / ".join("%.2f" % TTS[(p, sv)]["ttfb"] for sv in SERVERS)]
        else:
            row += ["-"]
        row += [" / ".join("%.1f%%" % (100 * PS[(p, sv)]["success"]) for sv in SERVERS),
                " / ".join("%.2f" % PS[(p, sv)]["median"] for sv in SERVERS)]
        rows.append(row)
    ws = sheet(wb, "Summary", header, rows, widths={1: 14, 2: 22, 3: 18, 4: 44, 5: 16, 6: 12, 7: 22, 8: 36, 9: 22, 10: 22,
                                                    11: 24, 12: 56, 13: 18, 14: 18, 15: 22, 16: 28, 17: 26},
               pct_cols=(6,),
               note="Residential proxy benchmark, 5-8 Oct 2026. Pooled over three test servers (USA, Germany, Singapore). "
                    "Success rates with 95% Wilson confidence interval. Per-server values in the columns on the right and in the other tabs.")
    for r in ws.iter_rows(min_row=4, max_row=3 + len(rows)):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        r[0].font = Font(bold=True)

    # ---------------- Infra per server
    rows = []
    for p in provs:
        for sv in SERVERS + ["all"]:
            s = P[p] if sv == "all" else PS[(p, sv)]
            rows.append([LABEL[p], SERVER_NAMES.get(sv, "All servers"), s["n"], s["ok"], s["success"], s["lo"], s["hi"],
                         s["median"], s["p90"], s["unique"], s["dup"], s["geo"]])
    sheet(wb, "Infra by server",
          ["Provider", "Server", "Requests", "Successful", "Success Rate", "CI low", "CI high", "Latency median, s",
           "Latency P90, s", "Unique IPs", "Duplicate Rate", "Geo Accuracy"], rows,
          widths={1: 14, 2: 18}, pct_cols=(5, 6, 7, 11, 12), num_cols={8: "0.00", 9: "0.00"},
          note="Test 1 (Infra): https://www.cloudflare.com/cdn-cgi/trace, new IP on every request. Latency = full request time, successful requests only.")

    # ---------------- Errors
    if len(infra):
        e = infra[infra["ok"] != 1].groupby(["provider", "err"]).size().reset_index(name="count")
        rows = [[LABEL[r.provider], r.err, int(r["count"]), (int(r["count"]) / P[r.provider]["n"])]
                for _, r in e.sort_values(["provider", "count"], ascending=[True, False]).iterrows() if r.provider in LABEL]
        sheet(wb, "Infra errors", ["Provider", "Error type", "Count", "Share of all requests"], rows,
              widths={1: 14, 2: 26}, pct_cols=(4,))

    # ---------------- Targets
    if TG:
        rows = []
        for p in provs:
            for t in ("wikipedia", "amazon"):
                for sv in SERVERS + ["all"]:
                    s = TG[(p, t)] if sv == "all" else tstat(p, t, sv)
                    if not s["n"]:
                        continue
                    lo, hi = wilson(s["k"], s["n"])
                    rows.append([LABEL[p], t, SERVER_NAMES.get(sv, "All servers"), s["n"], s["k"], s["k"] / s["n"], lo, hi,
                                 s["cap"] / s["n"], s["ch"] / s["n"], s["blk"] / s["n"], s["oth"] / s["n"], s["med"], s["p90"], s["kb"]])
        sheet(wb, "Targets", ["Provider", "Target", "Server", "Requests", "Success", "Target Success Rate", "CI low",
                              "CI high", "Captcha", "JS challenge", "Blocked 403/429/503", "Other errors",
                              "Latency median, s", "Latency P90, s", "Avg response, KB"], rows,
              widths={1: 14, 2: 11, 3: 18}, pct_cols=(6, 7, 8, 9, 10, 11, 12), num_cols={13: "0.00", 14: "0.00", 15: "0.0"},
              note="Test 2: Wikipedia (control) and Amazon search. Plain HTTP client with a Chrome TLS fingerprint, no JavaScript execution.")

    # ---------------- Sticky
    rows = []
    for p in provs:
        for sv in SERVERS + ["all"]:
            s = ST[p] if sv == "all" else STS[(p, sv)]
            if s["n"]:
                rows.append([LABEL[p], SERVER_NAMES.get(sv, "All servers"), s["n"], s["k"], s["rate"], s["lo"], s["hi"],
                             s["hold"], s["cons"], s["err"]])
    sheet(wb, "Sticky", ["Provider", "Server", "Sessions", "Stable sessions", "Sticky Session Stability", "CI low", "CI high",
                         "Median IP hold, min", "Request IP consistency", "Error Rate"], rows,
          widths={1: 14, 2: 18}, pct_cols=(5, 6, 7, 9, 10), num_cols={8: "0.0"},
          note="Test 3: 8 sessions per run, 30-minute sessions, one request every 30 s; evaluated in the first 25 minutes. "
               "Floppydata: only sessions with the format from its official documentation (from 6 Oct 09:30 UTC).")

    # ---------------- TTFB
    if TT:
        rows = []
        for p in provs:
            for sv in SERVERS + ["all"]:
                s = TT[p] if sv == "all" else TTS[(p, sv)]
                if s["n"]:
                    rows.append([LABEL[p], SERVER_NAMES.get(sv, "All servers"), s["n"], s["success"], s["tcp"], s["tunnel"],
                                 s["wait"], s["ttfb"], s["p90"], s["total"]])
        sheet(wb, "TTFB", ["Provider", "Server", "Requests", "Success Rate", "TCP to proxy, s", "Tunnel + TLS, s",
                           "Wait for first byte, s", "TTFB median, s", "TTFB P90, s", "Total time median, s"], rows,
              widths={1: 14, 2: 18}, pct_cols=(4,), num_cols={5: "0.00", 6: "0.00", 7: "0.00", 8: "0.00", 9: "0.00", 10: "0.00"},
              note="Separate test on 7 Oct (curl timings): 10 requests per provider and country per server, same Cloudflare target.")

    # ---------------- GEO
    if len(infra):
        rows = []
        for p in provs:
            for cc in ["us", "de", "gb", "fr", "es", "br", "in", "pl", "ua", "tr", "vn", "id"]:
                for sv in SERVERS + ["all"]:
                    g = infra[(infra["provider"] == p) & (infra["country_req"] == cc)]
                    if sv != "all":
                        g = g[g["server"] == sv]
                    if len(g):
                        s = infra_stats(g)
                        rows.append([LABEL[p], cc.upper(), SERVER_NAMES.get(sv, "All servers"), s["n"], s["success"], s["lo"],
                                     s["hi"], s["median"], s["p90"], s["geo"]])
        sheet(wb, "Performance by GEO", ["Provider", "Country", "Server", "Requests", "Success Rate", "CI low", "CI high",
                                         "Latency median, s", "Latency P90, s", "Geo Accuracy"], rows,
              widths={1: 14, 2: 9, 3: 18}, pct_cols=(5, 6, 7, 10), num_cols={8: "0.00", 9: "0.00"}, freeze="D2")
        # pivot: Success Rate by country (pooled)
        ccs = ["us", "de", "gb", "fr", "es", "br", "in", "pl", "ua", "tr", "vn", "id"]
        rows = []
        for p in provs:
            row = [LABEL[p]]
            for cc in ccs:
                g = infra[(infra["provider"] == p) & (infra["country_req"] == cc)]
                row.append(float(g["ok"].mean()) if len(g) else None)
            rows.append(row)
        sheet(wb, "GEO Success pivot", ["Provider"] + [c.upper() for c in ccs], rows, pct_cols=tuple(range(2, 14)),
              widths={i: 9 for i in range(2, 14)}, note="Success Rate by requested country, pooled over three servers.")

    # ---------------- Cost (formulas, parameters at the top)
    ws = wb.create_sheet("Cost per Successful Request")
    ws["A1"] = "Cost per 1000 successful requests = price per GB x (requests x wire bytes per request / 1e9) / successful requests x 1000"
    ws["A1"].font = Font(italic=True, color="555555")
    ws["A2"] = "Assumptions (edit after calibration with the provider panel counters):"
    ws["A2"].font = Font(bold=True)
    ws["B2"], ws["C2"], ws["D2"] = INFRA_OVERHEAD_BYTES, TARGET_OVERHEAD_BYTES, TARGET_WIRE_FACTOR
    ws["B3"], ws["C3"], ws["D3"] = "Infra overhead, bytes", "Target overhead, bytes", "Target wire/logged size"
    ws["A3"] = ""
    hdr = ["Provider", "Price, $/GB", "Package / tariff", "Infra: requests", "Infra: successful",
           "Infra: avg logged bytes", "Infra: cost per 1000 successful, $", "Text for the Summary tab (estimate)",
           "Wikipedia: requests", "Wikipedia: successful", "Wikipedia: avg logged bytes", "Wikipedia: cost per 1000 successful, $",
           "Amazon: requests", "Amazon: successful", "Amazon: avg logged bytes", "Amazon: cost per 1000 successful, $"]
    for j, h in enumerate(hdr, 1):
        c = ws.cell(5, j, h)
        c.fill, c.font = HEAD_FILL, HEAD_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[5].height = 48
    # Summary links to column H of rows 2..13: keep the same row numbers by writing rows from 6, and
    # make column H in rows 2..13 not needed: instead link Summary to this table through the offset below.
    for i, p in enumerate(provs):
        r = 6 + i
        s = P[p]
        inf_bytes = float(infra[infra["provider"] == p]["bytes"].mean()) if len(infra) else 0
        ws.cell(r, 1, LABEL[p])
        ws.cell(r, 2, PRICE[p][0])
        ws.cell(r, 3, PRICE[p][1])
        ws.cell(r, 4, s["n"])
        ws.cell(r, 5, s["ok"])
        ws.cell(r, 6, round(inf_bytes, 1))
        ws.cell(r, 7, "=IF(E%d=0,\"\",B%d*(D%d*(F%d+$B$2)/1E9)/E%d*1000)" % (r, r, r, r, r))
        ws.cell(r, 8, "=IF(E%d=0,\"\",\"est. \"&TEXT(G%d,\"0.0000\")&\" $ per 1000 successful (Infra)\"&IF(ISNUMBER(L%d),\"; Wikipedia \"&TEXT(L%d,\"0.000\")&\", Amazon \"&IF(ISNUMBER(P%d),TEXT(P%d,\"0.000\"),\"-\"),\"\"))" % (r, r, r, r, r, r))
        for k, (tname, c0) in enumerate((("wikipedia", 9), ("amazon", 13))):
            t = TG.get((p, tname)) if TG else None
            if t and t["n"]:
                g = targets[(targets["provider"] == p) & (targets["target"] == tname)]
                ws.cell(r, c0, t["n"]); ws.cell(r, c0 + 1, t["k"]); ws.cell(r, c0 + 2, round(float(g["bytes"].mean()), 0))
                L = lambda c: get_column_letter(c)
                ws.cell(r, c0 + 3, "=IF(%s%d=0,\"\",B%d*(%s%d*(%s%d*$D$2+$C$2)/1E9)/%s%d*1000)" % (
                    L(c0 + 1), r, r, L(c0), r, L(c0 + 2), r, L(c0 + 1), r))
        for cc in (7, 12, 16):
            ws.cell(r, cc).number_format = "0.0000"
    for j in range(1, 17):
        ws.column_dimensions[get_column_letter(j)].width = 16
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["C"].width = 34
    ws.column_dimensions["H"].width = 46
    ws.freeze_panes = "B6"
    # fix the Summary links (rows 4.. because the Summary note occupies rows 1-2)
    sm = wb["Summary"]
    for i, p in enumerate(provs):
        sm.cell(4 + i, 13).value = "='Cost per Successful Request'!H%d" % (6 + i)

    # ---------------- IP quality
    if ipq:
        rows = []
        for p in provs:
            q = ipq.get(p)
            if q:
                rows.append([LABEL[p], q["n"], q["avg"], q["med"], q["high"] / q["n"], q["proxy"] / q["n"], q["dc"] / q["n"],
                             q["vpn"] / q["n"], q["res"] / q["n"], q["geo"] / q["geo_n"] if q["geo_n"] else None])
        sheet(wb, "IP quality", ["Provider", "IPs checked", "Fraud Score avg (0-100)", "Fraud Score median", "High risk share",
                                 "Proxy Detection Rate", "Datacenter", "VPN", "Residential / ISP (ASN heuristic)",
                                 "Geo accuracy (IPinfo)"], rows, pct_cols=(5, 6, 7, 8, 9, 10),
              num_cols={3: "0.0", 4: "0.0"}, widths={1: 14},
              note="Scamalytics (score, datacenter/VPN/residential-proxy flags) and IPinfo Lite (ASN, country). Random sample of unique IPs per provider.")

    # ---------------- Runs (stability over time)
    if len(infra):
        g = infra.groupby(["provider", "server", "run_id"])
        rows = []
        for (p, sv, run), x in g:
            if p in LABEL:
                s = infra_stats(x)
                rows.append([LABEL[p], SERVER_NAMES.get(sv, sv), run, x["ts"].min(), s["n"], s["success"], s["median"]])
        sheet(wb, "Infra runs", ["Provider", "Server", "Run", "Started (UTC)", "Requests", "Success Rate", "Latency median, s"],
              rows, widths={1: 14, 2: 18, 3: 16, 4: 20}, pct_cols=(6,), num_cols={7: "0.00"},
              note="One row per provider, server and scheduled run: shows whether results are stable during the test.")

    # ---------------- Trend by run (pooled over the three servers)
    if len(infra):
        runs = sorted(infra["run_id"].unique())
        rows = []
        for p in provs:
            if p not in LABEL:
                continue
            row = [LABEL[p]]
            for run in runs:
                x = infra[(infra["provider"] == p) & (infra["run_id"] == run)]
                row.append(len(x[x["ok"] == 1]) / len(x) if len(x) else None)
            xl = infra[(infra["provider"] == p) & (infra["run_id"].isin(runs[-2:]))]
            row.append(len(xl[xl["ok"] == 1]) / len(xl) if len(xl) else None)
            rows.append(row)
        sheet(wb, "Trend Success by run", ["Provider"] + runs + ["Last 2 runs"], rows,
              widths={j: 15 for j in range(2, len(runs) + 3)}, pct_cols=tuple(range(2, len(runs) + 3)),
              note="Infra Success Rate per scheduled run (08:00 / 20:00 UTC), pooled over three servers (3,600 requests per provider per run: 100 x 12 countries x 3 servers). Last column: the two latest runs together.")
    if sticky:
        sruns = sorted(set(s["run"] for s in sticky))
        rows = []
        for p in provs:
            if p not in LABEL:
                continue
            row = [LABEL[p]]
            for run in sruns:
                x = [s for s in sticky if s["provider"] == p and s["run"] == run]
                row.append(sum(s["stable"] for s in x) / len(x) if x else None)
            xl = [s for s in sticky if s["provider"] == p and s["run"] in sruns[-2:]]
            row.append(sum(s["stable"] for s in xl) / len(xl) if xl else None)
            rows.append(row)
        sheet(wb, "Trend Sticky by run", ["Provider"] + sruns + ["Last 2 runs"], rows,
              widths={j: 15 for j in range(2, len(sruns) + 3)}, pct_cols=tuple(range(2, len(sruns) + 3)),
              note="Sticky Session Stability per run (24 sessions per provider per run: 8 x 3 servers). Floppydata runs before the official-format switch are empty. Small samples: read as a trend, not as a ranking.")

    # ---------------- Method
    ws = wb.create_sheet("Method and limits")
    text = [
        "Method and limitations (read before citing the numbers)",
        "",
        "Period: 5-8 October 2026 (UTC). Three Hetzner Cloud servers: USA (Ashburn), Germany, Singapore; direct internet, no VPN.",
        "12 providers, 12 countries (US, DE, GB, FR, ES, BR, IN, PL, UA, TR, VN, ID). Bright Data and Byteful were not tested (identity verification).",
        "We test our own provider Floppydata together with competitors: all scripts and raw data are published for verification.",
        "Infra test: https://www.cloudflare.com/cdn-cgi/trace, new IP on every request, timeout 30 s, 5 parallel requests, failures count as results.",
        "Geo Accuracy uses a single database (Cloudflare); the IPinfo cross-check is in the IP quality tab.",
        "Targets: Wikipedia (control) and Amazon search (US, DE, GB). Plain HTTP client with a Chrome TLS fingerprint, no JavaScript, so absolute Amazon numbers are lower than in a real browser.",
        "Sticky: sessions of 30 minutes, one check per 30 s, evaluated in the first 25 minutes (a session ends by design at 30 min). Where the duration is not set in the login (set in the panel or default), the provider default was tested.",
        "Floppydata sticky: only the format from its official documentation (session id + rotation-0) counts; earlier runs are excluded.",
        "Latency is the full request time (connect, tunnel, TLS, response); TTFB is measured separately (TTFB tab). It depends strongly on the distance between the test server and the exit country.",
        "Prices are as paid for the purchased packages (some with tax, some without), so they are not strictly comparable by volume; ProxyEmpire 8.47 includes tax ($7.00 without).",
        "Cost per Successful Request is an estimate: wire bytes per request are modelled (see assumptions at the top of that tab) and should be calibrated with provider panel counters.",
        "Rayobyte uses a gateway in Los Angeles; NodeMaven was tested with default settings; Evomi on the Core tariff; SOAX on the Sandbox tariff.",
        "The test is a three-day snapshot, not continuous monitoring. Differences smaller than the width of the confidence intervals are not significant.",
    ]
    for i, t in enumerate(text, 1):
        ws.cell(i, 1, t)
    ws["A1"].font = Font(bold=True, size=13)
    ws.column_dimensions["A"].width = 160
    for r in ws.iter_rows():
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    wb.save(a.out)
    print("Saved", a.out, "| infra rows", len(infra), "| targets rows", len(targets), "| sticky sessions", len(sticky),
          "| ttfb rows", len(ttfb), "| ip quality providers", len(ipq))


if __name__ == "__main__":
    main()
