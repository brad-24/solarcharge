#!/usr/bin/env python3
"""
SolarCharge: charge a Tesla from surplus solar.
https://github.com/brad-24/solarcharge  (MIT License)

Reads a Sungrow inverter (with smart meter) over Modbus TCP, reads a Tesla
Wall Connector Gen 3 over its local HTTP API, and controls charging over
Bluetooth with Tesla's vehicle-command tool, with Teslemetry as a backup.
Serves a dashboard on the local network.

Standard library only, written for Python 3.7 or later.

Run modes:
  python3 solarcharge.py                 normal (dry run unless config says otherwise)
  python3 solarcharge.py --check         read every device once and exit
  python3 solarcharge.py --simulate      fake inverter, charger and car, for trying the dashboard
  python3 solarcharge.py --simulate --speed 20   same, with the clock running 20x faster
"""

import argparse
import configparser
import json
import logging
import math
import os
import random
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0"
MODES = ("manual", "solar", "minsolar", "fast")
MILE_KM = 1.609344
BAR_PSI = 14.5038

log = logging.getLogger("solarcharge")


# ---------------------------------------------------------------- config

DEFAULTS = {
    "inverter": {
        "host": "",
        "port": "502",
        "unit": "1",
        "pv_register": "5030",
        "load_register": "5090",
        "grid_register": "5600",
    },
    "charger": {
        "host": "",
    },
    "tesla": {
        "vin": "",
        "token_file": "~/.solarcharge_token",
        "api_base": "https://api.teslemetry.com",
        "vehicle_poll_minutes": "0",
        "stream": "true",
        # TeslaTrack on this Pi (for example http://localhost:8084). When set, the car's battery level,
        # charge limit and time to full come from TeslaTrack instead of SolarCharge's own stream.
        "teslatrack_url": "",
        # when you got the car and its odometer then, so distance before TeslaTrack's records can be estimated
        "delivery_date": "",
        "delivery_odometer_km": "0",
        "battery_kwh": "60",
    },
    "schedule": {
        "full_charge_every_days": "7",
        "home_all_day": "mon,tue,wed,thu,fri,sat,sun",
        "forecast": "false",
        "latitude": "0",
        "longitude": "0",
        "pv_kwp": "6.6",
    },
    "ble": {
        "enabled": "false",
        "tool": "~/solarcharge/bin/tesla-control",
        "key_file": "~/.solarcharge_ble_key.pem",
        "adapter": "hci0",
        "fallback_teslemetry": "true",
        "read_battery_minutes": "3",
        "fast_tracking": "true",
        # most amp increases a day that may go through Teslemetry (1 credit each) while
        # Bluetooth is unavailable. Starts, stops and decreases are never held back.
        "teslemetry_raises_per_day": "10",
    },
    "fleet": {
        # Tesla's own Fleet API, used when Bluetooth can't get through and before Teslemetry.
        # Tesla bills about US$0.001 a command and gives every account US$10 a month, so it is
        # free in practice. See FLEET.md for the one time setup.
        "enabled": "false",
        "client_id": "",
        "client_secret": "",
        "domain": "",
        "redirect_uri": "",
        "api_base": "https://fleet-api.prd.na.vn.cloud.tesla.com",
        "key_file": "~/.solarcharge_fleet_key.pem",
        "token_file": "~/.solarcharge_fleet.json",
    },
    "control": {
        "dry_run": "true",
        "default_mode": "solar",
        "poll_seconds": "10",
        "min_amps": "5",
        "max_amps": "16",
        "nominal_volts": "230",
        "start_margin_w": "200",
        "grid_allowance_w": "300",
        "smooth_seconds": "90",
        "start_delay_s": "180",
        "stop_delay_s": "60",
        "min_run_s": "0",
        "min_off_s": "300",
        "amp_up_interval_s": "180",
        "amp_down_interval_s": "90",
        "amp_up_step": "4",
        "amp_up_headroom_w": "250",
        "amp_min_change": "2",
        "emergency_import_w": "1000",
        "max_amp_raises_per_hour": "6",
        "read_fail_stop_s": "60",
        "no_charge_grace_s": "180",
        "no_charge_backoff_s": "1800",
        "command_retry_s": "120",
    },
    "storage": {
        "ram_dir": "/dev/shm/solarcharge",
        "data_dir": "~/solarcharge/data",
        "readings_max_mb": "5",
        "events_max_kb": "512",
        "save_minutes": "30",
        "history_days": "3",
    },
    "tariff": {
        "import_c_per_kwh": "30",
        "feed_in_c_per_kwh": "5",
        "supply_c_per_day": "0",
        "bill_day": "1",
        # for the Sessions page's comparison with a fuel car
        "fuel_l_per_100km": "8",
        "fuel_c_per_l": "200",
        "fuel_name": "petrol",
    },
    "web": {
        "bind": "0.0.0.0",
        "port": "8080",
    },
}


class Settings(object):
    def __init__(self, path):
        # strict=False: a setting listed twice (say, a line added twice) uses the last one instead of failing
        cp = configparser.ConfigParser(strict=False)
        cp.read_dict(DEFAULTS)
        if path and os.path.exists(path):
            cp.read(path)
        self.cp = cp
        g, gi, gf, gb = cp.get, cp.getint, cp.getfloat, cp.getboolean

        self.inv_host = g("inverter", "host").strip()
        self.inv_port = gi("inverter", "port")
        self.inv_unit = gi("inverter", "unit")
        self.reg_pv = gi("inverter", "pv_register")
        self.reg_load = gi("inverter", "load_register")
        self.reg_grid = gi("inverter", "grid_register")

        self.charger_host = g("charger", "host").strip()

        self.vin = g("tesla", "vin").strip()
        self.token_file = os.path.expanduser(g("tesla", "token_file").strip())
        self.api_base = g("tesla", "api_base").strip().rstrip("/")
        self.vehicle_poll_min = gf("tesla", "vehicle_poll_minutes")
        self.stream = gb("tesla", "stream")
        self.teslatrack_url = g("tesla", "teslatrack_url").rstrip("/")
        self.delivery_date = g("tesla", "delivery_date").strip()
        self.delivery_odo = gf("tesla", "delivery_odometer_km")
        self.battery_kwh = gf("tesla", "battery_kwh")

        self.full_every = max(1, gi("schedule", "full_charge_every_days"))
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        self.home_days = [names.index(d.strip()[:3].lower()) for d in g("schedule", "home_all_day").split(",")
                          if d.strip()[:3].lower() in names]
        self.forecast = gb("schedule", "forecast")
        self.lat = gf("schedule", "latitude")
        self.lon = gf("schedule", "longitude")
        self.pv_kwp = gf("schedule", "pv_kwp")

        self.ble = gb("ble", "enabled")
        self.ble_tool = os.path.expanduser(g("ble", "tool").strip())
        self.ble_key = os.path.expanduser(g("ble", "key_file").strip())
        self.ble_adapter = g("ble", "adapter").strip()
        self.ble_fallback = gb("ble", "fallback_teslemetry")
        self.ble_read_min = gf("ble", "read_battery_minutes")
        self.ble_fast = gb("ble", "fast_tracking")
        self.cloud_raises_day = gi("ble", "teslemetry_raises_per_day")

        self.fleet = gb("fleet", "enabled")
        self.fleet_client_id = g("fleet", "client_id").strip()
        self.fleet_client_secret = g("fleet", "client_secret").strip()
        self.fleet_domain = g("fleet", "domain").strip().replace("https://", "").strip("/")
        self.fleet_redirect = g("fleet", "redirect_uri").strip() or (
            "https://%s/callback" % self.fleet_domain if self.fleet_domain else "")
        self.fleet_base = g("fleet", "api_base").strip().rstrip("/")
        self.fleet_key = os.path.expanduser(g("fleet", "key_file").strip())
        self.fleet_token_file = os.path.expanduser(g("fleet", "token_file").strip())

        self.dry_run = gb("control", "dry_run")
        self.default_mode = g("control", "default_mode").strip()
        self.poll = gf("control", "poll_seconds")
        self.min_amps = gi("control", "min_amps")
        self.max_amps = gi("control", "max_amps")
        self.nominal_volts = gf("control", "nominal_volts")
        self.start_margin = gf("control", "start_margin_w")
        self.allowance = gf("control", "grid_allowance_w")
        self.smooth = gf("control", "smooth_seconds")
        self.start_delay = gf("control", "start_delay_s")
        self.stop_delay = gf("control", "stop_delay_s")
        self.min_run = gf("control", "min_run_s")
        self.min_off = gf("control", "min_off_s")
        self.up_interval = gf("control", "amp_up_interval_s")
        self.down_interval = gf("control", "amp_down_interval_s")
        self.up_step = gi("control", "amp_up_step")
        self.up_headroom = gf("control", "amp_up_headroom_w")
        self.min_change = max(1, gi("control", "amp_min_change"))
        self.emergency_import = gf("control", "emergency_import_w")
        self.max_raises_hour = gi("control", "max_amp_raises_per_hour")
        self.read_fail_stop = gf("control", "read_fail_stop_s")
        self.no_charge_grace = gf("control", "no_charge_grace_s")
        self.no_charge_backoff = gf("control", "no_charge_backoff_s")
        self.cmd_retry = gf("control", "command_retry_s")
        # the measured pace for commands that cost credits; Bluetooth may use a quicker one
        self.cloud_up_interval, self.cloud_down_interval = self.up_interval, self.down_interval
        self.cloud_max_raises, self.cloud_headroom = self.max_raises_hour, self.up_headroom

        self.ram_dir = os.path.expanduser(g("storage", "ram_dir").strip())
        self.data_dir = os.path.expanduser(g("storage", "data_dir").strip())
        self.readings_max = int(gf("storage", "readings_max_mb") * 1024 * 1024)
        self.events_max = int(gf("storage", "events_max_kb") * 1024)
        self.save_every = gf("storage", "save_minutes") * 60
        self.history_minutes = int(gf("storage", "history_days") * 24 * 60)

        self.web_bind = g("web", "bind").strip()
        self.web_port = gi("web", "port")

        self.import_price = gf("tariff", "import_c_per_kwh") / 100.0
        self.feed_in_price = gf("tariff", "feed_in_c_per_kwh") / 100.0
        self.supply_price = gf("tariff", "supply_c_per_day") / 100.0
        self.bill_day = min(28, max(1, gi("tariff", "bill_day")))
        self.fuel_l100 = gf("tariff", "fuel_l_per_100km")
        self.fuel_price = gf("tariff", "fuel_c_per_l") / 100.0
        self.fuel_name = g("tariff", "fuel_name").strip() or "fuel"

        if self.default_mode not in MODES:
            self.default_mode = "solar"
        if self.min_amps < 1:
            self.min_amps = 1
        if self.max_amps < self.min_amps:
            self.max_amps = self.min_amps


# ---------------------------------------------------------------- devices

