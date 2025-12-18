#!/usr/bin/env python3
"""
Elm Mirror Server

A Python script to create and serve a read-only mirror of the Elm package server.
Supports syncing packages, serving them via HTTP, and verifying integrity.

Usage:
    python elm_mirror.py sync [--mirror-content DIR] [--package-list FILE]
    python elm_mirror.py serve --base-url URL [--mirror-content DIR] [--port PORT] [--host HOST] [--sync-interval SECS] [--package-list FILE]
    python elm_mirror.py verify [--mirror-content DIR]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from wsgiref.handlers import CGIHandler
from wsgiref.simple_server import make_server

# =============================================================================
# Constants
# =============================================================================

ELM_PACKAGE_SERVER = "https://package.elm-lang.org"
DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"

# Status values for packages
STATUS_SUCCESS = "success"
STATUS_PENDING = "pending"
STATUS_FAILED = "failed"
STATUS_IGNORED = "ignored"

# =============================================================================
# Registry Management
# =============================================================================


def load_registry(mirror_dir: Path) -> dict:
    """Load the registry from disk, or return an empty registry if not found."""
    registry_path = mirror_dir / "registry.json"
    if registry_path.exists():
        with open(registry_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"packages": []}


def save_registry(mirror_dir: Path, registry: dict) -> None:
    """Save the registry to disk atomically."""
    registry_path = mirror_dir / "registry.json"
    temp_path = mirror_dir / "registry.json.tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)

    # Atomic rename
    temp_path.replace(registry_path)


def get_package_status(registry: dict, package_id: str) -> str | None:
    """Get the status of a package, or None if not in registry."""
    for pkg in registry["packages"]:
        if pkg["id"] == package_id:
            return pkg["status"]
    return None


def set_package_status(registry: dict, package_id: str, status: str) -> None:
    """Set the status of a package, adding it if not present."""
    for pkg in registry["packages"]:
        if pkg["id"] == package_id:
            pkg["status"] = status
            return
    # Not found, add it
    registry["packages"].append({"id": package_id, "status": status})


def parse_package_id(package_id: str) -> tuple[str, str, str]:
    """Parse a package ID like 'author/name@version' into (author, name, version)."""
    match = re.match(r"^([^/]+)/([^@]+)@(.+)$", package_id)
    if not match:
        raise ValueError(f"Invalid package ID: {package_id}")
    return match.group(1), match.group(2), match.group(3)


# =============================================================================
# Package List (Whitelist) Handling
# =============================================================================


def load_package_list(package_list_path: str | None) -> set[str] | None:
    """
    Load a package list from a JSON file.

    Returns None if no package list (meaning sync all packages).
    Returns a set of package identifiers that should be synced.

    The set contains entries like:
    - "author/name" (all versions of this package)
    - "author/name@version" (specific version)
    """
    if package_list_path is None:
        return None

    with open(package_list_path, "r", encoding="utf-8") as f:
        package_list = json.load(f)

    return set(package_list)


def should_sync_package(package_id: str, package_list: set[str] | None) -> bool:
    """Check if a package should be synced based on the package list."""
    if package_list is None:
        return True

    # Check exact match (author/name@version)
    if package_id in package_list:
        return True

    # Check package name match (author/name)
    author, name, _ = parse_package_id(package_id)
    package_name = f"{author}/{name}"
    if package_name in package_list:
        return True

    return False


# =============================================================================
# HTTP Utilities
# =============================================================================


def fetch_url(url: str, timeout: int = 30) -> bytes:
    """Fetch a URL and return the response body as bytes."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "elm-mirror-server/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_json(url: str, timeout: int = 30) -> any:
    """Fetch a URL and parse the response as JSON."""
    data = fetch_url(url, timeout)
    return json.loads(data.decode("utf-8"))


# =============================================================================
# Sync Functionality
# =============================================================================


def fetch_all_packages_since(since: int = 0) -> list[str]:
    """Fetch the list of all packages from the Elm package server."""
    url = f"{ELM_PACKAGE_SERVER}/all-packages/since/{since}"
    return fetch_json(url)


def fetch_all_packages() -> dict[str, list[str]]:
    """Fetch the all-packages index (name -> versions mapping)."""
    url = f"{ELM_PACKAGE_SERVER}/all-packages"
    return fetch_json(url)


