# No Part Routes — 26.10.25 specification

Status: implementation approved by the user on September 30, 2026. This document remains the v1 requirements reference.

## Purpose

No Part Routes is a small HTTP adapter for Google's Routes API. It accepts an origin and destination, calculates a driving route with current traffic for departure now, and returns drive time, distance, and an optional route description.

It runs on Windows and Linux behind the owner's reverse proxy. Portability and straightforward manual configuration take priority over frameworks, databases, and automated credential management.

## Scope

- Python standard library only; no Flask, pip dependencies, or SQLite.
- Custom request handler using `ThreadingHTTPServer`.
- HTTPS requests to Google through the standard library, with certificate verification enabled.
- INI files for configuration and CSV for the persistent location cache.
- One driving route per request, using Google's default recommended route.
- Current traffic, leaving now, on every request.
- User-defined location labels and a shared cache for resolved addresses.
- Multiple manually managed client secrets with visual reminder names and notes.
- Human-readable HTTP documentation and an OpenAPI contract.
- Discoverable API and documentation versions so clients can detect changes and re-read instructions.

V1 excludes a web administration UI, accounts, automatic key exchange, general alternative-route enumeration, intermediate stops, caller-selected travel modes or departure times, and persistent route-result caching. A conditional avoid-tolls comparison is supported.

## HTTP contract

### POST /route

Require `Content-Type: application/json` and `Authorization: Bearer <secret>`.

Example request:

```json
{
  "from": {"label": "home"},
  "to": {"address": "100 Example Street, Houston, TX 77001"}
}
```

Each endpoint must contain exactly one of:

```json
{"label": "home"}
```

```json
{"address": "100 Example Street, Houston, TX 77001"}
```

```json
{"pluscode": "849VCWC8+R9"}
```

Plus codes may be full/global codes or compound codes with locality, such as `CWC8+R9 Mountain View, CA, USA`. Send a literal `+` in JSON and INI files. Google validates and resolves them through the Geocoding address parameter. They share cache entries and warming behavior with addresses. Documentation revision 2 adds this capability while retaining API version 1.0.0 at the user's explicit request.

```json
{"coordinates": {"latitude": 29.7604, "longitude": -95.3698}}
```

Labels are case-insensitive. Unknown labels are errors and must never be interpreted as addresses. Trim surrounding whitespace from labels and addresses; reject empty values. Coordinates must be finite numbers, with latitude between -90 and 90 and longitude between -180 and 180, inclusive. Reject malformed JSON, extra request fields, and conflicting location representations.

Successful responses contain exactly these six fields:

```json
{
  "driveMinutes": 18.4,
  "distanceMiles": 7.26,
  "summary": "Example route description",
  "usesTolls": false,
  "avoidTolls": null,
  "avoidTollsError": null
}
```

The example is illustrative, not a real route result.

- `driveMinutes`: numeric Google duration in seconds divided by 60, rounded to one decimal place.
- `distanceMiles`: numeric Google distance in meters divided by 1609.344, rounded to two decimal places.
- `summary`: Google's route description, or `null` when absent or empty. Do not generate a description with an LLM.
- `usesTolls`: boolean, based on the presence of Google's route-level `travelAdvisory.tollInfo`, including an empty object for unknown pricing.
- `avoidTolls`: null when unnecessary or unsuccessful; otherwise an object containing `driveMinutes`, `distanceMiles`, `summary`, and `usesTolls` for the second calculation.
- `avoidTollsError`: null when no comparison was needed or it succeeded; otherwise a sanitized `{code, message}` for the second calculation's failure.

The primary request explicitly sets `routeModifiers.avoidTolls=false`. Include `extraComputations=["TOLLS"]` and `routes.travelAdvisory.tollInfo` in the Google field mask on both calculations. If the primary uses tolls, make exactly one second request with `avoidTolls=true`, reusing the same resolved endpoints and route label snapshot. Avoidance is a preference, not a guarantee: report actual toll use for both results. Do not make a third call if the second result still uses tolls. Do not return toll prices. Both outbound attempts consume the existing Routes cap separately; no extra geocoding is needed for the second request.

Primary failure retains the existing HTTP error behavior. A second-calculation failure, including a daily-limit or usage-storage failure, returns HTTP 200 with the successful primary estimate, `avoidTolls=null`, and `avoidTollsError`. Preserve invalid-place-ID cache invalidation on either failure. Log the route preference and comparison error code without location data. The toll-comparison change originally kept API 1.0.2 and documentation revision 8 unchanged; the current release uses date-based API version 26.10.25 and documentation revision 10.