class Inverter(object):
    """Sungrow inverter over Modbus TCP, input registers (function 4)."""

    def __init__(self, s):
        self.s = s
        self.txn = 0

    def _read(self, addr, count):
        self.txn = (self.txn + 1) & 0xFFFF
        sock = socket.create_connection((self.s.inv_host, self.s.inv_port), timeout=3)
        try:
            sock.sendall(struct.pack(">HHHBBHH", self.txn, 0, 6, self.s.inv_unit, 4, addr, count))
            data = b""
            want = 9 + count * 2
            deadline = time.time() + 3
            while len(data) < want and time.time() < deadline:
                chunk = sock.recv(512)
                if not chunk:
                    break
                data += chunk
                if len(data) >= 9 and data[7] & 0x80:
                    break
        finally:
            sock.close()
        if len(data) < 9:
            raise IOError("short Modbus reply")
        if data[7] & 0x80:
            raise IOError("Modbus exception %d at %d" % (data[8], addr))
        n = data[8]
        return list(struct.unpack(">" + "H" * (n // 2), data[9:9 + n]))

    def _s32(self, addr, signed):
        w = self._read(addr, 2)
        raw = (w[1] << 16) | w[0]
        return struct.unpack(">i" if signed else ">I", struct.pack(">I", raw))[0]

    def read(self):
        pv = self._s32(self.s.reg_pv, False)
        load = self._s32(self.s.reg_load, True)
        grid = self._s32(self.s.reg_grid, True)
        if pv > 50000 or abs(load) > 100000 or abs(grid) > 100000:
            raise IOError("implausible reading pv=%s load=%s grid=%s" % (pv, load, grid))
        return {"pv": pv, "load": load, "grid": grid}


class WallConnector(object):
    """Tesla Wall Connector Gen 3 local API, read only."""

    def __init__(self, s):
        self.s = s

    def read(self):
        url = "http://%s/api/1/vitals" % self.s.charger_host
        with urlopen(url, timeout=4) as r:
            v = json.loads(r.read().decode("utf-8"))
        plugged = bool(v.get("vehicle_connected"))
        charging = bool(v.get("contactor_closed"))
        amps = v.get("vehicle_current_a")
        if amps is None:
            amps = v.get("currentA_a", 0.0)
        amps = float(amps or 0.0)
        if not charging:
            amps = 0.0
        volts = float(v.get("grid_v") or 0.0)
        return {
            "plugged": plugged,
            "charging": charging,
            "amps": round(amps, 1),
            "volts": round(volts, 1),
            "session_wh": v.get("session_energy_wh"),
            "session_s": v.get("session_s"),
            "raw_state": v.get("evse_state"),
        }


class Teslemetry(object):
    """Vehicle commands through Teslemetry (Tesla Fleet API paths)."""

    def __init__(self, s):
        self.s = s

    def _token(self):
        try:
            with open(self.s.token_file) as f:
                return f.read().strip()
        except IOError:
            return ""

    def configured(self):
        return bool(self.s.vin) and bool(self._token())

    def _call(self, method, path, body=None, timeout=45):
        token = self._token()
        if not token or not self.s.vin:
            return False, "Teslemetry token or VIN not set", None
        url = self.s.api_base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "solarcharge/" + VERSION)
        try:
            with urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8") or "{}")
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            return False, "HTTP %d %s" % (e.code, detail), None
        except (URLError, socket.timeout, OSError) as e:
            return False, "network error: %s" % e, None
        return True, "", payload

    def command(self, name, body=None):
        ok, err, payload = self._call(
            "POST", "/api/1/vehicles/%s/command/%s" % (self.s.vin, name), body or {})
        if not ok:
            return False, err
        resp = (payload or {}).get("response") or {}
        if resp.get("result") is True:
            return True, ""
        reason = str(resp.get("reason") or payload)
        # Harmless "already in that state" replies count as success.
        if name == "charge_start" and reason in ("is_charging",):
            return True, reason
        if name == "charge_stop" and reason in ("not_charging",):
            return True, reason
        return False, reason

    def charge_state(self):
        ok, err, payload = self._call(
            "GET", "/api/1/vehicles/%s/vehicle_data?endpoints=charge_state" % self.s.vin, timeout=30)
        if not ok:
            return None, err
        cs = ((payload or {}).get("response") or {}).get("charge_state") or {}
        return {
            "soc": cs.get("battery_level"),
            "limit": cs.get("charge_limit_soc"),
            "state": cs.get("charging_state"),
        }, ""

    def ping(self):
        ok, err, payload = self._call("GET", "/api/1/vehicles/%s" % self.s.vin, timeout=20)
        if not ok:
            return False, err
        resp = (payload or {}).get("response") or {}
        return True, "%s, state %s" % (resp.get("display_name") or "vehicle", resp.get("state"))




class Weather(object):
    """Today's weather from Open-Meteo: now, today's range and the next hours, refreshed every 30 minutes."""

    def __init__(self, s):
        self.s = s
        self.data = None
        self.updated = 0.0
        self.error = ""

    def start(self):
        th = threading.Thread(target=self._run, name="weather")
        th.daemon = True
        th.start()

    def _run(self):
        while True:
            try:
                self.data = self.fetch()
                self.updated = time.time()
                self.error = ""
                time.sleep(30 * 60)
            except Exception as e:
                self.error = str(e)
                log.warning("Weather unavailable: %s", e)
                time.sleep(10 * 60)

    def fetch(self):
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f"
               "&current=temperature_2m,apparent_temperature,weather_code,is_day"
               "&hourly=temperature_2m,precipitation_probability,weather_code,is_day,shortwave_radiation"
               "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum"
               "&timezone=auto&forecast_days=2") % (self.s.lat, self.s.lon)
        req = Request(url, headers={"User-Agent": "solarcharge/" + VERSION})
        with urlopen(req, timeout=20) as r:
            return self.parse(json.loads(r.read().decode("utf-8")), self.s.pv_kwp)

    @staticmethod
    def parse(j, kwp):
        c, h, d = j.get("current") or {}, j.get("hourly") or {}, j.get("daily") or {}
        col = lambda src, k, i: (src.get(k) or [None] * (i + 1))[i] if i < len(src.get(k) or []) else None
        hours = []
        for i, t in enumerate(h.get("time") or []):
            rad = col(h, "shortwave_radiation", i)
            hours.append({"t": t, "temp": col(h, "temperature_2m", i), "rain": col(h, "precipitation_probability", i),
                          "code": col(h, "weather_code", i), "day": col(h, "is_day", i),
                          # same rough conversion as the daily forecast: panels give about 80% of rated output
                          "solar_kw": round(rad / 1000.0 * kwp * 0.8, 1) if rad is not None else None})
        return {
            "now": {"temp": c.get("temperature_2m"), "feels": c.get("apparent_temperature"),
                    "code": c.get("weather_code"), "day": c.get("is_day")},
            "today": {"min": col(d, "temperature_2m_min", 0), "max": col(d, "temperature_2m_max", 0),
                      "rain": col(d, "precipitation_probability_max", 0), "rain_mm": col(d, "precipitation_sum", 0)},
            "hours": hours,
        }

    def snapshot(self, now):
        if not self.data:
            return {"ok": False, "error": self.error or "Loading"}
        hour = time.strftime("%Y-%m-%dT%H:00", time.localtime(now))
        nxt = [x for x in self.data["hours"] if x["t"] >= hour][:12]
        return {"ok": True, "now": self.data["now"], "today": self.data["today"], "hours": nxt,
                "updated": self.updated}


class Forecast(object):
    """Seven day solar forecast from Open-Meteo (free, no account), refreshed every three hours."""

    def __init__(self, s):
        self.s = s
        self.days = []
        self.updated = 0.0
        self.error = ""

    def start(self):
        th = threading.Thread(target=self._run, name="forecast")
        th.daemon = True
        th.start()

    def _run(self):
        while True:
            try:
                self.days = self.fetch()
                self.updated = time.time()
                self.error = ""
                time.sleep(3 * 3600)
            except Exception as e:
                self.error = str(e)
                log.warning("Solar forecast unavailable: %s", e)
                time.sleep(15 * 60)

    def fetch(self):
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%.3f&longitude=%.3f"
               "&daily=shortwave_radiation_sum,precipitation_probability_max,cloud_cover_mean,sunset"
               "&timezone=auto&forecast_days=8") % (self.s.lat, self.s.lon)
        req = Request(url, headers={"User-Agent": "solarcharge/" + VERSION})
        with urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8")).get("daily") or {}
        return self.parse(d, self.s.pv_kwp)

    @staticmethod
    def parse(d, kwp):
        out = []
        dates = d.get("time") or []
        rad = d.get("shortwave_radiation_sum") or []
        rain = d.get("precipitation_probability_max") or []
        cloud = d.get("cloud_cover_mean") or []
        sunset = d.get("sunset") or []
        for i, day in enumerate(dates):
            mj = rad[i] if i < len(rad) and rad[i] is not None else None
            # MJ/m2 to kWh/m2, times system size, times a typical 0.8 performance ratio
            kwh = round(mj / 3.6 * kwp * 0.8, 1) if mj is not None else None
            c = cloud[i] if i < len(cloud) else None
            if mj is None:
                label = "unknown"
            elif mj >= 24 and (c is None or c < 35):
                label = "sunny"
            elif mj >= 17:
                label = "mostly sunny"
            elif mj >= 11:
                label = "partly cloudy"
            else:
                label = "cloudy"
            out.append({"date": day, "solar_kwh": kwh, "label": label,
                        "rain": rain[i] if i < len(rain) else None, "cloud": c,
                        "sunset": sunset[i] if i < len(sunset) else None})
        return out

class BleTesla(object):
    """Sends charging commands straight to the car over Bluetooth using Tesla's own
    tesla-control tool. Free: no account, no credits, no internet needed."""

    ARGS = {"charge_start": ["charging-start"], "charge_stop": ["charging-stop"]}

    def __init__(self, s):
        self.s = s
        self.ok_count = 0
        self.fail_count = 0
        self.busy_count = 0
        self.last_ok = 0.0
        self.last_error = ""

    def configured(self):
        return bool(self.s.ble and self.s.vin) and os.path.exists(self.s.ble_tool) and os.path.exists(self.s.ble_key)

    def _run(self, args, timeout=45):
        cmd = [self.s.ble_tool, "-ble", "-vin", self.s.vin, "-key-file", self.s.ble_key,
               "-bt-adapter", self.s.ble_adapter, "-connect-timeout", "20s", "-command-timeout", "15s"] + args
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                               universal_newlines=True)
        except subprocess.TimeoutExpired:
            return False, "timed out", ""
        except OSError as e:
            return False, str(e), ""
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip().replace("\n", " ")
        return p.returncode == 0, err[-300:], out

    @staticmethod
    def _busy(err):
        return "maximum number of ble devices" in (err or "").lower()

    def _translate(self, name, ok, err):
        if ok:
            return True, ""
        low = err.lower()
        if name == "charge_start" and "is_charging" in low:
            return True, "is_charging"
        if name == "charge_stop" and "not_charging" in low:
            return True, "not_charging"
        if name == "charge_start" and "complete" in low:
            return False, "complete"
        return False, err or "failed"

    def command(self, name, body=None):
        if name == "set_charging_amps":
            args = ["charging-set-amps", str(int((body or {}).get("charging_amps", 5)))]
        elif name in self.ARGS:
            args = list(self.ARGS[name])
        else:
            return False, "unsupported over Bluetooth: " + name
        ok, err, _ = self._run(args)
        res = self._translate(name, ok, err)
        # the car accepts only a few Bluetooth connections at once (phone keys use them too):
        # wait briefly for a slot instead of giving up
        tries = 0
        while not res[0] and self._busy(res[1]) and tries < 3:
            self.busy_count += 1
            tries += 1
            time.sleep(10)
            ok, err, _ = self._run(args)
            res = self._translate(name, ok, err)
        if not res[0] and res[1] != "complete" and not self._busy(res[1]):
            # the car's main computer may be asleep: wake it (free over Bluetooth) and try once more
            self._run(["-domain", "vcsec", "wake"], timeout=35)
            time.sleep(8)
            ok, err, _ = self._run(args)
            res = self._translate(name, ok, err)
        if res[0] or res[1] == "complete":
            self.ok_count += 1
            self.last_ok = time.time()
        else:
            self.fail_count += 1
            self.last_error = res[1]
        return res

    def charge_state(self):
        ok, err, out = self._run(["state", "charge"], timeout=40)
        if not ok:
            return None, err or "failed"
        try:
            data = json.loads(out)
        except ValueError:
            return None, "unreadable reply"

        def find(d, names):
            if isinstance(d, dict):
                for k, v in d.items():
                    if k in names and not isinstance(v, (dict, list)):
                        return v
                for v in d.values():
                    r = find(v, names)
                    if r is not None:
                        return r
            return None

        soc = find(data, ("batteryLevel", "battery_level"))
        limit = find(data, ("chargeLimitSoc", "charge_limit_soc"))
        mins = find(data, ("minutesToFullCharge", "minutes_to_full_charge"))
        rng = find(data, ("batteryRange", "battery_range"))
        res = {}
        try:
            if soc is not None:
                res["soc"] = int(round(float(soc)))
            if limit is not None:
                res["limit"] = int(round(float(limit)))
            if mins is not None:
                res["ttf_h"] = round(float(mins) / 60.0, 2)
            if rng is not None and 0 <= float(rng) < 1000:
                res["range_km"] = int(round(float(rng) * MILE_KM))
        except (TypeError, ValueError):
            pass
        return (res or None), ("" if res else "no charge data in reply")

    def is_awake(self):
        """True or False from the car's security computer, which answers without waking the car.
        None if it couldn't be reached."""
        ok, err, out = self._run(["body-controller-state"], timeout=40)
        if not ok:
            return None
        try:
            st = json.loads(out)
        except ValueError:
            return None
        sleep = st.get("vehicleSleepStatus") or st.get("vehicle_sleep_status") or ""
        return "AWAKE" in sleep and "ASLEEP" not in sleep

    def ping(self):
        ok, err, out = self._run(["body-controller-state"], timeout=40)
        if not ok:
            return False, err or "failed"
        try:
            st = json.loads(out)
            sleep = st.get("vehicleSleepStatus") or st.get("vehicle_sleep_status") or ""
            return True, "car reached over Bluetooth" + (", %s" % sleep.replace("VEHICLE_SLEEP_STATUS_", "").lower() if sleep else "")
        except ValueError:
            return True, "car reached over Bluetooth"


