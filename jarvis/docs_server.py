"""Docs-only server for Anchor Browser.  Run:  python -m jarvis.docs_server

Serves ONLY the synthetic service-docs site (/site/...) on port 8001. This is the
only thing you expose through a tunnel; the dispatch app and its API (port 8000)
stay on localhost. No customer data lives here."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
docs = FastAPI(title="Brightline service docs (synthetic)", docs_url=None, redoc_url=None, openapi_url=None)
docs.mount("/site", StaticFiles(directory=ROOT / "site", html=True), name="site")


@docs.get("/")
def root():
    return RedirectResponse("/site/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(docs, host="127.0.0.1", port=8001, log_level="warning")
