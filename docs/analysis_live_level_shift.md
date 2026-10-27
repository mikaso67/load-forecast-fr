# Live level shift after the 2026-10-19 refit

Analysis of 2026-10-26. Read-only: production states, refits and frozen inputs were read from the
runtime folder, every rerun ran on a copy outside it, nothing was written to the database.
The replays of the production chain use its own history, including 2025 for the interval pool.
No decision and no choice of this analysis rests on 2025, which remains the test year already
consumed. All numbers come from `scripts/analyze_live_level_shift.py` (section 8).
Update of the same day: the 2026 SYNOP observations now run to 2026-10-25, so section 5 compares the
GFS temperature used by production with the observed one.

## 1. Question

Against real-time actuals, the daily mean error (forecast minus actual, MW) moved down for both
variants from the delivery of 2026-10-19, the first day of the 2026-10-19 weekly refit.
"Before" is 10-13 to 10-17. "After" is the clean days 10-19, 10-20, 10-23 and 10-24.
10-21 and 10-22 ran on the backup chain, 10-25 is not scored (clock change) and 10-26 is incomplete.

| Mean daily error (MW) | before | after | change |
|---|---|---|---|
| Variant B, served aggregate | +1,153 | -320 | -1,474 |
| Variant A, shadow aggregate | +229 | -1,241 | -1,470 |
| RTE D-1 | +77 | -277 | -354 |

I tested four explanations: the refit itself, a change in the real-time series, a real change
in demand, and an error of the GFS temperature input. The last one is now settled (section 5).

## 2. The refit is not the cause

**Exact reproduction first.** I loaded the stored states, refits and frozen inputs and reran the
production function on a copy, in both variants, and compared with the stored forecasts. Three checks,
all to 0.000000 MW (42 in total):
- the three stateless experts (EXP-004, EXP-005, EXP-008) on each of the 11 stored days from 10-15
  to 10-27 with the weekly models in force (22 checks);
- all four experts and the aggregate over the chain from the stored 10-17 state, on 10-19 to 10-24
  (12 checks);
- all four experts and the aggregate for one step from the stored state of the previous day, on
  10-16, 10-17, 10-26 (10-26 refit adopted) and 10-27 (8 checks).

EXP-007 and the aggregate of 10-15 are not checked: the state of 10-14 is not stored.

**Same inputs, older models.** I forecast every day with the weekly models of 10-12, 10-19 and
10-26 on the same frozen inputs. Over the days where two model sets apply, the mean level moves as
follows when the next week's refit replaces the previous one:

| Variant | EXP-004 | EXP-005 | EXP-008 |
|---|---|---|---|
| A | -10 MW | -8 MW | -140 MW |
| B | 0 MW | +1 MW | -38 MW |

The OLS and GAM refits change the level by about 10 MW. LightGBM moves individual days by up to
about 500 MW in either direction, but its mean change is small. The 10-26 refit, the third point,
behaves the same way.

**Full chain without the refit.** I reran the whole chain from the stored 10-17 state twice. The
factual run adopts the 10-19 refit. The counterfactual keeps the 10-12 models and the running
Kalman filter, with no restart. Mean error on the four clean days:

| Variant | Scenario | EXP-004 | EXP-005 | EXP-007 | EXP-008 | Aggregate |
|---|---|---|---|---|---|---|
| A | factual | -153 | -518 | -1,247 | -2,119 | -1,241 |
| A | no refit | -143 | -507 | -1,251 | -1,959 | -1,217 |
| B | factual | +440 | +48 | -304 | -943 | -320 |
| B | no refit | +439 | +50 | -307 | -872 | -312 |

Without the refit, the aggregate error would have been 24 MW (A) and 8 MW (B) smaller, against a
shift of about 1,470 MW. The Kalman restart changes EXP-007 by less than 5 MW. The ML-Poly weights
are the same in both runs.

**Training data and vintage mix.** Every frozen input from 10-15 to 10-27 holds consolidated rows
up to 2026-08-31 and real-time rows after that. The D21 offsets do not change between days (mean
+1,022 MW, estimated from July and August), and no September consolidated row is stored. The
Kalman variances were not refitted at 10-19, because 10-05 was already in the same quarter. The
10-19 refit therefore saw one more week of real-time rows than the 10-12 refit, and nothing else
changed. The counterfactual above shows that this extra week does not matter.

