#!/usr/bin/env python
"""Live Python HTTP backend server for Cognee RCA Dashboard.

Serves static website files and exposes REST endpoints wrapping the real
Cognee RCA database ingestion and query analysis.
"""

import asyncio
import json
import logging
import os
import pathlib
import sys
import tempfile
import traceback
import warnings
from http.server import SimpleHTTPRequestHandler, HTTPServer

# --- setup import paths to reach rca and tests modules ------------------------
dashboard_dir = pathlib.Path(__file__).resolve().parent
repo_dir      = dashboard_dir.parent
sys.path.insert(0, str(repo_dir))

# Quiet cognee down so logs are clean
warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)
for name in ("cognee", "LiteLLM", "httpx", "sqlalchemy"):
    logging.getLogger(name).setLevel(logging.ERROR)

from rca import env  # noqa: E402
from rca.baseline import build_baseline  # noqa: E402
from rca.diff_sync import sync_release  # noqa: E402
from rca.query import analyze  # noqa: E402
from tests.fixtures import incidents as I  # noqa: E402
from tests.fixtures.repo_builder import build_fixture_repo  # noqa: E402

# --- Global State -------------------------------------------------------------
GLOBAL_REPO = None
GLOBAL_TEMP_DIR = None
INITIALIZED = False
INIT_STATS = {}


async def initialize_cognee_graph():
    """Build a synthetic git repo, baseline it, and sync releases in Cognee."""
    global GLOBAL_REPO, GLOBAL_TEMP_DIR, INITIALIZED, INIT_STATS

    print("[backend] Initializing Cognee Graph environment...")
    await env.init()
    await env.reset()
    await env.init()

    # Create temporary directory for fixture repo
    GLOBAL_TEMP_DIR = tempfile.TemporaryDirectory(prefix="rca_backend_")
    tmp_path = pathlib.Path(GLOBAL_TEMP_DIR.name)

    print(f"[backend] Building synthetic fixture payment-app repository under {tmp_path}...")
    GLOBAL_REPO = build_fixture_repo(tmp_path)

    print("[backend] Parsing AST and building baseline code graph in Cognee...")
    stats = await build_baseline(str(GLOBAL_REPO.repo_path))
    INIT_STATS = {
        "files": stats.files,
        "functions": stats.functions,
        "classes": stats.classes,
        "imports": stats.imports,
        "edges": stats.edges,
    }
    print(
        f"[backend] Baseline constructed: {stats.files} files · {stats.functions} functions · {stats.edges} edges"
    )

    # Sync releases chronologically
    for rel in GLOBAL_REPO.releases:
        print(f"[backend] Synchronizing release {rel.tag} diffs into Cognee...")
        await sync_release(
            str(GLOBAL_REPO.repo_path),
            rel.prev_sha,
            rel.commit_sha,
            rel.tag,
            rel.timestamp,
        )

    INITIALIZED = True
    print("[backend] Cognee RCA Graph initialization complete! Ready for query diagnostics.")


# --- Request Handler ----------------------------------------------------------


class RCABackendHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        # Serve static files from the dashboard folder where this script lives
        super().__init__(*args, directory=str(dashboard_dir), **kwargs)

    def end_headers(self):
        # Add CORS and disable caching during development
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            self.handle_api_get()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self.handle_api_post()
        else:
            self.send_error(404, "Endpoint not found")

    def handle_api_get(self):
        if self.path == "/api/status":
            res = {
                "initialized": INITIALIZED,
                "stats": INIT_STATS,
                "repo_path": str(GLOBAL_REPO.repo_path) if GLOBAL_REPO else None,
            }
            self.send_json_response(200, res)

        elif self.path == "/api/incidents":
            # Return all fixtures incidents dynamically
            res_list = []
            for inc in I.ALL_INCIDENTS:
                res_list.append(
                    {
                        "incident_id": inc.incident_id,
                        "description": inc.description,
                        "stack_trace": inc.stack_trace,
                        "language": inc.language,
                        "tags": inc.tags,
                        "root_cause_qualified_name": inc.root_cause_qualified_name,
                        "root_cause_file_path": inc.root_cause_file_path,
                        "root_cause_release_tag": inc.root_cause_release_tag,
                        "occurred_after_tag": inc.occurred_after_tag,
                        "occurred_before_tag": inc.occurred_before_tag,
                    }
                )
            self.send_json_response(200, res_list)

        elif self.path == "/api/releases":
            # Return releases information with file diffs
            if not GLOBAL_REPO:
                self.send_json_response(400, {"error": "Repository not initialized"})
                return

            res_list = []
            for rel in GLOBAL_REPO.releases:
                # Compile changes list
                changes = []
                for ch in rel.changed_symbols:
                    changes.append(
                        {
                            "symbol": ch.qualified_name,
                            "file": ch.file_path,
                            "type": ch.change_type,
                        }
                    )

                # Fetch diffs
                diffs = {}
                for ch in rel.changed_symbols:
                    if ch.file_path not in diffs:
                        # Query git diff for this file in this release
                        try:
                            import subprocess

                            out = subprocess.run(
                                [
                                    "git",
                                    "diff",
                                    rel.prev_sha,
                                    rel.commit_sha,
                                    "--",
                                    ch.file_path,
                                ],
                                cwd=GLOBAL_REPO.repo_path,
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            diffs[ch.file_path] = out.stdout.strip()
                        except Exception as e:
                            diffs[ch.file_path] = f"// Error reading diff: {e}"

                res_list.append(
                    {
                        "tag": rel.tag,
                        "sha": rel.commit_sha[:8],
                        "date": rel.timestamp,
                        "msg": f"Release commit for {rel.tag}",
                        "changes": changes,
                        "diffs": diffs,
                    }
                )
            self.send_json_response(200, res_list)

        else:
            self.send_error(404, f"API endpoint {self.path} not found")

    def handle_api_post(self):
        global INITIALIZED, INIT_STATS, GLOBAL_REPO, GLOBAL_TEMP_DIR
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self.send_json_response(400, {"error": "Invalid JSON body"})
            return

        if self.path == "/api/init":
            # Run re-initialization
            try:
                asyncio.run(initialize_cognee_graph())
                self.send_json_response(
                    200,
                    {
                        "status": "initialized",
                        "stats": INIT_STATS,
                        "repo_path": str(GLOBAL_REPO.repo_path),
                    },
                )
            except Exception as e:
                tb = traceback.format_exc()
                self.send_json_response(500, {"error": str(e), "traceback": tb})

        elif self.path == "/api/reset":
            # Purge database and reset backend state
            try:
                
                print("[backend] Purging Cognee database storage...")
                asyncio.run(env.reset())
                
                INITIALIZED = False
                INIT_STATS = {}
                
                if GLOBAL_TEMP_DIR:
                    try:
                        import shutil
                        shutil.rmtree(GLOBAL_TEMP_DIR.name, ignore_errors=True)
                    except Exception:
                        pass
                    GLOBAL_TEMP_DIR = None
                GLOBAL_REPO = None
                
                print("[backend] Database reset complete! Returned to clean slate.")
                self.send_json_response(200, {"status": "uninitialized"})
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[backend] Error resetting database: {e}\n{tb}")
                self.send_json_response(500, {"error": str(e), "traceback": tb})

        elif self.path == "/api/analyze":
            if not INITIALIZED:
                self.send_json_response(400, {"error": "Cognee graph is not initialized"})
                return

            incident_id = data.get("incident_id")
            k_hops = int(data.get("k_hops", 2))
            release_window = data.get("release_window")

            if release_window == "all" or not release_window:
                release_window = None
            elif isinstance(release_window, str):
                release_window = [release_window]

            # Find matching incident
            matching_inc = None
            for inc in I.ALL_INCIDENTS:
                if inc.incident_id == incident_id:
                    matching_inc = inc
                    break

            if not matching_inc:
                self.send_json_response(400, {"error": f"Incident {incident_id} not found"})
                return

            # Call real Cognee RCA Analyze query
            try:
                # Capture real log lines or run
                print(
                    f"[backend] Running live analyze() for traceback of {incident_id} (k_hops={k_hops}, window={release_window})..."
                )
                findings = asyncio.run(
                    analyze(
                        matching_inc.stack_trace,
                        k_hops=k_hops,
                        release_window=release_window,
                    )
                )

                # Format findings
                res_findings = []
                for idx, f in enumerate(findings):
                    res_findings.append(
                        {
                            "rank": idx + 1,
                            "name": f.qualified_name,
                            "file": f.file_path,
                            "release": f.release_tag,
                            "type": f.change_type,
                            "hops": f.hops,
                            "score": int(f.score),
                            "diff_hunk": f.diff_hunk,
                            "isTrue": f.qualified_name
                            == matching_inc.root_cause_qualified_name,
                        }
                    )

                self.send_json_response(
                    200,
                    {
                        "incident_id": incident_id,
                        "findings": res_findings,
                        "verdict": "Verdict: Culprit identified by Cognee graph." if res_findings else "Verdict: Clean. No matching modified release symbols found.",
                    },
                )
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[backend] Error in analyze(): {e}\n{tb}")
                self.send_json_response(500, {"error": str(e), "traceback": tb})
        else:
            self.send_error(404, f"API endpoint {self.path} not found")

    def send_json_response(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))


# --- Server Start -------------------------------------------------------------


def run_server():
    server_address = ("127.0.0.1", 8080)

    print(f"[backend] Starting API server on http://{server_address[0]}:{server_address[1]}...")
    httpd = HTTPServer(server_address, RCABackendHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[backend] Server shutting down.")
        httpd.server_close()
        if GLOBAL_TEMP_DIR:
            GLOBAL_TEMP_DIR.cleanup()


if __name__ == "__main__":
    run_server()
