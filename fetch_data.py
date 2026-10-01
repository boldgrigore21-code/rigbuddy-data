"""Rig Buddy market data job.

Runs on a schedule (GitHub Actions), pulls oil/gas prices from the U.S. EIA
and the weekly rig count from Baker Hughes, and writes one small file:
docs/market.json. Every phone running Rig Buddy reads that one file.

If a source fails, the last good numbers for that source are kept and the
problem is listed under "errors", so the app never goes blank.
"""

import datetime as dt
import io
import json
import os
import re
import sys

import openpyxl
import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "market.json")
EIA_KEY = os.environ.get("EIA_API_KEY", "").strip()
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 RigBuddyData/1.0"
}
BH_HOME = "https://rigcount.bakerhughes.com/"
BH_NA = "https://rigcount.bakerhughes.com/na-rig-count"

# EIA API v2 routes and series ids (daily spot prices)
EIA_SERIES = {
    "wti": ("petroleum/pri/spt", "RWTC", "$/bbl"),
    "brent": ("petroleum/pri/spt", "RBRTE", "$/bbl"),
    "gas": ("natural-gas/pri/fut", "RNGWHHD", "$/MMBtu"),
}


DEBUG = []


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def bh_get(url, timeout=40):
    """Fetch a Baker Hughes page and note what came back, for troubleshooting."""
    r = requests.get(url, headers=UA, timeout=timeout)
    ctype = r.headers.get("content-type", "")
    note = {"url": url, "status": r.status_code, "type": ctype, "bytes": len(r.content)}
    if "html" in ctype or "text" in ctype:
        txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S | re.I)
        txt = re.sub(r"<[^>]+>", " ", txt)
        note["text"] = re.sub(r"\s+", " ", txt)[:1500]
        note["links"] = re.findall(r'href="([^"]*static-files[^"]*)"', r.text)[:12]
    DEBUG.append(note)
    if r.status_code != 200:
        raise ValueError(f"{url} answered {r.status_code}")
    return r


# ---------------------------------------------------------------- EIA prices
def eia_latest(route, series):
    url = f"https://api.eia.gov/v2/{route}/data/"
    params = {
        "api_key": EIA_KEY,
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": series,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 10,
    }
    r = requests.get(url, params=params, headers=UA, timeout=40)
    r.raise_for_status()
    rows = r.json().get("response", {}).get("data", [])
    pts = []
    for row in rows:
        try:
            v = float(row["value"])
        except (TypeError, ValueError, KeyError):
            continue
        pts.append((row["period"], v))
    pts.sort(reverse=True)
    if not pts:
        raise ValueError(f"EIA returned no data for {series}")
    latest = pts[0]
    prev = pts[1] if len(pts) > 1 else None
    return {
        "v": round(latest[1], 3),
        "p": round(prev[1], 3) if prev else None,
        "date": latest[0],
    }


# ------------------------------------------------------- Baker Hughes rigs
def bh_us_total():
    """US total, change and date from the summary table on the home page."""
    html = bh_get(BH_HOME).text
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|\s+", " ", text)
    # e.g. "U.S. 25 Sept 2026 599 +4"
    m = re.search(
        r"U\.\s?S\.?\s+(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4})\s+([\d,]{2,5})\s+([+\-−]?\s?\d+)",
        text,
    )
    if not m:
        raise ValueError("couldn't find the U.S. row on the Baker Hughes home page")
    date = parse_bh_date(m.group(1))
    v = int(m.group(2).replace(",", ""))
    ch = int(m.group(3).replace("−", "-").replace(" ", "").replace("+", ""))
    return {"v": v, "ch": ch}, date


def parse_bh_date(s):
    s = s.replace(".", "").strip()
    parts = s.split()
    if len(parts) == 3:
        mon = parts[1][:3].title()
        try:
            return dt.datetime.strptime(f"{parts[0]} {mon} {parts[2]}", "%d %b %Y").date().isoformat()
        except ValueError:
            pass
    return None