Use `DRIVE`, `TRAFFIC_AWARE`, and `computeAlternativeRoutes=false` on each Google request. Omit Google's optional `departureTime` so it defaults to the time Google processes the request (leaving now). An explicit local timestamp can arrive in the past and be rejected due to latency or clock differences. Request only the Google fields needed for this contract. Do not silently return a traffic-unaware or previously cached estimate if the requested calculation fails.

### Discovery and health

| Endpoint | Behavior |
| --- | --- |
| `GET /version` | Public API version, documentation revision, and documentation links; no Google call |
| `GET /locations` | Authenticated list of loaded labels and configured values; no Google call |
| `GET /docs` | Public, browser-readable HTML instructions with fictional examples |
| `GET /openapi.json` | Public, machine-readable request, response, authentication, and error contract |
| `GET /health` | Public basic liveness response; no Google call |

Public documentation explains location formats, units, rounding, current-traffic behavior, authentication, errors, and applicable Google attribution requirements. It must not reveal configured labels, personal addresses, secrets, or cache contents. Authenticated `GET /locations` returns `{"locations": {label: location}}`, using normalized lowercase label names and their user-configured address, pluscode, or coordinate object. Preserve the configured representation, not the internal resolved address/place ID. Empty configuration returns `{"locations":{}}`. This uses the latest valid location snapshot, requires the same Bearer secret as routes, returns no keys/notes/cache contents, calls no Google APIs, and consumes no call limits. Location edits reload automatically as described below. Settings and secrets require a restart.

JSON remains the HTTP wire format; it is not used for settings or persistent cache storage. Health checks indicate that the service is running, not that Google credentials or quota have been verified.

### Version tracking and documentation refresh

`GET /version` returns:

```json
{
  "apiVersion": "26.10.25",
  "docsVersion": 10,
  "docsUrl": "docs",
  "openapiUrl": "openapi.json"
}
```

The example represents the current release. URLs are relative to the service base URL ending in `/`, allowing deployment beneath a reverse-proxy path prefix.

- `apiVersion` uses the date-based `YY.MM.DD` release number. It identifies the published release, not local configuration or cache contents; it does not encode compatibility.
- `docsVersion` is a monotonically increasing integer. Increment it whenever published instructions, examples, documentation, or the OpenAPI contract change, and whenever `apiVersion` changes. Documentation-only edits also increment it.
- Every service HTTP response, including errors, carries `X-No-Part-Routes-API-Version` and `X-No-Part-Routes-Docs-Version` headers. Keep the route success body limited to its six agreed fields.
- `/version`, `/docs`, and `/openapi.json` report the same released versions. The HTML docs show both versions visibly; OpenAPI uses `info.version` for `apiVersion` and a top-level `x-docs-version` for `docsVersion`.
- Serve `/version`, `/docs`, and `/openapi.json` with `Cache-Control: no-store` so clients and the reverse proxy do not reuse stale discovery information. Configure the reverse proxy to preserve the version headers and honor these cache directives.
- Maintain a concise human-readable change history in `/docs`, identifying the versions, changed behavior, and any migration instructions.

Document this client workflow explicitly for agents and other consumers:

1. On first connection and at the start of a new session, read `/version`.
2. If the client has no saved documentation revision, or either version differs from its saved value, fetch `/docs` and `/openapi.json` before issuing route requests.
3. Remember the versions associated with the instructions it actually read. If response headers subsequently report a changed version, refresh the documentation before the next route request.
4. If the refreshed API contract is incompatible with the integration, stop route calls until the integration is updated; re-reading documentation alone does not guarantee compatibility.

The service provides change signals and instructions; clients must implement the refresh workflow. There is no push notification or automatic ability to force an external agent to read documentation.

### Errors

Use a consistent JSON error body:

```json
{
  "error": {
    "code": "UNKNOWN_LABEL",
    "message": "The requested location label is not configured."
  }
}
```

| HTTP status | Meaning |
| --- | --- |
| 400 | Invalid JSON, invalid location, unknown label, or unsupported request field |
| 401 | Missing or invalid client secret |
| 404 | Unknown HTTP path |
| 405 | Unsupported method for an existing path |
| 413 | Request body exceeds the configured implementation limit |
| 415 | Unsupported request content type |
| 422 | Google cannot resolve a supplied address or find a route |
| 429 | Local daily Google-call cap reached |
| 502 | Google rejects the upstream request or returns an unusable response |
| 503 | Google quota exhaustion or temporary service unavailability |
| 504 | Google request timeout |

