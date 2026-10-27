# Production scoring and drift monitoring

This document fixes, before go-live, how I score each delivered forecast and when the drift monitor
raises an alert. It implements sections 4.2 and 5 of `docs/production_design.md` (decision D20, P6
and P7). Code: `src/load_forecast/monitoring.py`. Calibration: `scripts/calibrate_monitoring.py`,
which writes `data/monitoring/calibration.json`. Tests: `tests/test_monitoring.py` and the score
storage test in `tests/test_serving.py`.

An alert opens a review. It never changes the model. A model change is a new experiment.

## 1. Scoring

`score_day` scores one delivery day on the slots where the actual and every available forecast are
present, so all of them are compared on exactly the same timestamps.

| Forecast | What it is | When it is missing |
|---|---|---|
| served | the forecast named by the choice file of the day (primary or fallback), headline score (P6 A) | no delivery (choice of kind `none`) |
| model | the primary run of `forecast.state_runs` (P6 B), shown beside the headline | no completed primary run |
| rte_d1 | raw RTE D-1 | never on a scored day |
| rte_star | RTE* of decision D15, prior calendar year bias by local hour | always on real-time actuals; in early January when the previous December is not consolidated |

For each forecast the row holds the number of slots, MAE, RMSE, MAPE and the daily mean error
(forecast minus actual, MW). Served and model also carry the coverage and mean width of the 80 %
interval, or nothing when the day has no interval (fallback days).

Vintages. The provisional score, the morning of D+1, uses real-time actuals and has no RTE*,
because the RTE* correction was estimated against consolidated data. The final score uses
consolidated actuals and adds RTE*. Each newer vintage is a new row of `forecast.scores`; no row is
updated. `ForecastStore.record_score` refuses a row whose `choice_hash` is not the one of the choice
file of the day, so a score can only describe the forecast that was actually served. The metrics
are stored as one JSON document in the `metrics` column of the existing table.

Over a period, the headline is the served forecast with the number of fallback and no-delivery
days beside it; the model score is next to it. Comparisons with RTE use the paired daily difference
and the 7-day block bootstrap of the evaluation module, on a quarter or more.

## 2. Level-shift alert: two-sided CUSUM

For a delivery day d, e_d is the mean of actual minus served forecast over the scored slots, and

    z_d = (e_d - mu_s) / sigma_s
    S+_d = max(0, S+_(d-1) + z_d - k)
    S-_d = max(0, S-_(d-1) - z_d - k)

with s the meteorological season of the Paris day d, k = 0.5 and an alarm when S+_d > h or
S-_d > h. Rules, all tested:
- Both sums start from 0 before the first scored day.
- After an alarm, the alert is logged and both sums restart from 0 on the next day.
- A day with fewer than 40 scored slots, or without a delivered forecast, is missing: the sums carry
  over unchanged, no alarm can happen, the day is counted.
- On the eighth consecutive missing day both sums restart at 0, with an event in the output.
- The CUSUM alerts on consolidated actuals only, because mu_s and sigma_s are on that scale. On
  real-time actuals it is computed and shown, never alerted.

Frozen values (MW), from the candidate 1 daily errors of 2024 and 2025, 731 days, every day with
at least 46 scored slots:

| Season | Days | mu_s | sigma_s | one sigma |
|---|---|---|---|---|
| winter (Dec to Feb) | 181 | 7.1 | 1426.7 | 1427 MW |
| spring | 184 | -133.0 | 834.5 | 835 MW |
| summer | 184 | 94.3 | 644.5 | 644 MW |
| autumn | 182 | 46.9 | 711.7 | 712 MW |

Winter daily errors are about twice as dispersed as summer ones. With one sigma for the whole year,
winter would trigger most false alarms and summer drifts would be missed.

### 2.1 Threshold h

Target fixed before calibration: in-control yearly false-alarm probability P(at least one alarm in
365 scored days) of 0.2.

Method. Textbook CUSUM tables assume independent days. I simulate in-control years instead, by a
seasonal calendar-position block bootstrap of the standardized reference series: each simulated
year starts at a uniform calendar position and is built from consecutive blocks of b days; each
block is copied from the reference at a start day whose calendar position is within 15 days of the
block's target date (29 February shares the place of 28 February). 20,000 simulated years per
block length, seed 20240. Before the first alarm the reset rule never applies, so the probability
of at least one alarm is the probability that the largest of S+ and S- over the year exceeds h.
h is the smallest value on a 0.01 grid that meets the target.

| Block length | h meeting 0.2 | Monte Carlo 95 % interval of that quantile | P(alarm) at h = 11.46 |
|---|---|---|---|
| 7 days | 11.46 | 11.37 to 11.52 | 0.200 |
| 14 days | 10.65 | 10.57 to 10.72 | 0.131 |
| 28 days | 9.34 | 9.26 to 9.40 | 0.063 |
| independent normal days (contrast) | 6.21 | | 0.001 |

**I fix h = 11.46**, the largest of the three block lengths, with k = 0.5.