def bh_report_url():
    """Newest weekly report file linked from the North America page."""
    html = bh_get(BH_NA).text
    best = None
    for m in re.finditer(r'<a[^>]+href="([^"]*static-files/[0-9a-f\-]{36})"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, label = m.group(1), re.sub(r"<[^>]+>|\s+", " ", m.group(2))
        d = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", label)
        if not d:
            # The date can sit just outside the link text
            around = re.sub(r"<[^>]+>|\s+", " ", html[max(0, m.start() - 300): m.end() + 300])
            d = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", around)
        if not d or "report" not in (label + html[m.end(): m.end() + 200]).lower():
            continue
        y = int(d.group(3))
        y = y + 2000 if y < 100 else y
        try:
            when = dt.date(y, int(d.group(1)), int(d.group(2)))
        except ValueError:
            continue
        if best is None or when > best[0]:
            best = (when, href if href.startswith("http") else "https://rigcount.bakerhughes.com" + href)
    if not best:
        raise ValueError("couldn't find the weekly report link on the North America page")
    return best[1], best[0].isoformat()


def find_row(wb, label, sheet_words):
    """Find this week / change for one row (e.g. 'Permian') in the weekly report.

    Baker Hughes lays the summary sheets out as
        Name | This week | +/- | Last week | +/- | Year ago
    but columns have moved over the years, so we read the header row when we
    can, and otherwise check the numbers against each other before trusting them.
    """
    want = label.lower()
    sheets = sorted(
        wb.worksheets,
        key=lambda ws: 0 if any(w in ws.title.lower() for w in sheet_words) else 1,
    )
    for ws in sheets:
        header = None
        for row in ws.iter_rows(values_only=True, max_row=400):
            cells = list(row)
            texts = [str(c).strip().lower() if isinstance(c, str) else "" for c in cells]
            if any("+/-" in t or "change" in t for t in texts) or any(t in ("this week", "current") for t in texts):
                header = texts
            idx = next((i for i, t in enumerate(texts) if t == want or t.rstrip(" *") == want), None)
            if idx is None:
                continue
            nums = []
            for j in range(idx + 1, len(cells)):
                c = cells[j]
                if isinstance(c, (int, float)) and not isinstance(c, bool):
                    nums.append((j, float(c)))
            if not nums:
                continue
            # 1) Header tells us where "+/-" is
            if header:
                chg_cols = [j for j, t in enumerate(header) if j > idx and ("+/-" in t or "change" in t)]
                if chg_cols:
                    vals = dict(nums)
                    first_num_col = nums[0][0]
                    if chg_cols[0] in vals and first_num_col < chg_cols[0]:
                        return {"v": int(round(nums[0][1])), "ch": int(round(vals[chg_cols[0]]))}, ws.title
            # 2) This week, +/-, last week   (this - last == change)
            if len(nums) >= 3 and abs(nums[0][1] - nums[2][1] - nums[1][1]) < 0.01:
                return {"v": int(round(nums[0][1])), "ch": int(round(nums[1][1]))}, ws.title
            # 3) This week, last week
            if len(nums) >= 2 and nums[0][1] >= 0 and nums[1][1] >= 0 and abs(nums[0][1] - nums[1][1]) <= max(15, 0.2 * nums[0][1]):
                return {"v": int(round(nums[0][1])), "ch": int(round(nums[0][1] - nums[1][1]))}, ws.title
    return None, None


def bh_regions():
    url, report_date = bh_report_url()
    log("Baker Hughes report:", report_date, url)
    r = bh_get(url, timeout=120)
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    out, found_in = {}, {}
    for key, label, words in (("permian", "Permian", ("basin",)), ("nm", "New Mexico", ("state",))):
        val, sheet = find_row(wb, label, words)
        if val:
            out[key] = val
            found_in[key] = sheet
    log("Found:", found_in)
    return out, report_date


# ---------------------------------------------------------------- main
def main():
    try:
        with open(OUT) as f:
            old = json.load(f)
    except Exception:
        old = {}

    data = {
        "v": 1,
        "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "prices": dict(old.get("prices") or {}),
        "rigs": dict(old.get("rigs") or {}),
        "sources": {
            "prices": "U.S. Energy Information Administration (EIA), daily spot prices",
            "rigs": "Baker Hughes Rig Count",
        },
        "errors": [],
    }

    # Prices
    if not EIA_KEY:
        data["errors"].append("prices: EIA_API_KEY secret is not set")
    else:
        for key, (route, series, unit) in EIA_SERIES.items():
            try:
                val = eia_latest(route, series)
                val["unit"] = unit
                data["prices"][key] = val
                log(key, val)
            except Exception as e:  # keep yesterday's number
                data["errors"].append(f"prices.{key}: {e}")

    # Rig count: US total
    try:
        us, week = bh_us_total()
        if week and data["rigs"].get("week") and week < data["rigs"]["week"]:
            raise ValueError(f"home page shows an older week ({week}) than we already have")
        if week and week != data["rigs"].get("week"):
            # New week: clear regional numbers until the new report is read
            data["rigs"].pop("permian", None)
            data["rigs"].pop("nm", None)
        data["rigs"]["us"] = us
        if week:
            data["rigs"]["week"] = week
        log("US rigs", us, week)
    except Exception as e:
        data["errors"].append(f"rigs.us: {e}")

    # Rig count: Permian and New Mexico from the weekly report
    try:
        regions, report_date = bh_regions()
        week = data["rigs"].get("week")
        # Only use the report if it matches the week of the US total (within 3 days)
        if week and report_date:
            gap = abs((dt.date.fromisoformat(week) - dt.date.fromisoformat(report_date)).days)
            if gap > 3:
                raise ValueError(f"report is for {report_date}, US total is for {week}")
        for k in ("permian", "nm"):
            if k in regions:
                data["rigs"][k] = regions[k]
            else:
                data["errors"].append(f"rigs.{k}: row not found in the weekly report")
    except Exception as e:
        data["errors"].append(f"rigs.regions: {e}")

    if DEBUG and any(e.startswith("rigs") for e in data["errors"]):
        data["debug"] = DEBUG
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    log("Wrote", OUT)
    for e in data["errors"]:
        log("WARN", e)
    # Fail the run (GitHub emails you) only if nothing at all came through
    if not data["prices"] and not data["rigs"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