def fetch_package_endpoint(author: str, name: str, version: str) -> dict:
    """Fetch the endpoint.json for a package."""
    url = f"{ELM_PACKAGE_SERVER}/packages/{author}/{name}/{version}/endpoint.json"
    return fetch_json(url)


def fetch_package_elm_json(author: str, name: str, version: str) -> dict:
    """Fetch the elm.json for a package."""
    url = f"{ELM_PACKAGE_SERVER}/packages/{author}/{name}/{version}/elm.json"
    return fetch_json(url)


def download_package_zip(zip_url: str, dest_path: Path) -> None:
    """Download a package zip file to the destination path."""
    data = fetch_url(zip_url, timeout=120)

    # Write to temp file first, then rename for atomicity
    temp_path = dest_path.with_suffix(".zip.tmp")
    with open(temp_path, "wb") as f:
        f.write(data)
    temp_path.replace(dest_path)


def compute_sha1(file_path: Path) -> str:
    """Compute the SHA-1 hash of a file."""
    sha1 = hashlib.sha1()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
    return sha1.hexdigest()


def sync_package(
    mirror_dir: Path,
    package_id: str,
    registry: dict
) -> bool:
    """
    Sync a single package. Returns True on success, False on failure.
    Updates the registry with the new status.
    """
    author, name, version = parse_package_id(package_id)
    package_dir = mirror_dir / "packages" / author / name / version

    try:
        # Create package directory
        package_dir.mkdir(parents=True, exist_ok=True)

        # Fetch endpoint.json to get zip URL and hash
        endpoint = fetch_package_endpoint(author, name, version)
        zip_url = endpoint["url"]
        expected_hash = endpoint["hash"]

        # Save hash.json
        hash_path = package_dir / "hash.json"
        with open(hash_path, "w", encoding="utf-8") as f:
            json.dump({"hash": expected_hash}, f)

        # Fetch and save elm.json
        elm_json = fetch_package_elm_json(author, name, version)
        elm_json_path = package_dir / "elm.json"
        with open(elm_json_path, "w", encoding="utf-8") as f:
            json.dump(elm_json, f, indent=4)

        # Download package zip
        zip_path = package_dir / "package.zip"
        download_package_zip(zip_url, zip_path)

        # Verify hash
        actual_hash = compute_sha1(zip_path)
        if actual_hash != expected_hash:
            print(f"  Hash mismatch for {package_id}: expected {expected_hash}, got {actual_hash}")
            set_package_status(registry, package_id, STATUS_FAILED)
            return False

        set_package_status(registry, package_id, STATUS_SUCCESS)
        return True

    except Exception as e:
        print(f"  Error syncing {package_id}: {e}")
        set_package_status(registry, package_id, STATUS_FAILED)
        return False


def run_sync(mirror_dir: Path, package_list: set[str] | None) -> None:
    """Run a full sync operation."""
    print("Starting sync...")

    # Ensure mirror directory exists
    mirror_dir.mkdir(parents=True, exist_ok=True)

    # Load existing registry
    registry = load_registry(mirror_dir)
    existing_ids = {pkg["id"] for pkg in registry["packages"]}

    # Fetch current package list from Elm server
    print("Fetching package list from Elm package server...")
    remote_packages = fetch_all_packages_since(0)
    print(f"Found {len(remote_packages)} packages on remote server")

    # Fetch and save all-packages index
    print("Fetching all-packages index...")
    all_packages = fetch_all_packages()
    all_packages_path = mirror_dir / "all-packages"
    with open(all_packages_path, "w", encoding="utf-8") as f:
        json.dump(all_packages, f)

    # Determine which packages need to be synced
    # New packages are at the beginning of remote_packages (newest first)
    new_packages = []
    for pkg_id in remote_packages:
        if pkg_id not in existing_ids:
            # Add to registry as pending
            registry["packages"].insert(0, {"id": pkg_id, "status": STATUS_PENDING})
            existing_ids.add(pkg_id)
            new_packages.append(pkg_id)

    print(f"Found {len(new_packages)} new packages to sync")

    # Also find packages that previously failed and should be retried
    failed_packages = [
        pkg["id"] for pkg in registry["packages"]
        if pkg["status"] == STATUS_FAILED
    ]
    print(f"Found {len(failed_packages)} previously failed packages to retry")

    # Combine new and failed packages for syncing
    packages_to_sync = new_packages + failed_packages

    # Filter by package list if provided
    if package_list is not None:
        filtered = []
        for pkg_id in packages_to_sync:
            if should_sync_package(pkg_id, package_list):
                filtered.append(pkg_id)
            else:
                # Mark as ignored if not in package list
                set_package_status(registry, pkg_id, STATUS_IGNORED)
        packages_to_sync = filtered
        print(f"After filtering by package list: {len(packages_to_sync)} packages to sync")

    # Sync packages
    success_count = 0
    fail_count = 0

    for i, pkg_id in enumerate(packages_to_sync, 1):
        print(f"[{i}/{len(packages_to_sync)}] Syncing {pkg_id}...")

        if sync_package(mirror_dir, pkg_id, registry):
            success_count += 1
        else:
            fail_count += 1

        # Save registry periodically (every 10 packages)
        if i % 10 == 0:
            save_registry(mirror_dir, registry)

    # Final save
    save_registry(mirror_dir, registry)

    print(f"\nSync complete: {success_count} succeeded, {fail_count} failed")


