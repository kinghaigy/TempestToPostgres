# TempestToPostgres

Listens for [Tempest Weather Station UDP broadcasts](https://apidocs.tempestwx.com/reference/tempest-udp-broadcast) on port 50222 and inserts a timestamped row into PostgreSQL on every observation (every 60s). There are other observations (like high wind events, lighting strike events, but these are ignored). 

This was a fully vibed project to get something going fast. Any issues, blame OpenAI. Hopefully this is of use to someone else. 

## What gets recorded

Each row contains:

| Internal field | Source | Notes |
|---|---|---|
| `timestamp` | `obs_st` index 0 | UTC epoch → `timestamptz` |
| `wind_average` | `obs_st` index 2 | m/s |
| `wind_direction` | `obs_st` index 4 | degrees |
| `air_temperature` | `obs_st` index 7 | °C |
| `relative_humidity` | `obs_st` index 8 | % |
| `solar_radiation` | `obs_st` index 11 | W/m² |
| `daily_rain` | `obs_st` index 12 accumulated | mm since 09:00 local time |
| `voltage` | `device_status` | last received battery voltage (V) |

The `daily_rain` value is a running sum of per-minute rain that resets at 09:00 local time each day. On startup the script loads the most recent `daily_rain` value from the database for the current 09:00–09:00 window so it continues accumulating correctly after a restart.

---

## Quick start

### 1. Configure

```bash
cp settings_sample.conf settings.conf
$EDITOR settings.conf
```

Set `connection_url` in `[database]` and adjust the `[column_mapping]` section to match your table's column names.

### 2. Create the database table

Example DDL using the default column names from `settings_sample.conf`:

```sql
CREATE TABLE weather_observations (
    id               BIGSERIAL PRIMARY KEY,
    recorded_at      TIMESTAMPTZ NOT NULL,
    wind_avg_ms      DOUBLE PRECISION,
    wind_dir_deg     INTEGER,
    air_temp_c       DOUBLE PRECISION,
    rel_humidity_pct DOUBLE PRECISION,
    solar_rad_wm2    DOUBLE PRECISION,
    daily_rain_mm    DOUBLE PRECISION,
    battery_volts    DOUBLE PRECISION
);
```

### 3. Install as a systemd service

The install script creates a virtual environment in `.venv/`, installs the Python dependencies there, copies the sample config if needed, and installs the systemd service. Keep the project files in the directory you want to run from long term before running `./install.sh`.

```bash
sudo ./install.sh
# Edit settings.conf if you haven't already, then:
sudo systemctl start tempest-logger
sudo journalctl -u tempest-logger -f
```

### 4. Run manually (test)

Use the virtual environment created by the installer if you want to run the logger directly:

```bash
./.venv/bin/python3 tempest_logger.py
```

---

## Files

| File | Purpose |
|---|---|
| `tempest_logger.py` | Main listener script |
| `settings_sample.conf` | Configuration template (committed to git) |
| `settings.conf` | Your local config (git-ignored) |
| `tempest-logger.service.template` | Systemd unit template – paths filled by `install.sh` |
| `install.sh` | Installs and enables the systemd service |
| `requirements.txt` | Python dependencies |