class FleetTesla(object):
    """Tesla's own Fleet API, reached with the same tesla-control tool over the internet. Commands
    are signed with SolarCharge's own key, which is added to the car once. Tesla bills about
    US$0.001 a command (a wake about US$0.02), well inside the US$10 a month every account gets."""

    AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
    TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    SCOPES = "openid offline_access vehicle_device_data vehicle_cmds vehicle_charging_cmds"

    def __init__(self, s):
        self.s = s
        self.ok_count = 0
        self.fail_count = 0
        self.fail_streak = 0
        self.wakes = 0
        self.last_ok = 0.0
        self.last_error = ""
        self.access_file = self.s.fleet_token_file + ".access"
        self.cache_file = self.s.fleet_token_file + ".session"

    def configured(self):
        s = self.s
        return bool(s.fleet and s.vin and s.fleet_client_id and os.path.exists(s.ble_tool)
                    and os.path.exists(s.fleet_key) and os.path.exists(s.fleet_token_file))

    # tokens
    def _load(self):
        with open(self.s.fleet_token_file) as f:
            return json.load(f)

    @staticmethod
    def _write_private(path, text):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    def save_tokens(self, resp):
        tok = {"access_token": resp["access_token"], "refresh_token": resp["refresh_token"],
               "expires_at": time.time() + float(resp.get("expires_in") or 28800)}
        self._write_private(self.s.fleet_token_file, json.dumps(tok))
        self._write_private(self.access_file, tok["access_token"])
        return tok

    @classmethod
    def post_form(cls, url, fields, timeout=30):
        from urllib.parse import urlencode
        req = Request(url, data=urlencode(fields).encode(), method="POST",
                      headers={"Content-Type": "application/x-www-form-urlencoded",
                               "User-Agent": "solarcharge/%s" % VERSION})
        try:
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            raise RuntimeError("HTTP %s %s" % (e.code, e.read().decode(errors="replace")[:300]))

    def _access(self):
        """A current access token in the file tesla-control reads, refreshed when close to expiry.
        Refresh tokens are single use, so each new one is saved straight away."""
        tok = self._load()
        if tok.get("expires_at", 0) - time.time() < 600 or not os.path.exists(self.access_file):
            resp = self.post_form(self.TOKEN_URL, {"grant_type": "refresh_token",
                                                   "client_id": self.s.fleet_client_id,
                                                   "refresh_token": tok["refresh_token"]})
            tok = self.save_tokens(resp)
            log.info("Tesla API login renewed")
        return self.access_file

    # commands
    def _run(self, args, timeout=60):
        try:
            token_path = self._access()
        except Exception as e:
            return False, "login: %s" % e, ""
        cmd = [self.s.ble_tool, "-vin", self.s.vin, "-key-file", self.s.fleet_key, "-token-file", token_path,
               "-session-cache", self.cache_file, "-command-timeout", "20s"] + args
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                               universal_newlines=True)
        except subprocess.TimeoutExpired:
            return False, "timed out", ""
        except OSError as e:
            return False, str(e), ""
        err = (p.stderr or "").strip().replace("\n", " ")
        return p.returncode == 0, err[-300:], (p.stdout or "").strip()

    @staticmethod
    def _asleep(err):
        low = (err or "").lower()
        return any(k in low for k in ("offline", "asleep", "408", "unavailable", "not online"))

    def command(self, name, body=None):
        if name == "set_charging_amps":
            args = ["charging-set-amps", str(int((body or {}).get("charging_amps", 5)))]
        elif name in BleTesla.ARGS:
            args = list(BleTesla.ARGS[name])
        else:
            return False, "unsupported: " + name
        ok, err, _ = self._run(args)
        res = BleTesla._translate(self, name, ok, err)
        if not res[0] and res[1] != "complete" and self._asleep(res[1]):
            # the car is asleep: wake it over the internet and try once more
            self.wakes += 1
            self._run(["wake"], timeout=60)
            time.sleep(20)
            ok, err, _ = self._run(args)
            res = BleTesla._translate(self, name, ok, err)
        if res[0] or res[1] == "complete":
            self.ok_count += 1
            self.fail_streak = 0
            self.last_ok = time.time()
        else:
            self.fail_count += 1
            self.fail_streak += 1
            self.last_error = res[1]
        return res

    def charge_state(self):
        return None, "not read over the Tesla API"

    def ping(self):
        try:
            self._access()
            req = Request(self.s.fleet_base + "/api/1/vehicles", headers={
                "Authorization": "Bearer " + open(self.access_file).read().strip()})
            with urlopen(req, timeout=20) as r:
                cars = json.loads(r.read().decode()).get("response") or []
        except Exception as e:
            return False, str(e)[:300]
        mine = [c for c in cars if c.get("vin") == self.s.vin]
        if not mine:
            return False, "logged in, but the VIN in config.ini isn't on this Tesla account"
        return True, "logged in, car is %s" % mine[0].get("state", "unknown")


class CarControl(object):
    """Bluetooth first, Teslemetry as the backup. Backup is used for starts, stops and
    amp reductions only, so a Bluetooth problem can never strand the car charging from
    the grid, but routine amp increases don't spend credits."""

    def __init__(self, ble, cloud, s, fleet=None):
        self.ble = ble
        self.cloud = cloud
        self.fleet = fleet
        self.s = s
        self.last_path = ""
        self.fallbacks = 0
        self.prev_amps = None
        self.busy_until = 0.0

    def configured(self):
        return self.ble.configured() or self._fleet_ok() or self.cloud.configured()

    def _fleet_ok(self):
        return self.fleet is not None and self.fleet.configured()

    def _done(self, path, name, body, ok, err):
        if ok or err == "complete":
            self.last_path = path
            self._remember(name, body)
        return ok, err

    def command(self, name, body=None):
        err = "not set up"
        busy = False
        if self.ble.configured():
            if time.time() < self.busy_until:
                # the car's Bluetooth was full a moment ago (phone keys): don't waste time retrying
                busy = True
            else:
                ok, err = self.ble.command(name, body)
                if ok or err == "complete":
                    return self._done("bluetooth", name, body, ok, err)
                log.warning("Bluetooth %s failed: %s", name, err)
                err = "Bluetooth: " + err
                if self.ble._busy(err):
                    busy = True
                    self.busy_until = time.time() + 300
                    log.warning("Car's Bluetooth is full (phone keys nearby); using the backup for the next 5 minutes")

        # Tesla's own API next: free in practice, so it can carry every kind of command
        if self._fleet_ok():
            ok, ferr = self.fleet.command(name, body)
            if ok or ferr == "complete":
                if self.ble.configured():
                    self.fallbacks += 1
                return self._done("fleet", name, body, ok, ferr)
            log.warning("Tesla API %s failed: %s", name, ferr)
            err = "Tesla API: " + ferr

        # Teslemetry last, as it costs credits: starts, stops and decreases always (so a
        # failure can never strand the car charging from the grid), increases only when
        # Bluetooth is busy or not set up
        reducing = name == "set_charging_amps" and self.prev_amps is not None and \
            int((body or {}).get("charging_amps", 0)) < self.prev_amps
        if not self.cloud.configured():
            return False, err
        if self.ble.configured() and not (self.s.ble_fallback and
                                          (busy or name in ("charge_start", "charge_stop") or reducing)):
            return False, err
        if self.ble.configured() or self._fleet_ok():
            log.warning("Using Teslemetry for %s instead", name)
            self.fallbacks += 1
        ok, cerr = self.cloud.command(name, body)
        return self._done("teslemetry", name, body, ok, cerr)

    def cloud_only(self):
        """True while commands would go through Teslemetry (and cost credits) instead of a free route."""
        if self._fleet_ok() and self.fleet.fail_streak < 2:
            return False
        if not self.ble.configured():
            return True
        if not (self.s.ble_fallback and self.cloud.configured()):
            return False
        # while the car's Bluetooth is full, or the last command had to use Teslemetry, assume the
        # next one will too, until Bluetooth works again
        return time.time() < self.busy_until or self.last_path == "teslemetry"

    def _remember(self, name, body):
        if name == "set_charging_amps":
            self.prev_amps = int((body or {}).get("charging_amps", 0))

    def charge_state(self):
        if self.ble.configured():
            return self.ble.charge_state()
        return self.cloud.charge_state()

    def status(self):
        return {"ble": self.ble.configured(), "ble_ok": self.ble.ok_count, "ble_fail": self.ble.fail_count,
                "ble_busy": self.ble.busy_count,
                "ble_last_ok": self.ble.last_ok, "ble_error": self.ble.last_error,
                "path": self.last_path, "fallbacks": self.fallbacks,
                "fleet": self._fleet_ok(),
                "fleet_ok": self.fleet.ok_count if self.fleet else 0,
                "fleet_fail": self.fleet.fail_count if self.fleet else 0,
                "fleet_wakes": self.fleet.wakes if self.fleet else 0,
                "fleet_error": self.fleet.last_error if self.fleet else "",
                "busy_until": self.busy_until}


class TeslemetryStream(object):
    """Listens to Teslemetry's free streaming feed (server sent events) for the car's
    battery level, charge limit, temperatures, range, odometer and tyre pressures. Costs no credits and never wakes the car; values
    arrive whenever the car is awake and they change."""

    def __init__(self, s, on_update):
        self.s = s
        self.on_update = on_update
        self.connected = False
        self.last_error = ""

    def start(self):
        th = threading.Thread(target=self._run, name="stream")
        th.daemon = True
        th.start()

    def _token(self):
        try:
            with open(self.s.token_file) as f:
                return f.read().strip()
        except IOError:
            return ""

    def _run(self):
        backoff = 5
        while True:
            token = self._token()
            if not token or not self.s.vin:
                time.sleep(60)
                continue
            try:
                req = Request("%s/sse/%s" % (self.s.api_base, self.s.vin))
                req.add_header("Authorization", "Bearer " + token)
                req.add_header("Accept", "text/event-stream")
                req.add_header("User-Agent", "solarcharge/" + VERSION)
                with urlopen(req, timeout=600) as r:
                    if not self.connected:
                        log.info("Car data stream connected")
                    self.connected = True
                    backoff = 5
                    buf = []
                    for raw in r:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            buf.append(line[5:].strip())
                        elif line == "" and buf:
                            self._handle("".join(buf))
                            buf = []
            except HTTPError as e:
                self.last_error = "HTTP %d" % e.code
            except Exception as e:
                self.last_error = str(e)
            if self.connected:
                log.info("Car data stream disconnected (%s), reconnecting", self.last_error or "closed")
            self.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

    @staticmethod
    def _num(v):
        if isinstance(v, dict):
            for k in ("value", "intValue", "floatValue", "doubleValue", "stringValue"):
                if k in v:
                    v = v[k]
                    break
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _bool(v):
        if isinstance(v, dict):
            v = v.get("boolValue", v.get("value"))
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() == "true"
        return None

    @staticmethod
    def _text(v):
        if isinstance(v, dict):
            v = v.get("stringValue", v.get("value"))
        return v.strip() if isinstance(v, str) else None

    @classmethod
    def _enum(cls, v, prefix):
        t = cls._text(v)
        return t.replace(prefix, "") if t else None

    def _handle(self, payload):
        try:
            ev = json.loads(payload)
        except ValueError:
            return
        data = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(data, dict):
            return
        upd = {}
        for key in ("BatteryLevel", "Soc"):
            if key in data and "soc" not in upd:
                v = self._num(data[key])
                if v is not None and 0 <= v <= 100:
                    upd["soc"] = int(round(v))
        if "TimeToFullCharge" in data:
            v = self._num(data["TimeToFullCharge"])
            if v is not None and 0 <= v < 100:
                upd["ttf_h"] = round(v, 2)
        if "ChargeLimitSoc" in data:
            v = self._num(data["ChargeLimitSoc"])
            if v is not None and 0 < v <= 100:
                upd["limit"] = int(round(v))
        if "InsideTemp" in data:
            v = self._num(data["InsideTemp"])
            if v is not None and -40 < v < 90:
                upd["cabin"] = round(v, 1)
        if "OutsideTemp" in data:
            v = self._num(data["OutsideTemp"])
            if v is not None and -40 < v < 70:
                upd["outside"] = round(v, 1)
        # Tesla reports distances in miles and tyre pressures in bar
        for key in ("RatedRange", "EstBatteryRange"):
            if key in data and "range_km" not in upd:
                v = self._num(data[key])
                if v is not None and 0 <= v < 1000:
                    upd["range_km"] = int(round(v * MILE_KM))
        # battery health
        if "EnergyRemaining" in data:
            v = self._num(data["EnergyRemaining"])
            if v is not None and 0 <= v < 200:
                upd["energy_kwh"] = round(v, 2)
        for key, name in (("BrickVoltageMax", "brick_max"), ("BrickVoltageMin", "brick_min")):
            if key in data:
                v = self._num(data[key])
                if v is not None and 1.5 < v < 5:
                    upd[name] = round(v, 3)
        # security
        if "Locked" in data:
            v = self._bool(data["Locked"])
            if v is not None:
                upd["locked"] = v
        if "DoorState" in data and isinstance(data["DoorState"], dict):
            upd["doors_open"] = sorted(k for k, v in data["DoorState"].items() if self._bool(v))
        for key, name in (("FdWindow", "win_fd"), ("FpWindow", "win_fp"), ("RdWindow", "win_rd"), ("RpWindow", "win_rp")):
            if key in data:
                v = self._enum(data[key], "WindowState")
                if v:
                    upd[name] = v
        if "SentryMode" in data:
            v = self._enum(data["SentryMode"], "SentryModeState")
            if v:
                upd["sentry"] = v
        # where the car is: used on the Pi only to work out home or away, never stored or shown
        if "Location" in data and isinstance(data["Location"], dict):
            lat, lon = self._num(data["Location"].get("latitude")), self._num(data["Location"].get("longitude"))
            if lat is not None and lon is not None and (lat or lon):
                upd["_loc"] = (lat, lon)
        # charging away from home, and energy that reached the battery
        if "FastChargerPresent" in data:
            v = self._bool(data["FastChargerPresent"])
            if v is not None:
                upd["fast_charger"] = v
        for key, name in (("DCChargingEnergyIn", "dc_in"), ("ACChargingEnergyIn", "ac_in")):
            if key in data:
                v = self._num(data[key])
                if v is not None and 0 <= v < 500:
                    upd[name] = round(v, 3)
        # software
        if "Version" in data:
            v = self._text(data["Version"])
            if v:
                upd["version"] = v.split()[0]
        if "SoftwareUpdateVersion" in data:
            v = self._text(data["SoftwareUpdateVersion"])
            upd["update_version"] = v.split()[0] if v else ""
        if "VehicleSpeed" in data:
            v = self._num(data["VehicleSpeed"])
            if v is not None and 0 <= v < 200:
                upd["speed_kmh"] = int(round(v * MILE_KM))
        if "Odometer" in data:
            v = self._num(data["Odometer"])
            if v is not None and 0 <= v < 2000000:
                upd["odo_km"] = round(v * MILE_KM, 1)
        for key, name in (("TpmsPressureFl", "tp_fl"), ("TpmsPressureFr", "tp_fr"),
                          ("TpmsPressureRl", "tp_rl"), ("TpmsPressureRr", "tp_rr")):
            if key in data:
                v = self._num(data[key])
                if v is not None and 0.5 < v < 6:
                    upd[name] = round(v * BAR_PSI, 1)
                    upd["tp_t"] = time.time()
        if upd:
            self.on_update(upd)


