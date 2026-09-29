"""FastAPI application factory: CDS Hooks endpoints + operational endpoints."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.adapters.base import PatientSource, SourceUnavailableError
from app.api.cards import alert_to_card
from app.api.schemas import CdsRequest, CompareRequest, FeedbackRequest
from app.audit.store import AuditStore
from app.config import Settings
from app.context import build_context
from app.logging_setup import configure_logging, pseudonymize, request_id_var
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
    if isinstance(value, dict) and value.get("resourceType") == "Bundle":
        return [e["resource"] for e in value.get("entry", []) if "resource" in e]
    return []


def create_app(
    settings: Settings | None = None,
    *,
    sources: dict[str, PatientSource] | None = None,
    engine: RulesEngine | None = None,
    audit: AuditStore | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = engine or RulesEngine.from_dir(settings.rules_dir)
    service = CdsService(engine, sources if sources is not None else build_sources(settings))
    audit = audit or AuditStore(settings.audit_db)
    metrics = Metrics()
    metrics.rules_loaded.set(len(engine.rules))
    limiter = RateLimiter(settings.rate_limit_per_minute)

    app = FastAPI(title="medsafe-cds", version="0.1.0", description=f"CDS Hooks prototype. {DISCLAIMER}")
    app.state.service, app.state.audit, app.state.metrics, app.state.settings = (
        service,
        audit,
        metrics,
        settings,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        started = time.perf_counter()
        path = request.url.path
        route_path = "/cds-services/{id}" if path.startswith("/cds-services/") and path.count("/") == 2 else path
        try:
            if path not in _NOT_RATE_LIMITED and request.method != "OPTIONS":
                client = request.client.host if request.client else "unknown"
                allowed, retry = limiter.allow(client)
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

    # ------------------------------------------------------------------ CDS Hooks
    @app.get("/cds-services")
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
        orders = [
            r for r in _bundle_entries(req.context.get("draftOrders")) if r.get("resourceType") == "MedicationRequest"
        ]
        return orders

    @app.post("/cds-services/{service_id}")
    def invoke(service_id: str, req: CdsRequest, response: Response) -> dict[str, Any]:
        if service_id not in SERVICES:
            raise HTTPException(status_code=404, detail="unknown CDS service")
        if req.hook != HOOK:
            raise HTTPException(status_code=400, detail=f"hook must be {HOOK!r}")
        patient_id = req.context.get("patientId")
        if not isinstance(patient_id, str) or not patient_id:
            raise HTTPException(status_code=422, detail="context.patientId is required")
        orders = _draft_orders(req)
        if not orders:
            raise HTTPException(status_code=422, detail="context.draftOrders must contain a MedicationRequest")
        mode = SERVICES[service_id][0]
        source = str((req.extension or {}).get("org.medsafe.source", "fhir"))
        rid = request_id_var.get()
        try:
            if source == "prefetch" or (
                req.prefetch and {"patient", "medications", "conditions", "observations"} <= set(req.prefetch)
            ):
                pf = req.prefetch or {}
                ctx = build_context(
                    pf["patient"],
                    _bundle_entries(pf["medications"]),
                    _bundle_entries(pf["conditions"]),
                    _bundle_entries(pf["observations"]),
                    source="prefetch",
                    as_of_policy=service.as_of_policy,
                )
                source = "prefetch"
            else:
                ctx = service.load_context(source, patient_id)
        except UnknownSourceError:
            raise HTTPException(status_code=422, detail=f"unknown data source {source!r}") from None
        except (SourceUnavailableError, KeyError, ValueError) as exc:
            # Fail open: an unavailable data source must never block ordering.
            logger.warning(
                "data source unavailable; returning no cards (fail-open)",
                extra={"source": source, "error": str(exc)},
            )
            metrics.source_errors.labels(source).inc()
            audit.record_source_error(request_id=rid, source=source, error=str(exc))
            response.headers["X-Medsafe-Degraded"] = "source-unavailable"
            return {"cards": []}

        pseudo = pseudonymize(patient_id, settings.pseudonym_salt)
        cards: list[dict[str, Any]] = []
        for draft in orders:
            evaluated = service.evaluate_ctx(ctx, draft, mode)
            for alert in evaluated.result.alerts:
                card = alert_to_card(alert, draft)
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
    @app.get("/api/sources")
    def list_sources() -> dict[str, Any]:
        return {name: src.status() for name, src in service.sources.items()}

    @app.get("/api/patients")
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

    @app.post("/api/compare")
    def compare(req: CompareRequest) -> dict[str, Any]:
        """Run both modes on the same data and show which alerts context suppressed (used by the UI)."""
        try:
            ctx = service.load_context(req.source, req.patientId)
            draft = draft_order(req.patientId, req.rxcui)
        except UnknownSourceError:
            raise HTTPException(status_code=404, detail="unknown source") from None
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown patient or drug") from None
        except SourceUnavailableError as exc:
            metrics.source_errors.labels(req.source).inc()
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        base = service.evaluate_ctx(ctx, draft, "baseline")
        cont = service.evaluate_ctx(ctx, draft, "context")
        return {
            "disclaimer": DISCLAIMER,
            "patient": {
                "id": ctx.patient_id,
                "source": ctx.source,
                "sex": ctx.sex,
                "as_of": ctx.as_of.isoformat(),
                "age": round(ctx.age_years() or 0),
                "meds": [m.name for m in ctx.meds],
                "unmapped_meds": ctx.unmapped_meds,
                "conditions": sorted(ctx.conditions),
                "egfr": _lab(ctx, "egfr"),
                "creatinine": _lab(ctx, "creatinine"),
                "potassium": _lab(ctx, "potassium"),
                "weight": _lab(ctx, "weight"),
            },
            "order": base.order.name,
            "baseline": [alert_to_card(a, draft) for a in base.result.alerts],
            "context": [alert_to_card(a, draft) for a in cont.result.alerts],
            "suppressed": [
                {"ruleId": s.rule_id, "ruleVersion": s.rule_version, "reasons": list(s.reason)}
                for s in cont.result.suppressed
            ],
        }

    # ------------------------------------------------------------------ audit (protected when a key is set)
    def require_key(x_api_key: str | None = Header(default=None)) -> None:
        if settings.audit_api_key and x_api_key != settings.audit_api_key:
            raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")

    @app.get("/api/audit", dependencies=[Depends(require_key)])
    def audit_events(kind: str | None = None, rule_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
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
        for name, src in service.sources.items():
            st = src.status()
            checks[name] = st
            # The service is "ready" if at least the rules load; sources degrade to fail-open.
        if not ok:
            response.status_code = 503
        return {"status": "ready" if ok else "not-ready", "checks": checks}

    @app.get("/metrics")
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
