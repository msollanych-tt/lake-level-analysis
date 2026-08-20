"""Fetch, analyse, and render Coquitlam Lake Forebay level data."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
from plotly.offline import get_plotlyjs

STATION = "08MH149"
DATA = Path("/app/data")
OUTPUT = Path("/app/output")
HERE = Path(__file__).parent
HISTORICAL_TTL = 24 * 60 * 60
REALTIME_TTL = 6 * 60 * 60
GEOMET = "https://api.weather.gc.ca/collections"
WATER_OFFICE = "https://wateroffice.ec.gc.ca/services/real_time_data/csv/inline"
BC_HYDRO = "https://www.bchydro.com/info/res_hydromet/data/coq.txt"


def fresh(path: Path, ttl: int) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < ttl


def get(url: str) -> str:
    request = Request(url, headers={"User-Agent": "lake-level-analysis/1.0"})
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8")


def api_url(collection: str, **query: str | int) -> str:
    return f"{GEOMET}/{collection}/items?{urlencode(query)}"


def fetch_historical(refresh: bool) -> pd.DataFrame:
    cache = DATA / "historical.json"
    if fresh(cache, HISTORICAL_TTL) and not refresh:
        return pd.DataFrame(json.loads(cache.read_text()))

    records: list[dict] = []
    # HYDAT has finalized daily means through the prior calendar year.
    for year in range(1985, datetime.now(UTC).year):
        payload = json.loads(
            get(
                api_url(
                    "hydrometric-daily-mean",
                    STATION_NUMBER=STATION,
                    datetime=f"{year}-01-01/{year}-12-31",
                    limit=1000,
                    f="json",
                )
            )
        )
        records.extend(feature["properties"] for feature in payload["features"])
    frame = pd.DataFrame(records)[["DATE", "LEVEL"]].rename(columns={"DATE": "date", "LEVEL": "level"})
    frame = frame.dropna().drop_duplicates("date").sort_values("date")
    DATA.mkdir(parents=True, exist_ok=True)
    cache.write_text(frame.to_json(orient="records", date_format="iso"))
    return frame


def fetch_realtime(refresh: bool) -> pd.DataFrame:
    cache = DATA / "realtime.json"
    if fresh(cache, REALTIME_TTL) and not refresh:
        return pd.DataFrame(json.loads(cache.read_text()))

    # Water Office sometimes provides more than the GeoMet 30-day feed. Preserve
    # its response for diagnostics, but use GeoMet's documented JSON schema here.
    water_office = DATA / "water-office-current.csv"
    water_office_frame = pd.DataFrame()
    try:
        params = urlencode(
            [
                ("stations[]", STATION),
                ("parameters[]", "46"),
                ("start_date", f"{datetime.now().year}-05-01 00:00:00"),
                ("end_date", datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")),
            ]
        )
        water_office.write_text(get(f"{WATER_OFFICE}?{params}"))
        rows = []
        for row in csv.DictReader(io.StringIO(water_office.read_text())):
            if row.get("Parameter/Paramètre") == "46" and row.get("Value/Valeur"):
                rows.append({"datetime": row["Date"], "level": float(row["Value/Valeur"])})
        water_office_frame = pd.DataFrame(rows)
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"Water Office unit values unavailable: {error}", file=sys.stderr)

    if not water_office_frame.empty:
        frame = water_office_frame.drop_duplicates("datetime").sort_values("datetime")
        DATA.mkdir(parents=True, exist_ok=True)
        cache.write_text(frame.to_json(orient="records"))
        return frame

    payload = json.loads(
        get(
            api_url(
                "hydrometric-realtime",
                STATION_NUMBER=STATION,
                sortby="-DATETIME",
                limit=10000,
                f="json",
            )
        )
    )
    records = [
        {"datetime": f["properties"]["DATETIME_LST"], "level": f["properties"]["LEVEL"]}
        for f in payload["features"]
        if f["properties"]["LEVEL"] is not None
    ]
    frame = pd.DataFrame(records).drop_duplicates("datetime").sort_values("datetime")
    DATA.mkdir(parents=True, exist_ok=True)
    cache.write_text(frame.to_json(orient="records"))
    return frame


def fetch_bc_hydro(refresh: bool) -> pd.DataFrame:
    cache = DATA / "bc-hydro.txt"
    if not fresh(cache, REALTIME_TTL) or refresh:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(get(BC_HYDRO))

    rows = []
    for line in cache.read_text().splitlines():
        match = re.match(r"(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\s+([0-9.]+)", line)
        if match:
            rows.append({"datetime": f"{match.group(1)} {match.group(2)}", "level_geodetic": float(match.group(3))})
    return pd.DataFrame(rows)


def fetch(refresh: bool) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return fetch_historical(refresh), fetch_realtime(refresh), fetch_bc_hydro(refresh)


def serialise(frame: pd.DataFrame, columns: list[str]) -> dict[str, list]:
    return {column: [None if pd.isna(v) else round(float(v), 4) for v in frame[column]] for column in columns}


def summer_frame(daily: pd.DataFrame, year: int, end_day: int | None = None) -> pd.DataFrame:
    result = daily[(daily.index.year == year) & (daily.index.month >= 6) & (daily.index.month <= 9)].copy()
    result["summer_day"] = (result.index - pd.Timestamp(year=year, month=6, day=1)).days + 1
    if end_day:
        result = result[result["summer_day"] <= end_day]
    return result


def slope_cm_per_day(frame: pd.DataFrame, days: int) -> float | None:
    subset = frame.tail(days).dropna(subset=["level"])
    if len(subset) < max(4, days // 2):
        return None
    x = (subset.index - subset.index[0]).total_seconds() / 86400
    return float(pd.Series(subset["level"].to_numpy()).cov(pd.Series(x)) / pd.Series(x).var() * 100)


def compare_text(value: float | None, baseline: float | None, unit: str) -> str:
    if value is None or baseline is None:
        return "not enough data for a comparison"
    delta = value - baseline
    direction = "higher" if delta > 0 else "lower"
    return f"{abs(delta):.1f} {unit} {direction}"


def analyse(historical: pd.DataFrame, realtime: pd.DataFrame, bc_hydro: pd.DataFrame) -> dict:
    historical["date"] = pd.to_datetime(historical["date"], utc=True).dt.tz_localize(None)
    historical = historical.set_index("date").sort_index()
    realtime["datetime"] = pd.to_datetime(realtime["datetime"], utc=True)
    realtime = realtime.set_index("datetime").sort_index()
    current_daily = realtime["level"].resample("1D").mean().to_frame()
    current_daily.index = current_daily.index.tz_localize(None)
    daily = pd.concat([historical, current_daily]).sort_index()
    daily = daily[~daily.index.duplicated(keep="last")]

    current_year = int(realtime.index.max().year)
    as_of = realtime.iloc[-1]
    as_of_date = realtime.index.max().tz_convert(None).normalize()
    summer_day = (as_of_date - pd.Timestamp(year=current_year, month=6, day=1)).days + 1
    current = summer_frame(daily, current_year, summer_day)
    current_last = current.iloc[-1]

    # Pair approximate feeds to convert the latest WSC datum into the WUP datum.
    offset = None
    if not bc_hydro.empty:
        bc_hydro["datetime"] = pd.to_datetime(bc_hydro["datetime"])
        bc_last = bc_hydro.iloc[0]["level_geodetic"]
        offset = float(bc_last - as_of["level"])
    geodetic = float(as_of["level"] + offset) if offset is not None else None

    primary_years = range(2010, current_year)
    same_days = []
    for year in primary_years:
        sample = daily[(daily.index.year == year) & (daily.index.dayofyear == as_of_date.dayofyear)]
        if not sample.empty:
            same_days.append({"year": year, "level": float(sample.iloc[-1]["level"])})
    levels = pd.DataFrame(same_days)
    percentile = float((levels["level"] <= as_of["level"]).mean() * 100) if not levels.empty else None
    median_level = float(levels["level"].median()) if not levels.empty else None
    last_year_level = next((x["level"] for x in same_days if x["year"] == current_year - 1), None)

    recent_7 = slope_cm_per_day(current, 7)
    recent_30 = slope_cm_per_day(current, 30)
    year_ago = summer_frame(daily, current_year - 1, summer_day)
    last_year_rate = slope_cm_per_day(year_ago, 30)
    historical_rates = [slope_cm_per_day(summer_frame(daily, year, summer_day), 30) for year in primary_years]
    historical_rates = [x for x in historical_rates if x is not None]
    median_rate = float(pd.Series(historical_rates).median()) if historical_rates else None

    def build_envelope(years: range, label: str) -> dict:
        summers = [summer_frame(daily, year) for year in years]
        combined = pd.concat([x[["summer_day", "level"]] for x in summers if not x.empty])
        result = combined.groupby("summer_day")["level"].quantile([0.1, 0.5, 0.9]).unstack()
        result.columns = ["p10", "median", "p90"]
        return {
            "label": label,
            "days": result.index.astype(int).tolist(),
            **{key: result[key].round(4).tolist() for key in result.columns},
        }

    full_start_year = int(historical.index.year.min())
    envelopes = {
        "primary": build_envelope(primary_years, f"2010–{current_year - 1}"),
        "full": build_envelope(range(full_start_year, current_year), f"{full_start_year}–{current_year - 1}"),
    }
    selected_years = [year for year in [current_year - 2, current_year - 1, current_year] if year >= 2010]
    year_series = []
    derivative_series = []
    drawdown_series = []
    for year in selected_years:
        season = summer_frame(daily, year, summer_day if year == current_year else None)
        if season.empty:
            continue
        rate = season["level"].diff().rolling(7, min_periods=4).mean() * 100
        drawdown = (season.iloc[0]["level"] - season["level"]) * 100
        year_series.append({"year": year, **serialise(season, ["summer_day", "level"]) | {"days": season["summer_day"].astype(int).tolist(), "levels": season["level"].round(4).tolist()}})
        derivative_series.append({"year": year, "days": season["summer_day"].astype(int).tolist(), "rate_cm_day": [None if pd.isna(x) else round(float(x), 3) for x in rate]})
        drawdown_series.append({"year": year, "days": season["summer_day"].astype(int).tolist(), "drawdown_cm": drawdown.round(3).tolist()})

    current_drawdown = float((current.iloc[0]["level"] - current.iloc[-1]["level"]) * 100) if len(current) else None
    last_drawdown = float((year_ago.iloc[0]["level"] - year_ago.iloc[-1]["level"]) * 100) if len(year_ago) else None
    remaining = None
    if geodetic is not None:
        remaining = max(0, min(100, (geodetic - 140.23) / (154.86 - 140.23) * 100))

    conclusions = [
        f"At {as_of['level']:.2f} m (WSC assumed datum), today is at the {percentile:.0f}th percentile of 2010–{current_year - 1} same-date levels; it is {compare_text(as_of['level'], median_level, 'm')} than the historical median.",
        f"Relative to {current_year - 1}, this date is {compare_text(as_of['level'], last_year_level, 'm')}. The 2015 same-date drought reference was 44.90 m.",
        f"Summer drawdown since June 1 is {current_drawdown:.0f} cm, versus {last_drawdown:.0f} cm by the same day last summer." if current_drawdown is not None and last_drawdown is not None else "Current-year realtime coverage is too short to calculate complete summer drawdown.",
        f"The 30-day net level-change rate is {recent_30:.2f} cm/day, versus {last_year_rate:.2f} cm/day last summer and {median_rate:.2f} cm/day for the historical median." if all(x is not None for x in [recent_30, last_year_rate, median_rate]) else "Realtime coverage is insufficient for a 30-day derivative comparison.",
    ]
    if remaining is not None:
        conclusions.append(f"The datum cross-check estimates {geodetic:.2f} m geodetic, roughly {remaining:.0f}% of the WUP operating elevation range above its 140.23 m lower bound. This is not a volume estimate.")

    return {
        "station": {"number": STATION, "name": "Coquitlam Lake Forebay"},
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "current_year": current_year,
        "as_of": {"date": as_of_date.strftime("%Y-%m-%d"), "level": round(float(as_of["level"]), 3), "geodetic": round(geodetic, 3) if geodetic else None},
        "conclusions": conclusions,
        "stats": [
            {"label": "Current level", "value": f"{as_of['level']:.2f} m", "detail": "WSC assumed datum"},
            {"label": "Geodetic cross-check", "value": f"{geodetic:.2f} m" if geodetic else "Unavailable", "detail": "BC Hydro feed; approximate datum conversion"},
            {"label": "Same-date percentile", "value": f"{percentile:.0f}th" if percentile else "Unavailable", "detail": f"vs 2010–{current_year - 1}"},
            {"label": "30-day dH/dt", "value": f"{recent_30:.2f} cm/day" if recent_30 else "Unavailable", "detail": "negative means net drawdown"},
        ],
        "summer": {"envelopes": envelopes, "years": year_series},
        "derivatives": derivative_series,
        "drawdowns": drawdown_series,
        "same_day": {"label": as_of_date.strftime("%b %-d"), "levels": same_days},
        "footer": "Water level is net reservoir-storage change — inflow minus drinking-water diversion, BC Hydro operations, fish flows, and evaporation — not direct water consumption. Historical data: ECCC MSC GeoMet HYDAT; current observations: GeoMet realtime (provisional); datum cross-check: BC Hydro. The WUP operating range uses a separate geodetic datum.",
    }


def render(analysis: dict) -> None:
    template = (HERE / "template.html").read_text()
    page = template.replace("__PLOTLY_JS__", get_plotlyjs()).replace("__ANALYSIS_JSON__", json.dumps(analysis))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "index.html").write_text(page)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="ignore local cache")
    parser.add_argument("--fetch-only", action="store_true")
    args = parser.parse_args()
    historical, realtime, bc_hydro = fetch(args.refresh)
    if args.fetch_only:
        return
    render(analyse(historical, realtime, bc_hydro))


if __name__ == "__main__":
    main()
