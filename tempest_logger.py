#!/usr/bin/env python3
"""
Tempest Weather Station UDP Logger

Listens for Tempest hub UDP broadcasts on port 50222, extracts obs_st and
device_status messages, accumulates 9am-to-9am daily rainfall, and writes
a timestamped row to PostgreSQL on every observation.

Configuration is read from settings.conf in the same directory as this script.
"""

import json
import logging
import os
import socket
import sys
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import psycopg2
    from psycopg2 import sql as pgsql
except ModuleNotFoundError:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_python = os.path.join(script_dir, ".venv", "bin", "python3")
    if os.path.exists(venv_python):
        os.execv(venv_python, [venv_python, os.path.abspath(__file__), *sys.argv[1:]])
    raise

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UDP_PORT = 50222
UDP_BUFFER = 4096
RAIN_RESET_HOUR = 9  # local clock hour at which the daily rain total resets

# Indices into the obs_st "obs" array
_OBS_TIMESTAMP = 0
_OBS_WIND_AVG = 2
_OBS_WIND_DIR = 4
_OBS_AIR_TEMP = 7
_OBS_REL_HUM = 8
_OBS_SOLAR = 11
_OBS_RAIN_MIN = 12  # rain accumulation over the previous minute (mm)

# The eight fields that must always be present in [column_mapping]
# Only timestamp is strictly required in [column_mapping].
# All other built-in fields are included in the insert only if mapped.
REQUIRED_FIELDS = ("timestamp",)

# The full set of built-in fields the script knows how to populate.
# Any subset may appear in [column_mapping].
BUILTIN_FIELDS = (
    "timestamp",
    "wind_average",
    "wind_direction",
    "air_temperature",
    "relative_humidity",
    "solar_radiation",
    "daily_rain",
    "voltage",
)

# Optional fields that can be added to [column_mapping].
# Each value is a callable that receives the raw obs_st message dict and
# returns the value to store (or None if the key is absent in the message).
OPTIONAL_FIELDS = {
    "serial_number":    lambda msg: msg.get("serial_number"),
    "hub_sn":           lambda msg: msg.get("hub_sn"),
    "firmware_revision": lambda msg: msg.get("firmware_revision"),
}