Do not relay raw upstream error bodies or credentials to callers. Set a 15-second timeout for each Google HTTP call and do not automatically retry paid API calls in v1. Bound request body size to 16 KiB and validate authentication and input before making Google calls. Return `Cache-Control: no-store` for route responses.

## Configuration files

Resolve application file paths relative to the application directory, regardless of the launcher's current working directory. Read text files as UTF-8. Disable INI interpolation so percent characters in secrets and addresses remain literal.

### settings.ini

```ini
[server]
host = 127.0.0.1
port = 8080

[google]
api_key = paste-your-google-api-key-here
```

The Google key is stored as plain text. No encryption, secret manager, or credential exchange is required. Configuration containing real keys stays out of version control; distributable examples contain placeholders only.

The listening address and port are configurable. Loopback is the default for a reverse proxy on the same machine. A proxy on another machine requires an explicitly configured reachable listening address.

### apiKeys.ini

```ini
[Muse]
secret = CHANGE-ME-Muse-95c70f6833b641e7957f41f908afc2b1
note = Muse agent UI

[Grok bot]
secret = CHANGE-ME-Grok-146a8b5998504d0aa70ba09b46884f96
note = Bot on my server

[OpenAI]
secret = CHANGE-ME-OpenAI-bf1c701e0d0c4216b157c2b662512ac9
note = OpenAI integration
```

- Each section defines an accepted shared secret.
- Names and notes are visual reminders only, with no effect on permissions.
- All configured secrets grant identical access.
- Secrets are manually chosen, nonempty, single-line strings. There is no general minimum length or complexity requirement. Startup rejects secrets beginning with `CHANGE-ME-` and the legacy public sample secrets `1234`, `my-grok-secret`, and `a-longer-secret-if-i-want`. Every section must be replaced or removed before startup, including unused sections. Use a unique random secret for each client.
- Internal characters are preserved. Surrounding whitespace is trimmed when reading INI values; it is not part of the configured secret.
- Compare secrets exactly and case-sensitively, using constant-time comparison.
- Do not include secrets in URLs, responses, documentation, or logs.
- Adding, replacing, or deleting a secret requires restarting the server.
- Startup requires at least one valid secret. There is no unauthenticated routing mode.

### locations.ini

```ini
[home]
address = 100 Example Street, Houston, TX 77001

[work]
latitude = 29.7604
longitude = -95.3698

[meeting]
pluscode = 849VCWC8+R9
```

Each section defines a label containing an address, a pluscode, or a complete latitude/longitude pair. Reject conflicting definitions, invalid coordinates, empty labels, and labels that collide after case normalization. Labels cannot refer to other labels. Full and compound plus codes are resolved and cached using the existing address-resolution flow.

User-provided labels, addresses, and coordinates remain until edited or removed. They are configuration data, not Google response cache data. Settings and secret edits require restarting. For locations.ini, route and authenticated locations requests read the file after a 0.5-second save-settling interval, validate the entire file, and atomically publish a complete snapshot. Detect changed contents even when size and modification time match. Each in-flight route resolves both endpoints against one snapshot. Invalid, unreadable, or concurrently changing files retain the last valid snapshot; invalid/unreadable changes emit sanitized, deduplicated log warnings. A fixed file reloads on a subsequent request. An empty valid file clears all labels. Label edits do not invalidate unrelated address-keyed cache entries or perform background geocoding; newly used addresses resolve through the existing cache. Startup still requires valid configuration.

## Location resolution and caching

Both named addresses and directly submitted addresses use the same cache. User-supplied coordinates bypass geocoding.

Resolution flow:

1. Resolve a label from `locations.ini`, if supplied.
2. Use user-supplied coordinates directly, if supplied.
3. For an address, look up its shared cache entry.
4. On a miss, call Google's Geocoding API, obtain a usable place ID, and store permitted resolution data.
5. Send the resolved place ID or user-supplied coordinates to Routes.
6. Fetch a fresh route result for the current request.

Normalize cache address keys conservatively: trim surrounding whitespace, collapse repeated whitespace, and compare case-insensitively. Preserve punctuation, apartment/unit information, and the original user-supplied address. Do not attempt fuzzy address equivalence. Two labels with the same normalized address share one entry.

