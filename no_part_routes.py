"""Portable, standard-library HTTP adapter for Google Routes."""
from __future__ import annotations

import argparse
import configparser
import contextvars
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import http.client
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from api_documentation import API_VERSION, DOCS_VERSION, openapi, version_info

LOG = logging.getLogger("no_part_routes")
REQUEST_ID = contextvars.ContextVar("request_id", default="background")
BASE_DIR = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024
GOOGLE_TIMEOUT = 15
LEGACY_EXAMPLE_SECRETS = frozenset({"1234", "my-grok-secret", "a-longer-secret-if-i-want"})
CACHE_FIELDS = ("address_key", "source_address", "place_id", "latitude", "longitude",
                "resolved_at", "coordinates_expires_at")


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return parsed.astimezone(timezone.utc)


class APIError(Exception):
    def __init__(self, status, code, message, invalid_fields=()):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.invalid_fields = tuple(invalid_fields)


class ConfigError(Exception):
    pass


def invalid(message="Invalid location or request."):
    return APIError(400, "INVALID_REQUEST", message)


def coordinates(value):
    if not isinstance(value, dict) or set(value) != {"latitude", "longitude"}:
        raise invalid("Coordinates require latitude and longitude only.")
    for field, limit in (("latitude", 90), ("longitude", 180)):
        number = value[field]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not -limit <= number <= limit or not math.isfinite(number)):
            raise invalid("Coordinates must be finite numbers within geographic ranges.")
    return {key: float(number) for key, number in value.items()}


def location(value, labels):
    if not isinstance(value, dict) or len(value) != 1:
        raise invalid("Each location requires exactly one representation.")
    if "coordinates" in value:
        return {"coordinates": coordinates(value["coordinates"])}
    field = next(iter(value))
    if field not in ("address", "pluscode", "label") or not isinstance(value[field], str):
        raise invalid("Use an address, pluscode, label, or coordinates.")
    text = value[field].strip()
    if not text:
        raise invalid("Addresses, plus codes, and labels cannot be empty.")
    if field == "label":
        if text.casefold() not in labels:
            raise APIError(400, "UNKNOWN_LABEL", "The requested location label is not configured.")
        return labels[text.casefold()]
    # Google accepts full and locality-qualified compound plus codes in its address parameter.
    # Canonicalize to address internally so labels/direct requests share warming and caching.
    # Google's geocoding URL limit is 16,384 characters after URL encoding.
    if len(urllib.parse.urlencode({"address": text})) > 14000:
        raise invalid("Address or plus code is too long.")
    return {"address": text}


def read_ini(path):
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8-sig") as stream:
            parser.read_file(stream)
        if parser.defaults():
            raise ConfigError(f"{path.name}: DEFAULT values are not supported.")
        return parser
    except (OSError, configparser.Error, UnicodeError):
        raise ConfigError(f"Cannot read {path.name}; check its INI syntax and permissions.") from None


def parse_locations(places):
    labels, configured = {}, {}
    for section in places.sections():
        name = section.strip().casefold()
        if not name or name in labels:
            raise ConfigError("Location labels must be nonempty and unique ignoring case.")
        fields = dict(places[section])
        try:
            if set(fields) in ({"address"}, {"pluscode"}):
                labels[name] = location(fields, {})
                configured[name] = {next(iter(fields)): labels[name]["address"]}
            elif set(fields) == {"latitude", "longitude"}:
                labels[name] = {"coordinates": coordinates({k: float(v) for k, v in fields.items()})}
                configured[name] = labels[name]
            else:
                raise ValueError()
        except (ValueError, APIError):
            raise ConfigError("Each location needs an address, pluscode, OR valid latitude/longitude.") from None
    return labels, configured


