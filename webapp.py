from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from dashboard import build_dashboard_payload

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

app = FastAPI(title="Garmin Adaptive Triathlon Dashboard", version="4.0.0")
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "garmin-adaptive-dashboard"}


@app.get("/api/dashboard")
def dashboard() -> dict:
    return build_dashboard_payload()


if __name__ == "__main__":
    uvicorn.run(
        "webapp:app",
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8080")),
        reload=False,
        workers=1,
    )
