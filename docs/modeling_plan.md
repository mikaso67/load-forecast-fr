# Modeling plan

Status: approved on 2026-10-05 (D14); the model ladder is completed through EXP-012 and the 2025 final test.
Scope: features, model ladder, experiment sequence and the D9 cold threshold for the validation year 2024.
`docs/protocol.md` takes precedence over this file.

## 0. Decision summary

| # | Choice | Recommendation | Status |
|---|--------|----------------|--------|
| M1 | Time base of calendar features and model slots | local Europe/Paris wall-clock half-hour | approved 2026-10-05 |
| M2 | Holidays and bridges as features | holiday coded as a Sunday, one bridge indicator, one Christmas-week indicator | approved 2026-10-05 |
| M3 | Lagged consumption | same local slot of D-2 and D-7, plus the last usable value of D-1 | approved 2026-10-05 |
| M4 | Temperature features | heating and cooling degrees, then a smoothed temperature, from the national mean | approved 2026-10-05 |
| M5 | Structure of batch models | one model per local half-hour slot (48 models) | approved 2026-10-05 |
| M6 | GAM library | statsmodels `GLMGam` (penalized B-splines), smoothing chosen on training data only | approved 2026-10-05 |
| M7 | Online correction | Kalman filter on the GAM effects, variances fitted on training data, update with full day D-2 | approved 2026-10-05 |
| M8 | Aggregation | ML-Poly on GAM, GAM+Kalman, LightGBM and linear experts | approved 2026-10-05 |
| M9 | Prediction intervals | rolling split-conformal residual quantiles per slot | approved 2026-10-05 |
| M10 | Hyperparameter tuning | inner walk-forward on 2023, never on 2024 itself | approved 2026-10-05 (D13) |
| M11 | Weather-missing days | weather-free backup model for that day, flagged in reports | approved 2026-10-05 |
| M12 | D9 cold threshold | label temperature source, then reference period 2022-2023 | approved 2026-10-05 (D9 amended) |

## 1. Facts this plan rests on

All checked in this session or in accepted tasks.
- Cutoff for D: 12:00 Europe/Paris on D-1. Under D2 the last usable consumption slot starts at
  09:30 local on D-1 (2024-01-15: cutoff 2024-01-14 11:00 UTC, last slot start 08:30 UTC;
  2024-07-15: cutoff 10:00 UTC, last slot start 07:30 UTC). Computed with `forecast_cutoff` and
  `last_usable_slot_start`.
- Weather starts 2021-03-23 (D11). The first 2024 refit (Monday 2024-01-01) trains on 1014 days,
  which contain 22 weekday holidays and 7 bridge days. The 2022-2023 crisis drop sits inside them.
- My weather scan: 2 weather-missing days (2021-03-23, 2022-12-01) and 4 fallback days, none in 2024.
- Reference to beat on 2024: RTE D-1 MAE 1364.6 MW. EXP-001 seasonal naive: MAE 3249.4 MW.
- None of statsmodels, scikit-learn, scipy, LightGBM or pyGAM is installed in the shared venv today.
  Every library choice below has the condition "installs on Python 3.13 Windows, pinned".

## 2. Candidate features

Rule for every feature: its value for any slot of D is a function of information published strictly
before cutoff(D) (protocol section 2 and checklist section 7). Every fitted part of a feature
(degree-day base, smoothing constant, encoding) is fitted inside the training window of the refit.

### 2.1 Calendar (known years in advance, checklist item 10)

| Feature | Definition | Why valid at the cutoff |
|---------|------------|-------------------------|
| Local slot | index 0 to 47 of the local half-hour | deterministic from the timestamp |
| Day of week | Monday to Sunday, local date of D | deterministic |
| Position in year | Fourier terms (linear) or cyclic spline (GAM) on local day of year | deterministic |
| Holiday | D7 list, `is_holiday` in `evaluation.py` | official list, known years ahead |
| Bridge | D8 rule, `is_bridge` | deterministic rule |
| Christmas week | local date in Dec 24 to Dec 31 | deterministic |
| DST day | `is_dst_day` | deterministic |

