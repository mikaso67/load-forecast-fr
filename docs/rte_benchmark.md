# RTE D-1 Benchmark and Forecast Bias Analysis

This document investigates the systematic bias observed in RTE's day-ahead electricity consumption
forecast (`prevision_j1`) compared to the consolidated/definitive consumption (`consommation`) in eCO2mix,
as identified in EXP-002 in the archived report.

I restrict all historical analyses and backtest measurements cover data up to 2024-12-31 Europe/Paris only.
No 2025 value is used or evaluated.
A dedicated check on recent operational real-time data since 2026-07-01 is reported separately to assess real-time bias.

---

## 1. Executive Summary

In the 2024 validation year, RTE's D-1 forecast (`prevision_j1`) exhibits a severe, persistent under-forecast bias
against the definitive consumption (`consommation`) in a development build of the clean model `load_30min`:
- Mean bias (`prevision_j1 - consommation`): -1,146.25 MW (standard deviation: 1,257.60 MW; 95% bootstrap CI: [-1,261.44, -1,014.85] MW).
- Baseline RTE error metrics on 2024: MAE = 1,364.58 MW, RMSE = 1,701.57 MW, MAPE = 2.731%.
- The ratio of absolute mean bias to MAE is 84.0% (1,146.25 MW out of 1,364.58 MW).

Critically, an independent analysis on real-time consumption (`eco2mix-national-tr`) since 2026-07-01 shows that RTE's
D-1 forecast is nearly unbiased in real time (+130.39 MW mean bias, MAE 657.55 MW).
Empirically, the negative offset is present against the consolidated series (-1,146 MW in 2024) and absent against real-time (+130 MW in summer 2026).
The cause is not established. Plausible hypotheses include: ex-post upward revisions during revenue meter consolidation,
differences in loss accounting or non-telemeasured generation estimates, seasonal factors, or a shift in RTE's own forecasting models between 2024 and 2026.

Consequently, any statistical model trained directly on consolidated data inherently predicts the consolidated level.
Comparing such a model against raw RTE D-1 credits the model with an MAE reduction (in 2024, 392.59 MW for a flat offset a1 up to 467.93 MW for an hour-of-day offset a2; in 2023, 303.84 MW for a1 up to 328.50 MW for a2) that primarily reflects a level alignment rather than superior dynamics, though the hour-of-day correction also absorbs the diurnal shape of the bias.
To provide an interview-defensible evaluation, this document formulates leak-free bias-corrected RTE benchmarks
available at the D-1 cutoff, compares them across 2023 and 2024, and proposes clear options for developer decision.

---

## 2. Measurement of RTE Bias (Data up to 2024-12-31)

I measured these results from a development build of the clean model `load_30min`.
For reproduction, I set `LFF_CLEAN_TABLE` to the built relation and read it using the PostgreSQL role `lff_agent`.

### 2.1 Multi-Year Bias (2021 to 2024)

Query:
The clean-table SQL below uses `clean_query` templates: `{table}` is the relation selected by `LFF_CLEAN_TABLE`. The recorded outputs came from a development build of the clean model `load_30min`.

```sql
SELECT
    EXTRACT(YEAR FROM value_time AT TIME ZONE 'Europe/Paris')::int AS paris_year,
    COUNT(*) AS total_slots,
    COUNT(consommation) AS n_cons,
    COUNT(prevision_j1) AS n_prev_j1,
    COUNT(prevision_j) AS n_prev_j,
    ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_j1_mw,
    ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_j1_mw,
    ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_j1_mw,
    ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_j1_mw,
    ROUND((100.0 * AVG(ABS(prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mape_j1_pct,
    ROUND((100.0 * AVG((prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mean_bias_j1_pct,
    ROUND(AVG(prevision_j - consommation)::numeric, 2) AS mean_bias_j_mw,
    ROUND(AVG(ABS(prevision_j - consommation))::numeric, 2) AS mae_j_mw,
    ROUND(AVG(prevision_j1)::numeric, 2) AS mean_prev_j1_mw,
    ROUND(AVG(consommation)::numeric, 2) AS mean_cons_mw
FROM {table}
WHERE (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
GROUP BY paris_year
ORDER BY paris_year;
```

Measured results:

