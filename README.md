# Coquitlam Lake summer drawdown report

Generates an offline, single-file Plotly report for Water Survey of Canada
station 08MH149 (Coquitlam Lake Forebay).

```sh
make
open output/index.html
```

The report uses cached source data unless historical data is older than 24
hours or realtime data is older than six hours. `make fetch` forces a refresh.
All Python dependencies run in Docker; no host Python installation is needed.

## What it measures

Lake level is a net storage signal: inflow minus drinking-water withdrawals,
BC Hydro operations, fish flows, and evaporation. It is not a direct meter of
Metro Vancouver water consumption. The report compares same-day levels,
summer drawdown, and 7/30-day level derivatives against recent history.

Sources:

- ECCC MSC GeoMet HYDAT daily means and realtime observations
- ECCC Water Office (attempted for the current-year unit-value history)
- BC Hydro Coquitlam Lake Forebay feed for a datum cross-check