### M1. Time base of calendar features

Options:
- A. Local wall-clock half-hour (recommended). Behaviour (wake-up, work, lighting, cooking) follows
  local clock time, so a model indexed by local slot keeps the 07:00 ramp at slot 14 all year.
- B. UTC half-hour. Simpler joins, but every behavioural peak moves by one slot twice a year, so the
  model must learn two profiles from half the data each.

Consequences of A: a 23-hour day has no slots for local 02:00 and 02:30; a 25-hour day has local 02:00
and 02:30 twice, and both use the model of that local slot. Storage and joins stay in UTC; the local
slot is a derived column. My local-lag addendum measures the same question for the naive baseline (UTC versus local lag).

### M2. Holidays and bridges as features

The training window has only 22 weekday holidays and 7 bridges, so one effect per holiday is not estimable.
Options:
- A. Holiday coded as a Sunday in the day-of-week feature, plus one bridge indicator and one
  Christmas-week indicator (recommended). I measured the paired holiday drop (6.54 GW over 32
  holidays) close to the paired Sunday drop (6.45 GW over 197 Sundays), so the Sunday profile
  is a reasonable prior for holidays.
- B. One holiday indicator, separate from day of week. One more parameter per slot, estimated from 22 days.
- C. One indicator per holiday. Not estimable with 2 or 3 occurrences each.
Known limits of A: August and school holidays are left to the day-of-year term; school holiday
calendars (published in advance) are a later one-change experiment if a source is found.

### 2.2 Lagged consumption (D2)

### M3. Lagged consumption

The same slot of D-1 is after the cutoff for every slot of D (D-1 is known only up to 09:30 local).
Allowed values: the whole of D-2 and earlier, and D-1 up to 09:30 local.
Options:
- A. Same local slot of D-2 and of D-7, plus the last usable value (D-1, 09:30 local slot)
  (recommended). D-2 brings the current level, D-7 the same weekday, the last value the most recent level.
- B. Same slot of D-7 only. Robust to day-type changes, but loses the most recent level.
- C. No lag; leave level tracking to the Kalman step (M7). Cleanest for the GAM + Kalman design, where
  lags and the online correction partly do the same job.
Caveat for A and B: when D-2 or D-7 was a holiday or bridge, the lag is low for a reason that does not
apply to D. Add the day type of the lag day as an indicator, or replace a holiday lag by the nearest
ordinary same weekday. The lag also comes from consolidated data in the backtest (accepted leak, item 12).
The DST mapping of a lag follows M1: the same local slot, matched through UTC timestamps, never by row offset.

### 2.3 Weather (D11)

Only the national temperature exists now (equal-weight mean of 12 GFS points, interpolated to
30 minutes with steps of the selected run only). RH, wind, radiation and cloud cover come with the
second backfill pass and are later experiments.

Why training on forecasts rather than observations is right here: the model learns the relation between
consumption and forecast temperature, with the same forecast error it will meet in production.
A model trained on observed temperature would see sharper inputs in training than in production.

### M4. Temperature features

| Feature | Definition | Validity |
|---------|------------|----------|
| T | national temperature at the slot, run selected for D | run published before the cutoff (D11) |
| HDD | max(0, b_h - T) | b_h fixed a priori or fitted on the training window |
| CDD | max(0, T - b_c) | same |
| Smoothed T | T_s(t) = a * T_s(t-1) + (1 - a) * T(t), over each day's own selected run | see below |
| Daily min, max of T over D | from the same run | same run, known at the cutoff |