@dataclass
class Config:
    host: str
    port: int
    google_key: str
    secrets: tuple[str, ...]
    labels: dict
    log_max_bytes: int = 1048576
    log_backup_count: int = 5
    daily_routes_limit: int = 150
    daily_geocoding_limit: int = 300
    configured_locations: dict | None = None
    locations_path: Path | None = None

    @classmethod
    def load(cls, directory):
        settings = read_ini(directory / "settings.ini")
        keys = read_ini(directory / "apiKeys.ini")
        places = read_ini(directory / "locations.ini")
        try:
            host = settings.get("server", "host").strip()
            port = settings.getint("server", "port")
            google_key = settings.get("google", "api_key").strip()
        except (configparser.Error, ValueError):
            raise ConfigError("settings.ini requires server host/port and google api_key.") from None
        if not host or not 1 <= port <= 65535:
            raise ConfigError("Choose a listening host and a port from 1 to 65535.")
        if (not google_key or "\n" in google_key or "\r" in google_key
                or google_key.startswith("paste-your-")):
            raise ConfigError("Paste your Google API key into settings.ini.")
        secrets = []
        for section in keys.sections():
            secret = keys.get(section, "secret", fallback="").strip()
            if not secret or "\n" in secret or "\r" in secret:
                raise ConfigError("Every apiKeys.ini section needs a nonempty single-line secret.")
            if secret.startswith("CHANGE-ME-") or secret in LEGACY_EXAMPLE_SECRETS:
                raise ConfigError("Replace every sample secret in apiKeys.ini, or remove unused sections.")
            secrets.append(secret)
        if not secrets:
            raise ConfigError("Add at least one shared secret to apiKeys.ini.")
        labels, configured_locations = parse_locations(places)
        try:
            limits = [settings.getint(section, option, fallback=default) for section, option, default in (
                ("logging", "max_bytes", 1048576), ("logging", "backup_count", 5),
                ("usage", "daily_routes_limit", 150), ("usage", "daily_geocoding_limit", 300))]
            if any(value < 1 for value in limits):
                raise ValueError()
        except (ValueError, configparser.Error):
            raise ConfigError("Logging sizes and daily usage limits must be positive integers.") from None
        return cls(host, port, google_key, tuple(secrets), labels, *limits,
                   configured_locations=configured_locations, locations_path=directory / "locations.ini")


class LocationStore:
    """Publish whole validated snapshots; an in-flight route keeps one snapshot."""
    def __init__(self, path, labels, configured):
        self.path = Path(path)
        self.current = (labels, configured)
        self.lock = threading.Lock()
        self.digest = None
        self.failed_digest = None
        self.read_failed = False

    def snapshot(self):
        with self.lock:
            try:
                before = self.path.stat()
                # Avoid reading the brief truncate/write phase of a normal editor save.
                if time.time() - before.st_mtime < 0.5:
                    return self.current
                raw = self.path.read_bytes()
                after = self.path.stat()
                if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
                    return self.current
            except OSError:
                if not self.read_failed:
                    LOG.warning("event=locations_reload_failed reason=unreadable keeping_previous=true")
                    self.read_failed = True
                return self.current
            self.read_failed = False
            digest = hashlib.sha256(raw).digest()
            if digest == self.digest or digest == self.failed_digest:
                return self.current
            try:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_file(io.StringIO(raw.decode("utf-8-sig")))
                if parser.defaults():
                    raise ConfigError("DEFAULT values are unsupported.")
                candidate = parse_locations(parser)
            except (ConfigError, configparser.Error, UnicodeError):
                LOG.warning("event=locations_reload_failed reason=invalid keeping_previous=true")
                self.failed_digest = digest
                return self.current
            changed = candidate != self.current
            self.current, self.digest, self.failed_digest = candidate, digest, None
            if changed:
                LOG.info("event=locations_reloaded count=%d", len(candidate[0]))
            return self.current


class RequestLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = REQUEST_ID.get()
        return True


def configure_file_logging(directory, config):
    log_directory = Path(directory) / "logs"
    log_directory.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_directory / "application.log", maxBytes=config.log_max_bytes,
                                  backupCount=config.log_backup_count, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s request_id=%(request_id)s %(message)s",
                                  datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    handler.addFilter(RequestLogFilter())
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    return handler


