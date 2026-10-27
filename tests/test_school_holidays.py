from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from load_forecast.cutoff import delivery_slots
from load_forecast.school_holidays import (
    ACADEMY_DEPARTMENTS,
    SHARE_COLUMN,
    holiday_share,
    read_department_population,
    read_holiday_periods,
    with_school_holidays,
    zone_weights,
)

HEADER = 'description;population;start_date;end_date;location;zones;annee_scolaire'
CALENDAR_FILE = Path('data/external/school_calendar/fr-en-calendrier-scolaire.csv')
POPULATION_FILE = Path('data/external/insee_population/donnees_departements.csv')
ZONE_OF = {'Zone A': ('Besançon', 'Bordeaux', 'Clermont-Ferrand', 'Dijon', 'Grenoble', 'Limoges',
                      'Lyon', 'Poitiers'),
           'Zone C': ('Créteil', 'Montpellier', 'Paris', 'Toulouse', 'Versailles')}


def zone(academy: str) -> str:
    return next((z for z, names in ZONE_OF.items() if academy in names), 'Zone B')


def calendar(tmp_path: Path) -> Path:
    """Toussaint, winter holidays (zones staggered) and summer pupil and teacher rows of 2023-2024."""
    rows = [HEADER]
    winter = {'Zone C': ('2024-02-09T23:00:00+00:00', '2024-02-25T23:00:00+00:00'),
              'Zone A': ('2024-02-16T23:00:00+00:00', '2024-03-03T23:00:00+00:00'),
              'Zone B': ('2024-02-23T23:00:00+00:00', '2024-03-10T23:00:00+00:00')}
    for academy in ACADEMY_DEPARTMENTS:
        rows.append('Vacances de la Toussaint;-;2023-10-20T22:00:00+00:00;2023-11-05T23:00:00+00:00;'
                    f'{academy};{zone(academy)};2023-2024')
        start, end = winter[zone(academy)]
        rows.append(f"Vacances d'Hiver;-;{start};{end};{academy};{zone(academy)};2023-2024")
        rows.append(f"Vacances d'Été;Élèves;2024-07-05T22:00:00+00:00;2024-09-01T22:00:00+00:00;"
                    f'{academy};{zone(academy)};2023-2024')
        rows.append(f"Vacances d'Été;Enseignants;2024-07-05T22:00:00+00:00;2024-08-29T22:00:00+00:00;"
                    f'{academy};{zone(academy)};2023-2024')
    rows.append("Vacances d'Hiver;-;2024-02-16T23:00:00+00:00;2024-03-03T23:00:00+00:00;Corse;Corse;2023-2024")
    path = tmp_path / 'calendar.csv'
    path.write_text('\n'.join(rows), encoding='utf-8')
    return path


def equal_population() -> pd.Series:
    codes = [d for departments in ACADEMY_DEPARTMENTS.values() for d in departments]
    return pd.Series(1.0, index=codes)


def test_mapping_covers_mainland_departments_once():
    codes = [d for departments in ACADEMY_DEPARTMENTS.values() for d in departments]
    assert len(codes) == 94 == len(set(codes))
    expected = {f'{n:02d}' for n in range(1, 96) if n != 20}
    assert set(codes) == expected


def test_periods_are_local_dates_with_exclusive_end(tmp_path):
    periods = read_holiday_periods(calendar(tmp_path), 2023, 2023)
    assert set(periods['zone']) == {'Zone A', 'Zone B', 'Zone C'}
    summer = periods.loc[periods['description'] == "Vacances d'Été"]
    assert set(summer['first_day']) == {date(2024, 7, 6)}  # Saturday, from 22:00 UTC in summer time
    assert set(summer['end_day']) == {date(2024, 9, 2)}  # pupils' row, Monday of the return
    winter_c = periods.loc[(periods['zone'] == 'Zone C') & (periods['description'] == "Vacances d'Hiver")]
    assert set(winter_c['first_day']) == {date(2024, 2, 10)}  # from 23:00 UTC in winter time


def test_share_follows_zones_and_weights(tmp_path):
    periods = read_holiday_periods(calendar(tmp_path), 2023, 2023)
    weights = zone_weights(periods, equal_population())
    assert weights == pytest.approx({'Zone A': 32 / 94, 'Zone B': 41 / 94, 'Zone C': 21 / 94})
    days = [date(2024, 2, 9), date(2024, 2, 10), date(2024, 2, 17), date(2024, 2, 24),
            date(2024, 2, 26), date(2024, 3, 10), date(2024, 3, 11), date(2024, 7, 6), date(2024, 9, 1)]
    share = holiday_share(days, periods, weights)
    expected = [0, 21, 53, 94, 73, 41, 0, 94, 94]
    np.testing.assert_allclose(share.to_numpy(), np.array(expected) / 94)


def test_day_outside_the_file_is_refused(tmp_path):
    periods = read_holiday_periods(calendar(tmp_path), 2023, 2023)
    weights = zone_weights(periods, equal_population())
    for day in (date(2023, 10, 20), date(2024, 9, 2)):  # before the first period, at the last exclusive end
        with pytest.raises(ValueError, match='outside'):
            holiday_share([day], periods, weights)


def test_non_midnight_bound_is_refused(tmp_path):
    path = tmp_path / 'bad.csv'
    path.write_text(f"{HEADER}\nVacances d'Hiver;-;2024-02-09T12:00:00+00:00;2024-02-25T23:00:00+00:00;"
                    'Paris;Zone C;2023-2024', encoding='utf-8')
    with pytest.raises(ValueError, match='midnight'):
        read_holiday_periods(path, 2023, 2023)


def test_builder_adds_one_column_from_the_date_only():
    day = date(2024, 2, 12)  # Monday
    slots = pd.DatetimeIndex(delivery_slots(day, '30min'))

    def base(slots, history, day):
        return pd.DataFrame({'x': history.reindex(slots).to_numpy()}, index=slots)

    history = pd.Series(1.0, index=slots)
    share = {day: 0.4, date(2024, 2, 10): 0.4}
    plain = with_school_holidays(base, share)(slots, history, day)
    assert list(plain.columns) == ['x', SHARE_COLUMN]
    assert (plain[SHARE_COLUMN] == 0.4).all()
    assert (with_school_holidays(base, share, 'working_days')(slots, history, date(2024, 2, 10))[SHARE_COLUMN]
            == 0.0).all()
    # The share does not read consumption: dropping every history value leaves it unchanged.
    empty = with_school_holidays(base, share)(slots, history.iloc[:0], day)
    assert (empty[SHARE_COLUMN] == 0.4).all() and empty['x'].isna().all()
    with pytest.raises(ValueError, match='No school holiday share'):
        with_school_holidays(base, share)(slots, history, date(2024, 2, 13))


@pytest.mark.skipif(not (CALENDAR_FILE.exists() and POPULATION_FILE.exists()),
                    reason='downloaded open data files are not in this checkout')
def test_downloaded_files_are_consistent():
    periods = read_holiday_periods(CALENDAR_FILE, 2020, 2024)
    population = read_department_population(POPULATION_FILE)
    weights = zone_weights(periods, population)
    assert sum(weights.values()) == pytest.approx(1.0)
    share = holiday_share([date(2023, 12, 25), date(2023, 6, 13), date(2024, 8, 15)], periods, weights)
    assert share.tolist() == pytest.approx([1.0, 0.0, 1.0])