Confidence: high. The comparison is an exact rerun, not an estimate.

## 3. The real-time series did not change

- Vintages: from 10-05 to 10-24, 942 of the 960 half-hours have a single real-time vintage. The
  median revision is 0 MW and the mean is -14 MW.
- RTE D-1 against the same real-time actuals stays in its usual range. Its weekly mean error was
  between -245 and +401 MW from September to October. If the series had dropped by 1,500 MW, the
  RTE error would have jumped by the same amount.
- Revision gap, re-measured on the most recent consolidated months: +944 MW in July (7-day block
  95% interval [823, 1,028]) and +1,100 MW in August ([1,008, 1,180]), with weekly values between
  840 and 1,320 MW and no step. September and October are not consolidated yet. In any case, a
  revision gap moves the level by a constant and cannot create a step on one particular day.

Confidence: high that the step does not come from the series. The revision gap for September and
October remains unmeasured.

## 4. A real rise in demand, under-forecast by every expert

Real-time consumption rose sharply with the first cold spell of the season:

| Weekdays | actual (MW) | GFS temperature used (C) |
|---|---|---|
| 10-12 to 10-16 | 44,696 | 16.5 |
| 10-19 to 10-23 | 49,919 | 11.8 |

Comparing the clean weekdays 10-19, 10-20 and 10-23 with 10-12 to 10-16:
- actual consumption rose by 4,981 MW;
- RTE D-1 predicted a rise of 4,618 MW (93%);
- our aggregate predicted 3,772 MW (76%), in both variants.

The missing 1,200 MW is the shift.

The calendar-only model, which has no temperature and no lags, went from +3,009 MW to -1,337 MW
in variant A, a change of -4,346 MW. That is the size of the demand jump relative to what the
calendar alone expects. The pure temperature model (EXP-003, no lags) absorbed most of it but not
all: its error changed by -905 MW. Each expert's change in mean error, variant A:

| EXP-004 | EXP-005 | EXP-007 | EXP-008 | Aggregate |
|---|---|---|---|---|
| -588 | -1,357 | -1,368 | -1,973 | -1,470 |

Variant B gives the same picture (-590, -1,373, -1,436, -1,783, -1,474).

Sensitivity of the 10-19 models to a forecast 1 C colder, on 10-20, 10-23 and 10-24:

| EXP-003 | EXP-004 | EXP-005 | EXP-008 |
|---|---|---|---|
| 1,629 MW | 986 MW | 783 to 987 MW | 584 to 784 MW |

With these slopes, the gap would be closed by a GFS forecast about 1.5 to 2 C too warm, or by a
heating response at the first cold days that is steeper than the curve learned over all seasons.
Section 5 tests the first reading against observed temperatures.

The pattern already appears in the backtest. Weekly mean error against consolidated actuals:
- Week of 2023-10-22 (first cold week, 18 to 14 C): EXP-008 moved from -201 to -1,325 MW,
  EXP-007 from +295 to -287, and RTE D-1 from -481 to -1,386.
- Weeks of 2024-11-03 and 2024-11-10 (load up by 3,600 MW): every expert sat between -584 and
  -897 MW, after values between -127 and +816 MW in the two weeks before.

So the first heating weeks are a known weak spot of these models, and of RTE in 2023. The 2026
miss, about 1,470 MW, is larger than in those two autumns. It rests on four clean days, so I give
no interval: a 7-day block bootstrap would have a single block.

Confidence: high that this is a demand rise the experts under-forecast. Section 5 shows that the
weather input is not the reason.

## 5. The GFS temperature is not the reason

**Data.** The 2026 SYNOP observations (12 stations, the same cities as the 12 GFS points, 3-hourly)
run to 2026-10-25 21:00 UTC. At 1,643 of the 1,665 synoptic hours since 2026-03-31 all 12
stations report. The GFS side is the series production used: the pinned 00 UTC run of D-1, hourly
steps, equal-weight mean of the 12 points. I read it from the latest frozen input and checked it
against the 11 frozen inputs of 10-15 to 10-27 (maximum difference 0.000000 C). I compare GFS and
observation at the eight synoptic hours of each delivery day. 2026-10-18 and 2026-10-25 have no row
in `raw.gfs_days` but the frozen series holds their run, so I use it.