At startup, warm saved addresses that lack a usable cached place ID. Do not resolve coordinate-only labels. Warming failures are reported without preventing the server from serving other locations; a later request can attempt resolution again. Deduplicate concurrent resolutions of the same address to avoid unnecessary Google calls.

An edited label naturally uses the cache key for its new address. Missing cache files are recreated. Preserve validated recoverable rows from a damaged cache under a separate filename with Google coordinates removed, report the problem without personal location data, and create a clean cache. Unparseable content is not retained. A cache write failure should be reported but should not discard an otherwise successful route response.

### locationCache.csv

The application manages this file; users do not normally edit it. Use Python's CSV reader/writer so commas, quoting, and newlines are handled correctly.

Proposed columns:

```text
address_key,source_address,place_id,latitude,longitude,resolved_at,coordinates_expires_at
```

`source_address` is the user-supplied address, not a Google-formatted address. Timestamps are UTC. V1 writes only place IDs and leaves Google coordinate fields empty, since they are unnecessary for routing. Retention handling still applies to coordinates found in an existing cache file.

| Data | Retention |
| --- | --- |
| User-supplied configuration and addresses | Until edited or removed |
| Google-returned place IDs | May be retained indefinitely |
| Google-returned coordinates | At most 30 consecutive calendar days from retrieval |
| Google route duration, distance, and description | No persistent cache |

Delete expired Google coordinate values at startup and periodically while running, even if the entry is never requested. Never extend coordinate expiry merely because the cache entry was read. A valid retained place ID remains usable after its coordinates expire; coordinate expiration alone must not trigger a new geocoding call. If Google explicitly reports that a retained place ID is invalid, invalidate it and resolve the original address again on the next request.

Coordinate cleanup must remove expired values from the managed CSV and avoid retaining stale coordinate copies in temporary or recovery files. Damaged files containing Google coordinates must not become indefinite archives; remove them within the same maximum retention period.

Coordinate cache expiration applies to Google-derived values, not independently user-supplied coordinates.

Coordinate access and CSV updates are synchronized across request threads. Save through a temporary file in the same directory and atomically replace the managed cache. Support one server process per application directory; sharing this CSV among multiple processes is outside v1 scope.

### Cost expectations

Caching avoids repeated explicit geocoding of the same address. It does not eliminate Routes calls: every route request needs a fresh traffic estimate. Routes can also geocode addresses internally, so adding Geocoding and a cache does not inherently reduce total API calls or charges compared with passing addresses directly to Routes. Startup warming creates Google usage for uncached saved addresses.

The Google project needs billing and both Routes API and Geocoding API enabled. These are manual infrastructure prerequisites; the launch scripts do not configure the Google account.

## Deployment and launchers

- `startWindows.bat`: find a suitable installed Python using the Windows launcher or `python`.
- `startLinux.sh`: find a suitable installed Python using `python3`.
- Require Python 3.10 or later; Python 3.10 is the verified minimum.
- No automatic Python installation, dependency installation, downloads, or administrator elevation.
- Check Python compatibility, required configuration, at least one client secret, writable cache storage, and the ability to bind the configured port.
- Report actionable failures and keep Windows startup errors visible when launched by double-clicking.
- Start in the foreground and print the listening address and documentation URL.
- Support orderly shutdown with Ctrl+C.

The distribution is a copyable application folder containing launch scripts, Python source, configuration examples, and documentation. A working deployment includes manually filled configuration files. A bundled portable Python runtime is deferred; it would require separate OS/architecture packages.

The owner's reverse proxy handles client HTTPS. Client authorization headers must reach the Python service. The service does not depend on forwarded headers for authentication. Browser CORS support is excluded by default and can be specified later if a browser UI needs cross-origin calls.

Do not serve arbitrary application files or expose configuration files through HTTP. Logs may include request status, elapsed time, and generic operational errors, but not keys, authorization headers, personal addresses, coordinates, or raw Google responses.

## Documentation and Google requirements

### Local logs and usage protection

Write `logs/application.log` with UTC timestamps, request IDs, known endpoints, status, duration, authenticated key section index, cache hits/misses, Google API attempts/outcomes, and attempted daily totals. Expose the correlation ID in `X-No-Part-Routes-Request-ID`. Do not log locations, secrets, route results, request bodies, raw errors, client IPs, or arbitrary paths/methods. Rotate at 1 MiB and keep five backups by default; configure positive `logging.max_bytes` and `logging.backup_count` in `settings.ini`.