| Paris year | Slots | Cons. filled | D-1 filled | D filled | D-1 Mean bias (MW) | D-1 Std (MW) | D-1 MAE (MW) | D-1 RMSE (MW) | D-1 MAPE (%) | D-1 Bias (%) | D Mean bias (MW) | D MAE (MW) | Mean D-1 (MW) | Mean Cons. (MW) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2021 | 13,630 | 13,630 | 13,630 | 13,630 | -354.03 | 1,052.41 | 820.34 | 1,110.33 | 1.629 | -0.693 | -396.65 | 734.90 | 49,712.73 | 50,066.76 |
| 2022 | 17,518 | 17,518 | 17,518 | 17,518 | -574.09 | 1,196.98 | 973.01 | 1,327.50 | 1.896 | -1.096 | -637.97 | 933.27 | 50,813.76 | 51,387.85 |
| 2023 | 17,518 | 17,518 | 17,518 | 17,518 | -1,130.96 | 1,199.83 | 1,317.93 | 1,648.81 | 2.684 | -2.324 | -1,142.77 | 1,223.70 | 48,592.52 | 49,723.48 |
| 2024 | 17,566 | 17,566 | 17,566 | 17,566 | -1,146.25 | 1,257.60 | 1,364.58 | 1,701.57 | 2.731 | -2.296 | -1,167.58 | 1,249.96 | 48,955.01 | 50,101.25 |

Findings:
1. In 2021 and 2022, the negative bias was moderate (-354.03 MW and -574.09 MW, or -0.69% and -1.10% of mean demand).
2. In 2023 and 2024, the negative bias doubled to -1,130.96 MW (-2.32%) and -1,146.25 MW (-2.30%).
3. The intraday forecast `prevision_j` exhibits the exact same bias (-1,142.77 MW in 2023 and -1,167.58 MW in 2024).
   Both D-1 and intraday D forecasts share this negative offset, suggesting that it is not caused by morning weather forecast error or forecast lead-time.
4. Hypothesis: The shift between 2022 and 2023 coincides with the European energy crisis and changes in baseline consumption behavior, but whether RTE altered its internal operational models remains an unverified hypothesis.

### 2.2 2024 Monthly Bias

Query:
```sql
SELECT
    EXTRACT(MONTH FROM value_time AT TIME ZONE 'Europe/Paris')::int AS month,
    COUNT(*) AS n_slots,
    ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_mw,
    ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_mw,
    ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_mw,
    ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_mw,
    ROUND((100.0 * AVG(ABS(prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mape_pct,
    ROUND((100.0 * AVG((prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mean_bias_pct,
    ROUND(AVG(prevision_j1)::numeric, 2) AS mean_prev_j1_mw,
    ROUND(AVG(consommation)::numeric, 2) AS mean_cons_mw
FROM {table}
WHERE (value_time AT TIME ZONE 'Europe/Paris') >= '2024-01-01 00:00:00'
  AND (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
GROUP BY month
ORDER BY month;
```

Measured results:

| Month | Slots | Mean bias (MW) | Std bias (MW) | MAE (MW) | RMSE (MW) | MAPE (%) | Mean bias (%) | Mean D-1 (MW) | Mean Cons. (MW) |
|---|---|---|---|---|---|---|---|---|---|
| 1 (Jan) | 1,488 | -1,543.53 | 1,722.71 | 1,906.14 | 2,312.63 | 2.936 | -2.402 | 63,722.51 | 65,266.05 |
| 2 (Feb) | 1,392 | -1,398.97 | 1,257.74 | 1,588.95 | 1,880.93 | 2.786 | -2.455 | 55,905.74 | 57,304.71 |
| 3 (Mar) | 1,486 | -1,031.55 | 1,175.54 | 1,281.24 | 1,563.67 | 2.448 | -1.998 | 52,251.82 | 53,283.37 |
| 4 (Apr) | 1,440 | -1,429.09 | 1,261.77 | 1,566.20 | 1,906.11 | 3.237 | -2.933 | 47,228.96 | 48,658.05 |
| 5 (May) | 1,488 | -1,601.59 | 1,227.80 | 1,660.10 | 2,017.81 | 3.868 | -3.728 | 41,397.13 | 42,998.72 |
| 6 (Jun) | 1,440 | -1,106.65 | 963.29 | 1,230.57 | 1,466.95 | 2.908 | -2.608 | 41,420.42 | 42,527.06 |
| 7 (Jul) | 1,488 | -958.13 | 1,097.74 | 1,176.57 | 1,456.79 | 2.659 | -2.142 | 42,688.10 | 43,646.23 |
| 8 (Aug) | 1,488 | -556.52 | 940.72 | 868.99 | 1,092.74 | 2.082 | -1.315 | 41,206.79 | 41,763.31 |
| 9 (Sep) | 1,440 | -1,113.17 | 940.95 | 1,200.49 | 1,457.37 | 2.728 | -2.506 | 42,137.08 | 43,250.26 |
| 10 (Oct) | 1,488 | -779.01 | 950.73 | 1,009.84 | 1,228.88 | 2.197 | -1.686 | 45,232.73 | 46,011.74 |
| 11 (Nov) | 1,440 | -942.73 | 1,373.35 | 1,307.65 | 1,665.39 | 2.356 | -1.680 | 53,685.76 | 54,628.50 |
| 12 (Dec) | 1,488 | -1,310.39 | 1,458.91 | 1,587.61 | 1,960.64 | 2.580 | -2.126 | 60,669.83 | 61,980.21 |

