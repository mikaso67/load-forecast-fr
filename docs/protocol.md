# Evaluation protocol

Status: Approved by the developer on 2026-10-03 (D11 approved after my weather source analysis was accepted).
Each decision below keeps only the choice, its reason and its conditions.

This file defines how every model in the project is trained, evaluated and compared with RTE D-1.
An experiment that does not follow it is not reported.

## 0. Decision summary

| # | Choice | Decision | Status |
|---|--------|----------|--------|
| D1 | Target resolution | 30 min | approved |
| D2 | Consumption available at the cutoff | values ending at least 2 h before the cutoff | approved |
| D3 | RTE forecasts as model inputs | forbidden | approved |
| D4 | Validation and test periods | validation 2024, locked test year 2025 | approved |
| D5 | Training window | expanding, from the first day with archived weather forecasts | approved |
| D6 | Refit frequency | weekly refit, daily update for online models | approved |
| D7 | Holiday definition | 11 national public holidays | approved |
| D8 | Bridge definition | Monday before a Tuesday holiday, Friday after a Thursday holiday | approved |
| D9 | Cold day definition | observed national daily temperature below 4.84 deg C (10th percentile of 2022-2023) | approved (amended 2026-10-05) |
| D10 | Bootstrap scheme | moving block bootstrap, 7-day blocks, 10 000 resamples, 14-day sensitivity check | approved |
| D12 | Daylight saving rows in the source | drop spring 02:xx artefacts, keep autumn rows as given, fill nothing | approved |
| D11 | Weather issue-time rule | NOAA GFS 0.25 deg, 00 UTC run of D-1, steps f021 to f047, same completeness test in backtest and production, fallback to the 18 UTC run of D-2 | approved |
| D13 | Hyperparameter tuning | inner walk-forward on 2023 only, never on 2024 or 2025 | approved |
| D14 | Modeling plan | `docs/modeling_plan.md` M1 to M12 as recommended | approved |
| D16 | Delivery deadline | inputs frozen at 12:00 Paris D-1, forecast computed after the freeze and delivered before 12:30 Paris D-1 | approved (2026-10-06) |

Fixed by the project contract, not open: cutoff at 12:00 Europe/Paris on D-1, archived weather forecasts only,
UTC storage, metrics MAE, RMSE, MAPE, 80% interval coverage target of 78% to 82%,
weekly block bootstrap on paired daily errors.

## 1. Forecast task

- Target: French national electricity consumption (MW) from eCO2mix, every slot of delivery day D.
- Delivery day D: from 00:00 to 24:00 Europe/Paris local time. It has 24 hours on ordinary days,
  23 hours on the last Sunday of March, 25 hours on the last Sunday of October.
- Issue time (cutoff): 12:00 Europe/Paris on D-1. In UTC this is 11:00 in winter time and 10:00 in
  summer time. The cutoff for D is computed from the local date of D-1, not from D:
  for D = 2025-03-31 the cutoff is 2025-03-30 10:00 UTC, because D-1 is already in summer time.
- Horizon: from 12 hours (D 00:00) to 36 hours (D 24:00) after the cutoff.

### D1. Target resolution

Decision: 30 min (48 slots on an ordinary day).
Reason: it keeps the daily shape (morning ramp, evening peak) while staying light enough for weekly
refits of a GAM, and it is the usual resolution of the load forecasting literature.
Condition: I must report, for each column (consumption, RTE D-1, RTE D) and each year,
the share of filled values at 15 and 30 min. If 30 min is not complete, fall back to 1 hour.

Comparison rule: the model and RTE D-1 are scored on the same slots. If a slot is aggregated (for
example 15 min to 30 min), the actuals and RTE D-1 are aggregated with exactly the same rule.

## 2. What is known at the cutoff

A forecast for D may only use information whose publication time is strictly before the cutoff.
A value published exactly at the cutoff is excluded.

### 2.1 Weather (D11)

Weather inputs are archived weather forecasts, never observations or reanalyses.
Source analysis: `docs/weather_sources.md` (accepted after three review rounds).

Decision: weather inputs for delivery day D come from the NOAA GFS 0.25 degree 00 UTC run of D-1,
steps f021 to f047 (f021 only to difference DSWRF). DSWRF is the hourly mean rebuilt as
k*A_k - (k-1)*A_(k-1) from the running mean since the last 6-hour reset. For TCDC the instantaneous
record is used. Hourly values are linearly interpolated to the 30 minute grid with steps of the same run
only. An hourly mean F(t-1 to t) is timestamped at t - 30 min before interpolation.