# =============================================================================
# WSGI Server
# =============================================================================


class ElmMirrorApp:
    """WSGI application for serving the Elm mirror."""

    def __init__(self, mirror_dir: Path, base_url: str):
        self.mirror_dir = mirror_dir
        self.base_url = base_url.rstrip("/")
        self.registry = load_registry(mirror_dir)
        self.registry_lock = threading.Lock()

    def reload_registry(self) -> None:
        """Reload the registry from disk."""
        with self.registry_lock:
            self.registry = load_registry(self.mirror_dir)

    def __call__(self, environ: dict, start_response) -> list[bytes]:
        """WSGI entry point."""
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")

        if method != "GET":
            return self._error_response(start_response, 405, "Method Not Allowed")

        # Route requests
        if path == "/all-packages":
            return self._serve_all_packages(start_response)
        elif path.startswith("/all-packages/since/"):
            return self._serve_all_packages_since(path, start_response)
        elif path.startswith("/packages/") and path.endswith("/endpoint.json"):
            return self._serve_endpoint_json(path, start_response)
        elif path.startswith("/packages/"):
            return self._serve_static_file(path, start_response)
        else:
            return self._error_response(start_response, 404, "Not Found")

    def _serve_all_packages(self, start_response) -> list[bytes]:
        """Serve the all-packages index."""
        file_path = self.mirror_dir / "all-packages"
        if not file_path.exists():
            return self._error_response(start_response, 404, "all-packages not found")

        with open(file_path, "rb") as f:
            content = f.read()

        start_response("200 OK", [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(content)))
        ])
        return [content]

    def _serve_all_packages_since(self, path: str, start_response) -> list[bytes]:
        """Serve the all-packages/since/<N> endpoint dynamically."""
        # Parse N from path
        match = re.match(r"^/all-packages/since/(\d+)$", path)
        if not match:
            return self._error_response(start_response, 400, "Invalid since parameter")

        n = int(match.group(1))

        with self.registry_lock:
            packages = self.registry.get("packages", [])

        total = len(packages)

        # Return first (total - N) packages (newest first)
        if n >= total:
            result = []
        else:
            result = [pkg["id"] for pkg in packages[:total - n]]

        content = json.dumps(result).encode("utf-8")

        start_response("200 OK", [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(content)))
        ])
        return [content]

    def _serve_endpoint_json(self, path: str, start_response) -> list[bytes]:
        """Serve endpoint.json dynamically with absolute URL."""
        # Parse path: /packages/<author>/<name>/<version>/endpoint.json
        match = re.match(r"^/packages/([^/]+)/([^/]+)/([^/]+)/endpoint\.json$", path)
        if not match:
            return self._error_response(start_response, 400, "Invalid endpoint path")

        author, name, version = match.groups()
        package_id = f"{author}/{name}@{version}"

        # Check package status
        with self.registry_lock:
            status = get_package_status(self.registry, package_id)

        if status == STATUS_PENDING:
            return self._error_response(
                start_response, 503,
                f"Package {package_id} has not been downloaded yet"
            )
        elif status == STATUS_FAILED:
            return self._error_response(
                start_response, 503,
                f"Package {package_id} failed to download and is not available"
            )
        elif status == STATUS_IGNORED:
            return self._error_response(
                start_response, 503,
                f"Package {package_id} is not available on this mirror"
            )

        # Load hash from hash.json
        hash_path = self.mirror_dir / "packages" / author / name / version / "hash.json"
        if not hash_path.exists():
            return self._error_response(start_response, 404, "Package hash not found")

        with open(hash_path, "r", encoding="utf-8") as f:
            hash_data = json.load(f)

        # Generate endpoint.json with absolute URL
        endpoint = {
            "url": f"{self.base_url}/packages/{author}/{name}/{version}/package.zip",
            "hash": hash_data["hash"]
        }

        content = json.dumps(endpoint).encode("utf-8")

        start_response("200 OK", [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(content)))
        ])
        return [content]

    def _serve_static_file(self, path: str, start_response) -> list[bytes]:
        """Serve static files from the packages directory."""
        # Prevent directory traversal
        if ".." in path:
            return self._error_response(start_response, 400, "Invalid path")

        # Map URL path to file path
        file_path = self.mirror_dir / path.lstrip("/")

        if not file_path.exists() or not file_path.is_file():
            return self._error_response(start_response, 404, "File not found")

        # Check if this is a package.zip and verify status
        if path.endswith("/package.zip"):
            match = re.match(r"^/packages/([^/]+)/([^/]+)/([^/]+)/package\.zip$", path)
            if match:
                author, name, version = match.groups()
                package_id = f"{author}/{name}@{version}"

                with self.registry_lock:
                    status = get_package_status(self.registry, package_id)

                if status == STATUS_PENDING:
                    return self._error_response(
                        start_response, 503,
                        f"Package {package_id} has not been downloaded yet"
                    )
                elif status == STATUS_FAILED:
                    return self._error_response(
                        start_response, 503,
                        f"Package {package_id} failed to download"
                    )
                elif status == STATUS_IGNORED:
                    return self._error_response(
                        start_response, 503,
                        f"Package {package_id} is not available on this mirror"
                    )

        # Determine content type
        if path.endswith(".json"):
            content_type = "application/json"
        elif path.endswith(".zip"):
            content_type = "application/zip"
        else:
            content_type = "application/octet-stream"

        with open(file_path, "rb") as f:
            content = f.read()

        start_response("200 OK", [
            ("Content-Type", content_type),
            ("Content-Length", str(len(content)))
        ])
        return [content]

    def _error_response(self, start_response, status_code: int, message: str) -> list[bytes]:
        """Return an error response."""
        status_messages = {
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable"
        }
        status_text = status_messages.get(status_code, "Error")

        content = json.dumps({"error": message}).encode("utf-8")

        start_response(f"{status_code} {status_text}", [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(content)))
        ])
        return [content]