class UsageGuard:
    """Reserve each attempted Google call on disk BEFORE it can spend usage."""
    def __init__(self, path, routes_limit, geocoding_limit, now=utc_now):
        self.path, self.now = Path(path), now
        self.limits = {"routes": routes_limit, "geocoding": geocoding_limit}
        self.lock = threading.Lock()
        self.day = self.now().date().isoformat()
        self.counts = {"routes": 0, "geocoding": 0}
        if self.path.exists():
            try:
                stored = read_ini(self.path)
                self.day = stored.get("daily", "date")
                date.fromisoformat(self.day)
                self.counts = {api: stored.getint("daily", api) for api in self.limits}
                if any(value < 0 for value in self.counts.values()):
                    raise ValueError()
            except (ConfigError, configparser.Error, ValueError):
                raise ConfigError("usage.ini is damaged; restore valid usage counters before starting.") from None
        self._save(self.day, self.counts)

    def _save(self, day, counts):
        parser = configparser.ConfigParser(interpolation=None)
        parser["daily"] = {"date": day, **{api: str(count) for api, count in counts.items()}}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix="usage.ini.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                parser.write(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def reserve(self, api):
        with self.lock:
            today = self.now().date().isoformat()
            # Do not reset counters if the system clock moves backward.
            day = max(today, self.day)
            counts = dict(self.counts) if day == self.day else dict.fromkeys(self.limits, 0)
            if counts[api] >= self.limits[api]:
                LOG.warning("event=usage_limit api=%s daily_count=%d limit=%d date=%s",
                            api, counts[api], self.limits[api], day)
                raise APIError(429, "DAILY_USAGE_LIMIT", "Local daily Google-call limit reached; try after midnight UTC.")
            counts[api] += 1
            try:
                self._save(day, counts)
            except OSError:
                LOG.error("event=usage_storage_failed api=%s Google call blocked", api)
                raise APIError(503, "USAGE_STORAGE_UNAVAILABLE", "Cannot persist usage counters; Google call was blocked.") from None
            self.day, self.counts = day, counts
            LOG.info("event=google_call_reserved api=%s daily_count=%d limit=%d date=%s",
                     api, counts[api], self.limits[api], day)


def address_key(address):
    return " ".join(address.split()).casefold()


