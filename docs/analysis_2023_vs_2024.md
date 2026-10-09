# The 2023 gains coincide with a large positive GAM bias

Diagnostic only: no new model, tuning or post-2024 data.

## 1. Method

I replay the existing 2023 inner runs and 2024 backtests with the frozen
EXP-005 GAM penalty 100, EXP-006 half-life 96 hours and EXP-007 Kalman
`restart` mode. The Kalman pre-registration guard checks the committed choice.
Consumption is queried from a development build of the clean model `load_30min` from 2021-03-23 to each
year's exclusive Paris year-end boundary; GFS delivery dates stop at that
year's December 31. No SYNOP observations are model inputs.

I join actuals, raw RTE D-1 and all three predictions by UTC timestamp,
drop any non-finite value, and group by Paris calendar month. MAE and mean
error are slot-weighted. Mean error is forecast minus actual; positive means
over-forecast. Gain is EXP-005 MAE minus the other model's MAE; positive means
improvement. The tables are descriptive; I do not test monthly significance.

The 2023 inner scores exclude missing-feature slots, matching the original
selection scripts. The 2024 backtests use the existing backup chain:
EXP-005 to EXP-004 to EXP-003 to the calendar model; EXP-006 and EXP-007
fall back to EXP-005. The years therefore have different coverage rules.
The 2023 gains were measured on the year used for parameter selection and
are optimistic estimates of transfer to another year.

## 2. 2023 inner walk-forward

All values below are MW, except slot counts. Bias columns show signed mean
error. The common set has 17,422 slots; 98 of 17,520 expected slots are
excluded (missing actuals or missing-feature predictions).

| Month | Slots | MAE 005 | Bias 005 | Gain 006 | Bias 006 | Gain 007 | Bias 007 |
|-------|-------|---------|----------|----------|----------|----------|----------|
| Jan | 1488 | 1795.07 | 1637.84 | 134.43 | 1496.28 | 543.29 | 449.24 |
| Feb | 1344 | 1797.74 | 1354.96 | 636.68 | 634.13 | 739.49 | -315.26 |
| Mar | 1438 | 1155.13 | 687.62 | 141.11 | 345.37 | 129.45 | 164.04 |
| Apr | 1392 | 1138.06 | 502.33 | 41.98 | 268.90 | 63.38 | 235.43 |
| May | 1488 | 1010.23 | 373.05 | 93.49 | 73.21 | 187.46 | 172.38 |
| Jun | 1440 | 667.76 | 628.20 | 190.39 | 358.45 | 212.91 | 65.74 |
| Jul | 1488 | 639.26 | 155.75 | -23.89 | 94.85 | -19.12 | 41.60 |
| Aug | 1488 | 1026.37 | 508.07 | 46.12 | 266.53 | 181.40 | -110.81 |
| Sep | 1440 | 521.46 | 298.80 | 73.54 | 255.29 | 63.11 | 118.99 |
| Oct | 1488 | 757.71 | 578.82 | 143.40 | 342.07 | 231.41 | 22.64 |
| Nov | 1440 | 862.01 | 468.73 | -75.54 | 496.18 | 22.31 | -2.69 |
| Dec | 1488 | 1357.30 | 238.68 | -24.13 | 88.91 | -5.55 | -273.83 |

Annual MAE is 1057.00 / 945.76 / 863.98 MW for EXP-005 / EXP-006 / EXP-007.
Annual bias is +615.05 / +392.37 / +48.80 MW. These reproduce the original
2023 selection scores. EXP-005 over-forecasts every month. EXP-006 reduces
absolute monthly bias in 11 months and EXP-007 in 11 months; the exceptions
are November and December, respectively.

January and February are 16.3% of scored slots but contribute 54.5% and
53.6% of the net annual absolute-error reduction for EXP-006 and EXP-007.
I compute each contribution as the sum of monthly gain times scored slots,
divided by the annual sum of gain times slots. Improvements extend beyond
these months: EXP-006 gains in 9 months, EXP-007 in 10.

## 3. 2024 backtest

Same units and definitions. The common set has 17,566 slots; the two
excluded slots are 2024-10-27 00:00 and 00:30 UTC, absent under D12.
All gains use unrounded MAEs.

