"""FastAPI application factory for ``kb cockpit``."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import stage
from .service import CockpitService

STATIC_DIR = Path(__file__).parent / "static"


class StageJob(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    command: list[str] = Field(min_length=1, max_length=64)


def create_app(config_path: Path) -> FastAPI:
    service = CockpitService(config_path)
    app = FastAPI(
        title="KiCad Cockpit", docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.state.cockpit = service

    @app.middleware("http")
    async def mutation_guard(request: Request, call_next):
        if (request.url.path.startswith("/api/")
                and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and request.headers.get("X-Cockpit-Token") != service.mutation_token):
            return JSONResponse(status_code=403, content={"detail": "invalid mutation token"})
        return await call_next(request)

    def state(board: str):
        try:
            return service.state(board)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/product")
    def product():
        return service.product()

    @app.get("/api/boards")
    def boards():
        return {"items": service.boards()}

    @app.get("/api/boards/{board}/status")
    def board_status(board: str):
        try:
            return service.status(board)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/boards/{board}/audit")
    def audit_peek(board: str):
        return state(board).audit_peek()

    @app.post("/api/boards/{board}/audit")
    def audit_run(board: str, force: bool = True):
        return state(board).audit_state(force=force)

    @app.get("/api/boards/{board}/review")
    def review_peek(board: str):
        return state(board).review_peek()

    @app.post("/api/boards/{board}/review")
    def review_run(board: str, force: bool = True):
        return state(board).review_state(force=force)

    @app.get("/api/boards/{board}/changes")
    def changes(board: str):
        return state(board).changes()

    @app.get("/api/boards/{board}/parts")
    def parts(board: str):
        return state(board).parts()

    @app.get("/api/boards/{board}/bom")
    def bom(board: str):
        try:
            return state(board).bom()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/boards/{board}/bom.csv")
    def bom_csv(board: str):
        try:
            body = state(board).bom_csv()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return Response(body, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{board}-bom.csv"'
        })

    @app.get("/api/boards/{board}/preview/schematic.pdf")
    def schematic(board: str):
        data = state(board).sch_pdf()
        if not data:
            raise HTTPException(status_code=404, detail="schematic preview unavailable")
        return Response(data, media_type="application/pdf")

    @app.get("/api/boards/{board}/preview/pcb.svg")
    def pcb_svg(board: str, side: str = Query("top", pattern="^(top|bottom)$")):
        data = state(board).pcb_svg(side)
        if not data:
            raise HTTPException(status_code=404, detail="PCB preview unavailable")
        return Response(data, media_type="image/svg+xml")

    @app.get("/api/boards/{board}/preview/pcb-layer.svg")
    def pcb_layer(board: str, layer: str, color: str | None = None):
        if not re.fullmatch(r"[A-Za-z0-9._]+", layer):
            raise HTTPException(status_code=400, detail="invalid layer")
        color = color if color and re.fullmatch(r"[0-9A-Fa-f]{6}", color) else None
        data = state(board).pcb_layer_svg(layer, color)
        if not data:
            raise HTTPException(status_code=404, detail="layer preview unavailable")
        return Response(data, media_type="image/svg+xml")

    @app.get("/api/boards/{board}/preview/pcb3d")
    def pcb_3d_status(board: str, side: str = Query("top", pattern="^(top|bottom)$")):
        return state(board).pcb_3d_state(side)

    @app.get("/api/boards/{board}/preview/pcb3d.png")
    def pcb_3d(board: str, side: str = Query("top", pattern="^(top|bottom)$")):
        data = state(board).pcb_png(side)
        if not data:
            raise HTTPException(status_code=404, detail="3D render not ready")
        return Response(data, media_type="image/png")

    @app.get("/api/boards/{board}/datasheets")
    def datasheets(board: str):
        return service.datasheets(state(board))

    @app.get("/api/boards/{board}/datasheets/{index}")
    def datasheet_meta(board: str, index: int):
        got = state(board).datasheet_meta(index)
        if not got:
            raise HTTPException(status_code=404, detail="datasheet not found")
        return got[1]

    @app.get("/api/boards/{board}/datasheets/{index}/search")
    def datasheet_search(board: str, index: int, q: str):
        return {"pages": state(board).datasheet_search(index, q)}

    @app.get("/api/boards/{board}/datasheets/{index}/page.png")
    def datasheet_page(board: str, index: int, page: int = 1, dpi: int = 150):
        data = state(board).datasheet_png(index, page, min(max(dpi, 72), 220))
        if not data:
            raise HTTPException(status_code=404, detail="datasheet page unavailable")
        return Response(data, media_type="image/png")

    @app.get("/api/boards/{board}/datasheets/{index}/file.pdf")
    def datasheet_pdf(board: str, index: int):
        data = state(board).datasheet_pdf_bytes(index)
        if not data:
            raise HTTPException(status_code=404, detail="datasheet not found")
        return Response(data, media_type="application/pdf")

    @app.get("/api/boards/{board}/stage")
    def stage_get(board: str):
        return service.stage_status(state(board))

    @app.post("/api/boards/{board}/stage/jobs")
    def stage_add(board: str, job: StageJob):
        sp = stage.paths_from_config(state(board).cfg)
        path = stage.add_job(sp, job.label, job.command)
        return {"created": path.name, **service.stage_status(state(board))}

    @app.delete("/api/boards/{board}/stage/jobs")
    def stage_clear(board: str):
        sp = stage.paths_from_config(state(board).cfg)
        return {"cleared": stage.clear_queue(sp), **service.stage_status(state(board))}

    @app.post("/api/boards/{board}/stage/run")
    def stage_run(board: str):
        st = state(board)
        sp = stage.paths_from_config(st.cfg)
        results = stage.drain_queue(sp)
        return {"results": results, **service.stage_status(st)}

    @app.get("/api/boards/{board}/release")
    def release_get(board: str):
        state(board)
        return service.release_peek(board)

    @app.post("/api/boards/{board}/release/check")
    def release_check(board: str):
        return service.release_check(board)

    @app.get("/api/events")
    async def events(board: str):
        state(board)

        async def stream():
            last = None
            while True:
                payload = service.status(board)
                encoded = json.dumps(payload, separators=(",", ":"))
                if encoded != last:
                    yield f"event: status\ndata: {encoded}\n\n"
                    last = encoded
                await asyncio.sleep(2)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no"
        })

    if STATIC_DIR.is_dir():
        assets = STATIC_DIR / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found")
            return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