# Conversion factors from m/s to each supported wind speed unit
WIND_CONVERSIONS = {
    "m/s":   1.0,
    "kph":   3.6,
    "knots": 1.94384,
    "mph":   2.23694,
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(path: str) -> ConfigParser:
    if not os.path.exists(path):
        log.error("Configuration file not found: %s", path)
        sys.exit(1)
    cfg = ConfigParser()
    cfg.read(path)
    for section in ("database", "table", "column_mapping"):
        if section not in cfg:
            log.error("Missing [%s] section in %s", section, path)
            sys.exit(1)
    col_map = dict(cfg["column_mapping"])
    missing = [f for f in REQUIRED_FIELDS if f not in col_map]
    if missing:
        log.error("Missing column_mapping keys: %s", ", ".join(missing))
        sys.exit(1)
    all_known = set(BUILTIN_FIELDS) | set(OPTIONAL_FIELDS)
    unknown = [k for k in col_map if k not in all_known]
    if unknown:
        log.warning("Unrecognised column_mapping keys (will be ignored): %s", ", ".join(unknown))
    wind_unit = cfg.get("units", "wind_speed", fallback="m/s").strip().lower()
    if wind_unit not in WIND_CONVERSIONS:
        log.error(
            "Invalid units.wind_speed '%s'. Must be one of: %s",
            wind_unit, ", ".join(WIND_CONVERSIONS),
        )
        sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def db_connect(url: str):
    conn = psycopg2.connect(url)
    conn.autocommit = False
    return conn


def _table_identifier(table: str) -> pgsql.Identifier:
    """Return a properly quoted Identifier for 'schema.table' or bare 'table'."""
    parts = table.split(".")
    return pgsql.Identifier(*parts)


def db_insert(conn, table: str, col_map: dict, row: dict, static_fields: dict) -> None:
    # Observation fields translated through col_map
    fields = list(row.keys())
    identifiers = [pgsql.Identifier(col_map[k]) for k in fields]
    values = [row[k] for k in fields]
    # Static fields: key is already the DB column name
    for col_name, val in static_fields.items():
        identifiers.append(pgsql.Identifier(col_name))
        values.append(val)
    query = pgsql.SQL(
        "INSERT INTO {tbl} ({cols}) VALUES ({vals})"
    ).format(
        tbl=_table_identifier(table),
        cols=pgsql.SQL(", ").join(identifiers),
        vals=pgsql.SQL(", ").join(pgsql.Placeholder() * len(values)),
    )
    with conn.cursor() as cur:
        cur.execute(query, values)
    conn.commit()


def db_load_last_rain(conn, table: str, col_map: dict, boundary: datetime) -> float:
    """
    Return the most recent daily_rain value recorded after *boundary*.
    Returns 0.0 if no row exists (e.g. fresh start or new rain period).
    """
    if "daily_rain" not in col_map:
        return 0.0
    try:
        ts_col = pgsql.Identifier(col_map["timestamp"])
        rain_col = pgsql.Identifier(col_map["daily_rain"])
        query = pgsql.SQL(
            "SELECT {rain} FROM {tbl} WHERE {ts} >= %s ORDER BY {ts} DESC LIMIT 1"
        ).format(tbl=_table_identifier(table), rain=rain_col, ts=ts_col)
        with conn.cursor() as cur:
            cur.execute(query, (boundary,))
            row = cur.fetchone()
        return float(row[0]) if row else 0.0
    except psycopg2.Error as exc:
        log.warning("Could not load last rain total from DB: %s", exc)
        conn.rollback()
        return 0.0


# ---------------------------------------------------------------------------
# Rain accumulation helpers
# ---------------------------------------------------------------------------

def rain_period_start(dt_local: datetime) -> datetime:
    """
    Return the 9am datetime that begins the current rain accumulation period.
    Before 09:00 local → yesterday at 09:00; otherwise → today at 09:00.
    The returned datetime is timezone-aware (same tzinfo as dt_local).
    """
    nine_am_today = dt_local.replace(
        hour=RAIN_RESET_HOUR, minute=0, second=0, microsecond=0
    )
    if dt_local < nine_am_today:
        return nine_am_today - timedelta(days=1)
    return nine_am_today


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "settings.conf")

    cfg = load_config(config_path)
    db_url = cfg["database"]["connection_url"]
    table = cfg["table"]["name"]
    col_map = dict(cfg["column_mapping"])
    static_fields = dict(cfg["static_fields"]) if "static_fields" in cfg else {}
    if static_fields:
        log.info("Static fields: %s", ", ".join(f"{k}={v!r}" for k, v in static_fields.items()))
    wind_unit = cfg.get("units", "wind_speed", fallback="m/s").strip().lower()
    wind_factor = WIND_CONVERSIONS[wind_unit]
    log.info("Wind speed will be stored in %s", wind_unit)

    conn = db_connect(db_url)
    log.info("Connected to database.")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", UDP_PORT))
    log.info("Listening for Tempest UDP broadcasts on port %d …", UDP_PORT)

    last_voltage: Optional[float] = None
    daily_rain_mm: float = 0.0
    current_boundary: Optional[datetime] = None

    while True:
        data, addr = sock.recvfrom(UDP_BUFFER)
        try:
            msg = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("Malformed UDP packet from %s: %s", addr, exc)
            continue

        msg_type = msg.get("type")

        # ── Device status: capture battery voltage ──────────────────────────
        if msg_type == "device_status":
            v = msg.get("voltage")
            if v is not None:
                last_voltage = float(v)
                log.debug("Device status – voltage: %.3f V", last_voltage)

        # ── Tempest observation ─────────────────────────────────────────────
        elif msg_type == "obs_st":
            obs_list = msg.get("obs")
            if not obs_list:
                log.warning("obs_st message contained no observations")
                continue
            obs = obs_list[0]

            try:
                ts_epoch = obs[_OBS_TIMESTAMP]
                wind_average = obs[_OBS_WIND_AVG]
                wind_direction = obs[_OBS_WIND_DIR]
                air_temperature = obs[_OBS_AIR_TEMP]
                relative_humidity = obs[_OBS_REL_HUM]
                solar_radiation = obs[_OBS_SOLAR]
                rain_per_min = float(obs[_OBS_RAIN_MIN] or 0.0)
            except (IndexError, TypeError) as exc:
                log.warning("Unexpected obs_st payload: %s", exc)
                continue

            # Convert to aware datetimes
            dt_utc = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            dt_local = dt_utc.astimezone()

            # ── Rain accumulation (9am–9am) ─────────────────────────────────
            boundary = rain_period_start(dt_local)

            if current_boundary is None:
                # First observation this session – try to resume from DB
                current_boundary = boundary
                daily_rain_mm = db_load_last_rain(conn, table, col_map, boundary)
                log.info(
                    "Resuming rain period starting %s – loaded %.2f mm from DB",
                    boundary.strftime("%Y-%m-%d %H:%M %Z"),
                    daily_rain_mm,
                )
            elif boundary > current_boundary:
                log.info(
                    "New rain period started at %s (previous total: %.2f mm)",
                    boundary.strftime("%Y-%m-%d %H:%M %Z"),
                    daily_rain_mm,
                )
                daily_rain_mm = 0.0
                current_boundary = boundary

            daily_rain_mm += rain_per_min

            # ── Write to database ───────────────────────────────────────────
            # Build every field the script can provide, then keep only those
            # that have a column mapping configured.
            candidates = {
                "timestamp":         dt_utc,
                "wind_average":      wind_average * wind_factor,
                "wind_direction":    wind_direction,
                "air_temperature":   air_temperature,
                "relative_humidity": relative_humidity,
                "solar_radiation":   solar_radiation,
                "daily_rain":        daily_rain_mm,
                "voltage":           last_voltage,
            }
            for field, extractor in OPTIONAL_FIELDS.items():
                if field in col_map:
                    candidates[field] = extractor(msg)
            row = {k: v for k, v in candidates.items() if k in col_map}

            try:
                db_insert(conn, table, col_map, row, static_fields)
                log.info(
                    "%s | wind %.1f %s %d° | temp %.1f °C | RH %.0f%% | "
                    "solar %d W/m² | rain %.2f mm | batt %s V",
                    dt_local.strftime("%Y-%m-%d %H:%M:%S"),
                    wind_average * wind_factor,
                    wind_unit,
                    wind_direction,
                    air_temperature,
                    relative_humidity,
                    solar_radiation,
                    daily_rain_mm,
                    last_voltage,
                )
            except psycopg2.Error as exc:
                log.error("Database insert failed: %s", exc)
                try:
                    conn.rollback()
                except psycopg2.Error:
                    pass
                # Attempt reconnect so the service self-heals
                try:
                    conn.close()
                except psycopg2.Error:
                    pass
                try:
                    conn = db_connect(db_url)
                    log.info("Reconnected to database.")
                except psycopg2.Error as reconn_exc:
                    log.error("Reconnect failed: %s", reconn_exc)


if __name__ == "__main__":
    main()