Completeness test: a run is complete at the cutoff if every needed step file and its .idx exist with
S3 LastModified strictly before the cutoff in UTC. Backtest and production apply this same test; the
backtest reads the archived LastModified.

Fallback: if the test fails, the 18 UTC run of D-2, steps f027 to f053, is used under the same test.
If that fails too, the day is reported as "weather missing" and handled by an explicit rule.
Every day stores run_init_time, source_url and a fallback flag, and every backtest report gives the
number of fallback days.

Reason: the 00 UTC run is fully published about 3.7 hours after its start, more than 6 hours before the
summer cutoff (observed on the sampled dates). The 06 UTC run lands only 11 to 19 minutes before the
summer cutoff and is kept as a later one-change experiment. Using the same run, the same steps and the
same completeness test in backtest and production keeps the backtest weather exactly as good as the
production weather, no better.

### 2.2 Consumption (D2)

eCO2mix publishes near real-time values, but the historical tables do not store when each value was
published. The backtest must therefore assume a publication delay.

Decision: a 2 hour publication delay. The last usable consumption slot ends at or before cutoff - 2 h.
Reason: it is a safe margin with almost no loss of information, and it avoids the leak that a too
short assumed delay would create.
Condition: in production the collector stores the ingestion time, so after one month the real delay
can be measured and the rule relaxed through an experiment.

Consequence for features: a "same slot yesterday" lag (D-1 at the same hour) is NOT available for most
slots of D. For a slot at 18:00 on D, D-1 18:00 is after the cutoff. Allowed lags are values at or
before the last usable slot, for example the same slot on D-2 or D-7, or the last known value at the cutoff.

Revisions: training uses consolidated values, even for recent days that were only real-time values at
the cutoff. Old vintages are not archived, so this small leak cannot be removed. I document it and,
once production has run for a while, I measure the gap between real-time and consolidated values.

### 2.2b Daylight saving rows in the source (D12)

Facts I measured in `docs/eco2mix_facts.md` and checked in its review:
- Spring change: every year the consolidated source holds two records for 01:00 to 01:45 UTC, one labelled
  02:xx local time and one 03:xx. 02:xx does not exist that night, and the RTE D-1 values of the 02:xx
  rows are frozen. The 02:xx rows are artefacts.
- Autumn change: 00:00 to 00:45 UTC is missing every year. The source labels the remaining rows as the
  second 02:xx hour (winter time); the consumption pattern on five changeover days suggests the first.
  This is not proven.

Decision: the clean layer drops the spring 02:xx artefact rows and keeps every autumn row at the time
the source gives. Nothing is filled or interpolated. The missing half-hours are excluded from scoring
for both the model and RTE D-1, as section 4 already requires, and are listed in every report.
Reason: no invented value, one documented rule, identical treatment of the model and RTE.
Follow-up: after the 2026-10-25 change, check on the real-time dataset whether the autumn rows are
the first or the second 02:xx hour. If the first, propose shifting them one hour earlier as a separate,
reviewed change.

### 2.3 RTE forecasts as inputs (D3)

Decision: RTE forecasts are forbidden as model inputs.
Reason: the comparison with RTE stays a clean contest between two independent forecasts, and the
history has no publication timestamps that would allow a leak-free use of them.

Fairness note: RTE's D-1 forecast may be published or updated after 12:00 on D-1. If so, RTE uses more
recent information than we do. The comparison is then conservative for our model. I should report
what is known about the publication time of the D-1 forecast.

### 2.4 Delivery deadline (D16)

The 12:00 Paris cutoff on D-1 is an information cutoff, not a delivery time.
At 12:00 the production run freezes its inputs with exactly the backtest rules: consumption under D2,
the weather run selected by the D11 completeness test evaluated at 12:00, and the calendar.
The forecast is computed after the freeze and must be delivered before 12:30 Paris on D-1.
Collection may start earlier, but no input selection is made before 12:00, so production and backtest
use the same information. If the forecast is not ready at 12:30, the fallback forecast (EXP-002 calendar
model, then seasonal naive) is served and the run is flagged as late.

## 3. Walk-forward backtest

The backtest replays production: for each day D, the forecast function is called with
`as_of = cutoff(D)` and may read only what is known at that moment. The same function serves
production, so the backtest tests the real code path.

### D4. Validation and test periods