The contrast row shows why the bootstrap is needed. The textbook threshold for independent days
(6.21) would give a simulated false-alarm probability of 0.94 a year with 7-day blocks, 0.94 with
14-day blocks and 0.95 with 28-day blocks: almost one false review every year.

Detection, simulated with the same blocks and a shift added from the first day, at h = 11.46:

| Shift | Detected within 365 days | Median delay | 90th percentile |
|---|---|---|---|
| 1 sigma (644 to 1427 MW) | 100 % | 21 days | 35 to 39 days |
| 0.5 sigma (322 to 713 MW) | 98 % | 79 to 80 days | 202 to 208 days |

These delays are counted in scored consolidated days. Consolidated data arrive with a delay, so the
calendar delay of an alert is longer (section 5 of the production design).

### 2.2 Dependence evidence

Autocorrelation of z_d over the 731 days (white-noise 95 % band: plus or minus 0.072):
- lag 1: 0.40, lag 2: 0.06; lags 3 to 60: 6 of 58 outside the band, largest absolute value 0.11
  (lags 20, 32 and 33 are about -0.10).
- After lag 2 most coefficients are small and negative. Their sum is what separates the block
  lengths below. A plausible reading, not tested, is that the online corrections (Kalman, the
  aggregation weights) pull the level back after a run of errors of one sign.

Variance of the mean (times n), relative to var(z) = 0.99:

| Quantity | Value |
|---|---|
| 1 + 2 x sum of autocorrelations up to lag 2 | 1.92 |
| same up to lag 7 / 14 / 28 / 60 | 1.67 / 1.25 / 0.48 / 0.34 |
| Bartlett long-run variance, bandwidth 30 / 60 | 1.12 / 0.73 |
| retained by 7-day blocks: Bartlett weights / bootstrap of the yearly mean | 1.72 / 1.64 |
| retained by 14-day blocks | 1.60 / 1.46 |
| retained by 28-day blocks | 1.16 / 0.98 |

Ratio of the Bartlett long-run variance at bandwidth 60 to the variance retained by each block
length: 0.43 (7 days), 0.46 (14 days), 0.63 (28 days) with Bartlett weights, 0.45, 0.50 and 0.75
with the bootstrap. Every block length retains more variance than the long-bandwidth estimate, so
this check gives no sign that 28-day blocks miss slow dependence. Two limits keep this a caution,
not a bound:
- With 731 days and seasonal means estimated on the same days, long-bandwidth estimates are noisy
  and pulled towards 0.
- The positive short-lag dependence (1.92 up to lag 2) is slightly larger than what 7-day blocks
  keep (1.64 to 1.72). If the negative coefficients after lag 2 were only noise, the 7-day false
  alarm probability would be somewhat underestimated. This is why I keep the largest h.

The simulated false-alarm probability is an estimate under the assumption that 2024 and 2025 are
in control and representative of the coming years, not a certified rate.

### 2.3 Reference period and the role of 2025

The reference is the served-forecast equivalent of candidate 1 (the aggregate with the seasonal
conformal interval) on 2024 and 2025, read from the stored backtest and test archives. No forecast
was computed for this calibration.
- 2025 is the test year, already evaluated once. Here it is used only to set a monitoring
  threshold, not to choose or tune a model, and no reported 2025 number changes. Its errors are no
  longer out of sample for h, and I state it next to h.
- I do not use 2023. It is the tuning year of every setting (D13) and its aggregate errors are not
  stored as served forecasts; reconstructing them would mean running the burn-in again.
- Two reference years per calendar position is thin: each block start is drawn among about 60
  overlapping starts. This is one more reason to report h for three block lengths.
- The standardization uses the same 731 days, so the reference z has mean 0 and variance 1 in
  each season by construction (2024 mean z +0.016, 2025 mean z -0.016).

## 3. Relative skill against raw RTE D-1

`relative_skill`: for each day, the mean of the daily MAE difference served minus raw RTE D-1 over
the last 28 calendar days, on the same actual vintage. It alerts when that mean is above 0 and the
window holds at least 21 scored days, so that a run of missing days cannot raise or hide an alert
on a handful of days. This is a fixed operational band with no claimed false-alarm rate.

On consolidated actuals the band would have alerted on 11 days of 2025 (largest 28-day mean
difference +156 MW). Raw RTE D-1 is offset by about -1,000 MW against consolidated data (D15), so
on that scale the band is lenient; on real-time actuals, where that offset is small, I expect it to
alert more often. I do not change the band for that: the comparison is what a user of either forecast
receives.

## 4. Not covered here

Interval coverage (item 3 of section 5) and the input checks (item 4) are not implemented in this
module. Their bands are defined in `docs/production_design.md` section 5.

## 5. How to reproduce

```powershell
$env:PYTHONPATH = 'src'
python scripts/calibrate_monitoring.py --check
```

`--check` exits 1 if the frozen constants of `monitoring.py` (mu_s, sigma_s, h) differ from the
recomputed ones. These constants are fixed before go-live and never tuned on production data.
