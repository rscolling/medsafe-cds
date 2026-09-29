"""FastAPI application factory: CDS Hooks endpoints + operational endpoints."""

from __future__ import annotations

import hmac
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.adapters.base import PatientSource, SourceUnavailableError
from app.api.cards import alert_to_card, unchecked_card
from app.api.schemas import CdsRequest, CompareRequest, FeedbackRequest
from app.audit.store import AuditStore
from app.config import Settings
from app.context import build_context
from app.logging_setup import configure_logging, pseudonymize, redact_error, request_id_var
from app.metrics import Metrics
from app.ratelimit import RateLimiter
from app.registry import build_sources
from app.rules.engine import DISCLAIMER, RulesEngine
from app.service import CdsService, UnknownSourceError, draft_order, orderable_drugs

logger = logging.getLogger("medsafe.api")
SERVICES = {
    "medsafe-order-sign": ("context", "Context-aware medication safety checks (order-sign)"),
    "medsafe-order-sign-baseline": (
        "baseline",
        "Naive drug-class baseline (order-sign) - for comparison only",
    ),
}
HOOK = "order-sign"
_PREFETCH = {
    "patient": "Patient/{{context.patientId}}",
    "medications": "MedicationRequest?patient={{context.patientId}}&status=active",
    "conditions": "Condition?patient={{context.patientId}}",
    "observations": "Observation?patient={{context.patientId}}&category=laboratory",
}
_NOT_RATE_LIMITED = {"/health", "/ready", "/metrics"}


def _bundle_entries(value: Any) -> list[dict[str, Any]]:
    """Resources of a FHIR Bundle. Request payloads are untrusted: anything malformed is skipped, never raised."""
    if not isinstance(value, dict) or value.get("resourceType") != "Bundle":
        return []
    entries = value.get("entry")
    if not isinstance(entries, list):
        return []
    return [e["resource"] for e in entries if isinstance(e, dict) and isinstance(e.get("resource"), dict)]