class TeslaTrackFeed(object):
    """Takes the car's battery level, charge limit and time to full from TeslaTrack on this Pi,
    so only one app holds the Teslemetry stream."""

    def __init__(self, s, ctl):
        self.s = s
        self.ctl = ctl
        self.connected = False
        self.last = None

    def start(self):
        th = threading.Thread(target=self._run, name="teslatrack")
        th.daemon = True
        th.start()

    def _run(self):
        n = 0
        while True:
            if n % 20 == 0:
                self._charging()
            n += 1
            try:
                with urlopen(self.s.teslatrack_url + "/api/vehicle", timeout=5) as r:
                    v = json.loads(r.read().decode("utf-8"))
                if not self.connected:
                    log.info("Car data from TeslaTrack connected")
                self.connected = True
                self.ctl.tt_home_days = v.get("home_days")
                upd = {k: v[k] for k in ("soc", "limit", "ttf_h", "range_km", "energy_kwh", "ac_in", "home")
                       if v.get(k) is not None}
                # only pass on readings that are new, so stale values don't look fresh
                if upd and v.get("t") != self.last:
                    self.last = v.get("t")
                    self.ctl.vehicle_update(upd)
            except Exception as e:
                if self.connected:
                    log.info("Car data from TeslaTrack unavailable (%s)", e)
                self.connected = False
            time.sleep(15)

    def _charging(self):
        """Away charging sessions (public chargers, Superchargers) that TeslaTrack recorded,
        shown on the Sessions page next to the home ones. Every 5 minutes."""
        try:
            with urlopen(self.s.teslatrack_url + "/api/charging", timeout=5) as r:
                recs = json.loads(r.read().decode("utf-8"))
            if isinstance(recs, list):
                self.ctl.tt_charging = recs
        except Exception:
            pass


# ---------------------------------------------------------------- simulator

class SimWorld(object):
    """Fake house, solar and car so the dashboard can be tried safely."""

    def __init__(self, clock):
        self.clock = clock
        self.plugged = True
        self.charging = False
        self.amps = 16
        self.cloud = 1.0
        self.spike_until = 0
        self.session_wh = 0.0
        self.last = clock()

    def step(self):
        now = self.clock()
        dt = max(0.0, now - self.last)
        self.last = now
        lt = time.localtime(now)
        hour = lt.tm_hour + lt.tm_min / 60.0
        sun = max(0.0, math.sin(math.pi * (hour - 5.5) / 13.0))
        self.cloud += random.uniform(-0.08, 0.08)
        if random.random() < 0.01:
            self.cloud = random.uniform(0.3, 1.0)
        self.cloud = min(1.0, max(0.25, self.cloud))
        pv = int(9500 * sun * self.cloud)
        self.plugged = not (17.0 <= hour < 17.5)
        house = 450 + random.randint(0, 150)
        if random.random() < 0.01:
            self.spike_until = now + random.randint(60, 600)
        if now < self.spike_until:
            house += 2200
        car_w = int(self.amps * 240) if (self.plugged and self.charging) else 0
        self.session_wh += car_w * dt / 3600.0
        load = house + car_w
        return {"pv": pv, "load": load, "grid": load - pv}


class SimInverter(object):
    def __init__(self, world):
        self.w = world

    def read(self):
        return self.w.step()


class SimCharger(object):
    def __init__(self, world):
        self.w = world

    def read(self):
        on = self.w.plugged and self.w.charging
        return {"plugged": self.w.plugged, "charging": on,
                "amps": float(self.w.amps) if on else 0.0, "volts": 240.0,
                "session_wh": int(self.w.session_wh), "raw_state": None, "session_s": None}


class SimTesla(object):
    def __init__(self, world):
        self.w = world

    def configured(self):
        return True

    def command(self, name, body=None):
        if name == "charge_start":
            self.w.charging = True
        elif name == "charge_stop":
            self.w.charging = False
        elif name == "set_charging_amps":
            self.w.amps = int((body or {}).get("charging_amps", self.w.amps))
        return True, ""

    def charge_state(self):
        return {"soc": int(48 + self.w.session_wh / 750.0), "limit": 80,
                "state": "Charging" if self.w.charging else "Stopped"}, ""

    def ping(self):
        return True, "simulated car"


# ---------------------------------------------------------------- storage

class Storage(object):
    """Routine readings go to RAM; events and periodic state go to the SD card."""

    def __init__(self, s):
        self.s = s
        for d in (s.ram_dir, s.data_dir):
            os.makedirs(d, exist_ok=True)
        self.readings_path = os.path.join(s.ram_dir, "readings.csv")
        self.state_path = os.path.join(s.data_dir, "state.json")
        self.sessions_path = os.path.join(s.data_dir, "sessions.json")
        self.events_path = os.path.join(s.data_dir, "events.log")
        self.archive_dir = os.path.join(s.data_dir, "archive")
        os.makedirs(self.archive_dir, exist_ok=True)

    MINUTES_HEADER = "time,solar_w,house_w,grid_w,car_w,car_amps,battery_pct,cabin_c,outside_c,range_km\n"
    DAILY_HEADER = ("date,solar_kwh,car_kwh,car_from_solar_kwh,imported_kwh,exported_kwh,starts,stops,commands,"
                    "odometer_km,driven_km,top_kmh,tyre_fl_psi,tyre_fr_psi,tyre_rl_psi,tyre_rr_psi,"
                    "battery_capacity_kwh,cell_spread_mv,battery_in_kwh,wh_per_km,away_charged_kwh,home_hours\n")

    def _append(self, path, header, lines):
        try:
            if os.path.exists(path):
                with open(path) as f:
                    first = f.readline()
                if first != header:
                    old_cols, new_cols = first.strip().split(","), header.strip().split(",")
                    if new_cols[:len(old_cols)] == old_cols:
                        # columns were added in an update: pad the existing rows to match
                        with open(path) as f:
                            rows = f.read().splitlines()[1:]
                        pad = "," * (len(new_cols) - len(old_cols))
                        with open(path + ".tmp", "w") as f:
                            f.write(header + "".join(r + pad + "\n" for r in rows if r))
                        os.replace(path + ".tmp", path)
                    else:
                        # columns changed: keep the old file and start a new one
                        os.replace(path, path[:-4] + "-old.csv")
            new = not os.path.exists(path)
            with open(path, "a") as f:
                if new:
                    f.write(header)
                f.write("".join(lines))
        except IOError as e:
            log.warning("could not write %s: %s", path, e)

    def append_minutes(self, day, lines):
        """One CSV per month of minute by minute data, kept forever (about 60 KB a day)."""
        if lines:
            self._append(os.path.join(self.archive_dir, "minutes-%s.csv" % day[:7]), self.MINUTES_HEADER, lines)

    def append_daily(self, line):
        self._append(os.path.join(self.archive_dir, "daily.csv"), self.DAILY_HEADER, [line])

    def archive_files(self):
        try:
            names = os.listdir(self.archive_dir)
        except OSError:
            return []
        return sorted((n for n in names if n.endswith(".csv")), reverse=True)

    def read_archive(self, name):
        if name not in self.archive_files():
            return None
        with open(os.path.join(self.archive_dir, name), "rb") as f:
            return f.read()

    def append_reading(self, row):
        path = self.readings_path
        try:
            if os.path.exists(path) and os.path.getsize(path) > self.s.readings_max:
                os.replace(path, path + ".1")
            new = not os.path.exists(path)
            with open(path, "a") as f:
                if new:
                    f.write("time,pv_w,load_w,grid_w,car_w,car_a,plugged,avg_avail_w,"
                            "ctrl_on,ctrl_amps,mode,reason\n")
                f.write(row + "\n")
        except IOError:
            pass

    def load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (IOError, ValueError):
            return {}

    def load_sessions(self):
        try:
            with open(self.sessions_path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (IOError, ValueError):
            return []

    def save_sessions(self, sessions):
        tmp = self.sessions_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(sessions, f, separators=(",", ":"))
            os.replace(tmp, self.sessions_path)
        except IOError as e:
            log.warning("could not save sessions: %s", e)

    def save_state(self, state):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, separators=(",", ":"))
            os.replace(tmp, self.state_path)
        except IOError as e:
            log.warning("could not save state: %s", e)


def setup_logging(storage, s):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%d/%m/%Y %H:%M:%S")
    fh = RotatingFileHandler(storage.events_path, maxBytes=s.events_max, backupCount=2)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ---------------------------------------------------------------- controller

def fmt_secs(sec):
    sec = int(max(0, sec))
    if sec >= 120:
        return "%d min" % round(sec / 60.0)
    return "%d s" % sec