def run_background_sync(
    mirror_dir: Path,
    package_list: set[str] | None,
    interval: int,
    app: ElmMirrorApp
) -> None:
    """Run sync in a background thread at the specified interval."""
    def sync_loop():
        while True:
            time.sleep(interval)
            print(f"\n[Background sync] Starting sync...")
            try:
                run_sync(mirror_dir, package_list)
                app.reload_registry()
                print("[Background sync] Complete")
            except Exception as e:
                print(f"[Background sync] Error: {e}")

    thread = threading.Thread(target=sync_loop, daemon=True)
    thread.start()


def run_serve(
    mirror_dir: Path,
    base_url: str,
    host: str,
    port: int,
    sync_interval: int | None,
    package_list: set[str] | None
) -> None:
    """Run the WSGI server."""
    app = ElmMirrorApp(mirror_dir, base_url)

    # Start background sync if interval is specified
    if sync_interval is not None:
        print(f"Starting background sync every {sync_interval} seconds")
        run_background_sync(mirror_dir, package_list, sync_interval, app)

    # Check if running as CGI
    if "GATEWAY_INTERFACE" in os.environ:
        print("Running as CGI")
        CGIHandler().run(app)
    else:
        print(f"Starting server on {host}:{port}")
        print(f"Base URL: {base_url}")
        server = make_server(host, port, app)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")


