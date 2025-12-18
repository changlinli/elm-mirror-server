# Elm Mirror Server Design Notes

## Elm Package Server API Behavior

### `/all-packages/since/<N>` Endpoint

**Important**: This endpoint returns packages in **reverse chronological order** (newest first).

Example response from `/all-packages/since/0`:
```json
[
  "edkelly303/elm-yafl@1.0.0",       // Most recently published
  "the-sett/elm-mlir@2.0.0",
  "maca/postgrest-admin-preview@15.0.0",
  ...
  "elm/core@1.0.0"                   // One of the oldest packages
]
```

**Semantics of `since/<N>`**:
- Returns all packages published since there were N total packages in the registry
- If there are currently 20,000 packages, `/all-packages/since/19990` returns the 10 newest packages
- `/all-packages/since/0` returns ALL packages (everything published since there were 0 packages)

### Implications for our mirror

When storing `registry.json`:
- We store the array as-is from the server (newest first)
- To compute `/all-packages/since/N` response: return the first `(total - N)` elements
- For incremental sync: new packages appear at the beginning, so we prepend

### `/all-packages` Endpoint

Returns a JSON object (not array) mapping package names to version arrays:
```json
{
  "elm/core": ["1.0.0", "1.0.1", "1.0.2", ...],
  "elm/json": ["1.0.0", "1.0.1", ...]
}
```

This is NOT ordered by publication time.

### `/packages/<author>/<name>/<version>/endpoint.json`

Returns the download URL and hash:
```json
{
  "url": "https://github.com/elm/core/zipball/1.0.5/",
  "hash": "9288a7574b778b4ebc6557d504a0b16c09daab43"
}
```

For our mirror, we store only `hash.json` with the hash, and the server dynamically generates
`endpoint.json` with an absolute URL based on `--base-url`.

---

## Final Design Decisions

### Single Python Script

One script (`elm_mirror.py`) with three subcommands:
- `sync` - one-time sync from Elm package server
- `serve` - WSGI web server (with optional background sync)
- `verify` - check integrity of mirrored packages

### Command-Line Interface

**`sync`**
- `--mirror-content` - mirror directory (default: `.`)
- `--package-list` - JSON file for selective sync (optional)

**`serve`**
- `--mirror-content` - mirror directory (default: `.`)
- `--base-url` - public URL for generated links (required)
- `--sync-interval` - seconds between background syncs (optional; if omitted, no sync)
- `--port` - port to listen on (default: `8000`)
- `--host` - interface to bind (default: `127.0.0.1`)
- `--package-list` - for background sync (only relevant if `--sync-interval` set)

**`verify`**
- `--mirror-content` - mirror directory (default: `.`)

### File Structure

```
<mirror-content>/
├── registry.json                  # Package list with status tracking
├── all-packages                   # JSON index (same format as official)
├── packages/
│   └── <author>/
│       └── <package>/
│           └── <version>/
│               ├── elm.json       # Copied from official server
│               ├── hash.json      # {"hash": "..."}
│               └── package.zip    # Downloaded from GitHub
```

### Package Status Tracking

`registry.json` structure:
```json
{
  "packages": [
    {"id": "elm/core@1.0.0", "status": "success"},
    {"id": "elm/json@1.0.0", "status": "pending"},
    {"id": "broken/package@1.0.0", "status": "failed"},
    {"id": "known-bad/package@1.0.0", "status": "ignored"}
  ]
}
```

Status values:
- `success` - successfully downloaded and mirrored
- `pending` - not yet attempted
- `failed` - attempted but failed
- `ignored` - intentionally skipped (e.g., known broken packages)

### Server Behavior

- Static files served directly (elm.json, package.zip, etc.)
- `/all-packages` - served from static file
- `/all-packages/since/<N>` - computed dynamically from `registry.json`, includes ALL packages regardless of status
- `/packages/<author>/<name>/<version>/endpoint.json` - generated dynamically with absolute URL from `--base-url`
- Requests for `package.zip` of `failed`/`pending` packages return 5xx with descriptive error

### Sync Behavior

- Append-only (package unpublishing is not possible in Elm ecosystem)
- Continue on individual package failures, track status in `registry.json`
- Retry failed packages at end of sync run
- Atomic updates to prevent inconsistent state on crash

### Package List Format

JSON file supporting both formats:
- Package names (all versions): `["elm/core", "elm/json"]`
- Specific versions: `["elm/core@1.0.5", "elm/json@1.1.3"]`
- Mixed: `["elm/core", "elm/json@1.1.3"]`

### Verification

Checks:
1. All `success` packages have `package.zip` present
2. SHA-1 hash matches what's in `hash.json`
3. Reports missing or corrupted packages

### WSGI Compatibility

The server uses Python's built-in `wsgiref` module:
- Standalone: `python elm_mirror.py serve ...` uses `wsgiref.simple_server`
- CGI: Can be invoked via `wsgiref.handlers.CGIHandler` (auto-detected via `GATEWAY_INTERFACE` env var)
- No third-party dependencies required