Findings:
1. Every month in 2024 exhibits negative mean bias.
2. The negative bias is largest in May (-1,601.59 MW, -3.73%), January (-1,543.53 MW, -2.40%), and April (-1,429.09 MW, -2.93%).
3. The smallest negative bias occurs in August (-556.52 MW, -1.32%) and October (-779.01 MW, -1.69%).

### 2.3 2024 Diurnal Bias by Hour of Day (Europe/Paris Time)

Query:
```sql
SELECT
    EXTRACT(HOUR FROM value_time AT TIME ZONE 'Europe/Paris')::int AS paris_hour,
    COUNT(*) AS n_slots,
    ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_mw,
    ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_mw,
    ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_mw,
    ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_mw,
    ROUND((100.0 * AVG(ABS(prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mape_pct,
    ROUND((100.0 * AVG((prevision_j1 - consommation)::numeric / NULLIF(consommation, 0)))::numeric, 3) AS mean_bias_pct,
    ROUND(AVG(prevision_j1)::numeric, 2) AS mean_prev_j1_mw,
    ROUND(AVG(consommation)::numeric, 2) AS mean_cons_mw
FROM {table}
WHERE (value_time AT TIME ZONE 'Europe/Paris') >= '2024-01-01 00:00:00'
  AND (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
GROUP BY paris_hour
ORDER BY paris_hour;
```

Measured results:

| Hour | Slots | Mean bias (MW) | Std bias (MW) | MAE (MW) | RMSE (MW) | MAPE (%) | Mean bias (%) | Mean D-1 (MW) | Mean Cons. (MW) |
|---|---|---|---|---|---|---|---|---|---|
| 00:00 | 732 | -1,933.39 | 843.35 | 1,941.59 | 2,109.09 | 3.942 | -3.929 | 47,850.11 | 49,783.50 |
| 01:00 | 732 | -1,217.62 | 910.15 | 1,277.37 | 1,519.82 | 2.750 | -2.620 | 45,127.98 | 46,345.60 |
| 02:00 | 730 | -1,072.82 | 932.15 | 1,165.73 | 1,420.79 | 2.577 | -2.375 | 44,230.55 | 45,303.37 |
| 03:00 | 732 | -1,467.28 | 806.59 | 1,478.41 | 1,674.10 | 3.390 | -3.371 | 41,909.07 | 43,376.35 |
| 04:00 | 732 | -1,167.89 | 805.12 | 1,200.69 | 1,418.20 | 2.828 | -2.756 | 40,729.47 | 41,897.35 |
| 05:00 | 732 | -561.28 | 870.98 | 807.07 | 1,035.67 | 1.888 | -1.293 | 41,579.15 | 42,140.44 |
| 06:00 | 732 | -158.26 | 986.33 | 785.97 | 998.28 | 1.796 | -0.361 | 44,298.46 | 44,456.72 |
| 07:00 | 732 | -73.17 | 1,095.71 | 869.33 | 1,097.40 | 1.868 | -0.193 | 47,846.03 | 47,919.20 |
| 08:00 | 732 | -417.25 | 1,104.84 | 911.65 | 1,180.30 | 1.803 | -0.795 | 50,383.22 | 50,800.47 |
| 09:00 | 732 | -736.95 | 1,130.77 | 1,048.63 | 1,349.07 | 1.975 | -1.361 | 52,036.65 | 52,773.60 |
| 10:00 | 732 | -1,054.65 | 1,133.24 | 1,251.66 | 1,547.50 | 2.337 | -1.966 | 52,806.08 | 53,860.73 |
| 11:00 | 732 | -1,234.72 | 1,123.37 | 1,370.77 | 1,668.76 | 2.560 | -2.318 | 53,192.17 | 54,426.89 |
| 12:00 | 732 | -1,307.36 | 1,293.20 | 1,492.78 | 1,838.28 | 2.718 | -2.390 | 54,046.07 | 55,353.42 |
| 13:00 | 732 | -1,832.84 | 1,385.14 | 1,933.25 | 2,296.80 | 3.557 | -3.393 | 53,504.31 | 55,337.15 |
| 14:00 | 732 | -1,886.67 | 1,330.33 | 1,976.89 | 2,308.01 | 3.805 | -3.662 | 51,568.71 | 53,455.38 |
| 15:00 | 732 | -1,953.20 | 1,328.84 | 2,038.02 | 2,361.86 | 4.012 | -3.871 | 50,165.29 | 52,118.49 |
| 16:00 | 732 | -1,838.32 | 1,276.67 | 1,915.23 | 2,237.66 | 3.879 | -3.743 | 48,904.05 | 50,742.37 |
| 17:00 | 732 | -1,306.18 | 1,308.80 | 1,533.98 | 1,848.44 | 3.183 | -2.784 | 48,957.75 | 50,263.93 |
| 18:00 | 732 | -628.92 | 1,287.75 | 1,137.92 | 1,432.34 | 2.241 | -1.335 | 51,183.74 | 51,812.67 |
| 19:00 | 732 | -798.73 | 1,302.09 | 1,183.41 | 1,526.80 | 2.161 | -1.455 | 53,406.86 | 54,205.59 |
| 20:00 | 732 | -1,667.03 | 1,195.93 | 1,719.17 | 2,051.16 | 3.162 | -3.054 | 51,834.26 | 53,501.28 |
| 21:00 | 732 | -1,502.60 | 1,175.09 | 1,564.70 | 1,907.03 | 2.980 | -2.847 | 49,658.40 | 51,160.99 |
| 22:00 | 732 | -1,083.02 | 1,192.22 | 1,247.00 | 1,610.09 | 2.424 | -2.093 | 49,032.06 | 50,115.08 |
| 23:00 | 732 | -609.59 | 1,027.71 | 898.11 | 1,194.30 | 1.711 | -1.138 | 50,656.78 | 51,266.37 |

