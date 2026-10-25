# No Part Routes

A small HTTP API for Google driving time and distance, with current traffic and departure now. Use saved labels, addresses, plus codes, or coordinates. Routes that use tolls also get an optional avoid-tolls comparison.

## Development status

API version **26.10.25**, documentation revision **10**. Startup rejects the public sample client secrets, including the original examples. The route request and response formats are unchanged. Recent updates added live location-file reload, saved-location discovery, toll comparisons, rotating logs, and persistent daily Google-call limits.

This is a Python standard-library application for a single server process per installation. There are no Python packages to install, no database, and no automatic Python installation.

## Run

Requires **Python 3.10 or newer**. Python 3.10 is the supported minimum; the automated suite has also been verified on Python 3.12. Earlier versions are not supported.

Your Google project needs billing, **Routes API**, and **Geocoding API** enabled. The application uses your Google key but does not configure the Google account.

1. Clone or download the source and keep the application files together in a writable folder.
2. Copy `settings.example.ini`, `apiKeys.example.ini`, and `locations.example.ini` to the same names without `.example`. The launchers create missing copies automatically and never overwrite existing files.
3. Put your Google key in `settings.ini`.
4. Replace **every** sample secret in `apiKeys.ini` with your own unique random value, or delete unused sections. A sample in any section prevents startup.
5. Add saved locations to `locations.ini`, or leave it empty.

On Windows, double-click `startWindows.bat` or run it from Command Prompt:

```bat
startWindows.bat
startWindows.bat --check
```

On Linux, open a terminal in the application folder:

```sh
./startLinux.sh
./startLinux.sh --check
```

If extraction did not preserve executable permissions, run `chmod +x startLinux.sh` once, or use `sh startLinux.sh`. The Linux launcher uses `python3`; the Windows launcher tries `py -3`, then `python`. The selected interpreter must meet the version requirement. The scripts work from their own application folder regardless of the terminal's starting directory.

You can also select an installed compatible Python directly:

```sh
python3.12 no_part_routes.py
python3.12 no_part_routes.py --check
```

With the sample server settings, open <http://127.0.0.1:8080/docs>. Keep the terminal open while using the service. Stop it with Ctrl+C.

`--check` validates local configuration, writable storage, and whether the listening port can be bound. It makes **no Google requests** and does not test Google credentials. It does create or update local logs, usage storage, and cache files. Normal startup warms uncached saved addresses and plus codes in the background, which can consume Geocoding calls immediately.

## Source checkout and private files

Only source, launchers, tests, documentation, sample INI files, and the sanitized Postman collection belong in the public repository.

| File | Purpose | Included in Git? |
| --- | --- | --- |
| `settings.example.ini`, `apiKeys.example.ini`, `locations.example.ini` | Public configuration templates | Yes |
| `settings.ini` | Listening settings, Google key, and optional limits | No |
| `apiKeys.ini` | Client secrets and private notes | No |
| `locations.ini` | Your saved labels, addresses, plus codes, and coordinates | No |
| `locationCache.csv` and its recovery/temp files | Resolved place IDs and original input addresses | No |
| `usage.ini` and temporary copies | Persistent daily attempted-call counts | No |
| `logs/` | Local operational logs | No |
| Other CSV/TSV files | Potentially private location data or exports | No |
| `NoPartRoutes.postman_collection.json` | Public localhost template with an empty client secret | Yes |
| Other Postman collections, environments, and globals | Local URLs, secrets, and request data | No |

The ignore rules also exclude Python environments, bytecode, test/build artifacts, editor files, and local tool configuration. Keep actual URLs, credentials, and personal locations out of source files, screenshots, and public exports. Git exclusions do not remove sensitive content pasted into a tracked file.

## Configuration and client access

All configuration and runtime paths are relative to the application folder.

In `settings.ini`:

```ini
[server]
host = 127.0.0.1
port = 8080

[google]
api_key = paste-your-google-api-key-here
```

Replace the Google placeholder before starting. Restart after changing settings.

`apiKeys.ini` contains one section per client, with a `secret` and an optional `note`. Section names and notes are reminders for the server owner. Every configured secret grants the same access to routes and saved locations. Secrets are case-sensitive and are not sent to Google.

Generate each client secret with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use `python3` on Linux if needed. Paste the generated value into the section's `secret` field. Never use the generated value shown by someone else's installation or a public example.

Startup rejects values beginning with `CHANGE-ME-`, plus the legacy public samples `1234`, `my-grok-secret`, and `a-longer-secret-if-i-want`. Remove an unused section entirely; leaving its sample secret blocks startup. There is no general minimum length rule for other manually chosen secrets, but unique random values are recommended. Restart after adding, changing, or removing secrets.

INI files use UTF-8. Surrounding whitespace is trimmed; internal spaces and percent characters are preserved. INI interpolation and `DEFAULT` sections are not supported. Configuration files are not served by the HTTP service.

## Saved locations

Each section in `locations.ini` defines one label and exactly one location representation:

