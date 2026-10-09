"""GFS archive selection, byte-range extraction and raw point decoding (D11)."""

import hashlib
import math
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

import eccodes
import requests

from load_forecast.cutoff import forecast_cutoff

S3_URL = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
VARIABLES = {
    "TMP": ("2 m above ground", "K"),
    "RH": ("2 m above ground", "%"),
    "UGRD": ("10 m above ground", "m s**-1"),
    "VGRD": ("10 m above ground", "m s**-1"),
    "DSWRF": ("surface", "W m**-2"),
    "TCDC": ("entire atmosphere", "%"),
}
# City centres sampled at the nearest GFS grid cell. Fixed representatives, no weights.
POINTS = {
    "paris": (48.8566, 2.3522), "lille": (50.6292, 3.0573),
    "strasbourg": (48.5734, 7.7521), "lyon": (45.7640, 4.8357),
    "marseille": (43.2965, 5.3698), "nice": (43.7102, 7.2620),
    "toulouse": (43.6047, 1.4442), "bordeaux": (44.8378, -0.5792),
    "nantes": (47.2184, -1.5536), "rennes": (48.1173, -1.6778),
    "dijon": (47.3220, 5.0415), "clermont_ferrand": (45.7772, 3.0870),
}


@dataclass(frozen=True)
class Run:
    init_time: datetime
    first_step: int
    fallback: bool

    @property
    def prefix(self) -> str:
        stamp = self.init_time
        return f"gfs.{stamp:%Y%m%d}/{stamp:%H}/atmos/gfs.t{stamp:%H}z.pgrb2.0p25.f"

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.prefix + f"{step:03d}" + suffix
                     for step in range(self.first_step, self.first_step + 27)
                     for suffix in ("", ".idx"))


@dataclass(frozen=True)
class ObjectInfo:
    modified: datetime
    size: int
    etag: str


def candidate_runs(delivery_day: date) -> tuple[Run, Run]:
    """Both runs cover the same UTC hours; the first step supports differencing."""
    primary = datetime.combine(delivery_day - timedelta(days=1), datetime.min.time(), UTC)
    return Run(primary, 21, False), Run(primary - timedelta(hours=6), 27, True)