Decision: validation year 2024, locked test year 2025.
Reason: tuning and model choice never see the test year, so the headline number is honest.
Calendar years keep one full winter in each period and make the results easy to explain.
All experiments (EXP files) report on the validation year 2024. The test year 2025 is run only for
milestone models chosen before looking at it, and every test run is recorded in my private test log so the
number of looks stays visible.

Condition: if my weather source analysis shows archived weather forecasts start in 2022, the 2024 validation trains on
2022 to 2023, which includes the 2022 to 2023 energy crisis drop in consumption. This is a real
regime change that the models must handle, not a reason to drop those years.

### D5. Training window

Decision: expanding window from the first usable day (first day with archived weather forecasts).
Reason: it gives the most data and stable estimates of yearly seasonality. Slowness to forget old
regimes is handled by the adaptive part of the project (Kalman filter on GAM effects), the approach
of the state-space literature on French load (de Vilmarest and Goude).
Condition: a rolling window of 3 years is tested later as a one-change experiment against this default.

### D6. Refit frequency

Decision: weekly refit of batch models (every Monday), daily state update for online models
(Kalman filter, aggregation weights).
Reason: good trade-off between cost and freshness. A daily refit would mean 365 GAM fits per
backtest year, and batch models only miss up to 6 days of data.
Condition: a refit at cutoff c uses only data available before c (section 2), and its model
serves every day until the next refit.

### 3.1 Other backtest rules

- No shuffling, no random cross-validation. Hyperparameters are tuned by an inner walk-forward on
  2023 only, never on the validation year 2024 or the test year 2025 (D13, amended 2026-10-05:
  tuning on 2024 and reporting 2024 would make the validation numbers optimistic).
- Online updates (Kalman state, aggregation weights) at the cutoff of D use only losses and
  observations known at that cutoff. Day D-1 is not complete at 12:00 on D-1, so the last full day of
  losses is D-2 (plus the morning of D-1 if the model works slot by slot).
- Random seeds are fixed and logged in MLflow.
- In the backtest every day must be forecast. A missing forecast is a bug, not a dropped day.
  In production a missing forecast is replaced by the seasonal naive forecast and logged.

## 4. Metrics

Evaluation set: the slots where the actual value, the model forecast and RTE D-1 all exist.
The number of dropped slots and their dates are reported.
Actual values: the latest published version (definitive, else consolidated). The version is logged.

For slots t = 1..n with actual y_t and forecast f_t:
- MAE = mean |y_t - f_t| (MW)
- RMSE = sqrt(mean (y_t - f_t)^2) (MW)
- MAPE = 100 * mean |y_t - f_t| / y_t (%). Safe here because national load never approaches zero.

Daily MAE: MAE over the slots of one local day (46, 48 or 50 slots at 30 min). Used for significance.

Reported for the model and RTE D-1 on the same slots: MAE, RMSE, MAPE overall, and MAE by day type.

### 4.1 Probabilistic forecasts

- The 80% interval is [q0.1, q0.9]. Coverage = share of slots where q0.1 <= y_t <= q0.9.
- Reported overall and by season: winter (Dec, Jan, Feb), spring (Mar to May), summer (Jun to Aug),
  autumn (Sep to Nov). Also mean interval width (MW) and pinball loss at 0.1, 0.5, 0.9.
- Coverage is reported with a 95% block bootstrap interval (section 6). Errors are strongly
  autocorrelated, so a year of slots carries far fewer independent observations than its count.
  A well calibrated model can land outside 78% to 82% by chance; the interval shows whether it does.
- Any calibration step (for example conformal residual quantiles) uses only out-of-sample residuals
  known at the cutoff.

## 5. Day types

Day types are evaluation labels, not features. They are defined on the local date of D.
Because they are labels, they may use observed temperature. Their thresholds are fixed before
the test year is evaluated and never changed after.

Precedence (one type per day): DST > holiday > bridge > cold > ordinary.
Cold is also reported as a flag on all days, so a cold holiday still counts in the cold MAE.

Expected sample sizes per year are small (computed for 2022 to 2026):
2 DST days, 7 to 10 holidays on weekdays (11 in total), 2 to 5 bridge days.
For DST, holiday and bridge, MAE is descriptive only. Significance is tested only overall and on
types with at least 20 days.

### 5.1 DST

The two local days with 23 or 25 hours: last Sunday of March and last Sunday of October.
Note: on 2024-03-31 DST coincided with Easter Sunday, which is not a public holiday.

### D7. Holiday

