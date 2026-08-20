"""Fetch, analyse, and render Coquitlam Lake Forebay level data."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
from plotly.offline import get_plotlyjs

LAKE_STATION = "08MH149"
DOWNSTREAM_STATION = "08MH002"
UPSTREAM_STATION = "08MH141"
# Coquitlam Lake surface area from the 2005 Water Use Plan (~12.5 km²).
# Volume and "equivalent centimetres" are first-order; the real stage-storage curve is not linear.
LAKE_AREA_M2 = 12.5e6
M3S_TO_CM_DAY = 86400.0 / LAKE_AREA_M2 * 100.0  # ~0.69 cm/day per m³/s
CM_TO_MM3 = LAKE_AREA_M2 * 0.01 / 1e6  # ~0.125 million m³ per centimetre
LAKE_COLOR = "#1d4f91"
RIVER_COLOR = "#b45309"
INFLOW_COLOR = "#0f766e"
# Treatment 2 instream flow release targets (m³/s) from the Coquitlam-Buntzen WUP Order.
# These are dam-release targets above Or Creek, not 08MH002. Reduced targets apply in dry years.
WUP_MONTHLY_TARGET = {2: 2.92, 3: 4.25, 4: 3.50, 5: 2.91, 6: 1.10, 7: 1.20, 8: 2.70, 9: 2.22, 10: 6.07, 11: 3.96, 12: 5.00}
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


def get(url: str, timeout: int = 45) -> str:
    request = Request(url, headers={"User-Agent": "lake-level-analysis/1.0"})
    with urlopen(request, timeout=timeout) as response:
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
                    STATION_NUMBER=LAKE_STATION,
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


def fetch_discharge_historical(station: str, cache_name: str, refresh: bool) -> pd.DataFrame:
    cache = DATA / cache_name
    if fresh(cache, HISTORICAL_TTL) and not refresh:
        return pd.DataFrame(json.loads(cache.read_text()))

    records: list[dict] = []
    for year in range(1985, datetime.now(UTC).year):
        payload = json.loads(
            get(
                api_url(
                    "hydrometric-daily-mean",
                    STATION_NUMBER=station,
                    datetime=f"{year}-01-01/{year}-12-31",
                    limit=1000,
                    f="json",
                )
            )
        )
        records.extend(feature["properties"] for feature in payload["features"])
    frame = pd.DataFrame(records)[["DATE", "DISCHARGE"]].rename(columns={"DATE": "date", "DISCHARGE": "flow"})
    frame = frame.dropna().drop_duplicates("date").sort_values("date")
    DATA.mkdir(parents=True, exist_ok=True)
    cache.write_text(frame.to_json(orient="records", date_format="iso"))
    return frame


def fetch_water_office(station: str, parameter: str, field: str, start: datetime, end: datetime) -> pd.DataFrame:
    params = urlencode(
        [
            ("stations[]", station),
            ("parameters[]", parameter),
            ("start_date", start.strftime("%Y-%m-%d %H:%M:%S")),
            ("end_date", end.strftime("%Y-%m-%d %H:%M:%S")),
        ]
    )
    text = get(f"{WATER_OFFICE}?{params}", timeout=120)
    (DATA / f"water-office-{station}-{parameter}.csv").write_text(text)
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("Parameter/Paramètre") == parameter and row.get("Value/Valeur"):
            rows.append({"datetime": row["Date"], field: float(row["Value/Valeur"])})
    return pd.DataFrame(rows)


def fetch_realtime(station: str, parameter: str, cache_name: str, field: str, refresh: bool) -> pd.DataFrame:
    cache = DATA / cache_name
    if fresh(cache, REALTIME_TTL) and not refresh:
        return pd.DataFrame(json.loads(cache.read_text()))

    # Unit values for the current calendar year. GeoMet daily-mean has no current-year
    # rows, and GeoMet realtime is only ~30 days, so Water Office is the YTD source.
    year = datetime.now(UTC).year
    start = datetime(year, 1, 1)
    mid = datetime(year, 5, 1)
    end = datetime.now(UTC).replace(tzinfo=None)
    water_office_frame = pd.DataFrame()
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        # Two chunks keeps each CSV in the size that already works for May–now.
        # Reuse a still-fresh May-start cache for the second half when present.
        parts = [fetch_water_office(station, parameter, field, start, mid)]
        legacy = DATA / cache_name.replace("-ytd.json", ".json")
        if legacy.exists() and not refresh and fresh(legacy, REALTIME_TTL):
            parts.append(pd.DataFrame(json.loads(legacy.read_text())))
        else:
            parts.append(fetch_water_office(station, parameter, field, mid, end))
        water_office_frame = pd.concat([p for p in parts if not p.empty], ignore_index=True)
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"Water Office {station} data unavailable: {error}", file=sys.stderr)

    if not water_office_frame.empty:
        frame = water_office_frame.drop_duplicates("datetime").sort_values("datetime")
        cache.write_text(frame.to_json(orient="records"))
        return frame

    geomet_field = "LEVEL" if field == "level" else "DISCHARGE"
    payload = json.loads(
        get(
            api_url(
                "hydrometric-realtime",
                STATION_NUMBER=station,
                sortby="-DATETIME",
                limit=10000,
                f="json",
            )
        )
    )
    records = [
        {"datetime": f["properties"]["DATETIME_LST"], field: f["properties"][geomet_field]}
        for f in payload["features"]
        if f["properties"][geomet_field] is not None
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


def fetch(refresh: bool) -> tuple[pd.DataFrame, ...]:
    return (
        fetch_historical(refresh),
        fetch_realtime(LAKE_STATION, "46", "realtime-ytd.json", "level", refresh),
        fetch_bc_hydro(refresh),
        fetch_discharge_historical(DOWNSTREAM_STATION, "downstream-historical.json", refresh),
        fetch_realtime(DOWNSTREAM_STATION, "47", "downstream-realtime-ytd.json", "flow", refresh),
        fetch_discharge_historical(UPSTREAM_STATION, "upstream-historical.json", refresh),
        fetch_realtime(UPSTREAM_STATION, "47", "upstream-realtime-ytd.json", "flow", refresh),
    )


def serialise(frame: pd.DataFrame, columns: list[str]) -> dict[str, list]:
    return {column: [None if pd.isna(v) else round(float(v), 4) for v in frame[column]] for column in columns}


def summer_frame(daily: pd.DataFrame, year: int, end_day: int | None = None) -> pd.DataFrame:
    result = daily[(daily.index.year == year) & (daily.index.month >= 6) & (daily.index.month <= 9)].copy()
    result["summer_day"] = (result.index - pd.Timestamp(year=year, month=6, day=1)).days + 1
    if end_day:
        result = result[result["summer_day"] <= end_day]
    return result


def annual_frame(daily: pd.DataFrame, year: int) -> pd.DataFrame:
    result = daily[daily.index.year == year].copy()
    result["calendar_day"] = result.index.strftime("%m-%d")
    return result


def summer_dates(days: pd.Series | list[int]) -> list[str]:
    """Return calendar labels on a common leap-year axis for cross-year charts."""
    origin = pd.Timestamp(year=2020, month=6, day=1)
    return [(origin + pd.Timedelta(int(day) - 1, unit="D")).strftime("%b %-d") for day in days]


def calendar_dates(index: pd.DatetimeIndex) -> list[str]:
    return index.strftime("%b %-d").tolist()


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


def clean_nums(values) -> list[float]:
    return [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v)) and pd.notna(v)]


def identical_axis_range(left, right, pad: float = 0.12) -> list[float]:
    """One numeric range for both dual-axis scales so zero is a single horizontal line."""
    vals = clean_nums(list(left) + list(right))
    if not vals:
        return [-1.0, 1.0]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    span = max(hi - lo, abs(hi), abs(lo), 1e-6)
    lo -= span * pad
    hi += span * pad
    lo = min(lo, 0.0)
    hi = max(hi, 0.0)
    if lo == 0 and hi == 0:
        return [-1.0, 1.0]
    return [round(lo, 4), round(hi, 4)]


def value_on_md(daily: pd.DataFrame, year: int, month: int, day: int, column: str) -> float | None:
    sample = daily[(daily.index.year == year) & (daily.index.month == month) & (daily.index.day == day)]
    if sample.empty:
        window = daily[(daily.index.year == year) & (daily.index.month == month) & (daily.index.day.between(day - 1, day + 1))]
        if window.empty:
            return None
        sample = window
    return float(sample.iloc[-1][column])


def cm_to_mm3(cm: float | None) -> float | None:
    return None if cm is None else cm * CM_TO_MM3


def wup_target_cms(month: int, day: int) -> float:
    if month == 1:
        return 5.90 if day <= 15 else 2.92
    return WUP_MONTHLY_TARGET[month]


def detect_flow_events(lake: pd.DataFrame, downstream: pd.DataFrame, upstream: pd.DataFrame, year: int) -> list[dict]:
    """Hydrometric event finder: storm inflow vs controlled spill vs the two in sequence.

    08MH141 is partial headwater inflow; 08MH002 includes dam release plus lower tributaries.
    WUP fish targets are 1–6 m³/s. Tens of m³/s with a falling lake after inflow collapses is ops, not fish.
    """
    frame = annual_frame(lake, year)[["level"]].join(annual_frame(downstream, year)[["flow"]], how="inner")
    inflow = annual_frame(upstream, year)[["flow"]].rename(columns={"flow": "inflow"})
    frame = frame.join(inflow, how="left")
    if len(frame) < 8:
        return []
    frame["dH_cm"] = frame["level"].diff() * 100
    inflow_cms = frame["inflow"].fillna(0)
    storm = inflow_cms >= 15
    spill = (frame["flow"] >= 25) & (frame["dH_cm"] <= -5) & (inflow_cms < 15)
    active = storm | spill | (frame["flow"] >= 40)
    dates = frame.index[active]
    if dates.empty:
        return []
    groups: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start = prev = dates[0]
    for day in dates[1:]:
        if (day - prev).days <= 3:
            prev = day
        else:
            groups.append((start, prev))
            start = prev = day
    groups.append((start, prev))

    events = []
    for start, end in groups:
        window = frame.loc[start:end]
        if window.empty:
            continue
        inflow_peak = float(window["inflow"].max()) if window["inflow"].notna().any() else 0.0
        outflow_peak = float(window["flow"].max())
        lake_peak_at = pd.Timestamp(window["level"].idxmax())
        lake_peak = float(window.loc[lake_peak_at, "level"])
        high = window[window["flow"] >= 25]
        last_high = pd.Timestamp(high.index.max()) if not high.empty else pd.Timestamp(end)
        if last_high < lake_peak_at:
            spill_trough = lake_peak
            drawdown_m = 0.0
        else:
            spill_trough = float(frame.loc[lake_peak_at:last_high, "level"].min())
            drawdown_m = lake_peak - spill_trough
        if pd.isna(drawdown_m):
            drawdown_m = 0.0
        candidate = last_high + pd.Timedelta(days=7)
        plateau_end = candidate if candidate <= pd.Timestamp(frame.index.max()) else pd.Timestamp(frame.index.max())
        plateau_slice = frame.loc[lake_peak_at:plateau_end]
        if plateau_slice.empty:
            trough_at = lake_peak_at
            lake_trough = lake_peak
        else:
            trough_at = pd.Timestamp(plateau_slice["level"].idxmin())
            lake_trough = float(plateau_slice.loc[trough_at, "level"])
        plateau_drop_m = lake_peak - lake_trough
        if pd.isna(plateau_drop_m):
            plateau_drop_m = drawdown_m
        after = window.loc[lake_peak_at:]
        peak_outflow_at = window["flow"].idxmax()
        peak_inflow_at = window["inflow"].idxmax() if window["inflow"].notna().any() else peak_outflow_at
        after_inflow = float(after["inflow"].iloc[1:].mean()) if len(after) > 1 else float(window["inflow"].iloc[-1])
        after_outflow = float(after["flow"].iloc[1:].mean()) if len(after) > 1 else float(window["flow"].iloc[-1])
        wup = wup_target_cms(int(peak_outflow_at.month), int(peak_outflow_at.day))
        if inflow_peak >= 15 and drawdown_m >= 0.5 and after_outflow >= 25 and after_inflow < 12:
            kind = "storm_then_spill"
            title = "Storm then controlled spill"
        elif inflow_peak >= 15 and (drawdown_m >= 0.5 or plateau_drop_m >= 0.5):
            kind = "storm_then_spill"
            title = "Storm inflow and lake drop"
        elif inflow_peak >= 15:
            kind = "storm_inflow"
            title = "Storm / rain-on-snow inflow"
        elif outflow_peak >= 25 and inflow_peak < 15 and drawdown_m >= 0.4:
            kind = "controlled_spill"
            title = "Controlled spill / flood storage"
        elif outflow_peak >= 25:
            kind = "high_flow"
            title = "High downstream flow"
        else:
            continue
        if outflow_peak < 25 and drawdown_m < 0.5 and plateau_drop_m < 0.5:
            continue
        shade_end = max(end, trough_at)
        events.append(
            {
                "kind": kind,
                "title": title,
                "start": start.strftime("%Y-%m-%d"),
                "end": shade_end.strftime("%Y-%m-%d"),
                "label_start": start.strftime("%b %-d"),
                "label_end": shade_end.strftime("%b %-d"),
                "overlaps_summer": start.month <= 9 and shade_end.month >= 6,
                "lake_peak_m": round(lake_peak, 2),
                "drawdown_m": round(drawdown_m, 2),
                "drawdown_mm3": round(cm_to_mm3(drawdown_m * 100), 1),
                "plateau_drop_m": round(plateau_drop_m, 2),
                "outflow_peak": round(outflow_peak, 1),
                "inflow_peak": round(inflow_peak, 1),
                "wup_target": wup,
                "outflow_vs_wup": round(outflow_peak / wup, 1) if wup else None,
                "peak_outflow_date": peak_outflow_at.strftime("%b %-d"),
                "peak_inflow_date": peak_inflow_at.strftime("%b %-d"),
                "spill_after_storm": after_outflow >= 25 and after_inflow < 12,
            }
        )
    events.sort(key=lambda row: row["drawdown_m"], reverse=True)
    return events[:8]


def event_conclusion(event: dict) -> str:
    fish = (
        f"Peak 08MH002 was {event['outflow_peak']:.0f} m³/s on {event['peak_outflow_date']}, "
        f"about {event['outflow_vs_wup']:.0f}× the WUP fish-flow target that week ({event['wup_target']:.2f} m³/s). "
        "WUP-mandated fish releases are 1–6 m³/s, not this."
    )
    if event["kind"] == "storm_then_spill":
        core = (
            f"{event['label_start']}–{event['label_end']}: {event['title'].lower()}. "
            f"08MH141 peaked at {event['inflow_peak']:.0f} m³/s ({event['peak_inflow_date']}); while 08MH002 was still spilling "
            f"the lake fell {event['drawdown_m']:.1f} m (~{event['drawdown_mm3']:.0f} million m³) from {event['lake_peak_m']:.2f} m"
            + (
                f", then another {event['plateau_drop_m'] - event['drawdown_m']:.1f} m as river flow receded"
                if event.get("plateau_drop_m", 0) >= event["drawdown_m"] + 0.4
                else ""
            )
            + f". {fish}"
        )
        if event.get("spill_after_storm"):
            return (
                core + " After the inflow collapsed, Port Coquitlam stayed high while the lake dropped — flood-storage spill, "
                "not Fraser snowmelt freshet (typically May–June) and not a construction drawdown with flat inflow."
            )
        return core + " Early for Fraser snowmelt freshet (typically May–June)."
    if event["kind"] == "controlled_spill":
        return (
            f"{event['label_start']}–{event['label_end']}: lake fell {event['drawdown_m']:.1f} m with high 08MH002 "
            f"({event['outflow_peak']:.0f} m³/s) while 08MH141 stayed low ({event['inflow_peak']:.1f} m³/s). {fish} "
            "That pattern is dam release or lower-tributary flow, not headwater freshet. Confirm construction/ops from notices; hydrometrics cannot name the operator's reason."
        )
    if event["kind"] == "storm_inflow":
        return (
            f"{event['label_start']}–{event['label_end']}: headwater inflow (08MH141) peaked at {event['inflow_peak']:.0f} m³/s. "
            "Lake storage mostly absorbed the pulse rather than a large subsequent drawdown."
        )
    return (
        f"{event['label_start']}–{event['label_end']}: 08MH002 peaked at {event['outflow_peak']:.0f} m³/s "
        f"({event['outflow_vs_wup']:.0f}× that week's WUP fish target)."
    )


def merge_daily(historical: pd.DataFrame, realtime: pd.DataFrame, value: str) -> pd.DataFrame:
    historical["date"] = pd.to_datetime(historical["date"], utc=True).dt.tz_localize(None)
    historical = historical.set_index("date").sort_index()
    realtime["datetime"] = pd.to_datetime(realtime["datetime"], utc=True)
    realtime = realtime.set_index("datetime").sort_index()
    current_daily = realtime[value].resample("1D").mean().to_frame()
    current_daily.index = current_daily.index.tz_localize(None)
    result = pd.concat([historical, current_daily]).sort_index()
    return result[~result.index.duplicated(keep="last")]


def flow_same_day(daily: pd.DataFrame, year_range: range, as_of_date: pd.Timestamp) -> list[dict]:
    values = []
    for year in year_range:
        sample = daily[(daily.index.year == year) & (daily.index.month == as_of_date.month) & (daily.index.day == as_of_date.day)]
        if not sample.empty:
            values.append({"year": year, "flow": float(sample.iloc[-1]["flow"])})
    return values


def flow_series(daily: pd.DataFrame, years: list[int], current_year: int, end_day: int, annual: bool = False) -> list[dict]:
    result = []
    for year in years:
        season = annual_frame(daily, year) if annual else summer_frame(daily, year, end_day if year == current_year else None)
        if not season.empty:
            change = season["flow"].diff().rolling(7, min_periods=4).mean()
            result.append(
                {
                    "year": year,
                    "dates": calendar_dates(season.index) if annual else summer_dates(season["summer_day"]),
                    "flow": season["flow"].round(4).tolist(),
                    "flow_change_cms_day": [None if pd.isna(x) else round(float(x), 4) for x in change],
                }
            )
    return result


def analyse(
    historical: pd.DataFrame,
    realtime: pd.DataFrame,
    bc_hydro: pd.DataFrame,
    downstream_historical: pd.DataFrame,
    downstream_realtime: pd.DataFrame,
    upstream_historical: pd.DataFrame,
    upstream_realtime: pd.DataFrame,
) -> dict:
    daily = merge_daily(historical, realtime, "level")
    downstream_daily = merge_daily(downstream_historical, downstream_realtime, "flow")
    upstream_daily = merge_daily(upstream_historical, upstream_realtime, "flow")
    realtime["datetime"] = pd.to_datetime(realtime["datetime"], utc=True)
    realtime = realtime.set_index("datetime").sort_index()

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
        sample = daily[(daily.index.year == year) & (daily.index.month == as_of_date.month) & (daily.index.day == as_of_date.day)]
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
            "dates": summer_dates(result.index),
            **{key: result[key].round(4).tolist() for key in result.columns},
        }

    def build_annual_envelope(years: range, label: str) -> dict:
        annual = [annual_frame(daily, year) for year in years]
        combined = pd.concat([x[["calendar_day", "level"]] for x in annual if not x.empty])
        result = combined.groupby("calendar_day")["level"].quantile([0.1, 0.5, 0.9]).unstack()
        result.columns = ["p10", "median", "p90"]
        dates = pd.to_datetime(["2020-" + day for day in result.index]).strftime("%b %-d").tolist()
        return {
            "label": label,
            "dates": dates,
            **{key: result[key].round(4).tolist() for key in result.columns},
        }

    full_start_year = int(daily.index.year.min())
    envelopes = {
        "primary": build_envelope(primary_years, f"2010–{current_year - 1}"),
        "full": build_envelope(range(full_start_year, current_year), f"{full_start_year}–{current_year - 1}"),
    }
    annual_envelopes = {
        "primary": build_annual_envelope(primary_years, f"2010–{current_year - 1}"),
        "full": build_annual_envelope(range(full_start_year, current_year), f"{full_start_year}–{current_year - 1}"),
    }
    selected_years = [year for year in [current_year - 2, current_year - 1, current_year] if year >= 2010]
    year_series = []
    derivative_series = []
    drawdown_series = []
    annual_year_series = []
    annual_derivative_series = []
    annual_drawdown_series = []
    for year in selected_years:
        season = summer_frame(daily, year, summer_day if year == current_year else None)
        if season.empty:
            continue
        rate = season["level"].diff().rolling(7, min_periods=4).mean() * 100
        drawdown = (season.iloc[0]["level"] - season["level"]) * 100
        dates = summer_dates(season["summer_day"])
        year_series.append({"year": year, **serialise(season, ["summer_day", "level"]) | {"days": season["summer_day"].astype(int).tolist(), "dates": dates, "levels": season["level"].round(4).tolist()}})
        derivative_series.append({"year": year, "days": season["summer_day"].astype(int).tolist(), "dates": dates, "rate_cm_day": [None if pd.isna(x) else round(float(x), 3) for x in rate]})
        drawdown_series.append({"year": year, "days": season["summer_day"].astype(int).tolist(), "dates": dates, "drawdown_cm": drawdown.round(3).tolist()})
        annual = annual_frame(daily, year)
        annual_rate = annual["level"].diff().rolling(7, min_periods=4).mean() * 100
        annual_drawdown = (annual.iloc[0]["level"] - annual["level"]) * 100
        annual_dates = calendar_dates(annual.index)
        annual_year_series.append({"year": year, "dates": annual_dates, "levels": annual["level"].round(4).tolist()})
        annual_derivative_series.append({"year": year, "dates": annual_dates, "rate_cm_day": [None if pd.isna(x) else round(float(x), 3) for x in annual_rate]})
        annual_drawdown_series.append({"year": year, "dates": annual_dates, "drawdown_cm": annual_drawdown.round(3).tolist()})

    downstream_same_day = flow_same_day(downstream_daily, primary_years, as_of_date)
    downstream_current = summer_frame(downstream_daily, current_year, summer_day)
    downstream_last_year = summer_frame(downstream_daily, current_year - 1, summer_day)
    upstream_current = summer_frame(upstream_daily, current_year, summer_day)
    downstream_now = float(downstream_current.iloc[-1]["flow"]) if not downstream_current.empty else None
    upstream_now = float(upstream_current.iloc[-1]["flow"]) if not upstream_current.empty else None
    downstream_percentile = (
        float((pd.DataFrame(downstream_same_day)["flow"] <= downstream_now).mean() * 100)
        if downstream_now is not None and downstream_same_day
        else None
    )
    downstream_30day = float(downstream_current.tail(30)["flow"].mean()) if len(downstream_current) >= 15 else None
    downstream_2025_30day = float(downstream_last_year.tail(30)["flow"].mean()) if len(downstream_last_year) >= 15 else None
    relationship = current[["level"]].join(downstream_current[["flow"]], how="inner")
    relationship["lake_level_change_cm_day"] = relationship["level"].diff().rolling(7, min_periods=4).mean() * 100
    relationship["river_flow_change_cms_day"] = relationship["flow"].diff().rolling(7, min_periods=4).mean()
    relationship["river_equivalent_cm_day"] = relationship["flow"] * M3S_TO_CM_DAY
    annual_relationship = annual_frame(daily, current_year)[["level"]].join(annual_frame(downstream_daily, current_year)[["flow"]], how="inner")
    annual_relationship["lake_level_change_cm_day"] = annual_relationship["level"].diff().rolling(7, min_periods=4).mean() * 100
    annual_relationship["river_flow_change_cms_day"] = annual_relationship["flow"].diff().rolling(7, min_periods=4).mean()
    annual_relationship["river_equivalent_cm_day"] = annual_relationship["flow"] * M3S_TO_CM_DAY
    events = detect_flow_events(daily, downstream_daily, upstream_daily, current_year)
    annual_wup_index = annual_frame(downstream_daily, current_year).index
    summer_wup_index = downstream_current.index
    wup = {
        "annual": {"dates": calendar_dates(annual_wup_index), "target": [wup_target_cms(ts.month, ts.day) for ts in annual_wup_index]},
        "summer": {
            "dates": summer_dates(downstream_current["summer_day"]) if not downstream_current.empty else [],
            "target": [wup_target_cms(ts.month, ts.day) for ts in summer_wup_index],
        },
    }

    current_drawdown = float((current.iloc[0]["level"] - current.iloc[-1]["level"]) * 100) if len(current) else None
    last_drawdown = float((year_ago.iloc[0]["level"] - year_ago.iloc[-1]["level"]) * 100) if len(year_ago) else None
    june1_now = value_on_md(daily, current_year, 6, 1, "level")
    june1_last = value_on_md(daily, current_year - 1, 6, 1, "level")
    june1_hist = [value_on_md(daily, year, 6, 1, "level") for year in primary_years]
    june1_hist = [x for x in june1_hist if x is not None]
    june1_median = float(pd.Series(june1_hist).median()) if june1_hist else None
    drought_year = min(same_days, key=lambda row: row["level"]) if same_days else None
    river_equiv = downstream_now * M3S_TO_CM_DAY if downstream_now is not None else None
    residual_cm = (abs(recent_30) - river_equiv) if recent_30 is not None and river_equiv is not None else None
    remaining = None
    if geodetic is not None:
        remaining = max(0, min(100, (geodetic - 140.23) / (154.86 - 140.23) * 100))

    derivative_range = {
        "summer": identical_axis_range(relationship["lake_level_change_cm_day"], relationship["river_equivalent_cm_day"]),
        "annual": identical_axis_range(annual_relationship["lake_level_change_cm_day"], annual_relationship["river_equivalent_cm_day"]),
    }

    conclusions = [
        f"At {as_of['level']:.2f} m (WSC assumed datum), today is at the {percentile:.0f}th percentile of 2010–{current_year - 1} same-date levels; it is {compare_text(as_of['level'], median_level, 'm')} than the historical median.",
        f"Relative to {current_year - 1}, this date is {compare_text(as_of['level'], last_year_level, 'm')}."
        + (f" The lowest same-date year in the 2010–{current_year - 1} record is {drought_year['year']} at {drought_year['level']:.2f} m." if drought_year else ""),
        f"On June 1 the lake was {june1_now:.2f} m, versus {june1_last:.2f} m last year and a 2010–{current_year - 1} median of {june1_median:.2f} m."
        if all(x is not None for x in [june1_now, june1_last, june1_median])
        else "June 1 starting level is incomplete for a year-over-year start comparison.",
        f"Summer drawdown since June 1 is {current_drawdown:.0f} cm (~{cm_to_mm3(current_drawdown):.1f} million m³), versus {last_drawdown:.0f} cm (~{cm_to_mm3(last_drawdown):.1f} million m³) by the same day last summer. Using 12.5 km² surface area: 1 cm ≈ {CM_TO_MM3:.3f} million m³."
        if current_drawdown is not None and last_drawdown is not None
        else "Current-year realtime coverage is too short to calculate complete summer drawdown.",
        f"The 30-day net level-change rate is {recent_30:.2f} cm/day, versus {last_year_rate:.2f} cm/day last summer and {median_rate:.2f} cm/day for the historical median."
        if all(x is not None for x in [recent_30, last_year_rate, median_rate])
        else "Realtime coverage is insufficient for a 30-day derivative comparison.",
    ]
    if remaining is not None:
        conclusions.append(f"The datum cross-check estimates {geodetic:.2f} m geodetic, roughly {remaining:.0f}% of the WUP operating elevation range above its 140.23 m lower bound. This is an elevation fraction, not a volume.")
    if downstream_now is not None and downstream_percentile is not None and river_equiv is not None:
        conclusions.append(
            f"Port Coquitlam downstream flow is {downstream_now:.2f} m³/s ({downstream_percentile:.0f}th percentile for this date). "
            f"If all of that flow came from the lake, it would drop the surface about {river_equiv:.1f} cm/day (~{cm_to_mm3(river_equiv):.2f} million m³/day). "
            "That is an upper bound: the gauge includes tributaries and possible Grant's Tomb contributions."
        )
    if residual_cm is not None and recent_30 is not None and recent_30 < 0:
        if residual_cm > 0:
            conclusions.append(
                f"Recent lake drop ({abs(recent_30):.1f} cm/day) exceeds that river-equivalent upper bound ({river_equiv:.1f} cm/day) by about {residual_cm:.1f} cm/day. "
                "The leftover is withdrawals, Buntzen diversion, evaporation, unmeasured inflow changes, and gauge error — not extra river release."
            )
        else:
            conclusions.append(
                f"Recent lake drop ({abs(recent_30):.1f} cm/day) is within the river-equivalent upper bound ({river_equiv:.1f} cm/day). "
                "Downstream flow could account for the recent drop if most of 08MH002 were dam release; the gauge cannot prove that."
            )
    if downstream_30day is not None and downstream_2025_30day is not None:
        flow_change = (downstream_30day / downstream_2025_30day - 1) * 100 if downstream_2025_30day else None
        if flow_change is not None and flow_change > 15:
            explanation = "Higher downstream flow is consistent with river releases contributing to drawdown, but it cannot isolate dam release from tributary or return flows."
        elif flow_change is not None and flow_change < -15:
            explanation = "Lower downstream flow does not support elevated river release as the primary explanation for faster reservoir drawdown."
        else:
            explanation = "Similar downstream flow does not explain a major difference in reservoir drawdown by itself."
        conclusions.append(f"Thirty-day downstream flow averages {downstream_30day:.2f} m³/s, {abs(flow_change):.0f}% {'higher' if flow_change >= 0 else 'lower'} than last summer. {explanation}")
    notable = [event for event in events if event["drawdown_m"] >= 0.8 or event["outflow_peak"] >= 50]
    conclusions.extend(event_conclusion(event) for event in notable[:3])

    return {
        "station": {"number": LAKE_STATION, "name": "Coquitlam Lake Forebay"},
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "current_year": current_year,
        "as_of": {"date": as_of_date.strftime("%Y-%m-%d"), "level": round(float(as_of["level"]), 3), "geodetic": round(geodetic, 3) if geodetic else None},
        "conclusions": conclusions,
        "stats": [
            {"label": "Current level", "value": f"{as_of['level']:.2f} m", "detail": "WSC assumed datum"},
            {"label": "Geodetic cross-check", "value": f"{geodetic:.2f} m" if geodetic else "Unavailable", "detail": "BC Hydro feed; approximate datum conversion"},
            {"label": "Same-date percentile", "value": f"{percentile:.0f}th" if percentile else "Unavailable", "detail": f"vs 2010–{current_year - 1}"},
            {"label": "30-day dH/dt", "value": f"{recent_30:.2f} cm/day" if recent_30 else "Unavailable", "detail": "negative means net drawdown"},
            {"label": "30-day volume change", "value": f"{cm_to_mm3(recent_30):+.2f} Mm³/day" if recent_30 is not None else "Unavailable", "detail": f"12.5 km² × dH/dt; 1 cm ≈ {CM_TO_MM3:.3f} Mm³"},
            {"label": "Downstream flow proxy", "value": f"{downstream_now:.2f} m³/s" if downstream_now is not None else "Unavailable", "detail": "08MH002 at Port Coquitlam"},
            {"label": "River as lake-cm/day", "value": f"{river_equiv:.1f} cm/day" if river_equiv is not None else "Unavailable", "detail": "upper bound if all 08MH002 came from the lake"},
            {"label": "Upper-river inflow", "value": f"{upstream_now:.2f} m³/s" if upstream_now is not None else "Unavailable", "detail": "08MH141, above the lake"},
        ],
        "summer": {"envelopes": envelopes, "years": year_series, "derivatives": derivative_series, "drawdowns": drawdown_series},
        "annual": {"envelopes": annual_envelopes, "years": annual_year_series, "derivatives": annual_derivative_series, "drawdowns": annual_drawdown_series},
        "derivatives": derivative_series,
        "drawdowns": drawdown_series,
        "same_day": {"label": as_of_date.strftime("%b %-d"), "levels": same_days},
        "colors": {"lake": LAKE_COLOR, "river": RIVER_COLOR, "inflow": INFLOW_COLOR},
        "events": events,
        "wup": wup,
        "axis_ranges": {"derivative": derivative_range},
        "river": {
            "station": {"number": DOWNSTREAM_STATION, "name": "Coquitlam River at Port Coquitlam"},
            "years": flow_series(downstream_daily, selected_years, current_year, summer_day),
            "annual_years": flow_series(downstream_daily, selected_years, current_year, summer_day, annual=True),
            "upstream_years": flow_series(upstream_daily, selected_years, current_year, summer_day),
            "annual_upstream_years": flow_series(upstream_daily, selected_years, current_year, summer_day, annual=True),
            "same_day": downstream_same_day,
            "relationship": {
                "flow": relationship["flow"].round(4).tolist(),
                "level": relationship["level"].round(4).tolist(),
                "lake_level_change_cm_day": [None if pd.isna(x) else round(float(x), 3) for x in relationship["lake_level_change_cm_day"]],
                "river_flow_change_cms_day": [None if pd.isna(x) else round(float(x), 4) for x in relationship["river_flow_change_cms_day"]],
                "river_equivalent_cm_day": [None if pd.isna(x) else round(float(x), 3) for x in relationship["river_equivalent_cm_day"]],
                "days": ((relationship.index - pd.Timestamp(year=current_year, month=6, day=1)).days + 1).astype(int).tolist(),
                "dates": relationship.index.strftime("%b %-d").tolist(),
            },
            "annual_relationship": {
                "flow": annual_relationship["flow"].round(4).tolist(),
                "level": annual_relationship["level"].round(4).tolist(),
                "lake_level_change_cm_day": [None if pd.isna(x) else round(float(x), 3) for x in annual_relationship["lake_level_change_cm_day"]],
                "river_flow_change_cms_day": [None if pd.isna(x) else round(float(x), 4) for x in annual_relationship["river_flow_change_cms_day"]],
                "river_equivalent_cm_day": [None if pd.isna(x) else round(float(x), 3) for x in annual_relationship["river_equivalent_cm_day"]],
                "dates": calendar_dates(annual_relationship.index),
            },
        },
        "footer": "Lake level is net reservoir-storage change — inflow minus drinking-water diversion, Buntzen diversion, direct dam release, fish flows, and evaporation — not direct water consumption. Volumes use a 12.5 km² surface-area approximation from the 2005 Water Use Plan; the real stage-storage curve is not linear. WUP fish-flow targets are dam releases above Or Creek (1–6 m³/s), not 08MH002. 08MH002 converted to lake-cm/day is an upper bound on dam release (tributaries and Grant's Tomb are included). 08MH141 is only a partial lake-inflow indicator. Historical data: ECCC MSC GeoMet HYDAT; current observations: Water Office/GeoMet (provisional); datum cross-check: BC Hydro.",
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
    sources = fetch(args.refresh)
    if args.fetch_only:
        return
    render(analyse(*sources))


if __name__ == "__main__":
    main()