class BodyLimitMiddleware:
    """Reject request bodies over ``max_bytes`` with 413 (checked on Content-Length and while streaming)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        too_big = JSONResponse({"detail": f"request body exceeds {self.max_bytes} bytes"}, status_code=413)
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await too_big(scope, receive, send)
            return
        # No usable Content-Length (chunked): buffer up to the limit, then replay the body to the app.
        chunks: list[Message] = []
        seen = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # client disconnected
                return
            chunks.append(message)
            seen += len(message.get("body", b""))
            if seen > self.max_bytes:
                await too_big(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        replay = iter(chunks)

        async def replayed() -> Message:
            return next(replay, {"type": "http.request", "body": b"", "more_body": False})

        await self.app(scope, replayed, send)


def create_app(
    settings: Settings | None = None,
    *,
    sources: dict[str, PatientSource] | None = None,
    engine: RulesEngine | None = None,
    audit: AuditStore | None = None,
    today: Callable[[], date] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = engine or RulesEngine.from_dir(settings.rules_dir)
    service = CdsService(engine, sources if sources is not None else build_sources(settings))
    if today is not None:
        service.today = today
    # M8: `auto` = wall clock for caller-supplied prefetch data (production semantics), record-anchored for the
    # bundled fixtures / VEHU (their dates are historical). An explicit `today` or `anchored` applies to everything.
    if settings.as_of_policy in ("today", "anchored"):
        service.as_of_policy = service.prefetch_policy = settings.as_of_policy
    else:
        service.prefetch_policy = "today"
    audit = audit or AuditStore(settings.audit_db)
    metrics = Metrics()
    metrics.rules_loaded.set(len(engine.rules))
    limiter = RateLimiter(settings.rate_limit_per_minute)
    ip_header = settings.client_ip_header.strip().lower()

    def _client_key(request: Request) -> str:
        """Rate-limit key. Behind a proxy every request shares the proxy's socket IP (one bucket for everyone), so an
        operator can name a *trusted* header (MEDSAFE_CLIENT_IP_HEADER). Its last entry is used: that is the one the
        trusted proxy appended, whereas earlier entries are client-controlled."""
        if ip_header:
            value = request.headers.get(ip_header, "")
            if value:
                return value.split(",")[-1].strip()[:64] or "unknown"
        return request.client.host if request.client else "unknown"

    settings.enforce_security(logger)  # production: raises InsecureConfigError; dev: loud warnings
    # Interactive docs are only served in keyless local dev; with an API key configured they are switched off
    # (a browser cannot send X-API-Key to /openapi.json). redirect_slashes off: both paths are served explicitly.
    docs_on = not settings.audit_api_key and settings.env != "production"

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        for name, src in service.sources.items():  # L7: release broker sockets / HTTP pools on shutdown
            close = getattr(src, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - shutdown must finish
                    logger.warning("closing source failed", extra={"source": name}, exc_info=True)

    app = FastAPI(
        lifespan=lifespan,
        title="medsafe-cds",
        version="0.1.0",
        description=f"CDS Hooks prototype. {DISCLAIMER}",
        docs_url="/docs" if docs_on else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_on else None,
        redirect_slashes=False,
    )
    app.state.service, app.state.audit, app.state.metrics, app.state.settings = (
        service,
        audit,
        metrics,
        settings,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Default handler echoes the submitted body ("input"), which can reflect patient data. Return locations only.
        detail = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()
        ]
        return JSONResponse({"detail": detail}, status_code=422)

    async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        path = request.url.path
        try:
            if path not in _NOT_RATE_LIMITED and request.method != "OPTIONS":
                allowed, retry = limiter.allow(_client_key(request))
                if not allowed:
                    metrics.rate_limited.inc()
                    response: Response = JSONResponse(
                        {"detail": "rate limit exceeded"},
                        status_code=429,
                        headers={"Retry-After": str(int(retry) + 1)},
                    )
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        except Exception:
            logger.exception("unhandled error", extra={"path": path})
            response = JSONResponse({"detail": "internal error"}, status_code=500)
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or "unmatched"  # templated path: bounded metric cardinality
        response.headers["X-Request-Id"] = rid
        metrics.http_requests.labels(request.method, route_path, str(response.status_code)).inc()
        metrics.http_latency.labels(route_path).observe(elapsed)
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": path,
                "status": response.status_code,
                "ms": round(elapsed * 1000, 1),
            },
        )
        request_id_var.reset(token)
        return response

    # Order matters (last added = outermost): body limit innermost, then observe (rate limit, metrics, request id), CORS
    # outermost so that 413 / 429 / 500 responses also carry CORS headers and the browser UI can read the error instead of a CORS failure.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(BaseHTTPMiddleware, dispatch=observe)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

    # ------------------------------------------------------------------ CDS Hooks
    @app.get("/cds-services")
    @app.get("/cds-services/", include_in_schema=False)
    def discovery() -> dict[str, Any]:
        return {
            "services": [
                {
                    "hook": HOOK,
                    "id": sid,
                    "title": desc.split(" (")[0],
                    "description": f"{desc}. {DISCLAIMER}",
                    "prefetch": _PREFETCH,
                    "usageRequirements": "Prototype on synthetic data. Pass extension['org.medsafe.source'] = 'fhir' | 'vista'.",
                }
                for sid, (_mode, desc) in SERVICES.items()
            ]
        }

    def _draft_orders(req: CdsRequest) -> list[dict[str, Any]]:
        return [
            r for r in _bundle_entries(req.context.get("draftOrders")) if r.get("resourceType") == "MedicationRequest"
        ]

    def _fail_open(
        response: Response, *, source: str, rid: str, exc: BaseException, ids: tuple[str, ...]
    ) -> dict[str, Any]:
        """Never block ordering: log (redacted), count, audit, and return no cards."""
        safe = redact_error(exc, *ids)
        logger.warning("evaluation failed; returning no cards (fail-open)", extra={"source": source, "error": safe})
        metrics.source_errors.labels(source).inc()
        audit.record_source_error(request_id=rid, source=source, error=safe)
        response.headers["X-Medsafe-Degraded"] = "source-unavailable"
        return {"cards": []}

    @app.post("/cds-services/{service_id}")
    @app.post("/cds-services/{service_id}/", include_in_schema=False)
    def invoke(service_id: str, req: CdsRequest, response: Response) -> dict[str, Any]:
        if service_id not in SERVICES:
            raise HTTPException(status_code=404, detail="unknown CDS service")
        if req.hook != HOOK:
            raise HTTPException(status_code=400, detail=f"hook must be {HOOK!r}")
        patient_id = req.context.get("patientId")
        if not isinstance(patient_id, str) or not patient_id or len(patient_id) > 128:
            raise HTTPException(status_code=422, detail="context.patientId is required (string, max 128 chars)")
        orders = _draft_orders(req)
        if not orders:
            raise HTTPException(status_code=422, detail="context.draftOrders must contain a MedicationRequest")
        if len(orders) > settings.max_draft_orders:
            raise HTTPException(
                status_code=422, detail=f"too many draft orders (max {settings.max_draft_orders} per request)"
            )
        mode = SERVICES[service_id][0]
        raw_source = (req.extension or {}).get("org.medsafe.source", "fhir")
        # Whitelist: `source` becomes a Prometheus label and an audit column, so it must never be caller-controlled text.
        if not isinstance(raw_source, str) or raw_source not in {*service.sources, "prefetch"}:
            raise HTTPException(status_code=422, detail="unknown data source")
        source = raw_source
        rid = request_id_var.get()
        ids = (patient_id,)
        try:
            pf = req.prefetch
            if source == "prefetch" or (pf and {"patient", "medications", "conditions", "observations"} <= set(pf)):
                pf = pf or {}
                ctx = build_context(
                    pf["patient"],
                    _bundle_entries(pf["medications"]),
                    _bundle_entries(pf["conditions"]),
                    _bundle_entries(pf["observations"]),
                    source="prefetch",
                    as_of_policy=service.prefetch_policy,
                    today=service.today(),
                )
                source = "prefetch"
            else:
                ctx = service.load_context(source, patient_id)
        except UnknownSourceError:
            raise HTTPException(status_code=422, detail="unknown data source") from None
        except (SourceUnavailableError, KeyError, ValueError, TypeError, AttributeError) as exc:
            return _fail_open(response, source=source, rid=rid, exc=exc, ids=ids)

        pseudo = pseudonymize(patient_id, settings.pseudonym_salt)
        cards: list[dict[str, Any]] = []
        try:
            for draft in orders:
                evaluated = service.evaluate_ctx(ctx, draft, mode)
                if evaluated.unchecked_reason:
                    metrics.alerts.labels("unchecked-order", mode, source, "data_gap").inc()
                    cards.append(unchecked_card(evaluated.unchecked_reason, mode))
                for alert in evaluated.result.alerts:
                    card = alert_to_card(alert, draft, as_of=ctx.as_of.isoformat(), as_of_policy=ctx.as_of_policy)
                    cards.append(card)
                    metrics.alerts.labels(alert.rule_id, mode, source, "data_gap" if alert.data_gap else "fire").inc()
                    audit.record_alert(
                        request_id=rid,
                        hook_instance=req.hookInstance,
                        patient_pseudo=pseudo,
                        source=source,
                        mode=mode,
                        rule_id=alert.rule_id,
                        rule_version=alert.rule_version,
                        card_uuid=card["uuid"],
                        data_gap=alert.data_gap,
                    )
                for s in evaluated.result.suppressed:
                    metrics.suppressed.labels(s.rule_id, source).inc()
        except Exception as exc:  # noqa: BLE001 - a rules/engine bug or bad order must fail open, not block ordering
            return _fail_open(response, source=source, rid=rid, exc=exc, ids=ids)
        logger.info(
            "hook evaluated",
            extra={"service": service_id, "source": source, "patient": pseudo, "cards": len(cards)},
        )
        return {"cards": cards}

    @app.post("/cds-services/{service_id}/feedback", status_code=200)
    def feedback(service_id: str, body: FeedbackRequest) -> dict[str, Any]:
        if service_id not in SERVICES:
            raise HTTPException(status_code=404, detail="unknown CDS service")
        rid = request_id_var.get()
        for item in body.feedback:
            reason = item.overrideReason
            code = reason.reason.code if reason and reason.reason else None
            audit.record_feedback(
                request_id=rid,
                card_uuid=item.card,
                outcome=item.outcome,
                override_code=code,
                comment=reason.userComment if reason else None,
            )
            metrics.overrides.labels(item.outcome).inc()
        return {}

    # ------------------------------------------------------------------ UI helper API
    # /api/sources, /api/patients and /api/compare disclose patient labels/clinical data and trigger source I/O, so they
    # require X-API-Key whenever one is configured (the bundled demo UI is for keyless localhost use).
    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        # Compare bytes: hmac.compare_digest raises TypeError (-> 500) on non-ASCII str.
        if settings.audit_api_key and not hmac.compare_digest(
            (x_api_key or "").encode("utf-8", "replace"), settings.audit_api_key.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/api/sources", dependencies=[Depends(require_key)])
    def list_sources() -> dict[str, Any]:
        return {name: src.status() for name, src in service.sources.items()}

    @app.get("/api/patients", dependencies=[Depends(require_key)])
    def patients(source: str) -> list[dict[str, str]]:
        try:
            src = service.source(source)
        except UnknownSourceError:
            raise HTTPException(status_code=404, detail="unknown source") from None
        return [{"id": p.id, "label": p.label, "kind": p.synthetic_kind, "note": p.note} for p in src.list_patients()]

    @app.get("/api/drugs")
    def drugs() -> list[dict[str, str]]:
        return orderable_drugs()

    @app.get("/api/rules")
    def rules() -> list[dict[str, str]]:
        return engine.catalog()

    @app.post("/api/compare", dependencies=[Depends(require_key)])
    def compare(req: CompareRequest) -> dict[str, Any]:
        """Run both modes on the same data and show which alerts context suppressed (used by the UI)."""
        try:
            ctx = service.load_context(req.source, req.patientId)
            draft = draft_order(req.patientId, req.rxcui)
        except UnknownSourceError:
            raise HTTPException(status_code=404, detail="unknown source") from None
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="unknown patient or drug") from None
        except SourceUnavailableError as exc:
            metrics.source_errors.labels(req.source).inc()
            logger.warning("compare: source unavailable", extra={"error": redact_error(exc, req.patientId)})
            raise HTTPException(status_code=503, detail="data source unavailable") from exc
        base = service.evaluate_ctx(ctx, draft, "baseline")
        cont = service.evaluate_ctx(ctx, draft, "context")
        return {
            "disclaimer": DISCLAIMER,
            "patient": {
                "id": ctx.patient_id,
                "source": ctx.source,
                "sex": ctx.sex,
                "as_of": ctx.as_of.isoformat(),
                "as_of_policy": ctx.as_of_policy,
                "age": round(ctx.age_years() or 0),
                "meds": [m.name for m in ctx.meds],
                "unmapped_meds": ctx.unmapped_meds,
                "excluded_meds": ctx.excluded_meds,
                "partial_meds": ctx.partial_meds,
                "conditions": sorted(ctx.conditions),
                "egfr": _lab(ctx, "egfr"),
                "creatinine": _lab(ctx, "creatinine"),
                "potassium": _lab(ctx, "potassium"),
                "weight": _lab(ctx, "weight"),
            },
            "order": base.order.name,
            "unchecked_reason": cont.unchecked_reason,
            "baseline": [alert_to_card(a, draft) for a in base.result.alerts],
            "context": [
                alert_to_card(a, draft, as_of=ctx.as_of.isoformat(), as_of_policy=ctx.as_of_policy)
                for a in cont.result.alerts
            ],
            "suppressed": [
                {"ruleId": s.rule_id, "ruleVersion": s.rule_version, "reasons": list(s.reason)}
                for s in cont.result.suppressed
            ],
        }

    # ------------------------------------------------------------------ audit (protected when a key is set)
    @app.get("/api/audit", dependencies=[Depends(require_key)])
    def audit_events(
        kind: str | None = None, rule_id: str | None = None, limit: int = Query(default=100, ge=1, le=1000)
    ) -> list[dict[str, Any]]:
        return audit.query(kind, rule_id, limit)

    @app.get("/api/audit/summary", dependencies=[Depends(require_key)])
    def audit_summary() -> dict[str, Any]:
        return audit.summary()

    # ------------------------------------------------------------------ ops
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready(response: Response) -> dict[str, Any]:
        checks: dict[str, Any] = {"rules": len(engine.rules), "audit_db": audit.ping()}
        ok = len(engine.rules) > 0
        degraded: list[str] = []
        for name, src in service.sources.items():
            st = src.status()  # cheap: cached / breaker state, never a blocking RPC under the connection lock
            checks[name] = st
            if st.get("reachable") is False:
                degraded.append(name)
        # Sources degrade to fail-open (no cards + X-Medsafe-Degraded), so the service stays "ready" while a source is
        # down, but the outage is reported here instead of being invisible.
        if not ok:
            response.status_code = 503
        out: dict[str, Any] = {"status": "ready" if ok else "not-ready", "checks": checks}
        if degraded:
            out["degraded"] = degraded
        return out

    @app.get("/metrics", dependencies=[Depends(require_key)])
    def prometheus() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app


def _lab(ctx: Any, key: str) -> dict[str, Any] | None:
    lab = ctx.latest_lab(key)
    return (
        None
        if lab is None
        else {
            "value": lab.value,
            "unit": lab.unit,
            "date": lab.when.isoformat(),
            "source": lab.source,
        }
    )


def app_factory() -> FastAPI:  # uvicorn --factory target
    return create_app()