def request(
    session: requests.Session, url: str, *,
    sleep: Callable[[float], None] = time.sleep,
    consume: Callable[[requests.Response], None] | None = None, **kwargs,
) -> requests.Response:
    """Bound retries; distinguish absent objects from transport/server failures."""
    for attempt in range(5):
        response = None
        try:
            response = session.get(url, timeout=(10, 120), allow_redirects=False, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                response.close()
                raise requests.ConnectionError("Retryable GFS response")
            if response.status_code not in (200, 206, 404):
                raise ValueError(f"GFS request failed with HTTP {response.status_code}")
            if consume:
                consume(response)
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            if response is not None:
                response.close()
            if attempt == 4:
                raise RuntimeError("GFS request failed after five attempts") from None
            sleep(2 ** attempt)
            continue
        except (KeyError, ValueError):
            if response is not None:
                response.close()
            raise
        return response
    raise AssertionError("Unreachable")


def list_objects(session: requests.Session, run: Run) -> dict[str, ObjectInfo]:
    """List every atmospheric step and index, with paginated S3 metadata."""
    objects = {}
    params = {"list-type": "2", "prefix": run.prefix, "max-keys": "1000"}
    seen_tokens = set()
    while True:
        with request(session, S3_URL, params=params) as response:
            if response.status_code != 200:
                raise ValueError("GFS bucket listing failed")
            root = ET.fromstring(response.content)
        for item in root.findall("s3:Contents", NS):
            key = item.findtext("s3:Key", namespaces=NS)
            modified = datetime.fromisoformat(item.findtext("s3:LastModified", namespaces=NS))
            if modified.tzinfo is None:
                raise ValueError("Naive S3 LastModified")
            objects[key] = ObjectInfo(
                modified.astimezone(UTC), int(item.findtext("s3:Size", namespaces=NS)),
                item.findtext("s3:ETag", namespaces=NS),
            )
        if root.findtext("s3:IsTruncated", namespaces=NS) == "false":
            return objects
        token = root.findtext("s3:NextContinuationToken", namespaces=NS)
        if not token or token in seen_tokens:
            raise ValueError("Invalid S3 pagination")
        seen_tokens.add(token)
        params["continuation-token"] = token


def unavailable_keys(run: Run, objects: dict[str, ObjectInfo], cutoff: datetime) -> list[str]:
    """All 54 objects must be nonempty and published strictly before the cutoff."""
    if cutoff.tzinfo is None:
        raise ValueError("Cutoff must be aware")
    return [key for key in run.keys if key not in objects or objects[key].size <= 0
            or objects[key].modified >= cutoff]


def select_run(
    session: requests.Session, delivery_day: date, *, now: datetime | None = None,
) -> tuple[Run | None, dict[str, ObjectInfo], dict[str, list[str]]]:
    """Use identical completeness checks for production and historical collection."""
    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Selection time must be aware")
    if now < forecast_cutoff(delivery_day):
        raise ValueError("Delivery day's cutoff has not arrived")
    failures = {}
    for run in candidate_runs(delivery_day):
        objects = list_objects(session, run)
        missing = unavailable_keys(run, objects, forecast_cutoff(delivery_day))
        if not missing:
            return run, {key: objects[key] for key in run.keys}, failures
        failures[run.init_time.isoformat()] = missing
    return None, {}, failures


def index_ranges(
    index: str, run: Run, step: int, size: int,
    variables: tuple[str, ...] = tuple(VARIABLES),
) -> dict[str, tuple[int, int, str]]:
    """Select exact variable/level/statistic records, never the averaged TCDC line."""
    if not variables or not set(variables) <= VARIABLES.keys():
        raise ValueError("Expected known GFS variables")
    lines = [line.split(":") for line in index.splitlines() if line.strip()]
    if any(len(line) < 6 for line in lines):
        raise ValueError("Malformed GFS index")
    offsets = [int(line[1]) for line in lines]
    if not offsets or offsets[0] != 0 or any(
        a >= b for a, b in zip(offsets, offsets[1:] + [size], strict=True)
    ):
        raise ValueError("Invalid GRIB index offsets")
    selected = {}
    for position, fields in enumerate(lines):
        if len(fields) < 6 or fields[2] != f"d={run.init_time:%Y%m%d%H}":
            raise ValueError("Index run does not match selected initialization")
        variable = fields[3]
        if variable not in variables or fields[4] != VARIABLES[variable][0]:
            continue
        reset = 6 * ((step - 1) // 6)
        expected = (f"{reset}-{step} hour ave fcst" if variable == "DSWRF"
                    else f"{step} hour fcst")
        if fields[5] != expected:
            continue
        if variable in selected:
            raise ValueError("Multiple matching GFS records")
        end = offsets[position + 1] - 1 if position + 1 < len(lines) else size - 1
        selected[variable] = offsets[position], end, ":".join(fields)
    if set(selected) != set(variables):
        raise ValueError("Required variable missing from GFS index")
    return selected


def check_object(response: requests.Response, info: ObjectInfo) -> None:
    """Fail if an archive object changed between completeness and download."""
    modified = parsedate_to_datetime(response.headers["Last-Modified"]).astimezone(UTC)
    if modified != info.modified.replace(microsecond=0) or response.headers["ETag"] != info.etag:
        raise ValueError("GFS object changed after completeness check")


def measure_run_volume(
    session: requests.Session, run: Run, objects: dict[str, ObjectInfo],
) -> dict[str, int]:
    """Measure selected GRIB payload bytes from indexes, without downloading fields."""
    grib_bytes = index_bytes = 0
    for step in range(run.first_step, run.first_step + 27):
        key = run.prefix + f"{step:03d}"
        with request(session, S3_URL + "/" + key + ".idx",
                     headers={"If-Match": objects[key + ".idx"].etag}) as response:
            if response.status_code != 200:
                raise ValueError("GFS index disappeared")
            check_object(response, objects[key + ".idx"])
            index_bytes += len(response.content)
            ranges = index_ranges(response.text, run, step, objects[key].size)
        grib_bytes += sum(end - start + 1 for start, end, _ in ranges.values())
    return {"grib_bytes": grib_bytes, "index_bytes": index_bytes,
            "total_bytes": grib_bytes + index_bytes}


def fetch_message(
    session: requests.Session, key: str, info: ObjectInfo, start: int, end: int,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Require the exact partial response, rejecting full-globe downloads."""
    def validate(response: requests.Response) -> None:
        if response.status_code != 206:
            raise ValueError("Server did not return a byte range")
        check_object(response, info)
        if response.headers.get("Content-Range") != f"bytes {start}-{end}/{info.size}":
            raise ValueError("Incorrect GRIB Content-Range")
        payload = response.content
        if (len(payload) != end - start + 1 or payload[:4] != b"GRIB"
                or payload[-4:] != b"7777" or int.from_bytes(payload[8:16]) != len(payload)):
            raise ValueError("Truncated or multiple GRIB messages")

    with request(session, S3_URL + "/" + key,
                 headers={"Range": f"bytes={start}-{end}", "If-Match": info.etag},
                 stream=True, consume=validate, sleep=sleep) as response:
        return response.content


def decode_points(payload: bytes, run: Run, step: int, variable: str) -> list[dict]:
    """Keep native values and intervals; DSWRF remains a raw running mean."""
    handle = eccodes.codes_new_from_message(payload)
    try:
        def get(key: str):
            return eccodes.codes_get(handle, key)
        if (get("dataDate") != int(run.init_time.strftime("%Y%m%d"))
                or get("dataTime") != run.init_time.hour * 100
                or get("endStep") != step or get("stepUnits") != 1
                or get("units") != VARIABLES[variable][1]):
            raise ValueError("Decoded GRIB metadata does not match the request")
        expected_start = 6 * ((step - 1) // 6) if variable == "DSWRF" else step
        if (get("startStep") != expected_start
                or get("stepType") != ("avg" if variable == "DSWRF" else "instant")):
            raise ValueError("Unexpected GRIB averaging interval")
        coordinates = list(POINTS.values())
        nearest = eccodes.codes_grib_find_nearest_multiple(
            handle, False, [latitude for latitude, _ in coordinates],
            [longitude % 360 for _, longitude in coordinates],
        )
        result = []
        for name, point in zip(POINTS, nearest, strict=True):
            value = float(point["value"])
            if not math.isfinite(value) or value == get("missingValue"):
                raise ValueError("Missing or nonfinite GFS point")
            result.append({"point": name, "latitude": float(point["lat"]),
                           "longitude": float(point["lon"]), "value": value,
                           "units": get("units"), "start_step": expected_start,
                           "end_step": step, "variable": variable})
        return result
    finally:
        eccodes.codes_release(handle)


def extract_run(
    session: requests.Session, run: Run, objects: dict[str, ObjectInfo],
    variables: tuple[str, ...] = tuple(VARIABLES), *, volume: dict[str, int] | None = None,
    steps: tuple[int, ...] | None = None,
) -> list[dict]:
    """Validate requested steps before insertion; default to the full candidate run."""
    rows = []
    steps = tuple(range(run.first_step, run.first_step + 27)) if steps is None else steps
    if len(set(steps)) != len(steps) or any(
        step not in range(run.first_step, run.first_step + 27) for step in steps
    ):
        raise ValueError("Expected distinct steps within the candidate run")
    for step in steps:
        key = run.prefix + f"{step:03d}"
        with request(session, S3_URL + "/" + key + ".idx",
                     headers={"If-Match": objects[key + ".idx"].etag}) as response:
            if response.status_code != 200:
                raise ValueError("GFS index disappeared")
            check_object(response, objects[key + ".idx"])
            ranges = index_ranges(response.text, run, step, objects[key].size, variables)
            if volume is not None:
                volume["index_bytes"] += len(response.content)
        for variable, (start, end, original_line) in ranges.items():
            payload = fetch_message(session, key, objects[key], start, end)
            if volume is not None:
                volume["grib_bytes"] += len(payload)
            digest = hashlib.sha256(payload).hexdigest()
            for point in decode_points(payload, run, step, variable):
                rows.append(point | {
                    "source_url": S3_URL + "/" + key, "message_hash": digest,
                    "index_line": original_line, "byte_start": start, "byte_end": end,
                    "file_last_modified": objects[key].modified,
                    "index_last_modified": objects[key + ".idx"].modified,
                })
    return rows
