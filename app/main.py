"""One process, two ports: the dashboard (default 1113) and the API (default 1311).

The API (/api/...) is reachable on both ports so the dashboard can call it same-origin.
The API port serves only the API and its docs; the dashboard port also serves the web UI.
"""
import asyncio
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import public, router

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "1113"))
API_PORT = int(os.environ.get("API_PORT", "1311"))
STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(
    title="Household Finance Tracker API",
    version="1.0.0",
    description="Salary, expenses, budgets, recurring charges and investments for a two-person household. "
                "All totals are in CHF; foreign-currency amounts (e.g. GBP) are stored with their CHF value. "
                "Agents: read GET /api/agent-guide first.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(public)
app.include_router(router)


@app.middleware("http")
async def api_port_is_api_only(request, call_next):
    server = request.scope.get("server")
    path = request.url.path
    if server and server[1] == API_PORT and not path.startswith(("/api", "/docs", "/redoc", "/openapi.json")):
        if path == "/":
            return RedirectResponse("/docs")
        return JSONResponse({"detail": f"Not found. API lives under /api, docs at /docs. "
                                       f"The dashboard is on port {DASHBOARD_PORT}."}, status_code=404)
    response = await call_next(request)
    if not path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=STATIC, html=True), name="dashboard")


def _listen(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((os.environ.get("HOST", "0.0.0.0"), port))
    return sock


def main():
    server = uvicorn.Server(uvicorn.Config(app, log_level=os.environ.get("LOG_LEVEL", "info")))
    print(f"Dashboard: http://localhost:{DASHBOARD_PORT}   API: http://localhost:{API_PORT}/api  "
          f"(docs: http://localhost:{API_PORT}/docs)", flush=True)
    asyncio.run(server.serve(sockets=[_listen(DASHBOARD_PORT), _listen(API_PORT)]))


if __name__ == "__main__":
    main()