Default to 150 Routes and 300 Geocoding HTTP attempts per UTC day, configured by positive `usage.daily_routes_limit` and `usage.daily_geocoding_limit`. Persist reservations atomically in `usage.ini` before outbound requests, including failures and startup warming. Synchronize threads; preserve counts across restarts; reset on the next attempt after midnight UTC, never on a backward clock change. Return 429 `DAILY_USAGE_LIMIT` for a blocked primary call and 503 `USAGE_STORAGE_UNAVAILABLE` if counters cannot be persisted before a primary call. Optional avoid-tolls failures preserve HTTP 200 and the primary estimate, with the error code/message in `avoidTollsError`. Damaged counter files stop startup rather than silently resetting. Cached resolutions and discovery do not consume the counts. These counts are conservative local guards, not Google's authoritative billing. Support one process per application directory.

`GET /docs` must be a complete human-readable HTML page, not merely a raw OpenAPI file or an interactive schema viewer. It works in a browser without external assets or JavaScript and also exposes its instructions as ordinary readable text for agent tools.

Include a quick-start guide, a copyable authenticated request example with a placeholder secret, all four location forms, success and error examples, units and rounding, label behavior, cache behavior, current-traffic semantics, version-refresh instructions, and the change history. Provide a link to `/openapi.json`. Use fictional examples and explain that labels are configured by the server owner; do not publish actual configured labels or their values.

Include a human-readable `README.md` in the eventual distribution covering Python and Google prerequisites, Windows/Linux startup, editing each INI file, adding/removing client secrets, restart behavior, cache recovery, reverse-proxy configuration, and common failures. HTTP documentation is for API consumers; the README also covers operating the service.

Explain to consumers that results are estimates for departure now and that summaries may be absent. Include applicable Google Maps attribution/display instructions and Terms of Use and Privacy Policy requirements in integration documentation. Consumer interfaces must handle these requirements when displaying Google-derived results; the route response contract does not replace them.

Prefer Google's official logo; space-constrained interfaces may use properly styled `Google Maps` text visibly beside results. Appending attribution to an optional JSON summary does not itself enforce compliant display.

Primary references checked during planning:

- [Routes API Compute Routes reference](https://developers.google.com/maps/documentation/routes/reference/rest/v2/TopLevel/computeRoutes)
- [Specify locations for a route](https://developers.google.com/maps/documentation/routes/specify_location)
- [Routes API policies and attribution](https://developers.google.com/maps/documentation/routes/policies)
- [Google Maps Platform service-specific terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)
- [Python HTTP server documentation](https://docs.python.org/3/library/http.server.html)

The cited service-specific terms apply to non-EEA billing accounts. Verify the applicable Google agreement before implementation if the billing account is in the EEA or has custom terms.

## Acceptance criteria for a later approved implementation

- Launch on Windows and Linux with installed Python and no third-party packages.
- Accept mixed label, address, pluscode, and coordinate endpoints.
- Return exactly the six agreed success fields with correct unit conversion and rounding. Verify conditional second calls, endpoint reuse, daily counting, and partial-error responses.
- Request driving with current traffic and departure now on every route call.
- Accept every configured client secret, except blocked public sample values, with equal permissions; reject invalid or absent secrets before Google calls.
- Share resolution entries between labels and directly submitted matching addresses.
- Warm missing saved-address entries and reuse cached place IDs without unnecessary coordinate refreshes.
- Delete expired Google coordinates without extending expiry on reads.
- Keep serving unrelated routes after a warming failure and recover visibly from a missing or damaged cache.
- Handle concurrent requests without corrupting the CSV or duplicating simultaneous resolution calls for the same address.
- Return documented errors for invalid input, no route, upstream failures, and timeouts without returning stale traffic data.
- Expose useful docs and OpenAPI without revealing real configuration or personal locations.
- Publish consistent API/documentation versions through `/version`, response headers, HTML docs, and OpenAPI; keep the route success body unchanged.
- Increment the documentation revision for documentation-only changes and provide explicit client refresh instructions and a change history.
- Serve complete browser-readable HTML documentation and include an operator README in the distribution.
- Keep secrets and personal location data out of logs and HTTP-accessible files.

Implementation, automated unit testing, and live Google verification with the configured test key are approved. Deployment to the owner's infrastructure remains a separate step.
