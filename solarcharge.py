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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0"
MODES = ("manual", "solar", "minsolar", "fast")

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
    },
    "web": {
        "bind": "0.0.0.0",
        "port": "8080",
    },
}


class Settings(object):
    def __init__(self, path):
        cp = configparser.ConfigParser()
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
        return bool(self.s.vin) and os.path.exists(self.s.ble_tool) and os.path.exists(self.s.ble_key)

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
        res = {}
        try:
            if soc is not None:
                res["soc"] = int(round(float(soc)))
            if limit is not None:
                res["limit"] = int(round(float(limit)))
            if mins is not None:
                res["ttf_h"] = round(float(mins) / 60.0, 2)
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


class CarControl(object):
    """Bluetooth first, Teslemetry as the backup. Backup is used for starts, stops and
    amp reductions only, so a Bluetooth problem can never strand the car charging from
    the grid, but routine amp increases don't spend credits."""

    def __init__(self, ble, cloud, s):
        self.ble = ble
        self.cloud = cloud
        self.s = s
        self.last_path = ""
        self.fallbacks = 0
        self.prev_amps = None
        self.busy_until = 0.0

    def configured(self):
        return self.ble.configured() or self.cloud.configured()

    def command(self, name, body=None):
        if self.ble.configured() and time.time() < self.busy_until and \
                self.s.ble_fallback and self.cloud.configured():
            # the car's Bluetooth was full a moment ago (phone keys): don't waste time retrying
            ok, err = self.cloud.command(name, body)
            if ok:
                self.last_path = "teslemetry"
                self.fallbacks += 1
                self._remember(name, body)
            return ok, err
        if self.ble.configured():
            ok, err = self.ble.command(name, body)
            if ok or err == "complete":
                self.last_path = "bluetooth"
                self._remember(name, body)
                return ok, err
            log.warning("Bluetooth %s failed: %s", name, err)
            reducing = name == "set_charging_amps" and self.prev_amps is not None and \
                int((body or {}).get("charging_amps", 0)) < self.prev_amps
            busy = self.ble._busy(err)    # car's Bluetooth slots are full: Bluetooth can't be used right now
            if busy:
                self.busy_until = time.time() + 300
                log.warning("Car's Bluetooth is full (phone keys nearby); using Teslemetry for the next 5 minutes")
            if not (self.s.ble_fallback and self.cloud.configured() and
                    (busy or name in ("charge_start", "charge_stop") or reducing)):
                return False, "Bluetooth: " + err
            log.warning("Using Teslemetry for %s instead", name)
            self.fallbacks += 1
        ok, err = self.cloud.command(name, body)
        if ok:
            self.last_path = "teslemetry"
            self._remember(name, body)
        return ok, err

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
                "busy_until": self.busy_until}