class Controller(object):
    def __init__(self, s, inverter, charger, tesla, storage, clock=time.time, simulated=False):
        self.s = s
        self.inv = inverter
        self.chg = charger
        self.tesla = tesla
        self.store = storage
        self.clock = clock
        self.simulated = simulated
        self.dry_run = s.dry_run and not simulated

        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending_mode = None

        st = storage.load_state() if storage else {}
        self.mode = st.get("mode") if st.get("mode") in MODES else s.default_mode
        self.history = deque(st.get("history", []), maxlen=s.history_minutes)
        self.days = st.get("days", {})
        self.today_key = st.get("today_key", "")
        self.today = st.get("today") or self._blank_today()
        self.events = deque(maxlen=40)

        self.on = False
        self.amps = s.min_amps
        self.samples = deque()
        self.above_since = None
        self.below_since = None
        self.not_charging_since = None
        self.self_started = False
        self.small_draw = False          # car drawing a little for itself (climate, Sentry), not charging
        self.own_use_until = 0.0         # a draw that carried on through a stop command: the car running itself
        self.stop_at = 0.0               # when the last stop was sent
        self.zero_since_stop = True      # the draw fell to nothing at some point after that stop
        self.cloud_cap_noted = False     # logged that today's Teslemetry amp increases are used up
        self.wc_plugged = False          # Wall Charger says the car is plugged in (so it's at home)
        self.tt_charging = []            # away charging sessions recorded by TeslaTrack
        self.tt_home_days = None         # weekdays the car is usually home, learned by TeslaTrack
        self.self_starts = deque()       # times the car restarted by itself, to catch a stop and start loop
        self.grid_samples = deque()
        self.raises = deque()
        self.credits_out = False
        self.on_since = 0.0
        self.off_since = 0.0
        self.last_amp_change = 0.0
        self.cmd_block_until = 0.0
        self.refused_until = 0.0
        self.inv_fail_since = None
        self.last_inv = None
        self.last_chg = None
        self.inv_ok = False
        self.chg_ok = False
        self.chg_fails = 0
        self.reason = "Starting up"
        self.avg_avail = 0.0
        self.volts = s.nominal_volts
        self.last_tick = None
        self.minute_key = None
        self.minute_acc = None
        self.last_save = clock()
        self.vehicle = st.get("vehicle")
        # car history from before TeslaTrack existed, kept untouched so TeslaTrack can bring it across
        self.legacy = {k: st[k] for k in ("car_history", "tyre_days", "health", "home_loc") if st.get(k)}
        self.vehicle_check = True       # one-off battery read when the car is awake (startup, plug in, charge end)
        self.vehicle_check_next = 0.0
        self.vehicle_next = 0.0
        self.snapshot = {}
        self.sessions = storage.load_sessions() if storage else []
        self.last_full = st.get("last_full")
        self.forecast = None
        self.weather = None
        self.session = st.get("session")
        self.archived_through = st.get("archived_through")   # last day written to the archive

    # ---- public, called from web thread

    def set_mode(self, mode):
        if mode not in MODES:
            return False
        with self.lock:
            self.pending_mode = mode
            if self.snapshot:
                snap = dict(self.snapshot)
                snap["mode"] = mode
                self.snapshot = snap
        self.wake.set()  # act on it now rather than at the next 10 second check
        return True

    def get_snapshot(self):
        with self.lock:
            return self.snapshot

    def get_history(self, minutes, points=0):
        """Minute rows [time, solar, house, car, grid, amps]. Beyond what is held in RAM, older
        minutes come from the monthly archive. With points, rows are averaged into that many buckets."""
        with self.lock:
            h = list(self.history)
        if minutes <= 0:
            return h
        now = self.clock()
        start = now - minutes * 60
        if not h or h[0][0] > start + 300:
            h = self._archive_rows(start, h[0][0] if h else now) + h
        h = [r for r in h if r[0] >= start]
        if points and len(h) > points:
            step = int(math.ceil(minutes * 60.0 / points / 60.0)) * 60
            out, cur, acc = [], None, None
            for r in h:
                b = int(r[0] // step) * step
                if b != cur:
                    if acc:
                        n = acc[0]
                        out.append([cur] + [int(v / n) for v in acc[1:5]] + [round(acc[5] / n, 1)])
                    cur, acc = b, [0, 0.0, 0.0, 0.0, 0.0, 0.0]
                acc[0] += 1
                for i in range(1, 6):
                    acc[i] += r[i]
            if acc:
                n = acc[0]
                out.append([cur] + [int(v / n) for v in acc[1:5]] + [round(acc[5] / n, 1)])
            h = out
        return h

    _arch_cache = {}

    def _archive_rows(self, start, end):
        """Minute rows between start and end from the monthly archive files, cached by file."""
        rows = []
        if not self.store:
            return rows
        d = datetime.fromtimestamp(start).date().replace(day=1)
        last = datetime.fromtimestamp(end).date()
        while d <= last:
            path = os.path.join(self.store.archive_dir, "minutes-%s.csv" % d.strftime("%Y-%m"))
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            hit = self._arch_cache.get(path)
            if not hit or hit[0] != mt:
                parsed = []
                with open(path) as f:
                    f.readline()
                    for line in f:
                        c = line.rstrip("\n").split(",")
                        try:
                            t = time.mktime(time.strptime(c[0], "%Y-%m-%d %H:%M"))
                            # file: time, solar, house, grid, car, amps
                            parsed.append([int(t), int(float(c[1] or 0)), int(float(c[2] or 0)),
                                           int(float(c[4] or 0)), int(float(c[3] or 0)), float(c[5] or 0)])
                        except (ValueError, IndexError):
                            continue
                hit = (mt, parsed)
                self._arch_cache[path] = hit
            rows.extend(r for r in hit[1] if start <= r[0] < end)
        return rows

    def get_days(self):
        with self.lock:
            d = dict(self.days)
            d[self.today_key] = dict(self.today)
        return d

    def get_bill(self, now=None):
        """Running cost of the current electricity bill, from the meter's daily import and export.
        The period starts on bill_day each month and runs to the day before the next one."""
        now = now or self.clock()
        today = datetime.fromtimestamp(now).date()
        bd = self.s.bill_day
        start = today.replace(day=bd) if today.day >= bd else \
            (today.replace(day=1) - timedelta(days=1)).replace(day=bd)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=bd)
        total_days = (nxt - start).days
        elapsed = (today - start).days + 1
        days = self.get_days()
        imp = exp = car = car_solar = 0.0
        have = []
        for k, d in days.items():
            try:
                day = date(*map(int, k.split("-")))
            except ValueError:
                continue
            if start <= day <= today:
                have.append(day)
                imp += d.get("import_wh", 0.0)
                exp += d.get("export_wh", 0.0)
                car += d.get("car_wh", 0.0)
                car_solar += d.get("car_solar_wh", 0.0)
        s = self.s
        imp_kwh, exp_kwh = imp / 1000.0, exp / 1000.0
        usage = imp_kwh * s.import_price
        credit = exp_kwh * s.feed_in_price
        supply = elapsed * s.supply_price
        total = usage + supply - credit
        # projection from the days with data, today counted by how much of it has passed
        frac = (now - time.mktime(today.timetuple())) / 86400.0
        span = max(0.25, len(have) - 1 + frac) if have else 0
        per_day = (usage - credit) / span if span else 0.0
        projected = per_day * total_days + total_days * s.supply_price
        car_grid_kwh = max(0.0, car - car_solar) / 1000.0
        return {
            "start": start.isoformat(), "end": (nxt - timedelta(days=1)).isoformat(),
            "days_total": total_days, "days_elapsed": elapsed,
            "first_data": min(have).isoformat() if have else None,
            "import_kwh": round(imp_kwh, 2), "export_kwh": round(exp_kwh, 2),
            "usage": round(usage, 2), "supply": round(supply, 2), "credit": round(credit, 2),
            "total": round(total, 2), "projected": round(projected, 2) if have else None,
            "import_price": s.import_price, "feed_in_price": s.feed_in_price, "supply_price": s.supply_price,
            "car_kwh": round(car / 1000.0, 2), "car_grid_kwh": round(car_grid_kwh, 2),
            "car_grid_cost": round(car_grid_kwh * s.import_price, 2),
        }

    # ---- helpers

    def _blank_today(self):
        return {"pv_wh": 0.0, "car_wh": 0.0, "car_solar_wh": 0.0,
                "import_wh": 0.0, "export_wh": 0.0, "commands": 0,
                "starts": 0, "stops": 0, "cloud_raises": 0}

    def event(self, msg, level=logging.INFO):
        if self.dry_run and msg.startswith(("Start", "Stop", "Set ")):
            msg = "[dry run] would " + msg[0].lower() + msg[1:]
        log.log(level, msg)
        self.events.appendleft({"t": self.clock(), "msg": msg})

    def _cloud(self):
        """True when the next command would cost a Teslemetry credit."""
        f = getattr(self.tesla, "cloud_only", None)
        return f() if f else not self.simulated

    def _pace(self):
        """(up interval, down interval, raises per hour, headroom) for the route commands take now."""
        s = self.s
        if self._cloud():
            return s.cloud_up_interval, s.cloud_down_interval, s.cloud_max_raises, s.cloud_headroom
        return s.up_interval, s.down_interval, s.max_raises_hour, s.up_headroom

    def _clamp(self, a):
        return int(max(self.s.min_amps, min(self.s.max_amps, a)))

    def _track(self, avg):
        """Deadband: raise only with real spare solar, lower only when importing past the allowance."""
        V = self.volts
        up = int(math.floor((avg - self._pace()[3]) / V))
        down = int(math.floor((avg + self.s.allowance) / V))
        if up >= self.amps + self.s.min_change:
            return self._clamp(up)
        if down < self.amps:
            return self._clamp(down)
        return self.amps

    def _count(self, key):
        self.today[key] += 1
        if self.session is not None:
            self.session[key] = self.session.get(key, 0) + 1

    def _send(self, now, name, body=None):
        if now < self.cmd_block_until:
            return False
        if name == "charge_stop":
            self.stop_at = now
            self.zero_since_stop = False
        if not self.dry_run:
            with self.lock:
                snap = dict(self.snapshot)
                if snap:
                    snap["reason"] = "Sending %s to the car" % {"charge_start": "start", "charge_stop": "stop",
                                                                 "set_charging_amps": "amps"}.get(name, name)
                    self.snapshot = snap
        self._count("commands")
        if self.dry_run:
            return True
        ok, err = self.tesla.command(name, body)
        if ok:
            if self.credits_out:
                self.credits_out = False
                self.event("Teslemetry commands working again")
            return True
        if "insufficient_credits" in err or "HTTP 402" in err:
            self.cmd_block_until = now + 1800
            if not self.credits_out:
                self.credits_out = True
                self.event("Out of Teslemetry credits. Top up in the Teslemetry console. "
                           "The car is left as it is; trying again every 30 minutes", logging.WARNING)
            return False
        if name == "charge_start" and err in ("complete", "charging_complete"):
            self.refused_until = now + self.s.no_charge_backoff
            self.event("Car says charging is complete, not trying again until %s"
                       % time.strftime("%H:%M", time.localtime(self.refused_until)))
            return False
        self.cmd_block_until = now + self.s.cmd_retry
        self.event("Command %s failed: %s. Retrying in %s" % (name, err, fmt_secs(self.s.cmd_retry)),
                   logging.WARNING)
        return False

    # ---- main tick

    def tick(self):
        now = self.clock()
        dt = 0.0 if self.last_tick is None else min(now - self.last_tick, self.s.poll * 3)
        self.last_tick = now

        with self.lock:
            if self.pending_mode:
                if self.pending_mode != self.mode:
                    self.event("Mode changed to %s" % self.pending_mode)
                    self.mode = self.pending_mode
                    self.above_since = None
                    self.below_since = None
                    self.refused_until = 0.0
                self.pending_mode = None
                self._save(now)

        self._roll_day(now)
        if self.store and self.archived_through != (datetime.fromtimestamp(now).date() - timedelta(days=1)).isoformat():
            self._archive_pending(now)

        # read inverter
        try:
            inv = self.inv.read()
            self.last_inv = inv
            if not self.inv_ok:
                self.event("Inverter readings OK")
            self.inv_ok = True
            self.inv_fail_since = None
        except Exception as e:
            inv = None
            if self.inv_ok or self.inv_fail_since is None:
                self.event("Inverter read failed: %s" % e, logging.WARNING)
            self.inv_ok = False
            if self.inv_fail_since is None:
                self.inv_fail_since = now

        # read charger
        chg = None
        if self.chg is not None:
            try:
                chg = self.chg.read()
                if not self.chg_ok:
                    self.event("Wall Charger readings OK")
                self.chg_ok = True
                self.chg_fails = 0
            except Exception as e:
                # The Wall Connector is on Wi-Fi and now and then misses one reading. Only report it
                # once three in a row (about 30 seconds) have failed, or straight away at start up.
                self.chg_fails = getattr(self, "chg_fails", 0) + 1
                if self.chg_ok and (self.chg_fails >= 3 or self.last_chg is None):
                    self.event("Wall Charger read failed: %s" % e, logging.WARNING)
                    self.chg_ok = False
                elif self.last_chg is None and self.chg_fails == 1:
                    self.event("Wall Charger read failed: %s" % e, logging.WARNING)
                    self.chg_ok = False
            if chg is not None:
                if self.last_chg is not None and chg["plugged"] != self.last_chg["plugged"]:
                    self.event("Car plugged in" if chg["plugged"] else "Car unplugged")
                if self.last_chg is not None and (
                        (chg["plugged"] and not self.last_chg["plugged"]) or
                        (self.last_chg["charging"] and not chg["charging"])):
                    self.vehicle_check = True
                self.last_chg = chg
                self.wc_plugged = bool(chg["plugged"])
                if chg["volts"] > 180:
                    self.volts = chg["volts"]

        car_w = 0.0
        if chg is not None and chg["charging"]:
            car_w = chg["amps"] * self.volts


        if inv is not None:
            self.samples.append((now, car_w - inv["grid"]))
            self.grid_samples.append((now, inv["grid"]))
        while self.grid_samples and now - self.grid_samples[0][0] > 30:
            self.grid_samples.popleft()
        while self.samples and now - self.samples[0][0] > self.s.smooth:
            self.samples.popleft()
        if self.samples:
            self.avg_avail = sum(x for _, x in self.samples) / len(self.samples)

        # publish the fresh readings first, so the dashboard stays live even if a
        # command to the car takes a while (a busy or sleeping car can take a minute or two)
        self._publish(now, inv, chg, car_w)
        self._control(now, inv, chg)
        self._vehicle_poll(now, chg)
        self._track_session(now, dt, inv, chg, car_w)

        if inv is not None:
            self._account(now, dt, inv, car_w)
            self.store and self.store.append_reading(",".join(str(x) for x in (
                time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(now)),
                inv["pv"], inv["load"], inv["grid"], int(car_w),
                chg["amps"] if chg else "", int(chg["plugged"]) if chg else "",
                int(self.avg_avail), int(self.on), self.amps, self.mode,
                '"%s"' % self.reason.replace('"', "'"))))

        if now - self.last_save >= self.s.save_every:
            self._save(now)

        self._publish(now, inv, chg, car_w)

    def _control(self, now, inv, chg):
        s = self.s
        V = self.volts
        min_w = s.min_amps * V

        if self.chg is None:
            self.reason = "Wall Charger address not set in config.ini, monitoring only"
            return
        if chg is None:
            self.reason = "Cannot read the Wall Charger, holding current state"
            return

        plugged = chg["plugged"]
        actual = chg["charging"]

        if not plugged:
            self.on = False
            self.above_since = self.below_since = self.not_charging_since = None
            self.refused_until = 0.0
            self.reason = "Car not plugged in"
            return

        # A plugged in car powers climate, Sentry and the like from the wall, which closes the
        # contactor and draws a few amps. That isn't a charge, and a stop command won't end it,
        # so leave it alone. It counts as the car running itself when:
        #   the draw is below the minimum charging amps, or
        #   the battery is known to be at its charge limit, or
        #   the draw carried on through a stop command without ever falling to nothing
        if chg["amps"] < 0.5:
            self.zero_since_stop = True
            self.own_use_until = 0.0
        if (actual and not self.on and not self.zero_since_stop and now - self.stop_at < 600
                and chg["amps"] < 10 and now >= self.own_use_until):
            self.own_use_until = now + s.no_charge_backoff
        small = actual and not self.on and (chg["amps"] < s.min_amps - 0.5 or self._at_limit()
                                            or now < self.own_use_until)
        if small and not self.small_draw:
            self.event("Car is drawing %.0f A to run itself (climate, battery cooling or staying awake), not charging. Leaving it alone"
                       % chg["amps"])
        self.small_draw = small
        if small:
            actual = False

        # keep our belief in step with the car (real mode only)
        if not self.dry_run:
            if actual and not self.on:
                self.on = True
                self.on_since = now - s.min_run
                self.amps = self._clamp(round(chg["amps"]) or s.min_amps)
                self.last_amp_change = now - max(s.up_interval, s.down_interval)  # allow an immediate correction
                if self.mode != "manual" and now >= self.refused_until:
                    self.self_started = True
                    self.event("Car started charging by itself at %.0f A" % chg["amps"])
                    # safety net: if it keeps starting again straight after each stop, stop sending stops
                    self.self_starts.append(now)
                    while self.self_starts and now - self.self_starts[0] > 600:
                        self.self_starts.popleft()
                    if len(self.self_starts) >= 3:
                        self.self_starts.clear()
                        self.self_started = False
                        self.refused_until = now + s.no_charge_backoff
                        self.event("Car keeps restarting after being told to stop. Leaving it alone until %s"
                                   % time.strftime("%H:%M", time.localtime(self.refused_until)), logging.WARNING)
            elif self.on and not actual:
                if self.not_charging_since is None:
                    self.not_charging_since = now
                if now - self.not_charging_since > s.no_charge_grace and now - self.on_since > s.no_charge_grace:
                    self.on = False
                    self.off_since = now
                    self.not_charging_since = None
                    if self.mode != "manual":
                        self.refused_until = now + s.no_charge_backoff
                        self.event("Car is not drawing power (full, at its limit, or stopped in the app). "
                                   "Pausing until %s" % time.strftime("%H:%M", time.localtime(self.refused_until)))
            else:
                self.not_charging_since = None

        if self.mode == "manual":
            self.reason = "Manual mode, not controlling the car"
            return

        if self.credits_out and now < self.cmd_block_until:
            self.reason = "Out of Teslemetry credits, can't control the car. Top up, or use the Tesla app"
            return

        if now < self.refused_until:
            self.reason = "Car is not accepting charge, trying again at %s" % time.strftime(
                "%H:%M", time.localtime(self.refused_until))
            return

        if not self.on and self._at_limit():
            self.above_since = None
            v = self.vehicle
            self.reason = "Battery at its charge limit (%d%% of %d%%), waiting until it drops" % (v["soc"], v["limit"])
            return

        # inverter fail safe
        if inv is None and self.mode in ("solar", "minsolar"):
            failed_for = now - (self.inv_fail_since or now)
            if self.on and failed_for >= s.read_fail_stop:
                if self._send(now, "charge_stop"):
                    self.event("Stop charging: no inverter readings for %s" % fmt_secs(failed_for))
                    self.on = False
                    self.off_since = now
                    self._count("stops")
            self.reason = "No inverter readings for %s" % fmt_secs(failed_for)
            return

        avg = self.avg_avail
        target = int(math.floor((avg + s.allowance) / V))

        want_on, want_amps = self.on, self.amps
        if self.mode == "fast":
            want_on, want_amps = True, s.max_amps
            self.reason = "Fast mode, charging at %d A" % s.max_amps
        elif self.mode == "minsolar":
            want_on = True
            want_amps = self._track(avg) if self.on else self._clamp((avg - s.up_headroom) / V)
            self.reason = "Minimum plus solar, %d A (%d W available)" % (want_amps, avg)
        elif not self.on:
            need = min_w + s.start_margin
            if avg >= need:
                if self.above_since is None:
                    self.above_since = now
                wait = max(s.start_delay - (now - self.above_since), s.min_off - (now - self.off_since))
                if wait <= 0:
                    want_on = True
                    want_amps = self._clamp((avg - s.up_headroom) / V)
                else:
                    self.reason = "Surplus %d W, starting in %s if it holds" % (avg, fmt_secs(wait))
            else:
                self.above_since = None
                self.reason = "Waiting for surplus: %d W available, need %d W" % (max(avg, 0), need)
        else:
            if target < s.min_amps:
                if self.below_since is None:
                    self.below_since = now
                wait = max(s.stop_delay - (now - self.below_since), s.min_run - (now - self.on_since))
                if self.self_started:
                    wait = 0  # the car started itself (usually on plug in) and there is no surplus: stop now
                if wait <= 0:
                    want_on = False
                else:
                    want_amps = s.min_amps
                    self.reason = "Surplus low (%d W), holding %d A, stopping in %s" % (
                        avg, s.min_amps, fmt_secs(wait))
            else:
                self.below_since = None
                self.self_started = False
                want_amps = self._track(avg)

        self._apply(now, want_on, want_amps, avg)

    def _at_limit(self):
        """True when the car recently reported a battery level at or above its charge limit. The car would
        refuse a start anyway, so sending one only wakes it (and may use a Teslemetry credit)."""
        v = self.vehicle or {}
        soc, limit = v.get("soc"), v.get("limit")
        if soc is None or not limit or time.time() - v.get("t", 0) > 24 * 3600:
            return False
        return soc >= limit

    def _apply(self, now, want_on, want_amps, avg):
        if want_on and not self.on:
            if not self._send(now, "set_charging_amps", {"charging_amps": want_amps}):
                return
            if self._send(now, "charge_start"):
                self.on = True
                self.amps = want_amps
                self.on_since = now
                self.last_amp_change = now
                self.above_since = None
                self.not_charging_since = None
                self._count("starts")
                self.event("Start charging at %d A (%d W available)" % (want_amps, avg))
                self.reason = "Charging at %d A" % want_amps
            return

        if not want_on and self.on:
            if self._send(now, "charge_stop"):
                self.on = False
                self.off_since = now
                self.below_since = None
                self._count("stops")
                self.self_started = False
                self.event("Stop charging (%d W available)" % avg)
                self.reason = "Stopped, waiting for surplus"
            return

        if self.on and want_amps != self.amps:
            since = now - self.last_amp_change
            new = None
            while self.raises and now - self.raises[0] > 3600:
                self.raises.popleft()
            up_iv, down_iv, max_raises, _ = self._pace()
            cloud = self._cloud()
            if self.mode == "fast":
                new = want_amps  # Fast goes straight to the target, no ramping
            elif want_amps > self.amps:
                # raise slowly, only when the extra solar has lasted, and not too often
                if since >= max(up_iv, down_iv) and len(self.raises) < max_raises:
                    # the daily cap is for Bluetooth setups falling back; Teslemetry only setups pace themselves
                    if cloud and isinstance(self.tesla, CarControl) and \
                            self.today.get("cloud_raises", 0) >= self.s.cloud_raises_day:
                        if not self.cloud_cap_noted:
                            self.cloud_cap_noted = True
                            self.event("Bluetooth unavailable and today's %d Teslemetry amp increases are used, "
                                       "so holding at %d A to save credits" % (self.s.cloud_raises_day, self.amps))
                    else:
                        new = min(want_amps, self.amps + self.s.up_step)
            elif want_amps < self.amps:
                # lower once the last change has settled, or quickly if importing heavily
                recent = [g for _, g in self.grid_samples]
                heavy = recent and sum(recent) / len(recent) > self.s.allowance + self.s.emergency_import
                if since >= down_iv or (heavy and since >= 20):
                    new = want_amps
            if new is not None and self._send(now, "set_charging_amps", {"charging_amps": new}):
                paid = getattr(self.tesla, "last_path", "teslemetry") == "teslemetry" and not self.simulated
                self.event("Set amps %d to %d (%d W available)%s" % (self.amps, new, avg, " via Teslemetry" if paid else (
                    " via Tesla API" if getattr(self.tesla, "last_path", "") == "fleet" else "")))
                if new > self.amps:
                    self.raises.append(now)
                    if paid:
                        self.today["cloud_raises"] = self.today.get("cloud_raises", 0) + 1
                self.amps = new
                self.last_amp_change = now

        if self.on and self.mode == "solar" and self.below_since is None:
            self.reason = "Charging at %d A from surplus" % self.amps

    def vehicle_update(self, upd):
        now = self.clock()
        full = False
        with self.lock:
            v = dict(self.vehicle or {})
            upd.pop("_loc", None)          # car tracking lives in TeslaTrack now
            if upd.get("ac_in") is not None:
                self._battery_in(upd["ac_in"], v.get("ac_in"))
            v.update(upd)
            v["t"] = time.time()
            self.vehicle = v
            if v.get("soc") is not None and v["soc"] >= 100 and (not self.last_full or now - self.last_full > 12 * 3600):
                self.last_full = now
                full = True
        if full:
            self.event("Battery reached 100%%. Next full charge due %s" % time.strftime(
                "%a %d %b", time.localtime(now + self.s.full_every * 86400)))
            self._save(now)

    def _battery_in(self, val, prev):
        """Energy that reached the battery while charging at home, from the car's AC counter (lock held)."""
        if prev is None or val < prev - 0.05:
            delta = val if prev is not None else 0.0    # counter reset: a new charge started
        else:
            delta = max(0.0, val - prev)
        if delta <= 0 or delta > 100 or not self.wc_plugged:
            return
        self.today["batt_in_kwh"] = self.today.get("batt_in_kwh", 0.0) + delta
        if self.session is not None:
            self.session["batt_kwh"] = self.session.get("batt_kwh", 0.0) + delta

    def _home_days(self):
        """Weekdays the car is usually home during the day: learned by TeslaTrack once it has enough to go
        on, with the configured days filling in any weekday it hasn't learned yet."""
        hd = self.tt_home_days
        if not hd or len(hd.get("known", [])) < 3:
            return set(self.s.home_days), False
        known = set(hd["known"])
        return set(wd for wd in self.s.home_days if wd not in known) | set(hd["home"]), True

    def mark_full(self):
        now = self.clock()
        with self.lock:
            self.last_full = now
        self.event("Full charge marked as done")
        self._save(now)

    def get_schedule(self):
        """When the next 100% charge is due, and the sunniest home day to do it."""
        home_days, learned = self._home_days()
        now = self.clock()
        today = datetime.fromtimestamp(now).date()
        with self.lock:
            last = self.last_full
            v = dict(self.vehicle or {})
        due = None
        if last:
            due = (datetime.fromtimestamp(last).date().toordinal() + self.s.full_every)
        days = []
        fc = self.forecast.days if self.forecast else []
        for f in fc:
            try:
                d = datetime.strptime(f["date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                continue
            if d < today:
                continue
            if d == today and self._day_over(f, now):
                continue    # the sun has set, so today can't be the full charge day any more
            item = dict(f)
            item["home"] = d.weekday() in home_days
            item["due"] = due is not None and d.toordinal() == due
            days.append(item)
        # pick the sunniest home day on or before the due date (or the first week if overdue/unknown)
        limit = due if (due is not None and due >= today.toordinal()) else today.toordinal() + 7
        cands = [x for x in days if x["home"] and x.get("solar_kwh") is not None and
                 datetime.strptime(x["date"], "%Y-%m-%d").date().toordinal() <= limit]
        late = False
        if not cands:
            # no home day before it's due: suggest the sunniest home day after instead
            cands = [x for x in days if x["home"] and x.get("solar_kwh") is not None]
            late = bool(cands) and due is not None
        best = max(cands, key=lambda x: x["solar_kwh"])["date"] if cands else None
        for x in days:
            x["best"] = x["date"] == best
        soc = v.get("soc")
        need = round((100 - soc) / 100.0 * self.s.battery_kwh / 0.9, 1) if soc is not None else None
        return {
            "every_days": self.s.full_every,
            "last_full": last,
            "due": datetime.fromordinal(due).strftime("%Y-%m-%d") if due else None,
            "days_left": (due - today.toordinal()) if due else None,
            "soc": soc,
            "need_kwh": need,
            "forecast": days,
            "forecast_error": self.forecast.error if self.forecast else "off",
            "best": best,
            "best_late": late,
            "home_learned": learned,
            "today": today.strftime("%Y-%m-%d"),
        }

    @staticmethod
    def _day_over(f, now):
        """True once the forecast day's sun has set (6 pm if the forecast has no sunset time)."""
        try:
            t = datetime.strptime(f.get("sunset") or "", "%Y-%m-%dT%H:%M")
        except ValueError:
            t = datetime.strptime(f["date"] + "T18:00", "%Y-%m-%dT%H:%M")
        return now >= time.mktime(t.timetuple())

    def _vehicle_poll(self, now, chg):
        # Over Bluetooth, reading the battery is free: read it every few minutes while charging.
        # Over Teslemetry each read costs credits, so it is off by default (the free stream is used instead).
        ble = isinstance(self.tesla, CarControl) and self.tesla.ble.configured()
        if chg is None or not chg["plugged"] or self.dry_run and not self.simulated:
            return
        if ble:
            if not chg["charging"]:
                if self.vehicle_check and now >= self.vehicle_check_next:
                    self.vehicle_check_next = now + 60
                    awake = self.tesla.ble.is_awake()
                    if awake is None:
                        return          # Bluetooth busy or out of reach: try again in a minute
                    self.vehicle_check = False
                    if awake:
                        cs, err = self.tesla.ble.charge_state()
                        if cs:
                            self.vehicle_update(cs)
                return
            if self.s.ble_read_min <= 0:
                return
            self.vehicle_check = False
            every = self.s.ble_read_min
        else:
            if self.s.vehicle_poll_min <= 0:
                return
            every = self.s.vehicle_poll_min
        if now < self.vehicle_next:
            return
        self.vehicle_next = now + every * 60
        try:
            cs, err = self.tesla.charge_state()
        except Exception as e:
            cs, err = None, str(e)
        if cs:
            self.vehicle_update(cs)
        else:
            log.warning("vehicle data failed: %s", err)

    # ---- charging sessions: from plugging in to unplugging, like evcc

    def _track_session(self, now, dt, inv, chg, car_w):
        if chg is None:
            return
        if chg["plugged"] and self.session is None:
            # The Wall Connector knows how long the car has been plugged in and how much
            # it has delivered, so a session already underway is picked up from its true start.
            # Energy delivered before tracking began is counted as grid, since its source is unknown.
            start = now
            try:
                age = float(chg.get("session_s") or 0)
                if 0 < age < 30 * 86400:
                    start = now - age
            except (TypeError, ValueError):
                pass
            self.session = {"start": start, "wh": 0.0, "solar_wh": 0.0, "charge_s": 0.0,
                            "peak_w": car_w, "wc_wh": float(chg.get("session_wh") or 0), "starts": 0,
                            "stops": 0, "commands": 0, "modes": [self.mode]}
            if start < now - 60:
                self.event("Picked up a session already in progress since %s (%.1f kWh so far)"
                           % (time.strftime("%H:%M", time.localtime(start)), self.session["wc_wh"] / 1000.0))
            return
        if self.session is None:
            return
        ss = self.session
        if chg["plugged"]:
            if dt > 0:
                h = dt / 3600.0
                ss["wh"] += car_w * h
                if inv is not None:
                    house = max(0.0, inv["load"] - car_w)
                    ss["solar_wh"] += max(0.0, min(car_w, max(0, inv["pv"]) - house)) * h
                if car_w > 50:
                    ss["charge_s"] += dt
            ss["peak_w"] = max(ss["peak_w"], car_w)
            if chg.get("session_wh"):
                ss["wc_wh"] = max(ss["wc_wh"], float(chg["session_wh"]))
            if self.mode not in ss["modes"]:
                ss["modes"].append(self.mode)
            return
        # unplugged: finish the session
        rec = self._session_view(ss, now, final=True)
        self.session = None
        if rec["kwh"] >= 0.05:
            with self.lock:
                self.sessions.append(rec)
            self.store and self.store.save_sessions(self.sessions)
            self.event("Session ended: %.1f kWh, %d%% solar" % (rec["kwh"], rec["solar_pct"]))
        self._save(now)

    def _session_view(self, ss, now, final=False):
        measured = ss["wh"]
        wh = ss["wc_wh"] if ss["wc_wh"] > 0 else measured
        scale = (wh / measured) if measured > 0 else 1.0
        solar = min(wh, ss["solar_wh"] * scale)
        grid = max(0.0, wh - solar)
        ip, fp = self.s.import_price, self.s.feed_in_price
        kwh, skwh, gkwh = wh / 1000.0, solar / 1000.0, grid / 1000.0
        return {
            "start": int(ss["start"]),
            "end": int(now) if final else None,
            "kwh": round(kwh, 2),
            "solar_kwh": round(skwh, 2),
            "grid_kwh": round(gkwh, 2),
            "solar_pct": int(round(100.0 * skwh / kwh)) if kwh > 0 else 0,
            "charge_min": int(round(ss["charge_s"] / 60.0)),
            "plugged_min": int(round((now - ss["start"]) / 60.0)),
            "peak_kw": round(ss["peak_w"] / 1000.0, 1),
            "starts": ss.get("starts", 0),
            "stops": ss.get("stops", 0),
            "commands": ss.get("commands", 0),
            "modes": list(ss.get("modes", [])),
            "cost": round(gkwh * ip + skwh * fp, 2),
            "saved": round(skwh * (ip - fp), 2),
            "import_price": ip,
            "feed_in_price": fp,
            # energy that reached the battery, from the car, and how much of the delivered energy that was
            "batt_kwh": round(ss["batt_kwh"], 2) if ss.get("batt_kwh") else None,
            "eff_pct": int(round(100.0 * ss["batt_kwh"] / kwh)) if ss.get("batt_kwh") and kwh > 0.5 else None,
        }

    _odo_cache = (0, None)

    def get_distance(self):
        """The car's odometer history from TeslaTrack, for distance driven over a period (cached 5 minutes)."""
        if not self.s.teslatrack_url:
            return None
        t, val = self._odo_cache
        if time.time() - t < 300:
            return val
        try:
            with urlopen(self.s.teslatrack_url + "/api/odometer", timeout=5) as r:
                val = json.loads(r.read().decode("utf-8"))
            if self.s.delivery_date:
                val["delivery"] = {"date": self.s.delivery_date, "odo": self.s.delivery_odo}
        except Exception:
            val = None
        self._odo_cache = (time.time(), val)
        return val

    def get_sessions(self):
        now = self.clock()
        with self.lock:
            done = list(self.sessions)
            cur = self._session_view(self.session, now) if self.session else None
        # add away sessions from TeslaTrack. Its records replace any away session SolarCharge itself
        # recorded at the same time before the split, which used an estimated price
        tt = []
        for a in self.tt_charging or []:
            try:
                tt.append((int(a["start"]), int(a["end"]), float(a["kwh"]), a))
            except (KeyError, TypeError, ValueError):
                continue
        done = [x for x in done if not (x.get("away") and any(abs(x["start"] - t[0]) < 3600 for t in tt))]
        for st, en, kwh, a in tt:
            place = a.get("place") or ""
            mins = max(1, int(round((en - st) / 60.0)))
            done.append({"start": st, "end": en, "kwh": kwh, "solar_kwh": 0.0, "grid_kwh": kwh, "solar_pct": 0,
                         "charge_min": mins, "plugged_min": mins, "peak_kw": None, "batt_kwh": kwh, "eff_pct": None,
                         "cost": a.get("cost") or 0.0, "saved": 0.0, "away": True, "kind": a.get("kind", "dc"),
                         "supercharger": bool(a.get("supercharger")) or "supercharger" in place.lower(),
                         "actual": bool(a.get("actual")),
                         "place": place})
        done.sort(key=lambda x: x["start"])
        return {"sessions": done, "current": cur, "distance": self.get_distance(),
                "fuel": {"l_per_100km": self.s.fuel_l100, "price": self.s.fuel_price, "name": self.s.fuel_name},
                "import_price": self.s.import_price, "feed_in_price": self.s.feed_in_price}

    def _account(self, now, dt, inv, car_w):
        if dt <= 0:
            return
        h = dt / 3600.0
        pv = max(0, inv["pv"])
        house = max(0.0, inv["load"] - car_w)
        solar_to_car = max(0.0, min(car_w, pv - house))
        t = self.today
        t["pv_wh"] += pv * h
        t["car_wh"] += car_w * h
        t["car_solar_wh"] += solar_to_car * h
        if inv["grid"] > 0:
            t["import_wh"] += inv["grid"] * h
        else:
            t["export_wh"] += -inv["grid"] * h

        key = int(now // 60)
        if key != self.minute_key:
            self._flush_minute()
            self.minute_key = key
            self.minute_acc = [0, 0.0, 0.0, 0.0, 0.0, 0.0]
        a = self.minute_acc
        a[0] += 1
        a[1] += pv
        a[2] += house
        a[3] += car_w
        a[4] += inv["grid"]
        a[5] += self.amps if self.on else 0

    def _flush_minute(self):
        a = self.minute_acc
        if not a or a[0] == 0:
            return
        n = float(a[0])
        with self.lock:
            self.history.append([self.minute_key * 60, int(a[1] / n), int(a[2] / n),
                                 int(a[3] / n), int(a[4] / n), round(a[5] / n, 1)])

    def _roll_day(self, now):
        key = time.strftime("%Y-%m-%d", time.localtime(now))
        if key == self.today_key:
            return
        with self.lock:
            if self.today_key:
                self.days[self.today_key] = {k: round(v, 1) if isinstance(v, float) else v
                                             for k, v in self.today.items()}
                for old in sorted(self.days)[:-60]:
                    del self.days[old]
            self.today_key = key
            self.today = self._blank_today()
            self.cloud_cap_noted = False

    def _archive_pending(self, now):
        """Writes every finished day not yet archived: minute data and the daily totals."""
        yday = datetime.fromtimestamp(now).date() - timedelta(days=1)
        with self.lock:
            hist = list(self.history)
            days = dict(self.days)
        if self.archived_through:
            try:
                start = date(*map(int, self.archived_through.split("-"))) + timedelta(days=1)
            except ValueError:
                start = yday
        else:
            # first run: archive whatever history is still held
            firsts = [datetime.fromtimestamp(p[0]).date() for p in hist[:1]]
            firsts += [date(*map(int, k.split("-"))) for k in sorted(days)[:1]]
            start = min(firsts) if firsts else yday
        by_min = {}
        for p in hist:
            by_min[int(p[0] // 60)] = [p[1], p[2], p[4], p[3], p[5], "", "", "", ""]
        d = start
        while d <= yday:
            key = d.isoformat()
            lines = []
            for m in sorted(by_min):
                t = m * 60
                if datetime.fromtimestamp(t).date() == d:
                    lines.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) + "," +
                                 ",".join(str(v) for v in by_min[m]) + "\n")
            self.store.append_minutes(key, lines)
            t = days.get(key)
            if t:
                # the car columns are kept for older files; TeslaTrack's archive has the car's figures now
                odo = driven = None
                tp = ",,,,,,%s,,," % ("%.2f" % t["batt_in_kwh"] if t.get("batt_in_kwh") else "")
                self.store.append_daily("%s,%.2f,%.2f,%.2f,%.2f,%.2f,%d,%d,%d,%s,%s,%s,%s\n" % (
                    key, t.get("pv_wh", 0) / 1000.0, t.get("car_wh", 0) / 1000.0, t.get("car_solar_wh", 0) / 1000.0,
                    t.get("import_wh", 0) / 1000.0, t.get("export_wh", 0) / 1000.0,
                    t.get("starts", 0), t.get("stops", 0), t.get("commands", 0),
                    "%.1f" % odo if odo is not None else "", "%.1f" % driven if driven is not None else "",
                    t.get("top_kmh", ""), tp))
            d += timedelta(days=1)
        self.archived_through = yday.isoformat()
        self._save(now)

    def _save(self, now):
        self.last_save = now
        if not self.store:
            return
        self.store.save_state({
            "mode": self.mode,
            "history": list(self.history),
            "days": self.days,
            "today_key": self.today_key,
            "today": self.today,
            "session": self.session,
            "last_full": self.last_full,
            "vehicle": self.vehicle,
            "archived_through": self.archived_through,
            "saved": now,
            **self.legacy,
        })

    def save(self):
        self._flush_minute()
        self._save(self.clock())

    def _publish(self, now, inv, chg, car_w):
        t = self.today
        car_kwh = t["car_wh"] / 1000.0
        snap = {
            "version": VERSION,
            "time": now,
            "dry_run": self.dry_run,
            "simulated": self.simulated,
            "mode": self.mode,
            "reason": self.reason,
            "inverter": {
                "ok": inv is not None,
                "pv": inv["pv"] if inv else None,
                "load": inv["load"] if inv else None,
                "grid": inv["grid"] if inv else None,
                "house": int(max(0, inv["load"] - car_w)) if inv else None,
            },
            "charger": {
                "configured": self.chg is not None,
                "ok": chg is not None,
                "plugged": chg["plugged"] if chg else None,
                "charging": chg["charging"] if chg else None,
                "amps": chg["amps"] if chg else None,
                "volts": round(self.volts, 1),
                "car_w": int(car_w),
                "session_kwh": round((chg.get("session_wh") or 0) / 1000.0, 2) if chg else None,
            },
            "control": {
                "on": self.on,
                "amps": self.amps,
                "avg_avail": int(self.avg_avail),
                "min_amps": self.s.min_amps,
                "max_amps": self.s.max_amps,
                "start_w": int(self.s.min_amps * self.volts + self.s.start_margin),
                "allowance": int(self.s.allowance),
            },
            "tesla": {
                "configured": self.tesla.configured() if self.tesla else False,
                "credits_out": self.credits_out,
                "control": self.tesla.status() if isinstance(self.tesla, CarControl) else None,
                "battery_kwh": self.s.battery_kwh,
                "vehicle": self.vehicle,
            },
            "today": {
                "pv_kwh": round(t["pv_wh"] / 1000.0, 2),
                "car_kwh": round(car_kwh, 2),
                "car_solar_kwh": round(t["car_solar_wh"] / 1000.0, 2),
                "import_kwh": round(t["import_wh"] / 1000.0, 2),
                "export_kwh": round(t["export_wh"] / 1000.0, 2),
                "solar_share": round(100.0 * t["car_solar_wh"] / t["car_wh"]) if t["car_wh"] > 50 else None,
                "commands": t["commands"],
                "starts": t["starts"],
                "stops": t["stops"],
            },
            "events": list(self.events)[:20],
        }
        with self.lock:
            self.snapshot = snap


# ---------------------------------------------------------------- web

class Handler(BaseHTTPRequestHandler):
    controller = None
    page_path = os.path.join(BASE_DIR, "dashboard.html")

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, name):
        try:
            with open(os.path.join(BASE_DIR, name), "rb") as f:
                body = f.read()
        except IOError:
            self.send_error(500, name + " missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        c = self.controller
        if u.path in ("/", "/index.html"):
            self._page("dashboard.html")
        elif u.path in ("/img/scene.jpg", "/img/scene-day.jpg", "/img/scene-dusk.jpg",
                        "/img/icon-180.png", "/favicon.ico"):
            name = "img/icon-180.png" if u.path == "/favicon.ico" else u.path.lstrip("/")
            try:
                with open(os.path.join(BASE_DIR, name), "rb") as f:
                    body = f.read()
            except IOError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if name.endswith(".png") else "image/jpeg")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/status":
            self._json(c.get_snapshot())
        elif u.path == "/api/history":
            q = parse_qs(u.query)
            try:
                minutes = int(q.get("minutes", ["1440"])[0])
                points = int(q.get("points", ["0"])[0])
            except ValueError:
                minutes, points = 1440, 0
            self._json(c.get_history(min(minutes, 60 * 24 * 400), min(max(points, 0), 5000)))
        elif u.path == "/api/sessions":
            self._json(c.get_sessions())
        elif u.path == "/api/export":
            self._json(c.store.archive_files() if c.store else [])
        elif u.path.startswith("/export/"):
            name = u.path[len("/export/"):]
            body = c.store.read_archive(name) if c.store else None
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="solarcharge-%s"' % name)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/sessions":
            self._page("sessions.html")
        elif u.path == "/api/schedule":
            self._json(c.get_schedule())
        elif u.path == "/api/weather":
            self._json(c.weather.snapshot(c.clock()) if c.weather else {"ok": False, "error": "off"})
        elif u.path == "/api/days":
            self._json(c.get_days())
        elif u.path == "/api/bill":
            self._json(c.get_bill())
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/fullcharge":
            self.controller.mark_full()
            self._json({"ok": True})
            return
        if u.path != "/api/mode":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 4096)).decode("utf-8") or "{}")
            mode = str(data.get("mode", ""))
        except (ValueError, TypeError):
            mode = ""
        if self.controller.set_mode(mode):
            self._json({"ok": True, "mode": mode})
        else:
            self._json({"ok": False, "error": "unknown mode"}, 400)


