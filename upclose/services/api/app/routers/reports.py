"""JSON, no-JavaScript HTML and withheld evidence routes share the same assembler."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from app.adapters.storage import ObjectStorage
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.enums import ReportState
from app.contracts.reports import FreeReport, PaidReport, ReportStatus, ShareDisableResponse, ShareEnableResponse
from app.report import REPORT_HEADERS, render_report
from app.report.render import render_report_status
from app.routers.sessions import owner_bearer
from app.services.problems import not_found
from app.services.reports import ReportService
from app.services.snapshots import Provenance
from app.settings import Settings

_PROVENANCE_LABELS: dict[Provenance, str] = {
    "synthetic": "Synthetic fixture result — no real inference",
    "cached": "Cached result",
    "live": "Live inference result",
}


def create_report_router(factory: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage | None = None) -> APIRouter:
    router = APIRouter()
    service = ReportService(factory, settings, storage)

    @router.get("/api/v1/reports/{slug}", response_model=FreeReport | PaidReport | ReportStatus)
    def get_report(slug: str, response: Response,
                   authorization: Annotated[str | None, Header()] = None) -> FreeReport | PaidReport | ReportStatus:
        result = service.get(slug, authorization)
        response.headers.update({key: value for key, value in REPORT_HEADERS.items() if key != "Content-Type"})
        if result.provenance_label is not None:
            response.headers["X-UpClose-Provenance"] = result.provenance_label
        if result.dto.state == ReportState.BUILDING:
            response.status_code = 202
            response.headers["Retry-After"] = "5"
        return result.dto

    @router.get("/r/{slug}", response_class=HTMLResponse)
    def get_report_html(slug: str, authorization: Annotated[str | None, Header()] = None) -> HTMLResponse:
        result = service.get(slug, authorization)
        provenance_label = (None if result.provenance_label is None
                            else _PROVENANCE_LABELS[result.provenance_label])
        html = (render_report_status(result.dto) if isinstance(result.dto, ReportStatus)
                else render_report(result.dto, provenance_label=provenance_label,
                    evidence_url=lambda fid: f"/api/v1/reports/{slug}/frames/{fid}"))
        headers = dict(REPORT_HEADERS)
        if result.provenance_label is not None:
            headers["X-UpClose-Provenance"] = result.provenance_label
        status = 202 if result.dto.state == ReportState.BUILDING else 200
        if status == 202:
            headers["Retry-After"] = "5"
        return HTMLResponse(html, status_code=status, headers=headers)

    @router.get("/api/v1/reports/{slug}/frames/{finding_id}")
    def get_frame(slug: str, finding_id: UUID,
                  authorization: Annotated[str | None, Header()] = None) -> Response:
        url = service.frame_url(slug, finding_id, authorization)
        return RedirectResponse(url, status_code=302, headers={key: value for key, value in REPORT_HEADERS.items() if key != "Content-Type"})

    @router.post("/api/v1/reports/{slug}/share", response_model=ShareEnableResponse)
    def enable_share(slug: str, token: Annotated[str, Depends(owner_bearer)]) -> ShareEnableResponse:
        return service.enable_share(slug, token)

    @router.delete("/api/v1/reports/{slug}/share", response_model=ShareDisableResponse)
    def disable_share(slug: str, token: Annotated[str, Depends(owner_bearer)]) -> ShareDisableResponse:
        return service.disable_share(slug, token)

    return router