class TeslemetryStream(object):
    """Listens to Teslemetry's free streaming feed (server sent events) for the car's
    battery level, charge limit and cabin temperature. Costs no credits and never wakes the car; values
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
        if upd:
            self.on_update(upd)


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
        self.reason = "Starting up"
        self.avg_avail = 0.0
        self.volts = s.nominal_volts
        self.last_tick = None
        self.minute_key = None
        self.minute_acc = None
        self.last_save = clock()
        self.vehicle = st.get("vehicle")
        # car telemetry for the Car chart: [time, battery %, cabin C], recorded as values arrive
        self.car_history = deque(st.get("car_history", []), maxlen=8000)
        self.vehicle_check = True       # one-off battery read when the car is awake (startup, plug in, charge end)
        self.vehicle_check_next = 0.0
        self.vehicle_next = 0.0
        self.snapshot = {}
        self.sessions = storage.load_sessions() if storage else []
        self.last_full = st.get("last_full")
        self.forecast = None
        self.session = st.get("session")

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

    def get_history(self, minutes):
        with self.lock:
            h = list(self.history)
        return h[-minutes:] if minutes > 0 else h

    def get_days(self):
        with self.lock:
            d = dict(self.days)
            d[self.today_key] = dict(self.today)
        return d

    # ---- helpers

    def _blank_today(self):
        return {"pv_wh": 0.0, "car_wh": 0.0, "car_solar_wh": 0.0,
                "import_wh": 0.0, "export_wh": 0.0, "commands": 0,
                "starts": 0, "stops": 0}

    def event(self, msg, level=logging.INFO):
        if self.dry_run and msg.startswith(("Start", "Stop", "Set ")):
            msg = "[dry run] would " + msg[0].lower() + msg[1:]
        log.log(level, msg)
        self.events.appendleft({"t": self.clock(), "msg": msg})

    def _clamp(self, a):
        return int(max(self.s.min_amps, min(self.s.max_amps, a)))

    def _track(self, avg):
        """Deadband: raise only with real spare solar, lower only when importing past the allowance."""
        V = self.volts
        up = int(math.floor((avg - self.s.up_headroom) / V))
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
            except Exception as e:
                if self.chg_ok or self.last_chg is None:
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

        # keep our belief in step with the car (real mode only)
        if not self.dry_run:
            if actual and not self.on:
                self.on = True
                self.on_since = now - s.min_run
                self.amps = self._clamp(round(chg["amps"]) or s.min_amps)
                self.last_amp_change = now - max(s.up_interval, s.down_interval)  # allow an immediate correction
                if self.mode != "manual":
                    self.self_started = True
                    self.event("Car started charging by itself at %.0f A" % chg["amps"])
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
            if self.mode == "fast":
                new = want_amps  # Fast goes straight to the target, no ramping
            elif want_amps > self.amps:
                # raise slowly, only when the extra solar has lasted, and not too often
                if since >= max(self.s.up_interval, self.s.down_interval) and len(self.raises) < self.s.max_raises_hour:
                    new = min(want_amps, self.amps + self.s.up_step)
            elif want_amps < self.amps:
                # lower once the last change has settled, or quickly if importing heavily
                recent = [g for _, g in self.grid_samples]
                heavy = recent and sum(recent) / len(recent) > self.s.allowance + self.s.emergency_import
                if since >= self.s.down_interval or (heavy and since >= 20):
                    new = want_amps
            if new is not None and self._send(now, "set_charging_amps", {"charging_amps": new}):
                self.event("Set amps %d to %d (%d W available)" % (self.amps, new, avg))
                if new > self.amps:
                    self.raises.append(now)
                self.amps = new
                self.last_amp_change = now

        if self.on and self.mode == "solar" and self.below_since is None:
            self.reason = "Charging at %d A from surplus" % self.amps

    def vehicle_update(self, upd):
        now = self.clock()
        full = False
        with self.lock:
            v = dict(self.vehicle or {})
            v.update(upd)
            v["t"] = time.time()
            self.vehicle = v
            self._car_point(now, upd)
            if v.get("soc") is not None and v["soc"] >= 100 and (not self.last_full or now - self.last_full > 12 * 3600):
                self.last_full = now
                full = True
        if full:
            self.event("Battery reached 100%%. Next full charge due %s" % time.strftime(
                "%a %d %b", time.localtime(now + self.s.full_every * 86400)))
            self._save(now)

    CAR_KEEP_S = 7 * 86400

    def _car_point(self, now, upd):
        """Adds battery level and cabin temperature to the car history (lock held)."""
        soc, cabin = upd.get("soc"), upd.get("cabin")
        if soc is None and cabin is None:
            return
        h = self.car_history
        if h and now - h[-1][0] < 120:
            # merge updates that arrive close together into one point
            last = h[-1]
            h[-1] = [last[0], soc if soc is not None else last[1], cabin if cabin is not None else last[2]]
            return
        if h and now - h[-1][0] < 600 and (soc is None or soc == h[-1][1]) and (cabin is None or cabin == h[-1][2]):
            return   # nothing new; a point every 10 minutes is plenty while values are steady
        h.append([now, soc, cabin])
        while h and now - h[0][0] > self.CAR_KEEP_S:
            h.popleft()

    def get_car_history(self, hours):
        with self.lock:
            h = list(self.car_history)
        if hours > 0:
            cut = self.clock() - hours * 3600
            h = [p for p in h if p[0] >= cut]
        return h

    def mark_full(self):
        now = self.clock()
        with self.lock:
            self.last_full = now
        self.event("Full charge marked as done")
        self._save(now)

    def get_schedule(self):
        """When the next 100% charge is due, and the sunniest home day to do it."""
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
            item["home"] = d.weekday() in self.s.home_days
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
        }

    def get_sessions(self):
        now = self.clock()
        with self.lock:
            done = list(self.sessions)
            cur = self._session_view(self.session, now) if self.session else None
        return {"sessions": done, "current": cur,
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
            "car_history": list(self.car_history),
            "saved": now,
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
            except ValueError:
                minutes = 1440
            self._json(c.get_history(minutes))
        elif u.path == "/api/carhistory":
            q = parse_qs(u.query)
            try:
                hours = int(q.get("hours", ["168"])[0])
            except ValueError:
                hours = 168
            self._json(c.get_car_history(hours))
        elif u.path == "/api/sessions":
            self._json(c.get_sessions())
        elif u.path == "/sessions":
            self._page("sessions.html")
        elif u.path == "/api/schedule":
            self._json(c.get_schedule())
        elif u.path == "/api/days":
            self._json(c.get_days())
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


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Charge a Tesla from surplus solar")
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.ini"))
    ap.add_argument("--check", action="store_true", help="read each device once and exit")
    ap.add_argument("--simulate", action="store_true", help="use fake devices")
    ap.add_argument("--speed", type=float, default=1.0, help="clock speed in simulate mode")
    args = ap.parse_args()

    s = Settings(args.config)

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
        if s.ble:
            tesla = CarControl(BleTesla(s), tesla, s)
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
                lt = time.localtime(clock())
                hr = lt.tm_hour + lt.tm_min / 60.0
                cabin = 21 + 17 * max(0.0, math.sin((hr - 8) / 12.0 * math.pi)) + random.uniform(-0.4, 0.4)
                ctl.vehicle_update({"soc": cs["soc"], "limit": cs["limit"], "cabin": round(cabin, 1)})
                time.sleep(2)
        th = threading.Thread(target=fake_stream)
        th.daemon = True
        th.start()
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
        else:
            ctl.forecast.start()

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