def start_web(controller, s):
    Handler.controller = controller
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer((s.web_bind, s.web_port), Handler)
    except OSError as e:
        log.error("Cannot open the dashboard on port %d (%s). Is SolarCharge already running? "
                  "Try: sudo systemctl stop solarcharge", s.web_port, e)
        raise SystemExit(1)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, name="web")
    th.daemon = True
    th.start()
    return srv


# ---------------------------------------------------------------- check mode

def run_check(s):
    print("SolarCharge %s check" % VERSION)
    print("Dry run: %s" % s.dry_run)
    print("\nInverter %s:%d" % (s.inv_host, s.inv_port))
    try:
        r = Inverter(s).read()
        print("  OK  PV %d W, load %d W, grid %d W (%s)" % (
            r["pv"], r["load"], r["grid"], "importing" if r["grid"] > 0 else "exporting"))
    except Exception as e:
        print("  FAILED: %s" % e)

    print("\nWall Charger %s" % (s.charger_host or "(not set)"))
    if s.charger_host:
        try:
            r = WallConnector(s).read()
            print("  OK  plugged in: %s, charging: %s, %.1f A at %.1f V" % (
                r["plugged"], r["charging"], r["amps"], r["volts"]))
        except Exception as e:
            print("  FAILED: %s" % e)

    if s.ble:
        print("\nBluetooth")
        b = BleTesla(s)
        if not os.path.exists(s.ble_tool):
            print("  tesla-control not found at %s" % s.ble_tool)
        elif not os.path.exists(s.ble_key):
            print("  No Bluetooth key at %s (see the pairing steps)" % s.ble_key)
        elif not s.vin:
            print("  VIN not set in config.ini")
        else:
            ok, msg = b.ping()
            print(("  OK  " if ok else "  FAILED: ") + msg)
            if ok:
                cs, err = b.charge_state()
                print("  Battery: %s" % (cs if cs else "not readable now (%s); fine if the car is asleep" % err))

    if s.fleet:
        print("\nTesla API")
        f = FleetTesla(s)
        if not f.configured():
            print("  Not ready: run the steps in FLEET.md (key, register, login)")
        else:
            ok, msg = f.ping()
            print(("  OK  " if ok else "  FAILED: ") + msg)

    print("\nTeslemetry")
    t = Teslemetry(s)
    if not s.vin:
        print("  VIN not set in config.ini")
    elif not t._token():
        print("  No token found in %s" % s.token_file)
    else:
        ok, msg = t.ping()
        print(("  OK  " if ok else "  FAILED: ") + msg)
    print()