Smoothed temperature models the thermal inertia of buildings. For past days it chains each day's own
selected run (the run selected for that day, published before that day's cutoff, so before cutoff(D)).
It never uses the run for D-1 from today's download, and never an observation. On a weather-missing day
the state is carried forward without update.

Options for the degree-day bases:
- A. Fixed a priori (for example 15 C heating, 20 C cooling), stated as assumptions (recommended for the
  linear rung). No fitting, no risk of tuning on 2024.
- B. Fitted per refit on the training window by profile least squares over a grid.
- C. No bases at all: the GAM learns the shape of the temperature effect (this is the GAM rung).
Population weights for the national temperature stay a later one-change experiment.

### 2.4 Forbidden, for the record

RTE forecasts (D3); observed weather or reanalysis as a feature; the same slot of D-1; any lag whose
value is published at or after the cutoff; centered or forward-looking windows; scalers or encoders fitted
on the full series; a crisis dummy whose break date was chosen by looking at data after the refit date.

## 3. Model ladder

Each rung uses the features above. One rung, one change.

### Rung 0. Seasonal naive (done, EXP-001)
Same UTC half-hour 7 days earlier, D2 applied. Local-time variant in the baseline addendum.

### Rung 1. Linear regression

### M5. Structure of batch models

- A. One model per local half-hour slot, 48 models (recommended). Each slot has its own daily profile,
  weekday effect and temperature sensitivity. Each model fits about 1000 rows, which is cheap for weekly
  refits, and the coefficients are easy to explain slot by slot. This is the structure of the EDF GAM
  literature on French load.
- B. One global model with slot interactions. Fewer parameters to share information between neighbouring
  slots, but the interaction design is long and harder to read.
For LightGBM (rung 3) the global form B is the natural one: trees learn slot interactions themselves.

Linear specification: day of week, Fourier terms of day of year (order 3), bridge and Christmas indicators,
HDD and CDD (M4 option A), then lags (M3). Ordinary least squares.

### Rung 2. GAM

Per slot: y = sum of smooth effects of T and smoothed T, cyclic effect of day of year, day-of-week factor,
indicators, optional lags. The temperature effect is learned as a curve, so no degree-day base is needed.

### M6. GAM library

- A. statsmodels `GLMGam` with penalized B-splines (recommended). Maintained, pure Python, separate
  penalty per term, partial effects available for the Kalman step. Smoothing weights chosen by GCV
  or by M10 on training data. Less automatic than mgcv: no REML, fewer smooth types.
- B. R `mgcv` through rpy2 or an R script. The reference implementation (REML smoothing selection,
  tensor products, `bam`). Adds R to the venv, Docker image and CI, and a second language to explain.
- C. pyGAM. Simple API, but releases are infrequent; Python 3.13 support must be checked before use.
Condition for A: if `GLMGam` cannot fit 48 slot models per weekly refit in reasonable time, or its
smoothing selection is unstable, the fallback is a hand-written penalized spline regression on
scikit-learn `SplineTransformer` features, which is about 40 lines.

### Rung 3. LightGBM

One global model, slot as a feature, the same features as the best GAM. L1 objective, since MAE is scored.
Known limit: trees do not extrapolate. A cold spell colder than any training day, or a level outside the
training range after a structural change, is predicted at the edge of the training values. Option to test
later: fit LightGBM on the GAM residuals instead of the load.
Hyperparameters (learning rate, leaves, rounds) follow M10.

### Rung 4. Online Kalman correction

### M7. Online correction

State-space model per slot (de Vilmarest and Goude, already cited in protocol D5):
y_t = theta_t' x_t + e_t, theta_t = theta_(t-1) + w_t, where x_t holds the GAM partial effects of that
slot (and an intercept). With Q = 0 this is recursive least squares; with Q > 0 the effects drift,
which is how the model can follow the 2022 crisis drop and the partial recovery.
Options:
- A. Static filter (Q = 0) started from the GAM fit. Few parameters, slow adaptation.
- B. Dynamic filter, diagonal Q and noise variance fitted by maximum likelihood on the training window
  (recommended). Adapts to level and sensitivity changes; the variances are the only fitted parameters.
- C. Intercept-only local level on the GAM residual. The simplest adaptive step, easy to explain,
  ignores changes in temperature sensitivity.
Timing (checklist item 7): at cutoff(D), every slot filter is updated with the full day D-2 only.
Using the morning of D-1 for early slots is a later one-change experiment. The GAM is refitted weekly
(D6); after each refit the filter restarts from the new effects, or keeps its state on the new effects:
this choice is tested inside rung 4, before the first EXP of the rung is reported.

### Rung 5. Aggregation of experts

### M8. Aggregation

Experts: linear, GAM, GAM + Kalman, LightGBM. Weights updated daily with losses known at the cutoff
(full day D-2), per slot or shared across slots.
- A. ML-Poly (Gaillard, Stoltz and van Erven, 2014) with the gradient trick (recommended). No learning
  rate to tune, which removes a tuning risk; about 30 lines in `src/`.
- B. Exponentially weighted average. Simplest, but its learning rate must be tuned (M10).
- C. Fixed weights fitted on 2023 out-of-sample forecasts. Not adaptive.
Condition: the experts need out-of-sample forecasts before 2024 for a burn-in, so their backtests start
on 2023-01-01, trained from 2021-03-23. Aggregation only helps if expert errors are not too correlated;
the EXP reports the correlation of daily errors between experts.

### Probabilistic layer

### M9. Prediction intervals

Target: the 80% interval covers 78% to 82% (overall and by season, with a block bootstrap interval).
- A. Rolling split-conformal: for each slot, [q0.1, q0.9] of the out-of-sample residuals of the last
  N days known at the cutoff (last full day D-2), added to the point forecast (recommended).
  Distribution-free and simple. N is chosen by M10. Adaptive conformal inference (Gibbs and Candes, 2021)
  is a later one-change experiment if coverage drifts across seasons.
- B. Quantile models (quantile GAM or LightGBM quantile objective) for q0.1 and q0.9. Conditional width,
  but two more models per expert and no coverage guarantee.
- C. Gaussian Kalman predictive variance. Free with rung 4, usually too narrow when the noise is not Gaussian.
Residuals used for calibration are out-of-sample only (protocol 4.1).

## 4. Tuning rule

### M10. Hyperparameter tuning

Protocol 3.1 says hyperparameters are tuned by walk-forward on the validation period, and the project contract
section 6 says nothing is tuned on the evaluation period. Read together they leave a gap: tuning on
2024 and then reporting 2024 makes every validation number optimistic.
- A. Inner walk-forward on 2023 (recommended). Hyperparameters (smoothing weights if not chosen by GCV,
  LightGBM settings, Kalman variances if not fitted by likelihood, conformal window N) are chosen on
  forecasts for 2023 trained from 2021-03-23, then frozen and applied to the 2024 backtest. 2024 is used
  only to compare experiments, as the project contract requires.
- B. Tune on 2024 as protocol 3.1 reads literally. More data for tuning, optimistic 2024 numbers,
  and the gap only shows on the locked 2025 test.
Accepted residual risk with A: choosing between experiments on 2024 is still selection on 2024.
That is why 2025 stays locked and is evaluated only for milestones chosen in advance.
If A is approved, protocol 3.1 needs one sentence changed (when I update the protocol).

## 5. Weather-missing days

### M11. Explicit rule required by D11

- A. On a weather-missing day, forecast with the weather-free backup model (rung 1 with calendar and
  lags only), refitted with the other models, and flag the day in every report (recommended).
  Every day is still forecast (protocol 3.1).
- B. Use the seasonal naive forecast, as protocol 3.1 already does for a missing production forecast.
  Simpler, much worse (EXP-001).
- C. Fall back to an older run (12 UTC of D-2). Needs a D11 change and a new completeness test.
In training, weather-missing days are left out of the fit for weather models. None falls in 2024.

## 6. Experiment sequence (validation year 2024)

A new model family is the single change when its reference is the previous rung with the same features.
Inside a family, one feature or setting changes at a time. Each EXP reports the full evaluation requirements (original contract section 6)
set, including MAE by day type and the paired bootstrap against both its reference and RTE D-1.

| EXP | Single change | Reference | Needs |
|-----|---------------|-----------|-------|
| EXP-002 | rung 1 linear per slot, calendar only (M1, M2, M5) | EXP-001 | backtest runner |
| EXP-003 | + HDD and CDD, fixed bases (M4 A) | EXP-002 | national temperature, TMP backfill to 2024-12-31, truncation test |
| EXP-004 | + lags (M3 A) | EXP-003 | |
| EXP-005 | rung 2: GAM, same features as the better of EXP-003 and EXP-004, smooth T instead of degrees | that EXP | M6 library installed |
| EXP-006 | + smoothed temperature | EXP-005 | |
| EXP-007 | rung 4: Kalman correction of the best GAM (M7 B) | best of EXP-005, EXP-006 | |
| EXP-008 | rung 3: LightGBM, same features as the best GAM | best GAM | M10 tuning on 2023 |
| EXP-009 | rung 5: ML-Poly aggregation of the experts above | best single expert | 2023 expert backtests |
| EXP-010 | intervals: rolling conformal (M9 A) on the best point model, point forecast unchanged | same model, no interval | |

Later one-change experiments already named elsewhere: population-weighted temperature, rolling 3-year window
(D5), 06 UTC run (D11), other weather variables, morning of D-1 in the Kalman update, relaxed D2 delay
once production measures it.

Milestones for the 2025 test run: chosen by the developer before any 2025 look, recorded in my private test log.
My suggestion is two at most: the best GAM + Kalman with intervals, and the aggregation.

## 7. D9: fixing the cold threshold

D9 requires a national daily mean temperature, a 10th percentile over a reference period, and one
threshold fixed before the test year is evaluated. Two things are open.

### M12a. Which temperature labels a day as cold

D9 allows observations because a label is not a feature. The project has no observation source yet.
- A. The national temperature (GFS forecast, run selected for D), daily mean over the local day.
  Available now, no new source. But it labels "forecast cold", not "cold": a cold day forecast as mild
  is not labelled cold, and those are the days where both forecasts fail. Fair between the model and RTE,
  but not the D9 definition.
- B. Station observations (Meteo-France synoptic data, published as open data) over a fixed station list,
  population weighted as D9 says (recommended). True cold days. Needs a source research task
  and a labels-only collector, stored apart from features so no feature query can join it.
- C. GFS analysis (step f000 of the 00, 06, 12 and 18 UTC runs of D) at the same 12 points. Same archive
  and decoder, close to observations, but a model analysis with known 2 m temperature biases, and a new backfill.
Update: I chose and implemented Option B with Meteo-France SYNOP station observations. The threshold 4.84 deg C was fixed on 2022-2023 (M12b option A). The provisional forecast-temperature label (Option A) remains only as historical context in early experiment logs.

### M12b. Reference period, from training data only

- A. Full calendar years 2022 and 2023 (recommended). Strictly before the validation year, so it is
  training data for every 2024 forecast. Whole years keep every season equally represented: the period from
  2021-03-23 holds three springs to autumns but only two Januaries to Marches, which would push a 10th
  percentile upward (warmer). About 730 daily means, so about 73 days fall below the threshold.
- B. 2022 to 2024, as D9 reads today ("all years before the test year"). One more winter. It uses the
  validation year, which is acceptable for a label but contradicts my original "training data only" requirement.
- C. From 2021-03-23 to 2023-12-31, every day. More days, but the season imbalance above.
Procedure: compute the threshold once, with the M12a source, on the M12b period, in a reviewed task;
write its value in protocol D9; then apply it unchanged to 2024 and 2025. The cold label is never a
model feature; temperature enters models only through section 2.3.
If A is approved, protocol D9 needs its reference period sentence changed (when I update the protocol).

## 8. Engineering needed before the sequence

Proposed tasks, for me to plan:
1. Backtest runner: weekly refit every Monday, daily forecast at `as_of = cutoff(D)`, one
   forecast function shared with production (protocol section 3), evaluation through `evaluation.py`.
2. Feature pipeline and the truncation test of checklist item 11, before EXP-003.
3. Label temperature source research and collector if M12a B is chosen.
4. TMP backfill complete to 2024-12-31 before EXP-003; 2023 complete before any M10 tuning.
