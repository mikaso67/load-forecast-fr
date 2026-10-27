"""School holidays as a day-ahead input: the share of mainland France whose school zone is on holiday.

The calendar is fixed by the Ministry of Education years ahead, so the value for a delivery day is
known long before its 12:00 D-1 cutoff and never depends on consumption. Zones A, B and C cover the
94 departments of mainland France without Corsica, the perimeter of the RTE national load.
"""

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path

import pandas as pd

from load_forecast import evaluation as ev
from load_forecast.backtest import FeatureBuilder

MAINLAND_ZONES = ('Zone A', 'Zone B', 'Zone C')
SHARE_COLUMN = 'school_holiday_share'
ENCODINGS = ('all_days', 'working_days')
# Pupils' rows only: the summer holidays also have a shorter teachers' row in the file.
PUPIL_POPULATIONS = ('-', 'Élèves')

# Departments of each mainland academy (academy boundaries of the Code de l'education, Normandie
# merged since 2020). Checked in tests: 94 departments, each once, names equal to the calendar file.
ACADEMY_DEPARTMENTS = {
    'Aix-Marseille': ('04', '05', '13', '84'),
    'Amiens': ('02', '60', '80'),
    'Besançon': ('25', '39', '70', '90'),
    'Bordeaux': ('24', '33', '40', '47', '64'),
    'Clermont-Ferrand': ('03', '15', '43', '63'),
    'Créteil': ('77', '93', '94'),
    'Dijon': ('21', '58', '71', '89'),
    'Grenoble': ('07', '26', '38', '73', '74'),
    'Lille': ('59', '62'),
    'Limoges': ('19', '23', '87'),
    'Lyon': ('01', '42', '69'),
    'Montpellier': ('11', '30', '34', '48', '66'),
    'Nancy-Metz': ('54', '55', '57', '88'),
    'Nantes': ('44', '49', '53', '72', '85'),
    'Nice': ('06', '83'),
    'Normandie': ('14', '27', '50', '61', '76'),
    'Orléans-Tours': ('18', '28', '36', '37', '41', '45'),
    'Paris': ('75',),
    'Poitiers': ('16', '17', '79', '86'),
    'Reims': ('08', '10', '51', '52'),
    'Rennes': ('22', '29', '35', '56'),
    'Strasbourg': ('67', '68'),
    'Toulouse': ('09', '12', '31', '32', '46', '65', '81', '82'),
    'Versailles': ('78', '91', '92', '95'),
}


def _local_midnight_date(values: pd.Series) -> pd.Series:
    local = pd.to_datetime(values, utc=True).dt.tz_convert('Europe/Paris')
    if (local != local.dt.normalize()).any():
        raise ValueError('Calendar bounds must be Paris local midnights')
    return local.dt.date


def read_holiday_periods(path: Path, first_start_year: int, last_start_year: int) -> pd.DataFrame:
    """Mainland pupil holidays, one row per academy and period, `end_day` exclusive (school resumes).

    Only school years starting between the two years are kept: the file also holds years before the
    2020 Normandie merger (Caen and Rouen) and draft future years, some with empty periods.
    """
    raw = pd.read_csv(path, sep=';', encoding='utf-8-sig', dtype=str)
    start_year = raw['annee_scolaire'].str[:4].astype(int)
    kept = raw.loc[raw['zones'].isin(MAINLAND_ZONES) & raw['population'].isin(PUPIL_POPULATIONS)
                   & start_year.between(first_start_year, last_start_year)]
    periods = pd.DataFrame({
        'academy': kept['location'], 'zone': kept['zones'], 'description': kept['description'],
        'school_year': kept['annee_scolaire'],
        'first_day': _local_midnight_date(kept['start_date']),
        'end_day': _local_midnight_date(kept['end_date'])}).reset_index(drop=True)
    if (periods['end_day'] <= periods['first_day']).any():
        raise ValueError('Every holiday period must end after it starts')
    return periods


def read_department_population(path: Path) -> pd.Series:
    """INSEE legal municipal population (PMUN) by department code."""
    table = pd.read_csv(path, sep=';', encoding='utf-8', dtype={'DEP': str})
    return table.set_index('DEP')['PMUN'].astype(float)


def zone_weights(periods: pd.DataFrame, population: pd.Series) -> dict[str, float]:
    """Population share of each zone in mainland France, from the academy of each department."""
    zones = periods.groupby('academy')['zone'].unique()
    if (zones.map(len) != 1).any():
        raise ValueError('An academy changes zone inside the calendar file')
    if set(zones.index) != set(ACADEMY_DEPARTMENTS):
        raise ValueError('Calendar academies differ from the department mapping')
    totals = dict.fromkeys(MAINLAND_ZONES, 0.0)
    for academy, departments in ACADEMY_DEPARTMENTS.items():
        totals[zones[academy][0]] += float(population.loc[list(departments)].sum())
    grand = sum(totals.values())
    return {zone: value / grand for zone, value in totals.items()}


def holiday_share(days: Iterable[date], periods: pd.DataFrame,
                  weights: Mapping[str, float]) -> pd.Series:
    """Weighted share of mainland France on school holiday for each local day."""
    by_zone = periods.drop_duplicates(['zone', 'first_day', 'end_day'])
    covered_from, covered_to = periods['first_day'].min(), periods['end_day'].max()
    out = {}
    for day in days:
        if not covered_from <= day < covered_to:
            raise ValueError(f'{day} is outside the school calendar file')
        inside = by_zone.loc[(by_zone['first_day'] <= day) & (day < by_zone['end_day']), 'zone']
        out[day] = sum(weights[zone] for zone in set(inside))
    return pd.Series(out, name=SHARE_COLUMN, dtype=float)


def encode(share: float, day: date, encoding: str) -> float:
    """`working_days` keeps the share on Monday to Friday outside public holidays only."""
    if encoding == 'all_days':
        return share
    if encoding == 'working_days':
        return share if day.weekday() < 5 and not ev.is_holiday(day) else 0.0
    raise ValueError(f'Unknown encoding {encoding}')


def with_school_holidays(builder: FeatureBuilder, share: Mapping[date, float],
                         encoding: str = 'all_days') -> FeatureBuilder:
    """Add the encoded share of the delivery day as one more column of `builder`."""
    if encoding not in ENCODINGS:
        raise ValueError(f'Unknown encoding {encoding}')

    def wrapped(slots: pd.DatetimeIndex, history: pd.Series, day: date) -> pd.DataFrame:
        frame = builder(slots, history, day)
        if day not in share:
            raise ValueError(f'No school holiday share for {day}')
        frame[SHARE_COLUMN] = encode(float(share[day]), day, encoding)
        return frame

    return wrapped