| Month | Slots | MAE 005 | Bias 005 | Gain 006 | Bias 006 | Gain 007 | Bias 007 |
|-------|-------|---------|----------|----------|----------|----------|----------|
| Jan | 1488 | 1453.78 | 51.91 | 80.54 | -74.55 | 38.17 | 23.61 |
| Feb | 1392 | 920.98 | -339.79 | -41.95 | 139.09 | -115.01 | -18.94 |
| Mar | 1486 | 797.28 | -1.00 | 22.90 | -27.13 | 2.55 | 180.17 |
| Apr | 1440 | 1026.24 | 251.50 | -13.78 | 309.22 | 109.25 | 289.90 |
| May | 1488 | 719.88 | 133.61 | -27.33 | -7.42 | -99.53 | 28.91 |
| Jun | 1440 | 514.56 | 205.74 | 23.62 | 68.24 | 38.38 | 51.00 |
| Jul | 1488 | 641.51 | -248.48 | 49.35 | -194.26 | 125.75 | -106.69 |
| Aug | 1488 | 957.89 | 25.95 | 14.73 | -85.67 | 190.63 | -67.60 |
| Sep | 1440 | 531.62 | -170.83 | 26.43 | -79.33 | -6.67 | 88.77 |
| Oct | 1488 | 597.71 | 17.34 | 17.07 | -53.59 | 107.45 | -6.19 |
| Nov | 1440 | 1125.02 | -333.05 | -0.05 | -160.72 | 46.86 | -315.68 |
| Dec | 1488 | 1641.69 | 71.65 | -104.13 | 9.84 | -126.00 | -207.69 |

Annual MAE is 911.85 / 907.71 / 885.32 MW; annual bias is
-26.43 / -14.37 / -5.35 MW. The accepted 2024 experiment scores reproduce.
EXP-005 has 7 positive-bias months and 5 negative-bias months, all between
-339.79 and +251.50 MW. Its consistent positive 2023 level bias is absent,
although monthly biases remain. EXP-006 gains in 7 months, EXP-007 in 8.
December loses 104.13 and 126.00 MW; Kalman also loses 115.01 MW in February
and 99.53 MW in May, offsetting gains elsewhere.

## 4. Interpretation

The 2023 gain is concentrated in January-February but is not confined to
them. Both additions reduce a large, consistently positive EXP-005 bias
in 2023. That bias is much smaller and changes sign in 2024. This supports
the interpretation that part of the 2023 gain came from correcting a
level mismatch that is largely absent in 2024; it does not identify its cause.
Post-crisis demand change is a hypothesis, not established by these tables.

The annual MAE gains shrink from 111.24 and 193.02 MW in 2023 to 4.14 and
26.53 MW in 2024. Smaller bias need not lower MAE: February 2024 has smaller
absolute bias for both additions but worse MAE. Selection-year optimism
and the different missing-slot rules also limit the comparison. The existing
paired 2024 tests report both gains as **not significant**:
[-39.91, +24.08] MW for EXP-006 and [-85.99, +25.38] MW for EXP-007
([EXP-006](../docs/experiments/EXP-006.md), [EXP-007](../docs/experiments/EXP-007.md)).
I make no new significance or causal claim.

## 5. Reproduction

From the repository root, with the shared virtual environment and PostgreSQL
and MLflow already running:

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src;scripts'
& ..\load-forecast-fr-venv\Scripts\python.exe -u scripts/analyze_2023_vs_2024.py > data/analysis_comparison047.log 2>&1
& ..\load-forecast-fr-venv\Scripts\python.exe -m pytest tests/ -q
& ..\load-forecast-fr-venv\Scripts\python.exe -m ruff check .
```

The first command saves predictions, monthly CSVs and annual summaries in
`data/analysis/comparison_047/`, and logs diagnostic copies under the existing EXP IDs
with the year boundaries and
`purpose=diagnostic replay, no tuning`. Subsequent runs reuse those saved
predictions and recompute the tables. This leaves canonical experiment runs
unchanged. For a fresh replay, first move the saved prediction files to a
new archive directory inside this checkout.

Code: `scripts/analyze_2023_vs_2024.py`,
`src/load_forecast/monthly_analysis.py`; tests in
`tests/test_monthly_analysis.py` check common finite slots, duplicate refusal,
Paris month boundaries and weighting on 46-slot and 50-slot DST days.

Diagnostic MLflow run IDs (canonical experiment runs remain the ones named
in their EXP files):

| Model | 2023 replay | 2024 replay |
|-------|-------------|-------------|
| EXP-005 | `2a7fa49c8cfd49c5a8679c062d452725` | `902d3630e6b64003bb399c657ca10429` |
| EXP-006 | `868d7c9b807e44438643eb8d52b123b9` | `236b8d17b2994bc1b1d960b0e9c0bf31` |
| EXP-007 | `7b0b9811163040da8fd0272fecf55449` | `3145afa151344832a239e24c02d783f8` |