Decision: the 11 national public holidays.
Reason: the list is official, unambiguous and known years in advance. It does not capture the
Christmas week effect, so Dec 24 to Dec 31 is reported in a separate diagnostic table, not as a day type.

The 11 holidays: Jan 1, Easter Monday, May 1, May 8, Ascension Thursday, Whit Monday, Jul 14, Aug 15,
Nov 1, Nov 11, Dec 25. Whit Monday is a public holiday even though many employers work it
(solidarity day). Alsace-Moselle holidays (Good Friday, Dec 26) are regional and ignored at the
national level.

### D8. Bridge

Decision: a bridge is the Monday before a Tuesday holiday or the Friday after a Thursday holiday.
Reason: it is the standard French "pont" and is deterministic. Long bridges (for example around a
Wednesday holiday) are ignored on purpose.
In 2025 this gives 5 days: May 2, May 9, May 30, Nov 10, Dec 26.

### D9. Cold

Temperature: national daily mean of observed station temperatures (amended 2026-10-05, modeling plan
M12). Observations are allowed here because this is a label, never a model input. The station source
is chosen by a research task.

Decision: a day is cold when its national daily mean temperature is below the 10th percentile of the
daily means over a reference period.
Reason: a percentile gives enough cold days for the cold MAE to rest on more than a handful of days,
and a threshold fixed in advance cannot be tuned on the results.
Condition: the reference period is the full calendar years 2022 and 2023 (amended 2026-10-05, so the
validation year 2024 never shapes the threshold).
The threshold is computed once, before the test year is evaluated, and its value is written here.
Value (fixed 2026-10-05, `docs/observed_temperature.md`): 4.84 deg C. Source: Meteo-France SYNOP,
the 12 stations nearest to the points of `weather.py:POINTS`, equal weights. Coverage rule: a station-day
counts only with all 8 synoptic reports (00 to 21 UTC every 3 hours), and a day is labelled only if at
least 10 of the 12 stations count; other days are reported as label missing. 2022-2023 give 725 labelled
days and 73 cold days. The number of cold days varies with the year and is reported with the results.

### 5.2 Ordinary

Every day that is not DST, holiday, bridge or cold. Weekends are included.

## 6. Significance

Comparison of a model M with a reference R (another experiment or RTE D-1):
1. Compute the daily MAE of M and R on the same slots, and the daily difference d = MAE_M - MAE_R.
2. Report the mean of d (negative means M is better).
3. Build a 95% interval by block bootstrap of the daily differences (D10).
4. If the interval contains 0, write "not significant".

### D10. Bootstrap scheme

Decision: moving block bootstrap (Kunsch 1989) with blocks of 7 consecutive days, 10 000 resamples,
percentile interval, fixed seed.
Reason: it uses every possible block and keeps the weekly and weather autocorrelation of daily errors.
Condition: a sensitivity check with 14-day blocks is reported for the headline comparison with RTE,
because cold spells can last more than a week.

## 7. Leakage checklist

Every model, feature pipeline and experiment is checked against this list. Reviewers tick it.

1. Every input for day D has a publication time strictly before cutoff(D).
2. Weather inputs are archived forecasts issued and available before the cutoff (section 2.1);
   never observations, reanalyses or forecasts from a later run.
3. Consumption inputs end at the last usable slot (section 2.2). No "same slot yesterday" lag.
4. RTE forecasts are not inputs (D3).
5. Every fitted object (scaler, encoding, mean, smoothing parameter, model, residual quantile)
   is fitted on data available before the cutoff of the first forecast it produces.
6. Hyperparameters and model choices come from the validation period only; the test year is not
   looked at during development.
7. Online updates (Kalman, aggregation weights) use only losses known at the cutoff (last full day D-2).
8. SQL and dbt models use no forward-looking windows: no `lead()`, no centered rolling means,
   no window frame that includes rows after the current one.
9. Timestamps are stored and joined in UTC; local time is used only for calendar features and labels.
   DST days have 23 and 25 hours and are tested.
10. Calendar features (holidays, bridges, day of week) are allowed: they are known years in advance.
11. Truncation test: features built for D from the full database equal features built from a copy
    truncated at cutoff(D). This one test catches most leaks at once.
12. Known accepted leak: consolidated values replace real-time values in training (section 2.2).

Automated tests for items 1 and 9 are specified in `docs/leakage_tests.md`.
Tests for items 3 and 11 are added once D2 is decided and the feature pipeline exists.