class LocationCache:
    """One process, coordinated threads, atomic CSV replacement."""
    def __init__(self, path, now=utc_now):
        self.path, self.now = Path(path), now
        self.lock = threading.RLock()
        # Fixed stripes bound memory while preventing duplicate concurrent resolutions.
        self.resolution_locks = [threading.Lock() for _ in range(64)]
        self.rows = {}
        self.cleanup_pending = False
        # Leftovers from an interrupted atomic write may contain legacy coordinates.
        for leftover in self.path.parent.glob(self.path.name + ".*.tmp"):
            leftover.unlink()
        self._load()

    def _sanitize_row(self, row):
        if None in row or any(row.get(field) is None for field in CACHE_FIELDS):
            raise ValueError("Invalid CSV row")
        if not row["source_address"] or row["address_key"] != address_key(row["source_address"]):
            raise ValueError("Invalid address key")
        timestamp(row["resolved_at"])
        if not row["place_id"]:
            raise ValueError("Missing place ID")
        if row["latitude"] or row["longitude"]:
            coordinates({"latitude": float(row["latitude"]), "longitude": float(row["longitude"])})
            expiry = timestamp(row["coordinates_expires_at"])
            latest = timestamp(row["resolved_at"]) + timedelta(days=30)
            if expiry > latest:
                raise ValueError("Invalid expiry")
            if expiry <= self.now():
                row["latitude"] = row["longitude"] = row["coordinates_expires_at"] = ""
        elif row["coordinates_expires_at"]:
            raise ValueError("Expiry without coordinates")
        return row

    def _load(self):
        if not self.path.exists():
            self._save()
            return
        safe_rows = []
        try:
            with self.path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream, strict=True)
                if tuple(reader.fieldnames or ()) != CACHE_FIELDS:
                    raise ValueError("Invalid CSV header")
                for row in reader:
                    safe_rows.append(self._sanitize_row(row))
            self.rows = {row["address_key"]: row for row in safe_rows}
        except (ValueError, csv.Error, UnicodeError):
            # Preserve recoverable content, never unknown or expired Google coordinates.
            recovered = [{**row, "latitude": "", "longitude": "", "coordinates_expires_at": ""}
                         for row in safe_rows]
            backup = self.path.with_name(self.path.name + ".damaged.csv")
            self._write(backup, recovered)
            LOG.warning("Damaged cache: preserved sanitized recoverable rows and started a new cache.")
            self.rows = {}
        self._save()

    def _write(self, path, rows):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                             dir=path.parent, prefix=path.name + ".", suffix=".tmp",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                writer = csv.DictWriter(stream, fieldnames=CACHE_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _save(self):
        self._write(self.path, list(self.rows.values()))

    def _save_best_effort(self):
        try:
            self._save()
        except OSError:
            LOG.warning("Cache write failed; current resolutions remain available in memory.")

    def cleanup(self):
        with self.lock:
            changed = False
            for row in self.rows.values():
                if row["coordinates_expires_at"] and timestamp(row["coordinates_expires_at"]) <= self.now():
                    row["latitude"] = row["longitude"] = row["coordinates_expires_at"] = ""
                    changed = True
            if changed or self.cleanup_pending:
                # Failure to delete expired persisted coordinates must remain visible and retryable.
                self.cleanup_pending = True
                self._save()
                self.cleanup_pending = False

    def resolve(self, address, geocode):
        key = address_key(address)
        stripe = int.from_bytes(hashlib.sha256(key.encode()).digest()[:2], "big") % 64
        with self.resolution_locks[stripe]:
            with self.lock:
                if key in self.rows:
                    LOG.info("event=location_cache result=hit")
                    return self.rows[key]["place_id"]
            LOG.info("event=location_cache result=miss")
            place_id = geocode(address)
            # Only store place IDs; coordinates are unnecessary for routing and need not be retained.
            row = dict.fromkeys(CACHE_FIELDS, "")
            row.update(address_key=key, source_address=address, place_id=place_id, resolved_at=stamp(self.now()))
            with self.lock:
                self.rows[key] = row
                self._save_best_effort()
            return place_id

    def invalidate(self, address, place_id):
        with self.lock:
            key = address_key(address)
            if key in self.rows and self.rows[key]["place_id"] == place_id:
                del self.rows[key]
                self._save_best_effort()


class GoogleClient:
    def __init__(self, key, opener=urllib.request.urlopen, usage=None):
        self.key, self.opener, self.usage = key, opener, usage

    def _request(self, request, api="routes"):
        if self.usage is not None:
            self.usage.reserve(api)
        started = time.monotonic()
        LOG.info("event=google_call_started api=%s", api)
        try:
            result = self._perform_request(request)
        except APIError as exc:
            LOG.warning("event=google_call_finished api=%s result=error code=%s duration_ms=%.1f",
                        api, exc.code, (time.monotonic() - started) * 1000)
            raise
        LOG.info("event=google_call_finished api=%s result=received duration_ms=%.1f",
                 api, (time.monotonic() - started) * 1000)
        return result

    def _perform_request(self, request):
        try:
            with self.opener(request, timeout=GOOGLE_TIMEOUT) as response:
                data = json.loads(response.read(2 * 1024 * 1024))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except urllib.error.HTTPError as exc:
            invalid_fields = []
            if exc.code == 400:
                try:
                    error = json.loads(exc.read(64 * 1024)).get("error", {})
                    for detail in error.get("details", []):
                        for violation in detail.get("fieldViolations", []):
                            field = violation.get("field", "")
                            description = violation.get("description", "").lower()
                            if field in ("origin.place_id", "origin.placeId", "destination.place_id", "destination.placeId") and any(
                                    text in description for text in ("invalid", "not found", "not exist")):
                                invalid_fields.append(field.split(".")[0])
                except (ValueError, AttributeError, TypeError):
                    pass
            status = 503 if exc.code == 429 or exc.code >= 500 else 502
            raise APIError(status, "GOOGLE_UNAVAILABLE" if status == 503 else "GOOGLE_REJECTED",
                           "Google could not complete the request.", invalid_fields) from None
        except (TimeoutError, socket.timeout):
            raise APIError(504, "GOOGLE_TIMEOUT", "Google request timed out.") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise APIError(504, "GOOGLE_TIMEOUT", "Google request timed out.") from None
            raise APIError(503, "GOOGLE_UNAVAILABLE", "Google is temporarily unreachable.") from None
        except (OSError, http.client.HTTPException):
            raise APIError(503, "GOOGLE_UNAVAILABLE", "Google connection was interrupted.") from None
        except (ValueError, UnicodeError):
            raise APIError(502, "GOOGLE_INVALID_RESPONSE", "Google returned an unusable response.") from None

    def geocode(self, address):
        query = urllib.parse.urlencode({"address": address, "key": self.key})
        data = self._request(urllib.request.Request("https://maps.googleapis.com/maps/api/geocode/json?" + query), api="geocoding")
        status = data.get("status")
        # Log only known enums, never Google's raw error text or arbitrary response values.
        safe_status = status if status in ("OK", "ZERO_RESULTS", "OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT", "UNKNOWN_ERROR", "REQUEST_DENIED", "INVALID_REQUEST") else "UNEXPECTED"
        LOG.info("event=geocoding_result status=%s", safe_status)
        if status == "ZERO_RESULTS":
            raise APIError(422, "ADDRESS_NOT_FOUND", "Google could not resolve an address.")
        if status in ("OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT", "UNKNOWN_ERROR"):
            raise APIError(503, "GOOGLE_UNAVAILABLE", "Google geocoding quota or service is unavailable.")
        if status != "OK":
            raise APIError(502, "GOOGLE_REJECTED", "Google rejected address resolution.")
        try:
            place_id = data["results"][0]["place_id"]
            if not isinstance(place_id, str) or not place_id.strip():
                raise ValueError()
            return place_id
        except (KeyError, IndexError, TypeError, ValueError):
            raise APIError(502, "GOOGLE_INVALID_RESPONSE", "Google returned an unusable location.") from None

    def route(self, origin, destination, *, avoid_tolls=False):
        # Omitting departureTime means now on Google's clock. An explicit local
        # "now" can be in the past on arrival (latency or clock skew) and is rejected.
        payload = {"origin": origin, "destination": destination, "travelMode": "DRIVE",
                   "routingPreference": "TRAFFIC_AWARE",
                   "computeAlternativeRoutes": False,
                   "routeModifiers": {"avoidTolls": avoid_tolls},
                   "extraComputations": ["TOLLS"]}
        request = urllib.request.Request("https://routes.googleapis.com/directions/v2:computeRoutes",
                                         data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-Goog-Api-Key": self.key,
                                                  "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.description,routes.travelAdvisory.tollInfo,fallbackInfo"})
        LOG.info("event=route_calculation preference=%s", "avoid_tolls" if avoid_tolls else "tolls_allowed")
        data = self._request(request)
        if isinstance(data.get("fallbackInfo"), dict) and data["fallbackInfo"].get("routingMode") == "TRAFFIC_UNAWARE":
            raise APIError(503, "TRAFFIC_UNAVAILABLE", "Google could not provide a traffic-aware route.")
        routes = data.get("routes", [])
        if routes == []:
            raise APIError(422, "NO_ROUTE", "Google could not find a driving route.")
        try:
            route = routes[0]
            duration = route.get("duration", "0s")
            if not isinstance(duration, str) or not re.fullmatch(r"\d+(?:\.\d{1,9})?s", duration):
                raise ValueError()
            meters = route.get("distanceMeters", 0)
            if isinstance(meters, bool) or not isinstance(meters, int) or meters < 0:
                raise ValueError()
            seconds = float(duration[:-1])
            miles = meters / 1609.344
            if not math.isfinite(seconds) or not math.isfinite(miles):
                raise ValueError()
            summary = route.get("description")
            if summary is not None and not isinstance(summary, str):
                raise ValueError()
            advisory = route.get("travelAdvisory", {})
            if not isinstance(advisory, dict):
                raise ValueError()
            uses_tolls = "tollInfo" in advisory
            # Empty tollInfo still means tolls exist, but Google lacks a price.
            if uses_tolls and not isinstance(advisory["tollInfo"], dict):
                raise ValueError()
            return {"driveMinutes": round(seconds / 60, 1),
                    "distanceMiles": round(miles, 2), "summary": summary or None,
                    "usesTolls": uses_tolls}
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, OverflowError):
            raise APIError(502, "GOOGLE_INVALID_RESPONSE", "Google returned an unusable route.") from None