# =============================================================================
# Verify Functionality
# =============================================================================


def run_verify(mirror_dir: Path) -> bool:
    """
    Verify the integrity of the mirror.
    Returns True if all checks pass, False otherwise.
    """
    print("Verifying mirror integrity...")

    registry = load_registry(mirror_dir)

    if not registry["packages"]:
        print("No packages in registry")
        return True

    success_packages = [
        pkg for pkg in registry["packages"]
        if pkg["status"] == STATUS_SUCCESS
    ]

    print(f"Checking {len(success_packages)} packages with status 'success'")

    errors = []

    for pkg in success_packages:
        package_id = pkg["id"]
        author, name, version = parse_package_id(package_id)
        package_dir = mirror_dir / "packages" / author / name / version

        # Check package.zip exists
        zip_path = package_dir / "package.zip"
        if not zip_path.exists():
            errors.append(f"{package_id}: package.zip missing")
            continue

        # Check hash.json exists
        hash_path = package_dir / "hash.json"
        if not hash_path.exists():
            errors.append(f"{package_id}: hash.json missing")
            continue

        # Load expected hash
        try:
            with open(hash_path, "r", encoding="utf-8") as f:
                hash_data = json.load(f)
            expected_hash = hash_data["hash"]
        except (json.JSONDecodeError, KeyError) as e:
            errors.append(f"{package_id}: invalid hash.json: {e}")
            continue

        # Verify hash
        actual_hash = compute_sha1(zip_path)
        if actual_hash != expected_hash:
            errors.append(
                f"{package_id}: hash mismatch (expected {expected_hash}, got {actual_hash})"
            )
            continue

        # Check elm.json exists
        elm_json_path = package_dir / "elm.json"
        if not elm_json_path.exists():
            errors.append(f"{package_id}: elm.json missing")
            continue

    if errors:
        print(f"\nFound {len(errors)} errors:")
        for error in errors:
            print(f"  - {error}")
        return False
    else:
        print("All checks passed!")
        return True


# =============================================================================
# CLI Argument Parsing
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Elm Mirror Server - Create and serve a read-only mirror of the Elm package server"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Sync packages from the Elm package server")
    sync_parser.add_argument(
        "--mirror-content",
        type=str,
        default=".",
        help="Directory to store mirror content (default: current directory)"
    )
    sync_parser.add_argument(
        "--package-list",
        type=str,
        default=None,
        help="JSON file containing list of packages to sync (default: sync all)"
    )

    # serve command
    serve_parser = subparsers.add_parser("serve", help="Serve the mirror via HTTP")
    serve_parser.add_argument(
        "--mirror-content",
        type=str,
        default=".",
        help="Directory containing mirror content (default: current directory)"
    )
    serve_parser.add_argument(
        "--base-url",
        type=str,
        required=True,
        help="Base URL for generated links (e.g., https://elm-mirror.example.com)"
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})"
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"Host to bind to (default: {DEFAULT_HOST})"
    )
    serve_parser.add_argument(
        "--sync-interval",
        type=int,
        default=None,
        help="Interval in seconds for background sync (default: no background sync)"
    )
    serve_parser.add_argument(
        "--package-list",
        type=str,
        default=None,
        help="JSON file containing list of packages to sync (for background sync)"
    )

    # verify command
    verify_parser = subparsers.add_parser("verify", help="Verify mirror integrity")
    verify_parser.add_argument(
        "--mirror-content",
        type=str,
        default=".",
        help="Directory containing mirror content (default: current directory)"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    mirror_dir = Path(args.mirror_content).resolve()

    if args.command == "sync":
        package_list = load_package_list(args.package_list)
        run_sync(mirror_dir, package_list)

    elif args.command == "serve":
        package_list = load_package_list(args.package_list)
        run_serve(
            mirror_dir=mirror_dir,
            base_url=args.base_url,
            host=args.host,
            port=args.port,
            sync_interval=args.sync_interval,
            package_list=package_list
        )

    elif args.command == "verify":
        success = run_verify(mirror_dir)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