Findings:
1. Midnight (00:00 Paris time) exhibits an extreme negative bias of -1,933.39 MW.
   RTE's MAE at 00:00 is 1,941.59 MW. Almost all errors at 00:00 are negative: 724 of 732 slots (98.91%).
2. The afternoon trough (13:00 to 16:00) exhibits a second peak of negative bias, reaching -1,953.20 MW at 15:00.
3. During the morning ramp (06:00 to 07:00), the bias drops to its minimum (-158.26 MW and -73.17 MW).
4. During the evening peak (18:00 to 19:00), the bias narrows to -628.92 MW and -798.73 MW, before widening again after 20:00 (-1,667.03 MW).
5. The diurnal structure indicates that the offset is modulated by the dispatch cycle and daily load profile, rather than being a flat, time-invariant constant.
   Therefore, an hour-of-day correction absorbs both the level shift and this diurnal profile distortion.

### 2.4 2024 Day-Type Bias (Protocol Section 5 with SYNOP Labels)

Day types are labeled using the protocol precedence rule (`src/load_forecast/evaluation.py`):
`DST > holiday > bridge > cold > ordinary`.
Cold days are labeled using Meteo-France SYNOP daily observations loaded in `labels.synop` with threshold 4.84 deg C.

Query:
```sql
SELECT value_time, consommation, prevision_j1
FROM {table}
WHERE (value_time AT TIME ZONE 'Europe/Paris') >= '2024-01-01 00:00:00'
  AND (value_time AT TIME ZONE 'Europe/Paris') < '2025-01-01 00:00:00'
ORDER BY value_time;
```

Measured results (slot-weighted):

| Day type | Days | Slots | Mean bias (MW) | Std bias (MW) | MAE (MW) | RMSE (MW) | MAPE (%) | Mean bias (%) |
|---|---|---|---|---|---|---|---|---|
| Ordinary | 329 | 15,792 | -1,089.85 | 1,178.72 | 1,299.75 | 1,605.32 | 2.656 | -2.226 |
| Cold | 22 | 1,056 | -1,657.05 | 1,851.93 | 2,062.04 | 2,484.39 | 2.962 | -2.374 |
| Holiday | 11 | 528 | -2,035.01 | 1,504.68 | 2,067.96 | 2,530.03 | 4.718 | -4.644 |
| Bridge | 2 | 96 | -279.06 | 1,208.56 | 921.75 | 1,234.21 | 2.305 | -0.626 |
| DST | 2 | 94 | -776.66 | 816.98 | 922.11 | 1,124.08 | 2.095 | -1.762 |
| cold_any | 22 | 1,056 | -1,657.05 | 1,851.93 | 2,062.04 | 2,484.39 | 2.962 | -2.374 |