class Application:
    def __init__(self, config, cache, google):
        self.config, self.cache, self.google = config, cache, google
        self._location_store = None
        self._location_config = None
        self._location_store_lock = threading.Lock()

    def authorized(self, header):
        return bool(self.client_index(header))

    def locations(self):
        return {"locations": self.location_snapshot()[1]}

    def location_snapshot(self):
        configured = self.config.configured_locations
        if configured is None:
            configured = self.config.labels
        if self.config.locations_path is None:
            return self.config.labels, configured
        with self._location_store_lock:
            if self._location_config is not self.config:
                self._location_store = LocationStore(self.config.locations_path, self.config.labels, configured)
                self._location_config = self.config
            store = self._location_store
        return store.snapshot()

    def client_index(self, header):
        if not header or not header.startswith("Bearer "):
            return 0
        supplied = header[7:].encode("utf-8")
        # Compare every configured secret, including Unicode values, without early exit.
        matched = 0
        for index, secret in enumerate(self.config.secrets, start=1):
            if hmac.compare_digest(supplied, secret.encode("utf-8")):
                matched = index
        return matched

    def waypoint(self, endpoint):
        if "coordinates" in endpoint:
            return {"location": {"latLng": endpoint["coordinates"]}}
        return {"placeId": self.cache.resolve(endpoint["address"], self.google.geocode)}

    def route(self, body):
        if not isinstance(body, dict) or set(body) != {"from", "to"}:
            raise invalid("Request requires from and to only.")
        # Validate BOTH endpoints before any paid Google request.
        labels, _ = self.location_snapshot()
        endpoints = [location(body[field], labels) for field in ("from", "to")]
        waypoints = [self.waypoint(endpoint) for endpoint in endpoints]
        def calculate(avoid_tolls=False):
            try:
                return (self.google.route(*waypoints, avoid_tolls=True) if avoid_tolls
                        else self.google.route(*waypoints))
            except APIError as exc:
                for field, endpoint, waypoint in zip(("origin", "destination"), endpoints, waypoints):
                    if field in exc.invalid_fields and "address" in endpoint:
                        self.cache.invalidate(endpoint["address"], waypoint["placeId"])
                raise

        primary = calculate()
        result = {**primary, "avoidTolls": None, "avoidTollsError": None}
        if primary["usesTolls"]:
            try:
                result["avoidTolls"] = calculate(avoid_tolls=True)
            except APIError as exc:
                LOG.warning("event=avoid_tolls_result result=error code=%s", exc.code)
                result["avoidTollsError"] = {"code": exc.code, "message": exc.message}
        return result

    def warm(self, stop):
        for endpoint in self.location_snapshot()[0].values():
            if stop.is_set():
                break
            if "address" in endpoint:
                try:
                    self.waypoint(endpoint)
                except APIError:
                    LOG.warning("A saved address could not be warmed; it can be retried on use.")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NoPartRoutes/" + API_VERSION
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # Base implementation logs request targets, which may contain private query data.
            pass

        def send_response(self, code, message=None):
            path = urllib.parse.urlsplit(getattr(self, "path", "")).path
            endpoint = path if path in ("/route", "/locations", "/version", "/docs", "/openapi.json", "/health") else "unknown"
            method = self.command if self.command in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE", "CONNECT") else "unknown"
            duration = (time.monotonic() - getattr(self, "started", time.monotonic())) * 1000
            LOG.info("event=http_request method=%s endpoint=%s client_key_index=%d status=%d duration_ms=%.1f",
                     method, endpoint, getattr(self, "client_key_index", 0), code, duration)
            super().send_response(code, message)

        def end_headers(self):
            self.send_header("X-No-Part-Routes-Request-ID", REQUEST_ID.get())
            self.send_header("X-No-Part-Routes-API-Version", API_VERSION)
            self.send_header("X-No-Part-Routes-Docs-Version", str(DOCS_VERSION))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def reply(self, status, value, content_type="application/json; charset=utf-8", extra=None):
            content = json.dumps(value, allow_nan=False).encode() if isinstance(value, (dict, list)) else value.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            if extra:
                for key, item in extra.items():
                    self.send_header(key, item)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def send_error(self, code, message=None, explain=None):
            # Parser/unsupported-method errors also remain JSON and do not echo the request.
            self.reply(code, {"error": {"code": "HTTP_ERROR", "message": "Invalid HTTP request."}})

        def __getattr__(self, name):
            if name.startswith("do_"):
                return self.dispatch
            raise AttributeError(name)

        def dispatch(self):
            token = REQUEST_ID.set(uuid.uuid4().hex[:16])
            self.started = time.monotonic()
            self.client_key_index = 0
            try:
                path = urllib.parse.urlsplit(self.path).path
                known = {"/route", "/locations", "/version", "/docs", "/openapi.json", "/health"}
                if path not in known:
                    raise APIError(404, "NOT_FOUND", "Unknown endpoint.")
                expected = "POST" if path == "/route" else "GET"
                if self.command != expected:
                    self.reply(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "Unsupported method."}}, extra={"Allow": expected})
                    return
                if path in ("/route", "/locations"):
                    if len(self.headers.get_all("Authorization", [])) == 1:
                        self.client_key_index = app.client_index(self.headers.get("Authorization"))
                    if not self.client_key_index:
                        self.reply(401, {"error": {"code": "UNAUTHORIZED", "message": "A valid shared secret is required."}}, extra={"WWW-Authenticate": "Bearer"})
                        return
                if path == "/route":
                    if self.headers.get_content_type() != "application/json":
                        raise APIError(415, "UNSUPPORTED_CONTENT_TYPE", "Use application/json.")
                    lengths = self.headers.get_all("Content-Length", [])
                    if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                        raise invalid("A single Content-Length is required; chunked requests are unsupported.")
                    length = int(lengths[0])
                    if length > MAX_BODY:
                        raise APIError(413, "BODY_TOO_LARGE", "Request body exceeds 16 KiB.")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise invalid("Incomplete request body.")
                    try:
                        body = strict_json(raw.decode("utf-8"))
                    except (ValueError, UnicodeError, RecursionError):
                        raise invalid("Request body must be valid UTF-8 JSON with unique fields.") from None
                    self.reply(200, app.route(body))
                elif path == "/locations":
                    self.reply(200, app.locations())
                elif path == "/version":
                    self.reply(200, version_info())
                elif path == "/health":
                    self.reply(200, {"status": "ok"})
                elif path == "/openapi.json":
                    self.reply(200, openapi())
                else:
                    docs = (BASE_DIR / "docs.html").read_text(encoding="utf-8")
                    docs = docs.replace("{{API_VERSION}}", API_VERSION).replace("{{DOCS_VERSION}}", str(DOCS_VERSION))
                    self.reply(200, docs, "text/html; charset=utf-8")
            except APIError as exc:
                self.reply(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            except (TimeoutError, socket.timeout):
                self.reply(408, {"error": {"code": "REQUEST_TIMEOUT", "message": "Request body timed out."}})
            except (BrokenPipeError, ConnectionResetError):
                LOG.info("event=client_disconnected")
            except Exception:
                LOG.error("Unexpected request failure; details omitted to protect configuration and location data.")
                self.reply(500, {"error": {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}})
            finally:
                REQUEST_ID.reset(token)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_TRACE = do_CONNECT = dispatch

    return Handler


def maintenance(cache, stop):
    while not stop.wait(60):
        try:
            cache.cleanup()
        except OSError:
            LOG.error("Could not persist expired-coordinate cleanup; check cache directory permissions.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate local configuration and storage without Google calls.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    server = None
    stop = threading.Event()
    workers = []
    file_handler = None
    try:
        config = Config.load(BASE_DIR)
        file_handler = configure_file_logging(BASE_DIR, config)
        LOG.info("event=startup api_version=%s docs_version=%d routes_limit=%d geocoding_limit=%d",
                 API_VERSION, DOCS_VERSION, config.daily_routes_limit, config.daily_geocoding_limit)
        usage = UsageGuard(BASE_DIR / "usage.ini", config.daily_routes_limit, config.daily_geocoding_limit)
        cache = LocationCache(BASE_DIR / "locationCache.csv")
        app = Application(config, cache, GoogleClient(config.google_key, usage=usage))
        server = ThreadingHTTPServer((config.host, config.port), handler_for(app))
        if args.check:
            print("Configuration, cache storage, and listening port checks passed. Google credentials were not tested.")
            return 0
        for target, params in ((app.warm, (stop,)), (maintenance, (cache, stop))):
            worker = threading.Thread(target=target, args=params, daemon=True)
            worker.start()
            workers.append(worker)
        print(f"No Part Routes listening on {config.host}:{config.port}", flush=True)
        print(f"Documentation: http://{config.host}:{config.port}/docs", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping No Part Routes.")
        return 0
    except ConfigError as exc:
        print(f"Startup failed: {exc}")
        return 1
    except OSError:
        print("Startup failed: check file permissions, listening address, and whether the port is already in use.")
        return 1
    finally:
        stop.set()
        if server is not None:
            server.server_close()
        for worker in workers:
            worker.join(timeout=GOOGLE_TIMEOUT + 1)
        if file_handler is not None:
            LOG.info("event=shutdown")
            LOG.removeHandler(file_handler)
            file_handler.close()


if __name__ == "__main__":
    import sys
    if sys.version_info < (3, 10):
        print("Python 3.10 or later is required.")
        raise SystemExit(1)
    raise SystemExit(main())
