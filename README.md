# Residential Proxy Benchmark 2026: raw data and scripts

Raw measurements, test scripts and the results workbook behind the
[Residential Proxy Benchmark 2026: 12 Providers Tested](https://floppydata.com/residential-proxy-benchmark/)
published by Floppydata.

**Disclosure:** Floppydata is one of the 12 tested providers and also the publisher of this benchmark.
Every provider was tested with the same scripts, servers, countries and schedule. That is why the
raw data and the code are public: you can check the numbers yourself.

## What was tested

- **Period:** 5-8 October 2026 (UTC).
- **Providers (12):** Decodo, Oxylabs, Evomi, SOAX, DataImpulse, Rayobyte, ProxyEmpire, Webshare, NodeMaven, IPRoyal, Floppydata, Geonode.
- **Countries (12):** US, DE, GB, FR, ES, BR, IN, PL, UA, TR, VN, ID.
- **Test servers (3):** Germany, US, Singapore (Hetzner cloud). In the files they are `de`, `us`, `sg`.
- **Tests:**
  1. Infrastructure: Success Rate, Latency, Duplicate Rate, Geo Accuracy (Cloudflare trace, 100 requests per provider and country, 08:00 and 20:00 UTC).
  2. Targets: Amazon (US, DE, GB) and Wikipedia, plain HTTP client with a Chrome TLS fingerprint, no JavaScript (11:00 and 23:00 UTC).
  3. Sticky sessions: 8 sessions per run, 30 minutes, one check every 30 seconds.
  4. TTFB: curl timings, 7 October.
  5. IP quality: Scamalytics and IPinfo on a sample of 300 unique IPs per provider.
  6. Cost per Successful Request: provider traffic counters read on 8 October 2026 at 14:50 UTC.

The full method, limitations and conclusions are in the article. The sheet "Method and limits" of the workbook repeats them.

## Repository content

| Path | What it is |
|---|---|
| `data/raw/{us,de,sg}/infra_*.csv` | Infrastructure test, one row per request |
| `data/raw/{us,de,sg}/targets_*.csv` | Amazon and Wikipedia test, one row per request |
| `data/raw/{us,de,sg}/sticky_*.csv` | Sticky session checks, one row per check |
| `data/raw/{us,de,sg}/ttfb_*.csv` | TTFB test, one row per request |
| `data/ip_quality_aggregated.csv` | IP quality results per provider (aggregated, no IP addresses) |
| `results/results.xlsx` | Full results: all tables, confidence intervals, per-run trends, method notes |
| `scripts/` | The test scripts and the analysis script |

## Anonymization

Exit IP addresses belong to the providers' residential pools, so they are not published.
In every `ip` column the real address is replaced by `ip_` plus 16 hex characters: a keyed hash (HMAC-SHA256)
with a random key that was used once and then discarded. The same address always gets the same value in all files,
so unique-IP counts, Duplicate Rate and sticky-session IP changes are unchanged. The hashes cannot be turned back
into addresses. The original logs, server addresses, credentials and API keys are not published.
No other columns were changed.

Because of this, the IP quality step (`ipquality.py`) cannot be re-run on this data. Its results are published
as aggregates in `data/ip_quality_aggregated.csv` and in the sheet "IP quality" of the workbook. To repeat that step
you need your own measurements and your own Scamalytics and IPinfo keys.

## Reproduce the numbers

You need Python 3.10+ with `pandas` and `openpyxl`.

```bash
pip install pandas openpyxl
mkdir -p work
for s in us de sg; do mkdir -p work/$s/data; cp data/raw/$s/*.csv work/$s/data/; done
python3 scripts/analyze.py --raw work --out my_results.xlsx
```

`my_results.xlsx` has the same sheets as `results/results.xlsx` (Summary, Infra by server, Targets, Sticky, TTFB,
Performance by GEO and others). The workbook is written with formulas: open it in Excel or LibreOffice and recalculate.
We ran this on the published CSV files and all tables except the IP quality columns match the published workbook.

## Run your own test

The scripts use the Python standard library, with two exceptions: `targets.py` needs `curl_cffi`
for the Chrome TLS fingerprint used in the benchmark (`--engine chrome`, the default when `curl_cffi`
is installed; `--engine urllib` is a plain-Python fallback that was not used for the published data),
and `analyze.py` needs pandas and openpyxl.

```bash
pip install curl_cffi            # for targets.py
python3 scripts/bench.py setup    # enter provider credentials once (stored in providers.json, never commit it)
python3 scripts/bench.py check    # one request per provider and country
python3 scripts/bench.py run      # infrastructure test, writes data/*.csv
python3 scripts/targets.py --help
python3 scripts/sticky.py --help
python3 scripts/ttfb.py --help
```

Credentials are entered through `setup` and are never part of the scripts. `providers.json` is in `.gitignore`.

## Column reference

- **infra:** `run_id, ts_utc, server, provider, test, target, country_req, ok, sec, ip, geo_cf, colo, bytes, err`
  (`ok` 1/0, `sec` request time, `geo_cf` country reported by Cloudflare, `colo` Cloudflare data centre).
- **targets:** `run_id, ts_utc, server, provider, target, country_req, url, ok, status, sec, bytes, result, engine`
  (`result` is success, captcha, challenge (JavaScript challenge), blocked_NNN or an error type;
  `engine` is the HTTP client: `chrome` = curl_cffi with a Chrome TLS fingerprint, used for every published row).
- **sticky:** `run_id, ts_utc, server, provider, session, country, t_sec, ok, ip, err`.
- **ttfb:** `run_id, ts_utc, server, provider, country_req, ok, http, ip, t_connect, t_tls, ttfb, t_total, err`.

## Licence

Code (`scripts/`): MIT, see `LICENSE`.
Data and workbook (`data/`, `results/`): CC BY 4.0. Please credit "Floppydata Residential Proxy Benchmark 2026" and link to the article.

## Contact

Corrections and questions: open an issue in this repository.