Statistical uncertainty:
- 2024 Daily mean bias bootstrap (moving block bootstrap, 7-day blocks, 10,000 draws, seed 20240):
  Mean = -1,146.21 MW, 95% confidence interval: [-1,261.44, -1,014.85] MW, statistically significant.
- Label-missing days: 1 day in 2024 (2024-02-05, 48 slots), counted as ordinary under protocol precedence.
- `cold_any`: Exactly equals the exclusive `cold` category (no public holiday or DST day was cold in 2024).

Findings:
1. Public holidays (11 days) exhibit the highest under-forecast bias (-2,035.01 MW, -4.64%), with MAE = 2,067.96 MW.
2. Cold days (22 days) show an aggravated under-forecast bias (-1,657.05 MW), with MAE = 2,062.04 MW.
3. Bridge days (2 days: 2024-05-10 and 2024-08-16) have a substantially smaller bias (-279.06 MW).
4. DST days (2 days: 23-hour and 25-hour days) have a bias of -776.66 MW.

---

## 3. Definitions, Perimeters, and Documentation Search

### 3.1 Real-Time Consumption (`eco2mix-national-tr`)

- Sources:
  - [ODRE eco2mix-national-tr dataset](https://opendata.reseaux-energies.fr/explore/dataset/eco2mix-national-tr/information/)
  - [RTE eCO2mix Donnees temps reel](https://www.rte-france.com/donnees-publications/eco2mix-donnees-temps-reel)
- Geographical perimeter: Metropolitan France excluding Corsica ("France metropolitaine continentale").
- Calculation formula:
  `Consommation (pertes comprises) = Production + Importations - Exportations - Pompage`
  (Formula cited in general documentation on services-rte.com; unverified without an exact permanent URL and quoted passage).
- Inclusions and exclusions:
  - Network losses (transmission and distribution losses) are stated to be included (unverified).
  - Hydro storage pumping (STEP) is stated to be excluded (unverified).
  - Non-telemeasured distributed generation (rooftop solar, small wind, small hydro) is estimated using fixed forfaits
    and added to the telemeasured generation (unverified).

### 3.2 Consolidated and Definitive Consumption (`eco2mix-national-cons-def`)

- Sources:
  - [ODRE eco2mix-national-cons-def dataset](https://opendata.reseaux-energies.fr/explore/dataset/eco2mix-national-cons-def/information/)
  - [RTE Specifications des fichiers en puissance](https://www.rte-france.com/sites/default/files/eco2mix_-_specifications_des_fichiers_en_puissance.pdf)
- Verification stages:
  1. Consolidated data ("Donnees consolidees"): Published in M+1 (mid-month following delivery; unverified). Incorporates the first
     reconciled meter readings from RTE and distribution grid operators (Enedis covering ~95%, plus local distribution companies ELDs; unverified market share and timing).
  2. Definitive data ("Donnees definitives"): Published in the second semester of year A+1 (unverified). Incorporates audited,
     final settlement revenue meters across all balancing perimeters (unverified).
- Target physical quantity: Gross consumption (consommation brute) on the same geographical perimeter (France continentale).

### 3.3 RTE Day-Ahead Forecast (`prevision_j1`)

- Sources:
  - [RTE Services: Prevision de la consommation](https://services-rte.com)
  - [RTE Specifications des fichiers en puissance](https://www.rte-france.com/sites/default/files/eco2mix_-_specifications_des_fichiers_en_puissance.pdf)
- Definition: Forecast of national gross electricity consumption established on day D-1 by RTE's National Operational Center
  (Centre National d'Exploitation du Systeme; unverified operational entity).
- Value format: Published as integer values in MW (unverified rule; observed in database where all values have integer precision).
- Purpose: Operational power system balancing, reserve sizing (~3 GW reserve margin; unverified), and grid security assessment.

### 3.4 Do They Measure the Same Physical Quantity?

Officially, both `consommation` and `prevision_j1` are described in general documentation as national gross electricity consumption
(France continentale, losses included, pumping excluded), but whether their exact operational definitions match is unverified.

Several hypotheses may explain the persistent empirical divergence:
1. Hypothesis: Telemetry vs Final Revenue Metering Reconciliation.
   RTE's operational forecasting tools may be calibrated on operational telemetries.
   If definitive consumption revises upward during ex-post revenue meter consolidation, the forecast appears biased against consolidated data (unverified hypothesis).
2. Hypothesis: Model Calibration Shift Following the 2022 Energy Crisis.
   Between 2022 and 2023, RTE's D-1 bias jumped from -574 MW to -1,131 MW, and intraday `prevision_j` jumped from -638 MW to -1,143 MW.
   Whether this reflects a deliberate model recalibration by RTE or post-crisis structural changes in consumption elasticity is an unverified hypothesis.

---

## 4. Dataset Overlap and Real-Time Bias Measurement

### 4.1 Overlap in `raw.eco2mix`

Query:
```sql
SELECT
    COUNT(*) AS total_overlap_count
FROM (
    SELECT value_time FROM raw.eco2mix WHERE source_dataset = 'eco2mix-national-cons-def'
    INTERSECT
    SELECT value_time FROM raw.eco2mix WHERE source_dataset = 'eco2mix-national-tr'
) s;
```

Measured results:
- Total overlapping timestamps across the full table: 0
- Overlapping timestamps up to 2024-12-31: 0

Stored bounds by source:

| Source dataset | Min value_time (UTC) | Max value_time (UTC) | Row count |
|---|---|---|---|
| `eco2mix-national-cons-def` | 2021-03-22 23:00:00+00 | 2026-06-30 21:45:00+00 | 184,896 |
| `eco2mix-national-tr` | 2026-06-30 22:00:00+00 | 2026-10-03 21:45:00+00 | 9,120 |

Explanation:
ODRE partitions historical datasets at 2026-06-30 22:00 UTC (which may reflect ODRE's archive cycle or the collector's cutoff; unverified).
Because the collector ingested historical data from ODRE exports, the two datasets are strictly partitioned without temporal overlap in the archive.

### 4.2 Reviewer's Measurement: Real-Time Bias Since 2026-07-01

To evaluate RTE D-1 performance against operational real-time consumption directly, we query `raw.eco2mix` on
`eco2mix-national-tr` starting from 2026-07-01. No 2025 value is used.

Query:
```sql
SELECT
    TO_CHAR(date_trunc('month', value_time AT TIME ZONE 'Europe/Paris'), 'YYYY-MM') AS month_label,
    COUNT(*) AS n_slots,
    ROUND(AVG(prevision_j1 - consommation)::numeric, 2) AS mean_bias_mw,
    ROUND(STDDEV(prevision_j1 - consommation)::numeric, 2) AS std_bias_mw,
    ROUND(AVG(ABS(prevision_j1 - consommation))::numeric, 2) AS mae_mw,
    ROUND(SQRT(AVG(POWER(prevision_j1 - consommation, 2)))::numeric, 2) AS rmse_mw
FROM raw.eco2mix
WHERE source_dataset = 'eco2mix-national-tr'
  AND value_time >= '2026-07-01 00:00+02'
  AND prevision_j1 IS NOT NULL
  AND consommation IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

Measured results:

| Month (real-time) | Slots | Mean bias (MW) | Std bias (MW) | MAE (MW) | RMSE (MW) |
|---|---|---|---|---|---|
| 2026-07 | 2,976 | +70.83 | 867.92 | 669.33 | 870.66 |
| 2026-08 | 2,976 | +180.85 | 880.69 | 708.60 | 898.92 |
| 2026-09 | 2,880 | +152.96 | 777.71 | 605.56 | 792.48 |
| 2026-10 (to Oct 3) | 284 | -3.24 | 683.59 | 526.26 | 682.39 |
| **Total** | **9,116** | **+130.39** | **840.96** | **657.55** | **850.99** |

Limits and interpretation:
- Sample covers 3 months only (July to early October 2026), primarily summer and early autumn.
- Different time period from the 2021-2024 backtest; no slot has both real-time and consolidated measurements (overlap is 0).
- Real-time values may undergo later revision during M+1 consolidation.
- **Consequence**: Against real-time consumption, RTE D-1 has a mean bias of only +130.39 MW (and an MAE of 657.55 MW),
  contrasting with the -1,146.25 MW bias observed against consolidated consumption.
  The data show that the offset is present against consolidated consumption and absent against real-time consumption.
  However, because the two series cover different time periods with zero overlap, the cause is not established:
  it may reflect ex-post revisions during consolidation, seasonal differences (summer vs full year),
  or changes in RTE's forecasting system between 2024 and 2026.

---

## 5. Leak-Free Bias Corrections and Benchmark Options

### 5.1 Why Prior Rolling Corrections Leaked

In round 1 of my analysis, rolling bias corrections (e.g. subtracting the mean error of the past 14 or 28 days) were computed using
`consommation` up to D-1 10:00.
However, `consommation` in `load_30min` is the consolidated series, published weeks later in M+1.
At the D-1 12:00 cutoff, recent consolidated values do not yet exist.
Therefore, rolling corrections using recent consolidated data constitute an **oracle lookahead leak**.

To construct a valid operational benchmark (RTE*), corrections must use **only information strictly available at the cutoff**.
Two leak-free candidate approaches are formulated:
- **Candidate (a)**: RTE D-1 adjusted by the mean bias of the previous full calendar year (known in its entirety).
  - (a1) Flat prior-year bias: `prevision_j1 - mean_bias(Y-1)`.
  - (a2) Prior-year bias by hour-of-day: `prevision_j1 - mean_bias_hour(Y-1)`.
- **Candidate (b)**: Rolling bias over errors older than the consolidation delay $L$:
  - $L = 45$ days (unverified; based on ODRE's M+1 publication timeline).
  - $L = 60$ days (unverified; conservative delay).
  - Window $W = 28$ days or $W = 56$ days (flat or by hour of day).

Note on January availability:
A forecast for 1 January strictly speaking requires December of the previous year, which is consolidated only in mid-January.
As measured by the reviewer, substituting the 12 months ending 45 days earlier changes the MAE for 1 January to 14 February by +2 MW in 2023 (918.1 to 920.4 MW) and +31 MW in 2024 (1,208.9 to 1,239.4 MW).
While small, candidate (a) can alternatively be defined as the 12 months ending 45 days before the forecast day if strict operational timing is required.

The implementation in `src/load_forecast/rte_reference.py` requires finite actual
and RTE D-1 pairs on at least 95% of the expected half-hour slots for each of the
24 Paris local hours in Y-1. The full Paris calendar-year grid includes both DST
transitions. Missing, nonfinite and off-grid pairs do not count; insufficient
coverage raises a clear error with the year and affected hours. This guard checks
data coverage, not publication availability, and leaves the a2 correction unchanged.

### 5.2 Evaluation and Selection on 2023 (Rule D13)

Under protocol rule D13, all model decisions and tuning choices are made on the 2023 development year only,
and evaluated unchanged on the 2024 validation year.

Candidate evaluation on 2023 and 2024:

| Benchmark candidate | 2023 MAE (MW) | 2023 Mean Err (MW) | 2024 MAE (MW) | 2024 Mean Err (MW) | Leak-free? |
|---|---|---|---|---|---|
| **Raw RTE D-1** | 1,317.93 | -1,130.96 | 1,364.58 | -1,146.25 | Yes (Raw) |
| **Candidate (a1) Prior-year flat** | 1,014.09 | -556.88 | 971.99 | -15.29 | Yes |
| **Candidate (a2) Prior-year by hour** | **989.43** | -556.88 | **896.65** | -15.29 | Yes (Best 2023) |
| Candidate (b) L=45d, W=28d flat | 1,018.71 | -151.57 | 1,000.91 | +65.47 | Yes (L=45d) |
| Candidate (b) L=45d, W=28d by hour | 1,000.25 | -151.61 | 929.93 | +65.49 | Yes (L=45d) |
| Candidate (b) L=45d, W=56d flat | 1,018.51 | -177.92 | 994.15 | +79.62 | Yes (L=45d) |
| Candidate (b) L=45d, W=56d by hour | 1,001.87 | -177.96 | 923.73 | +79.64 | Yes (L=45d) |
| Candidate (b) L=60d, W=28d flat | 1,039.99 | -186.32 | 1,001.22 | +95.75 | Yes (L=60d) |
| Candidate (b) L=60d, W=28d by hour | 1,028.59 | -186.36 | 938.33 | +95.78 | Yes (L=60d) |
| Candidate (b) L=60d, W=56d flat | 1,043.51 | -200.01 | 999.11 | +81.66 | Yes (L=60d) |
| Candidate (b) L=60d, W=56d by hour | 1,030.52 | -200.04 | 934.19 | +81.69 | Yes (L=60d) |
| *Oracle leaking rolling 7d* | *N/A* | *N/A* | *965.84* | *+1.88* | *No (Oracle)* |
| *Oracle leaking rolling 14d* | *N/A* | *N/A* | *962.46* | *+3.57* | *No (Oracle)* |
| *Oracle leaking rolling 28d* | *N/A* | *N/A* | *963.23* | *+12.90* | *No (Oracle)* |
| *Oracle leaking rolling 28d by hour* | *N/A* | *N/A* | *839.70* | *+13.26* | *No (Oracle)* |

Selection Analysis (D13):
1. **Selection on 2023**: Among all leak-free candidates, **Candidate (a2) [Prior-year bias by hour-of-day]** achieves the lowest MAE on 2023 (989.43 MW, an improvement of 328.50 MW over raw RTE MAE of 1,317.93 MW, and 24.66 MW over Candidate a1).
   Applied unchanged to 2024, Candidate (a2) achieves an MAE of **896.65 MW** (an improvement of 467.93 MW over raw RTE MAE of 1,364.58 MW; RMSE 1,171.25 MW, MAPE 1.785%, Mean Error -15.29 MW).
2. **Flat correction selection on 2023**: If a simpler flat offset is preferred, **Candidate (a1) [Prior-year flat bias]** achieves the lowest flat MAE on 2023 (1,014.09 MW, an improvement of 303.84 MW over raw RTE).
   Applied unchanged to 2024, Candidate (a1) achieves an MAE of **971.99 MW** (an improvement of 392.59 MW over raw RTE; RMSE 1,257.65 MW, MAPE 1.951%, Mean Error -15.29 MW).
3. **Comparison with Oracle**: The previously reported 839.70 MW was an oracle that leaked consolidated errors up to D-1 10:00.
   When restricted strictly to data known at the cutoff, the best valid operational correction yields 896.65 MW (or 971.99 MW for a flat offset).

---

## 5.3 Options for Developer Decision

The developer decides between three benchmark comparison options:

### Option 1: Raw RTE D-1 Only (Status Quo)
Compare models strictly against raw `prevision_j1` as published in eCO2mix.
- **Reference metric (2024)**: MAE = 1,364.58 MW, RMSE = 1,701.57 MW, MAPE = 2.731%.
- **Trade-offs**:
  - *Advantages*: Simple, matches verbatim wording of the project contract and public dashboard conventions.
  - *Disadvantages*: Methodologically fragile in job interviews. Because raw RTE has an inherent negative bias against consolidated data (-1,146 MW in 2024, -1,131 MW in 2023), any unbiased in-house model gains an MAE reduction (393 MW flat / 468 MW by-hour in 2024; 304 MW flat / 329 MW by-hour in 2023) largely by matching the level of the target series, rather than necessarily proving superior weather or calendar dynamics.

### Option 2: Dual Reporting with Leak-Free Operational Bias-Corrected RTE (RTE*)
Report every backtest experiment against both:
1. **Raw RTE D-1**: 1,364.58 MW (official unadjusted eCO2mix forecast).
2. **Operational RTE***: Either Candidate (a2) by hour (896.65 MW) or Candidate (a1) flat (971.99 MW), selected on 2023 under D13.
- **Trade-offs**:
  - *Advantages*: Fully leak-free and production-feasible. Provides a stricter reference than raw RTE D-1 (on 2024, Candidate a2 has a paired daily MAE difference of -467.90 MW against raw RTE, 95% block bootstrap CI [-545.44, -381.91] MW; Candidate a1 has a daily difference of -392.58 MW, 95% CI [-467.65, -309.29] MW). However, beating RTE* simply indicates lower error against the consolidated target than a level-adjusted benchmark; it does not prove algorithmic superiority, as RTE* is not an RTE product and relies on an ex-post level learned on consolidated history.
  - *Disadvantages*: Requires reporting two benchmark columns in experiment summaries.

### Option 3: Target Alignment on Real-Time Ingestion (Future Work)
Retarget the production system to forecast real-time consumption (`eco2mix-national-tr`) where RTE is nearly unbiased (+130.39 MW).
- **Trade-offs**:
  - *Advantages*: Eliminates the consolidation level distortion entirely in production.
  - *Disadvantages*: Cannot be backtested on 2021-2024 because ODRE did not archive historical real-time vintages.

---

## 6. Automated Verification

All figures, tables, and metrics in this document are reproduced deterministically by running:

```powershell
. .\scripts\load_env.ps1
& ..\load-forecast-fr-venv\Scripts\python.exe scripts/verify_rte_benchmark.py
```

The verification script connects to PostgreSQL as `lff_agent`, reads only data strictly prior to `2025-01-01 00:00:00 Europe/Paris` (plus `raw.eco2mix` for real-time measurements from `2026-07-01`), evaluates all leak-free and oracle corrections, and validates automated assertions before exiting with code 0.