# ---------------------------------------------------------------- Tesla API setup

def fleet_keygen(s):
    """Make SolarCharge's Tesla API key (kept on the Pi) and print the public half to publish."""
    if os.path.exists(s.fleet_key):
        print("A key already exists at %s, keeping it." % s.fleet_key)
    else:
        old = os.umask(0o077)
        try:
            subprocess.check_call(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                                   "-out", s.fleet_key])
        finally:
            os.umask(old)
        print("Made a new private key at %s. Keep it private." % s.fleet_key)
    pub = subprocess.check_output(["openssl", "ec", "-in", s.fleet_key, "-pubout"],
                                  stderr=subprocess.DEVNULL, universal_newlines=True)
    print("\nPublish this public key (it is safe to share) at")
    print("  https://%s/.well-known/appspecific/com.tesla.3p.public-key.pem\n" % (s.fleet_domain or "<your domain>"))
    print(pub)


def fleet_register(s):
    """Check the public key is published, then register the domain with Tesla."""
    if not (s.fleet_client_id and s.fleet_client_secret and s.fleet_domain):
        sys.exit("Fill in client_id, client_secret and domain under [fleet] in config.ini first.")
    url = "https://%s/.well-known/appspecific/com.tesla.3p.public-key.pem" % s.fleet_domain
    mine = subprocess.check_output(["openssl", "ec", "-in", s.fleet_key, "-pubout"],
                                   stderr=subprocess.DEVNULL, universal_newlines=True).strip()
    try:
        with urlopen(Request(url, headers={"User-Agent": "solarcharge"}), timeout=20) as r:
            published = r.read().decode().strip()
    except Exception as e:
        sys.exit("Couldn't read %s (%s). Publish the public key there first." % (url, e))
    if published.split() != mine.split():
        sys.exit("The key at %s doesn't match %s. Publish the output of --fleet-keygen." % (url, s.fleet_key))
    print("Public key found at %s and matches." % url)
    tok = FleetTesla.post_form(FleetTesla.TOKEN_URL, {
        "grant_type": "client_credentials", "client_id": s.fleet_client_id,
        "client_secret": s.fleet_client_secret, "audience": s.fleet_base,
        "scope": "openid vehicle_device_data vehicle_cmds vehicle_charging_cmds"})
    req = Request(s.fleet_base + "/api/1/partner_accounts", method="POST",
                  data=json.dumps({"domain": s.fleet_domain}).encode(),
                  headers={"Authorization": "Bearer " + tok["access_token"], "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as r:
            r.read()
    except HTTPError as e:
        sys.exit("Tesla refused the registration: HTTP %s %s" % (e.code, e.read().decode(errors="replace")[:300]))
    print("Registered %s with Tesla. Next: python3 %s --fleet-login" % (s.fleet_domain, os.path.abspath(__file__)))


def fleet_login(s):
    """Sign in to your Tesla account once and save the login, which SolarCharge then renews itself."""
    from urllib.parse import urlencode
    if not (s.fleet_client_id and s.fleet_client_secret and s.fleet_redirect):
        sys.exit("Fill in client_id, client_secret and domain under [fleet] in config.ini first.")
    state = "%016x" % random.getrandbits(64)
    url = FleetTesla.AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": s.fleet_client_id, "redirect_uri": s.fleet_redirect,
        "scope": FleetTesla.SCOPES, "state": state, "prompt_missing_scopes": "true"})
    print("1. Open this link in a browser and sign in to your Tesla account:\n\n%s\n" % url)
    print("2. Allow the access it asks for. You'll land on a 'page not found' at %s." % s.fleet_domain)
    print("   That's expected. Copy the whole address from the address bar.\n")
    back = input("3. Paste the address here and press Enter: ").strip()
    q = parse_qs(urlparse(back).query)
    if q.get("state", [""])[0] != state or not q.get("code"):
        sys.exit("That address doesn't hold a fresh sign in code. Run --fleet-login again.")
    resp = FleetTesla.post_form(FleetTesla.TOKEN_URL, {
        "grant_type": "authorization_code", "client_id": s.fleet_client_id,
        "client_secret": s.fleet_client_secret, "code": q["code"][0], "audience": s.fleet_base,
        "redirect_uri": s.fleet_redirect, "scope": FleetTesla.SCOPES})
    if "refresh_token" not in resp:
        sys.exit("Tesla didn't return a lasting login: %s" % str(resp)[:300])
    FleetTesla(s).save_tokens(resp)
    print("\nSigned in. Login saved to %s (private)." % s.fleet_token_file)
    print("\n4. Last step: on your phone, open https://tesla.com/_ak/%s" % s.fleet_domain)
    print("   and approve the key in the Tesla app (the car needs to be online).")
    print("\nThen set enabled = true under [fleet], and run --check.")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Charge a Tesla from surplus solar")
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.ini"))
    ap.add_argument("--check", action="store_true", help="read each device once and exit")
    ap.add_argument("--simulate", action="store_true", help="use fake devices")
    ap.add_argument("--speed", type=float, default=1.0, help="clock speed in simulate mode")
    ap.add_argument("--fleet-keygen", action="store_true", help="Tesla API setup: make the key")
    ap.add_argument("--fleet-register", action="store_true", help="Tesla API setup: register your domain")
    ap.add_argument("--fleet-login", action="store_true", help="Tesla API setup: sign in once")
    args = ap.parse_args()

    s = Settings(args.config)
    if args.fleet_keygen or args.fleet_register or args.fleet_login:
        (fleet_keygen if args.fleet_keygen else fleet_register if args.fleet_register else fleet_login)(s)
        return

    if not s.inv_host and not args.simulate:
        sys.exit("Set the inverter's IP address under [inverter] host in %s first." % args.config)

    if args.check:
        run_check(s)
        return

    clock = time.time
    if args.simulate:
        start_real = time.time()
        start_sim = time.mktime(time.localtime(start_real)[:3] + (9, 0, 0, 0, 0, -1))
        speed = max(1.0, args.speed)

        def clock():
            return start_sim + (time.time() - start_real) * speed

        s.ram_dir = os.path.join(s.ram_dir + "-sim")
        s.data_dir = os.path.join(s.ram_dir, "data")
        import shutil
        shutil.rmtree(s.ram_dir, ignore_errors=True)  # each simulation starts fresh
        world = SimWorld(clock)
        inverter, charger, tesla = SimInverter(world), SimCharger(world), SimTesla(world)
    else:
        speed = 1.0
        inverter = Inverter(s)
        charger = WallConnector(s) if s.charger_host else None
        tesla = Teslemetry(s)
        fleet = FleetTesla(s) if s.fleet else None
        if s.ble or fleet:
            tesla = CarControl(BleTesla(s), tesla, s, fleet)
            if s.ble_fast:
                # commands are free over Bluetooth, so follow the sun more closely
                s.up_interval = min(s.up_interval, 90)
                s.down_interval = min(s.down_interval, 45)
                s.max_raises_hour = max(s.max_raises_hour, 20)
                s.up_headroom = min(s.up_headroom, 150)

    storage = Storage(s)
    setup_logging(storage, s)
    ctl = Controller(s, inverter, charger, tesla, storage, clock=clock, simulated=args.simulate)

    log.info("SolarCharge %s starting. Mode %s. %s", VERSION, ctl.mode,
             "SIMULATION" if args.simulate else ("DRY RUN, no commands will be sent" if ctl.dry_run
                                                 else "LIVE, commands will be sent to the car"))
    if not args.simulate and not tesla.configured() and not ctl.dry_run:
        log.warning("Teslemetry token or VIN missing, commands will fail")

    if args.simulate:
        def fake_stream():
            while True:
                cs, _ = tesla.charge_state()
                # energy into the battery this charge, following the made up battery level
                fake_stream.soc0 = getattr(fake_stream, "soc0", cs["soc"])
                fake_stream.ac = max(getattr(fake_stream, "ac", 0.0), (cs["soc"] - fake_stream.soc0) / 100.0 * 57.6)
                ctl.vehicle_update({"soc": cs["soc"], "limit": cs["limit"], "range_km": int(cs["soc"] * 4.3),
                                    "ac_in": round(fake_stream.ac, 3), "home": True})
                time.sleep(2)
        th = threading.Thread(target=fake_stream)
        th.daemon = True
        th.start()
    elif s.teslatrack_url:
        TeslaTrackFeed(s, ctl).start()
    elif s.stream and s.vin:
        TeslemetryStream(s, ctl.vehicle_update).start()

    if s.forecast:
        ctl.forecast = Forecast(s)
        if args.simulate:
            import random as _r
            base = datetime.fromtimestamp(clock()).date()
            ctl.forecast.days = Forecast.parse({
                "time": [datetime.fromordinal(base.toordinal() + i).strftime("%Y-%m-%d") for i in range(8)],
                "shortwave_radiation_sum": [_r.choice([9, 14, 19, 25, 27]) for _ in range(8)],
                "precipitation_probability_max": [_r.choice([0, 10, 40, 80]) for _ in range(8)],
                "cloud_cover_mean": [_r.choice([10, 30, 60, 90]) for _ in range(8)]}, s.pv_kwp)
            ctl.last_full = clock() - 4 * 86400
            # a made up sunny spring day for the weather card
            ctl.weather = Weather(s)
            day0 = datetime.fromtimestamp(clock()).strftime("%Y-%m-%d")
            hrs = ["%sT%02d:00" % (day0, i) for i in range(24)]
            hrs += ["%sT%02d:00" % (datetime.fromordinal(base.toordinal() + 1).strftime("%Y-%m-%d"), i) for i in range(24)]
            curve = lambda i: max(0.0, math.sin(((i % 24) - 5.5) / 13.0 * math.pi))
            ctl.weather.data = Weather.parse({
                "current": {"temperature_2m": 21.4, "apparent_temperature": 19.8, "weather_code": 1, "is_day": 1},
                "hourly": {"time": hrs,
                           "temperature_2m": [round(14 + 13 * curve(i - 2), 1) for i in range(48)],
                           "precipitation_probability": [5 if i % 24 < 14 else 20 for i in range(48)],
                           "weather_code": [1 if i % 24 < 9 else 0 if i % 24 < 15 else 2 for i in range(48)],
                           "is_day": [1 if 5 <= i % 24 < 18 else 0 for i in range(48)],
                           "shortwave_radiation": [round(950 * curve(i)) for i in range(48)]},
                "daily": {"temperature_2m_max": [27, 29], "temperature_2m_min": [14, 15],
                          "precipitation_probability_max": [20, 10], "precipitation_sum": [0.4, 0]}}, s.pv_kwp)
            ctl.weather.updated = time.time()
        else:
            ctl.forecast.start()
            ctl.weather = Weather(s)
            ctl.weather.start()

    srv = start_web(ctl, s)
    log.info("Dashboard on http://%s:%d", s.web_bind if s.web_bind != "0.0.0.0" else "<pi address>", s.web_port)

    stop = {"flag": False}

    class Shutdown(BaseException):
        pass

    def handle(sig, frame):
        if stop["flag"]:
            return
        stop["flag"] = True
        ctl.wake.set()
        # interrupt whatever is running (even a slow Bluetooth command) so we can save and exit
        raise Shutdown()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    interval = s.poll / speed
    try:
        while not stop["flag"]:
            started = time.time()
            try:
                ctl.tick()
            except Exception as e:
                log.exception("tick failed: %s", e)
            wait = interval - (time.time() - started)
            if wait > 0 and not stop["flag"]:
                ctl.wake.wait(wait)
            ctl.wake.clear()
    except Shutdown:
        pass

    log.info("Shutting down, saving state")
    ctl.save()
    srv.shutdown()


if __name__ == "__main__":
    main()