| Day | GFS (C) | Observed (C) | GFS minus observed (C) |
|---|---|---|---|
| 10-10 | 13.95 | 14.81 | -0.86 |
| 10-11 | 13.42 | 13.60 | -0.18 |
| 10-12 | 14.96 | 14.57 | -0.21 (7 hours) |
| 10-13 | 16.67 | 17.25 | -0.58 |
| 10-14 | 16.60 | 16.68 | -0.08 |
| 10-15 | 17.59 | 16.74 | +0.86 |
| 10-16 | 16.76 | 15.94 | +0.82 |
| 10-17 | 13.75 | 13.96 | -0.21 |
| 10-18 | 11.11 | 11.00 | +0.11 |
| 10-19 | 11.85 | 11.45 | +0.39 |
| 10-20 | 12.21 | 12.39 | -0.17 |
| 10-21 | 11.38 | 12.02 | -0.63 |
| 10-22 | 11.70 | 12.11 | -0.41 |
| 10-23 | 11.87 | 12.80 | -0.93 |
| 10-24 | 10.69 | 11.47 | -0.78 |
| 10-25 | 12.72 | 13.26 | -0.54 |

**The GFS was not too warm.** Mean GFS minus observed, in C, with the excess over the reference
period 2026-09-01 to 10-09 (-0.27 C on average, daily standard deviation 0.41 C; the station and
the grid cell differ by a stable offset):

| Window | Mean error | Excess over reference |
|---|---|---|
| 10-13 to 10-17 (before) | +0.16 | +0.44 |
| 10-19, 10-20, 10-23, 10-24 (after, clean days) | -0.37 | -0.10 |
| 10-19 to 10-24 | -0.42 | -0.15 |

The error of the clean days is ordinary: the daily errors of 2026-04-01 to 10-09 have overlapping
4-day means with median -0.94 C and 5% to 95% range [-1.62, -0.02], and -0.37 exceeds 78% of them. GFS also
described the cooling correctly. Weekdays 10-12 to 10-16 then 10-19 to 10-23: GFS -4.71 C
(16.52 to 11.80), observed -4.08 C (16.23 to 12.15). So GFS overstated the cooling by 0.6 C, which
pushed the forecast up, not down. To explain a 1,470 MW shift the GFS would have had to be 1.5 to
2 C too warm (section 4). It was 0.4 C too cold.

By point, the clean days differ from the reference mostly at Marseille (+1.92 C against +0.45), Nice
(-2.99 against -2.09) and Dijon (-1.25 against -0.49). The other nine points move by less than
0.7 C. Nice and Clermont-Ferrand (-2.82 against -2.30) carry large stable offsets, as before.

**Rerun with the observed temperature.** I replaced the GFS temperature by the observed one on the
whole chain (same states, 10-19 refit, Kalman and ML-Poly running). The national error at the
synoptic hours is interpolated linearly to the 30-minute slots and subtracted from the GFS series.
"Observed" uses the observed temperature. "Excess removed" subtracts only the error beyond the
reference mean of each synoptic hour, because the models learned on GFS inputs with their usual
offset. The GFS scenario reproduces the stored forecasts to 0.000000 MW. Mean error against
real-time actuals on the clean days:

| Variant | Temperature | EXP-004 | EXP-005 | EXP-007 | EXP-008 | Aggregate |
|---|---|---|---|---|---|---|
| A | GFS (production) | -153 | -518 | -1,247 | -2,119 | -1,241 |
| A | excess removed | -202 | -551 | -1,421 | -2,166 | -1,369 |
| A | observed | -540 | -814 | -1,661 | -2,388 | -1,611 |
| B | GFS (production) | +440 | +48 | -304 | -943 | -320 |
| B | excess removed | +392 | +14 | -477 | -996 | -445 |
| B | observed | +53 | -248 | -715 | -1,200 | -683 |

With the observed temperature the under-forecast gets larger, by 370 MW (A) and 363 MW (B), because
the clean days were on average 0.37 C warmer than GFS said. The rerun therefore gives about
1,000 MW per C, in line with the slopes of section 4.

**Between the two weeks.** From the before window to the after window, the GFS error moved by about
-0.5 C (colder than observed), which at 1,000 MW per C raised the forecast by about 500 MW. That is
a linear estimate, not a rerun. The shift of -1,470 MW happened in spite of it. With the same GFS
quality in both weeks it would have been about -2,000 MW.

