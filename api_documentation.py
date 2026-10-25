"""Public contract; bump DOCS_VERSION for every published documentation change."""
API_VERSION = "26.10.25"
DOCS_VERSION = 10


def version_info():
    # Relative to the service base path, including when a proxy uses a prefix.
    return {"apiVersion": API_VERSION, "docsVersion": DOCS_VERSION,
            "docsUrl": "docs", "openapiUrl": "openapi.json"}


def openapi():
    text = {"type": "string", "minLength": 1}
    point = {"type": "object", "additionalProperties": False,
             "required": ["latitude", "longitude"], "properties": {
                 "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                 "longitude": {"type": "number", "minimum": -180, "maximum": 180}}}
    endpoint = {"oneOf": [{"type": "object", "additionalProperties": False,
                           "required": [field], "properties": {field: schema}}
                          for field, schema in (("label", text), ("address", text),
                                                ("pluscode", {**text, "description": "Full plus code, or compound code with locality; send a literal +, not %2B.", "examples": ["849VCWC8+R9", "CWC8+R9 Mountain View, CA, USA"]}),
                                                ("coordinates", point))]}
    error = {"type": "object", "additionalProperties": False, "required": ["error"],
             "properties": {"error": {"type": "object", "additionalProperties": False,
                                      "required": ["code", "message"], "properties": {"code": text, "message": text}}}}
    headers = {
        "X-No-Part-Routes-Request-ID": {"schema": {"type": "string"}, "description": "Correlates this request with the private application log."},
        "X-No-Part-Routes-API-Version": {"schema": {"type": "string"}, "description": "Published API version."},
        "X-No-Part-Routes-Docs-Version": {"schema": {"type": "integer"}, "description": "Changed revision means re-read docs."}}

    def response(description, schema):
        return {"description": description, "headers": headers,
                "content": {"application/json": {"schema": schema}}}

    estimate = {"type": "object", "additionalProperties": False,
               "required": ["driveMinutes", "distanceMiles", "summary", "usesTolls"], "properties": {
                   "driveMinutes": {"type": "number", "minimum": 0, "description": "Minutes, rounded to one decimal."},
                   "distanceMiles": {"type": "number", "minimum": 0, "description": "Miles, rounded to two decimals."},
                   "summary": {"type": ["string", "null"], "description": "Google route description when available."},
                   "usesTolls": {"type": "boolean", "description": "Google reports tolls, including tolls with unknown prices."}}}
    success = {**estimate,
               "required": estimate["required"] + ["avoidTolls", "avoidTollsError"],
               "properties": {**estimate["properties"],
                   "avoidTolls": {"oneOf": [estimate, {"type": "null"}],
                                  "description": "Second current-traffic estimate requested only if the primary route uses tolls. Avoiding tolls is a preference; check usesTolls. Null when unnecessary or unsuccessful."},
                   "avoidTollsError": {"oneOf": [error["properties"]["error"], {"type": "null"}],
                                       "description": "Second-request error code/message; primary route still returns HTTP 200. Null when no second request was needed or it succeeded."}}}
    errors = {400: "Invalid JSON, endpoint, or unknown label.", 401: "Missing or invalid secret.",
              405: "Unsupported method.", 408: "Client request timeout.", 413: "Body exceeds 16 KiB.",
              415: "Use application/json.", 422: "Unresolved address or no route.",
              429: "Local daily Google-call limit reached; resets at midnight UTC.",
              500: "Internal failure.", 502: "Google rejected the request or returned unusable data.",
              503: "Google quota or service unavailable.", 504: "Google timed out."}
    route = {"summary": "Drive with current traffic, leaving now", "operationId": "getRoute",
             "description": "First request allows tolls. If Google reports toll use, request one additional route with avoidTolls=true using the same resolved endpoints. Both attempts count against the Routes daily cap. Primary failure uses a normal HTTP error; second-request failure returns the primary estimate with avoidTollsError. No toll prices are returned.",
             "security": [{"SharedSecret": []}],
             "requestBody": {"required": True, "content": {"application/json": {"schema": {
                 "type": "object", "additionalProperties": False, "required": ["from", "to"],
                 "properties": {"from": endpoint, "to": endpoint}},
                 "example": {"from": {"label": "home"}, "to": {"coordinates": {"latitude": 29.7604, "longitude": -95.3698}}}}}},
             "responses": {"200": response("Fresh driving estimate.", success),
                           **{str(code): response(description, error) for code, description in errors.items()}}}
    version = {"type": "object", "required": ["apiVersion", "docsVersion", "docsUrl", "openapiUrl"],
               "properties": {"apiVersion": text, "docsVersion": {"type": "integer", "minimum": 1},
                              "docsUrl": text, "openapiUrl": text}}
    locations = {"type": "object", "additionalProperties": False, "required": ["locations"],
                 "properties": {"locations": {"type": "object", "additionalProperties": {
                     "oneOf": [option for option in endpoint["oneOf"] if option["required"] != ["label"]]}}}}
    return {"openapi": "3.1.0", "info": {"title": "No Part Routes", "version": API_VERSION,
             "description": "Read /version each session; re-read /docs and this contract when API or docs versions change. All routes use DRIVE, current traffic and departure now. GET /locations requires a shared secret and lists loaded labels and user-configured values; public discovery does not enumerate them."},
            "x-docs-version": DOCS_VERSION, "servers": [{"url": ".", "description": "Service base URL (including any proxy prefix)."}],
            "components": {"securitySchemes": {"SharedSecret": {"type": "http", "scheme": "bearer",
                           "description": "Manually chosen shared secret; all configured secrets have equal access."}}},
            "paths": {"/route": {"post": route},
                      "/locations": {"get": {"operationId": "getLocations", "summary": "List configured locations without Google calls",
                          "description": "Returns case-normalized labels and user-configured address, pluscode, or coordinates. Valid locations.ini edits reload on route/list requests after a 0.5-second save settling interval. Invalid or unreadable edits retain the previous snapshot and log a warning. No keys, notes, or Google cache data are returned.",
                          "security": [{"SharedSecret": []}], "responses": {
                              "200": response("Loaded configured locations; empty object when none are defined.", locations),
                              "401": response("Missing or invalid shared secret.", error),
                              "405": response("Use GET.", error)}}},
                      "/version": {"get": {"operationId": "getVersion", "responses": {"200": response("Version discovery.", version)}}},
                      "/health": {"get": {"operationId": "getHealth", "responses": {"200": response("Liveness only; no Google call.", {
                          "type": "object", "required": ["status"], "properties": {"status": {"const": "ok"}}})}}},
                      "/docs": {"get": {"operationId": "getDocs", "responses": {"200": {"description": "Human-readable instructions.",
                          "headers": headers, "content": {"text/html": {"schema": {"type": "string"}}}}}}},
                      "/openapi.json": {"get": {"operationId": "getOpenAPI", "responses": {"200": response("This contract.", {"type": "object"})}}}}}