```ini
[home]
address = 100 Example Street, Example City

[work]
latitude = 29.7604
longitude = -95.3698

[meeting]
pluscode = 849VCWC8+R9

[meetingWithLocality]
pluscode = CWC8+R9 Mountain View, CA, USA
```

These are illustrative examples. Replace them with your own locations. Use a full plus code, or include city/region context with a shortened code. Keep the literal `+` in both INI and JSON; do not replace it with `%2B`.

Labels ignore case and surrounding whitespace. An unknown label is an error, not an address fallback. An empty valid file clears all labels.

Save `locations.ini` to add, change, or remove labels without restarting. After a 0.5-second settling interval, the next route or locations request loads the latest valid file. Invalid or unreadable edits retain the previous valid labels and log a warning. Each route uses one complete snapshot. New addresses resolve when first used; existing cache entries remain reusable.

Authenticated `GET /locations` lists labels and their configured values without calling Google. All valid client secrets can read this list. Public discovery does not list your saved locations.

## API and integrations

Public endpoints are `GET /health`, `/version`, `/docs`, and `/openapi.json`. Routes and saved-location discovery require `Authorization: Bearer YOUR_SHARED_SECRET`.

Send JSON to `POST /route`:

```json
{
  "from": {"label": "home"},
  "to": {"coordinates": {"latitude": 29.7604, "longitude": -95.3698}}
}
```

Each endpoint contains exactly one of `label`, `address`, `pluscode`, or `coordinates`. You may mix forms. Coordinates bypass Geocoding. Both endpoints are validated before any paid Google request.

An illustrative response:

```json
{
  "driveMinutes": 18.4,
  "distanceMiles": 7.26,
  "summary": null,
  "usesTolls": false,
  "avoidTolls": null,
  "avoidTollsError": null
}
```

Minutes are rounded to one decimal and miles to two. `summary` is Google's description or null. Every calculation uses driving with current traffic and departure now; old route results are not cached.

The first calculation allows tolls. If Google reports toll use, the service makes one additional calculation with `avoidTolls=true`, reusing the resolved endpoints. `avoidTolls` contains the same four estimate fields. Avoidance is a preference, so check the alternative's `usesTolls`. Toll prices are not returned.

If the second calculation fails, HTTP 200 preserves the primary estimate and puts a safe error code/message in `avoidTollsError`. A primary failure returns a normal HTTP error. Each outbound attempt counts separately toward the daily cap.

Use `/docs` for full examples and errors, and `/openapi.json` for the machine-readable contract. Clients and agents should read `/version` each session and refresh both documents whenever the API or documentation version changes. Every response carries version headers and an `X-No-Part-Routes-Request-ID` for log correlation. Discovery URLs are relative to the service base URL, including a reverse-proxy prefix.

### Postman

Import `NoPartRoutes.postman_collection.json`. Its shipped `baseUrl` is localhost, `sharedSecret` is empty, and location values are public examples. Requests refer to `{{baseUrl}}`; authentication refers to `{{sharedSecret}}`. No Google key belongs in Postman.

In the collection's **Variables** tab, set:

| Variable | Value |
| --- | --- |
| `baseUrl` | Your service base URL without a trailing slash |
| `sharedSecret` | Your client secret from `apiKeys.ini` |
| `fromType`, `toType` | `label`, `address`, `pluscode`, or `coordinates` |
| `fromValue`, `toValue` | Matching label, address, plus code, or `latitude,longitude` |

Open **Route — build JSON from fields** and click **Send**. The pre-request script builds and escapes the JSON. Leave the Body tab's `{{routeBody}}` unchanged. Environment variables with matching names override collection values; clear unexpected overrides if edits appear ignored.

The collection includes saved locations and public discovery requests. Only sending a route calls Google. Do not commit a customized export over the public template: exports can contain your private URL, secrets, personal locations, and saved responses. Store private collection exports under a different filename, and keep environment/global exports local.

## Everyday operation

Run one server process per application folder. The cache and usage counters synchronize threads within that process; separate processes must not share those files.

`locationCache.csv` is created automatically and stores the original input address plus its Google place ID. Addresses and plus codes share entries after conservative case/whitespace normalization. Unit information, punctuation, and literal plus signs are preserved. The service does not persist route results or newly returned Google coordinates. Expired legacy Google coordinates are removed at startup and during maintenance.

Treat the entire cache as private location data. A damaged cache is replaced, with validated recoverable records saved in `locationCache.csv.damaged.csv` without Google coordinates. Removing the cache causes addresses to be geocoded again. Cache write warnings do not discard successful in-memory resolutions.

Optional settings, also shown in the sample configuration:

```ini
[logging]
max_bytes = 1048576
backup_count = 5

[usage]
daily_routes_limit = 150
daily_geocoding_limit = 300
```

Values must be positive integers. Restart to apply changes. The limits are separate: they are not a combined 450-call allowance or a billing guarantee.

Every attempted Google call, including failures and startup warming, is reserved in `usage.ini` before sending. Counts survive restarts and reset on the next attempt after midnight UTC. Moving the system clock backward does not reset them. Cached resolutions and discovery requests consume no Google calls.