**Verdict.** The GFS input is not the cause. The experts under-forecast the load even when they
receive the true temperature. The miss is in how the models turn this temperature into demand.
Confidence: high that the GFS error is not the cause, with the stated limits: four clean days, no
interval (a 7-day block bootstrap would have one block), SYNOP stations rather than the grid cells
that the model saw, and an equal-weight national mean that is also what production uses.

What "model response" covers here I cannot split further. It includes a heating curve that is too
flat at 10 to 14 C, because the learned curve averages all seasons; the delay with which a household
switches on heating after a first cool week; and any driver that the models do not see (wind, cloud
cover). One more point limits the reading. School holidays are not a feature of the production models, and
the Toussaint holidays of 2026 start on 10-17 in all three mainland zones, so the whole after window
is in holiday time. Unmodelled holidays would lower real demand and push the error the other way,
so the miss of 1,470 MW is, if anything, an underestimate of the missing heating response. I did
not measure the holiday effect. The backtest episodes of section 4 do not line up with the holidays
(2023: the week of 10-16 to 10-22 is before them, they start on 10-21; 2024: the week of 11-04 to
11-10 is after them, they end on 11-03), so holidays are not what drives the pattern.

## 6. Why A now looks worse than B

The July-August offsets apply from 10-11. From 10-13 on, B sits 870 to 980 MW above A every day,
before and after the shift. The D21 mapping lifts the real-time
lags and the online updates to the consolidated scale, so B forecasts the consolidated level.
Before 10-19 this made B too high against real-time actuals (+1,153 MW). After the demand surprise,
the same offset partly cancels the under-forecast (-320 MW). The ranking flipped because the error
moved, not because one variant reacted differently: both shifted by 1,470 MW. Against real-time
actuals B is expected to be about 1,000 MW too high (D21). Its small error now is therefore the
sum of two errors of opposite sign, not a sign of accuracy.

ML-Poly gives EXP-007 0.63 to 0.65 of the weight and EXP-004 none in both variants. EXP-004 was
the least affected expert after 10-19 (-588 MW in A), but the weights learned in a stable mild
period favoured the experts that were least biased then. With updates at D-2, ML-Poly cannot
re-weight within the first days of a regime change.

## 7. What I would change

These are proposals. None of them changes production code, settings or D21.

1. Change nothing in the refit, the training window, the Kalman restart or the mapping because
   of this episode: none of them caused it (section 2).
2. Leave the weather input as it is: its error on the clean days is ordinary (section 5). The
   2026 SYNOP observations are now in the database, so the same comparison can be repeated for any
   later episode with `scripts/analyze_live_level_shift.py`.
3. Make no public accuracy claim and draw no A against B conclusion from October: four clean days
   during a heating onset are not evidence for either variant. A against B should be judged on
   consolidated actuals once September and October are stored, as D21 requires, and over the
   eight weeks of the shadow period.
4. Watch whether the error returns to its usual level once the D-2 updates absorb the new level
   (Kalman, ML-Poly). The drift monitor should see this episode. If the CUSUM does not signal a
   1,470 MW shift, its calibration needs a look.
5. Possible later experiment, chosen on 2023 only (D13): a heating-onset feature (for example the
   number of days since the first day below a threshold, or a separate slope for the first cold
   weeks), with 2024 as validation. Section 5 puts the weak spot in the temperature response, so
   this is now the main candidate. I do not suggest a smoothed temperature: EXP-006 found no gain,
   and a smoothed input would react even more slowly at an onset.
6. Look at the learned heating curve between 8 and 16 C for each expert (partial effect of the
   temperature in the GAM, slope of the degree-day term in the OLS), on 2023 and 2024 data only, to
   see whether it is flat where the 2026 onset sits. Section 4 gives the sensitivity of each
   expert to 1 C (986 MW for EXP-004, 584 to 784 MW for EXP-008); with the true temperature EXP-004
   misses by -540 MW in A and EXP-008 by -2,388 MW.

## 8. Reproduction

```
python scripts/analyze_live_level_shift.py --runtime <production runtime folder> --work-dir <scratch folder>
```

The script refuses a work folder inside the runtime folder. It reads `raw.eco2mix`, `labels.synop` (2026 only), the stored
run states, refits and frozen inputs, and the 2023 and 2024 archives of `data/experiments/EXP-009`.
It needs no access to the forecast schema. The issued experts come from the stored states, which
are the same values the runs archived.
