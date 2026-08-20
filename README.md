# Coquitlam Lake summer drawdown report

Generates an offline, single-file Plotly report for Water Survey of Canada
station 08MH149 (Coquitlam Lake Forebay). A monthly GitHub Actions run
publishes it to [GitHub Pages](https://msollanych-tt.github.io/lake-level-analysis/).

```sh
make
open output/index.html
```

The report uses cached source data unless historical data is older than 24
hours or realtime data is older than six hours. `make fetch` respects those
caches; use `make refresh` only when a forced refresh is needed. All Python
dependencies run in Docker; no host Python installation is needed.

## What it measures

Lake level is a net storage signal: inflow minus drinking-water withdrawals,
BC Hydro operations, fish flows, and evaporation. It is not a direct meter of
Metro Vancouver water consumption. The report compares same-day levels,
summer drawdown, and 7/30-day level derivatives against recent history. It
also includes discharge at 08MH002 (Coquitlam River at Port Coquitlam) as a
downstream controlled-flow proxy, plus 08MH141 (Coquitlam River above the
lake) as partial inflow context.

08MH002 is not a dam-outflow gauge: it includes lower-river tributaries and
may include Metro Vancouver's contribution at Grant's Tomb. Discharge is also
converted to equivalent lake centimetres/day using a 12.5 km² surface-area
approximation from the 2005 Water Use Plan (1 cm ≈ 0.125 million m³). That
conversion is an upper bound on dam release, not a measurement.

Sources:

- ECCC MSC GeoMet HYDAT daily means and realtime observations
- ECCC Water Office (attempted for the current-year unit-value history)
- BC Hydro Coquitlam Lake Forebay feed for a datum cross-check

## License

MIT. See [LICENSE](LICENSE). That covers this repository's code, not the
hydrometric observations themselves.