**Keep `usage.ini` during normal operation and updates.** Deleting or editing it can reset the protection. A damaged file stops startup instead of silently clearing the counters; restore valid counters before continuing. Failure to persist a reservation blocks the Google call. These are local attempted-call counts, not Google's authoritative usage or billing. Review current [Google Maps pricing](https://developers.google.com/maps/billing-and-pricing/pricing) and your Google-side quotas separately.

Logs are in `logs/application.log`, with UTC timestamps, request IDs, known endpoints, status, timing, client key section index, cache outcomes, and Google-call counts. They omit secrets, locations, bodies, route results, client IPs, and raw Google error messages. Default rotation retains the current file and five backups of roughly 1 MiB each.

On Linux, follow the log with `tail -f logs/application.log`. In PowerShell:

```powershell
Get-Content .\logs\application.log -Tail 30 -Wait
```

## Reverse proxy

The sample settings listen on `127.0.0.1:8080`. For a proxy on another machine or trusted LAN access, choose a reachable host such as `0.0.0.0` in your own settings. External HTTPS, certificates, routing, and firewall rules remain the administrator's responsibility. The Python listener is intended for use behind your proxy and does not provide TLS.

Preserve Authorization and the response version/request-ID headers, and honor `Cache-Control: no-store`. For a URL prefix such as `/routes/`, strip it upstream so `/routes/route` reaches `/route`; retain the trailing slash on the service base URL. There is no cross-origin browser CORS support in v1.

Each Google call has a 15-second timeout and no automatic retries. Two uncached addresses plus a toll comparison can require four sequential calls; allow at least 75 seconds in the proxy and client.

## Updating and backup

Stop the server before replacing application files. Keep a private copy of `settings.ini`, `apiKeys.ini`, `locations.ini`, `locationCache.csv`, and `usage.ini`. Preserve the usage counters along with the rest of the installation; an old restored counter file may undercount attempts made since its snapshot.

Replace source, launchers, documentation, and sample files without overwriting your real configuration. Review new sample settings, then run `--check` before normal startup. Old installations using the original public sample client secrets must replace them before this release starts. Private URLs, secrets, and locations should stay local when updating the public Postman template.

## Common failures

- **Python missing or too old:** install a compatible interpreter and reopen the terminal. The Linux launcher specifically checks `python3`; invoke a compatible version directly if that name points to an older installation.
- **Replace every sample secret:** replace all sample values in `apiKeys.ini`, or remove unused sections. A populated Google key does not bypass this check.
- **Paste your Google key:** replace the placeholder in `settings.ini`.
- **Cannot start:** check INI syntax, directory permissions, the listening address, and whether the port is already occupied.
- **401:** check the Bearer header and exact client secret. Restart after secret changes.
- **422:** verify the address or that a driving route exists.
- **429 `DAILY_USAGE_LIMIT`:** the local daily cap was reached. Wait for midnight UTC or deliberately change the limit and restart.
- **503 `USAGE_STORAGE_UNAVAILABLE`:** fix storage or directory access before retrying; the Google call was blocked.
- **502:** verify key restrictions and that both Google APIs are enabled.
- **503:** check Google quota, billing, connectivity, or service availability.
- **504:** Google exceeded the per-call timeout.
- **Invalid location edit:** fix the file and save again. The previous valid labels remain active until a valid edit loads.

## Check

Run the automated suite with a compatible Python:

```sh
python3 -B -m unittest discover -s tests -v
```

Tests use temporary directories, mocked Google responses, and local HTTP connections. They do not use a real Google key or send Google queries. They cover configuration, blocked sample secrets, authentication, validation, caching/concurrency, live label reload, toll comparisons, usage persistence, log sanitization, and startup. The native Windows launcher test runs on Windows only.

Automated verification on October 25, 2026 used Python 3.10.12 and Python 3.12.15 on Linux: 91 tests passed and the Windows-only test was skipped on each interpreter. The Linux launcher was exercised with a compatible `python3` on PATH.

Separate live verification passed 13 smoke checks using 16 Google HTTP attempts: 12 Routes and 4 Geocoding, within a persisted combined cap of 25. Checks covered coordinate/address/plus-code/label routes, startup warming, normalized cache reuse, cache reload, a Golden Gate toll route with a successful avoid-tolls comparison, an explicit avoid-tolls request, reverse-direction routing, and local discovery/authentication/validation. Test locations were public examples; the installation's private configuration, cache, and usage counters were preserved.

Live Google verification is a separate operation. Count outbound attempts, not just incoming route requests: startup warming, address resolution, and toll comparisons can each add calls. Keep any live test configuration and output private.

For maintainers, use the date-based `YY.MM.DD` release number in `API_VERSION` and increment `DOCS_VERSION` in `api_documentation.py` when publishing behavior or documentation changes. Update the change history in `docs.html`. Review the intended Git file list for private data before publishing.

## Displaying Google results

Before displaying results in a consuming interface, review Google's [Routes API policies](https://developers.google.com/maps/documentation/routes/policies), attribution requirements, and [service-specific terms](https://cloud.google.com/maps-platform/terms/maps-service-terms). The JSON response does not provide the consuming application's branding, Terms of Use, or Privacy Policy.
