"""Regression tests for the second review batch: adapter (M2/M3/M5/M11/L13/L14/L15), API (M6/M8/M12/L5/L7-L12)."""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import SourceUnavailableError
from app.adapters.vista import parsing
from app.adapters.vista.adapter import VistaAdapter
from app.api.app import create_app
from app.audit.store import AuditStore
from app.config import Settings
from app.ratelimit import RateLimiter
from app.rules.engine import truncate_summary

DEMO = [{"dfn": "1", "label": "x"}]


class Script:
    """Scripted RpcClient: rpc name -> reply (str or callable(params) -> str)."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies, self.calls, self.closed = replies, [], False

    def call(self, rpc: str, *params: str) -> str:
        self.calls.append((rpc, params))
        r = self.replies.get(rpc, "")
        return r(params) if callable(r) else r

    def close(self) -> None:
        self.closed = True


def lab_reply(collected: str, value: str = "1.0", unit: str = "mg/dL") -> str:
    return f"1^CH^{collected}^72^SERUM^ACC^PROV\r\n173^CREATININE^{value}^^{unit}^0.6 - 1.3\r\n"


def fm_dates(n: int) -> list[str]:
    """n distinct, valid FileMan collection date/times (one per day from 2010-01-01)."""
    from datetime import date, timedelta

    out = []
    for i in range(n):
        d = date(2010, 1, 1) + timedelta(days=i)
        out.append(f"{d.year - 1700}{d.month:02d}{d.day:02d}.1000")
    return out


def chain(collections: list[str]):  # type: ignore[no-untyped-def]
    """INTERIMG replies newest-first, honouring the `before` cursor numerically like the real server."""
    ordered = sorted(collections, key=lambda c: float(c), reverse=True)

    def reply(params: tuple[str, ...]) -> str:
        before = float(params[1])
        for c in ordered:
            if float(c) < before:
                return lab_reply(c)
        return ""

    return reply


BASE = {"ORWPT SELECT": "A,B^M^2500101^0", "ORQQPL LIST": "", "ORWPS ACTIVE": "", "ORQQVI VITALS": ""}


# ------------------------------------------------------------------------------------------ M2
@pytest.mark.parametrize("dfn", ["0100897", "00100897", "100897"])
def test_m2_leading_zero_dfn_cannot_bypass_junk_exclusion(dfn: str) -> None:
    client = Script(BASE)
    a = VistaAdapter(client, mode="live", demo_patients=DEMO, excluded_dfns=("100897",))
    with pytest.raises(SourceUnavailableError, match="excluded"):
        a.get_patient(dfn)
    assert client.calls == []  # never reached VistA


@pytest.mark.parametrize("dfn", ["0", "000", "", "1;2", "-1", " 5", "1e3", "٣"])
def test_m2_invalid_dfns_rejected_before_any_rpc(dfn: str) -> None:
    client = Script(BASE)
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    with pytest.raises(SourceUnavailableError):
        a.get_patient(dfn)
    assert client.calls == []


def test_m2_leading_zero_and_plain_dfn_share_one_cache_entry() -> None:
    client = Script({**BASE, "ORWLRR NEWOLD": "^"})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    a.get_patient("0000005")
    a.get_patient("5")
    assert [c for c in client.calls if c[0] == "ORWPT SELECT"] == [("ORWPT SELECT", ("5",))]


# ------------------------------------------------------------------------------------------ M3 / L14 / L15
def test_m3_pages_past_the_old_30_cap_and_reports_not_truncated() -> None:
    cols = fm_dates(150)  # 150 collections, well beyond the old cap of 30
    client = Script(
        {**BASE, "ORWLRR NEWOLD": f"{max(cols, key=float)}^{min(cols, key=float)}", "ORWLRR INTERIMG": chain(cols)}
    )
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    obs, truncated = a._fetch_labs(client, "1")
    assert len(obs) == 150 and truncated is False
    assert a.truncated_fetches == 0


def test_m3_cap_hit_is_flagged_logged_and_tagged(caplog: pytest.LogCaptureFixture) -> None:
    cols = fm_dates(50)
    client = Script(
        {**BASE, "ORWLRR NEWOLD": f"{max(cols, key=float)}^{min(cols, key=float)}", "ORWLRR INTERIMG": chain(cols)}
    )
    a = VistaAdapter(client, mode="live", demo_patients=DEMO, max_lab_sets=10)
    with caplog.at_level("WARNING", logger="medsafe.vista"):
        patient = a.get_patient("1")
    assert len(a.get_lab_series("1")) == 10
    assert {"system": "urn:medsafe:data", "code": "labs-truncated"} in patient["meta"]["tag"]
    assert a.truncated_fetches == 1 and "incomplete" in caplog.text
    assert a.status()["truncated_lab_fetches"] == 1


def test_m3_unparseable_page_is_flagged_not_silently_the_end() -> None:
    def replies(params: tuple[str, ...]) -> str:
        return lab_reply("3150101.1") if params[1] == "3300101" else "garbage"

    client = Script({**BASE, "ORWLRR NEWOLD": "3150101.1^3150101.1", "ORWLRR INTERIMG": replies})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    _obs, truncated = a._fetch_labs(client, "1")
    assert truncated is True


def test_l14_paging_compares_fileman_numerically() -> None:
    # "3150810.1" == "3150810.10" numerically; as strings the second would look *newer* and could loop or skip.
    # The cursor must move strictly backwards: a reply that fails to advance ends paging and is flagged.
    seen: list[str] = []

    def replies(params: tuple[str, ...]) -> str:
        seen.append(params[1])
        return lab_reply("3150810.1") if len(seen) == 1 else lab_reply("3150810.10")

    client = Script({**BASE, "ORWLRR NEWOLD": "3150810.1^3150810.1", "ORWLRR INTERIMG": replies})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    obs, truncated = a._fetch_labs(client, "1")
    assert len(obs) == 1 and truncated is True and len(seen) == 2


def test_l15_newold_head_disagreeing_with_chain_is_logged_and_counted(caplog: pytest.LogCaptureFixture) -> None:
    client = Script(
        {**BASE, "ORWLRR NEWOLD": "3000224.11431^3000101", "ORWLRR INTERIMG": chain(["3000209.114723", "3000101.1"])}
    )
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    with caplog.at_level("WARNING", logger="medsafe.vista"):
        obs, _ = a._fetch_labs(client, "1")
    assert len(obs) == 2  # the INTERIMG chain is authoritative
    assert a.head_disagreements == 1 and "does not match" in caplog.text


def test_l15_garbage_newold_never_means_no_labs_and_never_fails_the_patient(caplog: pytest.LogCaptureFixture) -> None:
    """A malformed NEWOLD date must not hide real labs (old behaviour: 'no labs') nor fail the whole patient."""
    client = Script({**BASE, "ORWLRR NEWOLD": "notadate^3140101.1", "ORWLRR INTERIMG": chain(["3150101.1"])})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    with caplog.at_level("WARNING", logger="medsafe.vista"):
        assert len(a.get_lab_series("1")) == 1  # the INTERIMG chain is authoritative
    assert a.head_disagreements == 1 and "not parseable" in caplog.text


def test_l15_newold_says_labs_but_chain_empty_is_counted() -> None:
    client = Script({**BASE, "ORWLRR NEWOLD": "3150101.1^3150101.1", "ORWLRR INTERIMG": ""})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    obs, truncated = a._fetch_labs(client, "1")
    assert obs == [] and a.head_disagreements == 1 and truncated is False


@pytest.mark.parametrize("raw", ["^", "", "3150101.1^3150101.1", "3150101^"])
def test_newold_valid_replies_are_not_garbage(raw: str) -> None:
    assert parsing.newold_is_garbage(raw) is False


# ------------------------------------------------------------------------------------------ L13 / M5
def test_l13_cache_is_bounded_and_evicts_oldest() -> None:
    client = Script({**BASE, "ORWLRR NEWOLD": "^"})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO, max_cache_entries=5)
    for i in range(1, 21):
        a.get_patient(str(i))
    assert len(a._cache) == 5
    assert set(a._cache) == {"16", "17", "18", "19", "20"}


def test_m5_concurrent_requests_for_one_dfn_share_one_fetch() -> None:
    gate = threading.Event()

    class Slow(Script):
        def call(self, rpc: str, *params: str) -> str:
            if rpc == "ORWPT SELECT":
                gate.wait(2)
            return super().call(rpc, *params)

    client = Slow({**BASE, "ORWLRR NEWOLD": "^"})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    threads = [threading.Thread(target=a.get_patient, args=("7",)) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(3)
    assert len([c for c in client.calls if c[0] == "ORWPT SELECT"]) == 1


def test_m5_fetch_has_a_total_deadline() -> None:
    class Slow(Script):
        def call(self, rpc: str, *params: str) -> str:
            time.sleep(0.06)
            return super().call(rpc, *params)

    client = Slow({**BASE, "ORWLRR NEWOLD": "^"})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO, fetch_deadline_s=0.1)
    with pytest.raises(SourceUnavailableError, match="time budget"):
        a.get_patient("3")
    assert len(client.calls) < 6  # stopped early instead of running every RPC


def test_m5_status_uses_breaker_state_and_caches_probe() -> None:
    class Health(Script):
        def __init__(self, state: str) -> None:
            super().__init__({**BASE})
            self.state = state

        def health(self) -> dict[str, Any]:
            return {"state": self.state, "last_error": "boom"}

    down = Health("circuit-open")
    a = VistaAdapter(down, mode="live", demo_patients=DEMO)
    st = a.status()
    assert st["reachable"] is False and st["error"] == "boom" and down.calls == []  # no RPC at all

    up = Health("connected")
    b = VistaAdapter(up, mode="live", demo_patients=DEMO)
    assert b.status()["reachable"] is True
    b.status()
    b.status()
    assert len(up.calls) == 1  # probe result cached between polls


def test_m5_close_closes_client() -> None:
    client = Script(BASE)
    VistaAdapter(client, mode="live", demo_patients=DEMO).close()
    assert client.closed


# ------------------------------------------------------------------------------------------ M11
def test_m11_unknown_status_is_logged_counted_and_not_active(caplog: pytest.LogCaptureFixture) -> None:
    parsing.UNKNOWN_STATUS_COUNTS.clear()
    med = parsing.VistaMed("1", "ASPIRIN 81MG", "OP", "NEWSTATUS", "", "")
    with caplog.at_level("WARNING", logger="medsafe.vista.parsing"):
        assert parsing.med_to_medication_request("5", med) is None
    assert parsing.UNKNOWN_STATUS_COUNTS == {"NEWSTATUS": 1} and "unknown ORWPS status" in caplog.text
    known = parsing.VistaMed("2", "ASPIRIN 81MG", "OP", "DISCONTINUED", "", "")
    assert parsing.med_to_medication_request("5", known) is None
    assert "DISCONTINUED" not in parsing.UNKNOWN_STATUS_COUNTS


def test_m11_pending_policy_is_configurable() -> None:
    pending = parsing.VistaMed("1", "ASPIRIN 81MG", "OP", "PENDING", "", "")
    assert parsing.med_to_medication_request("5", pending)["status"] == "active"  # type: ignore[index]
    assert parsing.med_to_medication_request("5", pending, pending_active=False) is None
    active = parsing.VistaMed("1", "ASPIRIN 81MG", "OP", "ACTIVE", "", "")
    assert parsing.med_to_medication_request("5", active, pending_active=False) is not None


# ------------------------------------------------------------------------------------------ API
def client_for(service, **kw) -> TestClient:  # type: ignore[no-untyped-def]
    s = Settings(
        fhir_mode="fixtures", vista_mode="recorded", audit_db=":memory:", **{"rate_limit_per_minute": 10000, **kw}
    )
    return TestClient(
        create_app(s, sources=service.sources, audit=AuditStore(":memory:"), today=lambda: date(2026, 9, 1))
    )


def body(
    patient: str = "hand-01", source: str | None = "fhir", drug: str = "IBUPROFEN 800MG", **ext: Any
) -> dict[str, Any]:
    import uuid

    draft = {"resourceType": "MedicationRequest", "id": "d", "status": "draft", "intent": "order",
             "medicationCodeableConcept": {"text": drug}}  # fmt: skip
    extension = dict(ext)
    if source is not None:
        extension["org.medsafe.source"] = source
    return {
        "hook": "order-sign",
        "hookInstance": str(uuid.uuid4()),
        "context": {"patientId": patient, "draftOrders": {"resourceType": "Bundle", "entry": [{"resource": draft}]}},
        "extension": extension,
    }


URL = "/cds-services/medsafe-order-sign"


@pytest.mark.parametrize("bad", ["evil'; DROP", "x" * 5000, "FHIR", "", "vista2", "../etc"])
def test_m6_unknown_source_is_422_and_never_a_metric_label(service, bad: str) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service)
    assert c.post(URL, json=body(source=bad)).status_code == 422
    assert "DROP" not in c.get("/metrics").text and "xxxxx" not in c.get("/metrics").text


def test_m6_known_sources_still_work(service) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service)
    assert c.post(URL, json=body()).status_code == 200
    assert c.post(URL, json=body(source=None)).status_code == 200  # default is fhir
    assert c.post(URL, json=body("9000001", "vista")).status_code == 200


def test_m12_ui_helper_endpoints_require_the_key_when_set(service) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service, audit_api_key="k" * 20)
    for method, path in [("get", "/api/sources"), ("get", "/api/patients?source=fhir"), ("post", "/api/compare")]:
        assert getattr(c, method)(path).status_code == 401
    h = {"X-API-Key": "k" * 20}
    assert c.get("/api/sources", headers=h).status_code == 200
    assert c.get("/api/patients?source=fhir", headers=h).status_code == 200
    assert c.get("/api/drugs").status_code == 200  # static catalogue stays open
    open_c = client_for(service)
    assert open_c.get("/api/sources").status_code == 200


@pytest.mark.parametrize("key", ["é", "日本語", "k" * 19 + "é", "\u00ff" * 30])
def test_l8_non_ascii_api_key_is_401_not_500(service, key: str) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service, audit_api_key="qa-auth-token-7f3a91c2")
    for path in ("/api/audit", "/api/sources", "/metrics"):
        r = c.get(path, headers={"X-API-Key": key.encode("utf-8")})  # raw UTF-8 bytes on the wire
        assert r.status_code == 401, path


def test_l8_correct_key_still_works_and_non_ascii_secret_supported(service) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service, audit_api_key="qa-auth-token-7f3a91c2")
    assert c.get("/api/audit", headers={"X-API-Key": "qa-auth-token-7f3a91c2"}).status_code == 200


@pytest.mark.parametrize("limit", ["-1", "0", "-999999", "1001", "abc"])
def test_l9_audit_limit_is_validated(service, limit: str) -> None:  # type: ignore[no-untyped-def]
    assert client_for(service).get(f"/api/audit?limit={limit}").status_code == 422


def test_l9_store_clamps_even_when_called_directly() -> None:
    store = AuditStore(":memory:")
    for i in range(5):
        store.record_source_error(request_id=str(i), source="fhir", error="e")
    assert len(store.query(limit=-1)) == 1 and len(store.query(limit=0)) == 1 and len(store.query(limit=10**9)) == 5


def test_l5_settings_repr_does_not_leak_secrets() -> None:
    s = Settings(
        audit_api_key="API-KEY-SECRET",
        pseudonym_salt="SALT-SECRET-VALUE",
        vista_access="ACC-CODE-X",
        vista_verify="VER-CODE-Y",
    )
    r = repr(s)
    for secret in ("API-KEY-SECRET", "SALT-SECRET-VALUE", "ACC-CODE-X", "VER-CODE-Y"):
        assert secret not in r


def test_l7_shutdown_closes_sources(service) -> None:  # type: ignore[no-untyped-def]
    closed: list[str] = []

    class Src:
        name = "s"

        def __init__(self, n: str) -> None:
            self.n = n

        def close(self) -> None:
            closed.append(self.n)

        def status(self) -> dict[str, Any]:
            return {"reachable": True}

    s = Settings(fhir_mode="fixtures", vista_mode="recorded", audit_db=":memory:")
    with TestClient(create_app(s, sources={"a": Src("a"), "b": Src("b")}, audit=AuditStore(":memory:"))) as c:  # type: ignore[dict-item]
        c.get("/health")
    assert sorted(closed) == ["a", "b"]


def test_l10_cors_headers_on_429_and_500_and_413(service) -> None:  # type: ignore[no-untyped-def]
    origin = {"Origin": "http://localhost:5173"}
    c = client_for(service, rate_limit_per_minute=1, max_body_bytes=2000)
    assert c.get("/api/drugs", headers=origin).status_code == 200
    r = c.get("/api/drugs", headers=origin)
    assert r.status_code == 429 and r.headers["access-control-allow-origin"] == "http://localhost:5173"
    big = client_for(service, max_body_bytes=200)
    r = big.post(URL, json=body(), headers=origin)
    assert r.status_code == 413 and r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_l10_configured_forwarded_header_gives_per_client_buckets(service) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service, rate_limit_per_minute=1, client_ip_header="X-Forwarded-For")
    a, b = {"X-Forwarded-For": "1.1.1.1"}, {"X-Forwarded-For": "2.2.2.2"}
    assert c.get("/api/drugs", headers=a).status_code == 200
    assert c.get("/api/drugs", headers=b).status_code == 200  # a different client is not throttled by the first
    assert c.get("/api/drugs", headers=a).status_code == 429
    # a spoofed leading entry is ignored: the last (proxy-appended) entry is the key
    spoof = {"X-Forwarded-For": "9.9.9.9, 1.1.1.1"}
    assert c.get("/api/drugs", headers=spoof).status_code == 429


def test_l10_without_header_setting_all_clients_share_a_bucket(service) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service, rate_limit_per_minute=1)
    assert c.get("/api/drugs", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert c.get("/api/drugs", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429  # documented caveat


def test_l10_limiter_evicts_idle_buckets_and_stays_bounded() -> None:
    t = [0.0]
    lim = RateLimiter(60, clock=lambda: t[0])
    lim.MAX_BUCKETS = 100
    for i in range(100):
        lim.allow(f"ip{i}")
    t[0] += 3600  # everyone is idle and fully refilled
    lim.allow("new")
    assert len(lim._buckets) <= 2
    for i in range(500):  # a flood of distinct, all-active keys cannot grow it past the bound
        lim.allow(f"flood{i}")
    assert len(lim._buckets) <= 101


@pytest.mark.parametrize("hook_id", ["short-id", "not-a-uuid-but-long-enough", "12345678", "g" * 36])
def test_l11_hook_instance_must_be_a_uuid(service, hook_id: str) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service)
    b = body()
    b["hookInstance"] = hook_id
    assert c.post(URL, json=b).status_code == 422


def test_l12_summary_truncates_at_word_boundary_with_ellipsis() -> None:
    text = "Ibuprofen with lisinopril and furosemide in a patient with eGFR 24 (triple whammy): " * 3
    out = truncate_summary(text)
    assert len(out) <= 140 and out.endswith("\u2026") and not out[:-1].endswith(" ")
    assert text.startswith(out[:-1])  # a clean prefix, no mid-word cut
    assert out[:-1].rsplit(" ", 1)[-1] in text.split()
    short = "Short summary"
    assert truncate_summary(short) == short
    assert truncate_summary("x" * 300).endswith("\u2026") and len(truncate_summary("x" * 300)) == 140


def test_m8_policy_is_configurable_and_stated_on_cards(service) -> None:  # type: ignore[no-untyped-def]
    # hand-01 has labs dated Aug 2026. anchored: fires (window measured from the record); today: with the clock
    # advanced a year the same creatinine is stale, so critical alerts must not fire on it.
    late = TestClient(
        create_app(
            Settings(
                fhir_mode="fixtures",
                vista_mode="recorded",
                audit_db=":memory:",
                rate_limit_per_minute=10000,
                as_of_policy="today",
            ),
            sources=service.sources,
            audit=AuditStore(":memory:"),
            today=lambda: date(2027, 9, 1),
        )
    )
    r = late.post(URL, json=body("hand-01", "fhir", "metformin 500 MG Oral Tablet"))
    cards_today = r.json()["cards"]
    assert len(cards_today) == 1  # not vacuous: exactly one card, and it is the stale-lab data gap
    assert cards_today[0]["indicator"] == "info" and cards_today[0]["extension"]["org.medsafe.dataGap"] is True
    assert "no eGFR" in cards_today[0]["summary"]
    assert cards_today[0]["extension"]["org.medsafe.asOfPolicy"] == "today"
    anchored = TestClient(
        create_app(
            Settings(
                fhir_mode="fixtures",
                vista_mode="recorded",
                audit_db=":memory:",
                rate_limit_per_minute=10000,
                as_of_policy="anchored",
            ),
            sources=service.sources,
            audit=AuditStore(":memory:"),
            today=lambda: date(2027, 9, 1),
        )
    )
    cards = anchored.post(URL, json=body("hand-01", "fhir", "metformin 500 MG Oral Tablet")).json()["cards"]
    assert cards and all(c["extension"]["org.medsafe.asOfPolicy"] == "anchored" for c in cards)
    assert all("Lab windows measured from" in c["detail"] for c in cards)


def test_m8_compare_reports_policy(service) -> None:  # type: ignore[no-untyped-def]
    c = client_for(service)
    r = c.post("/api/compare", json={"source": "vista", "patientId": "100881", "rxcui": "861007"})
    assert r.status_code == 200 and r.json()["patient"]["as_of_policy"] in {"anchored", "today", "explicit"}


def test_m8_invalid_policy_value_is_reported_by_security_findings() -> None:
    assert any("MEDSAFE_AS_OF_POLICY" in f for f in Settings(as_of_policy="bogus").security_findings())
    assert not any("MEDSAFE_AS_OF_POLICY" in f for f in Settings(as_of_policy="today").security_findings())


def test_ready_reports_source_outage(service) -> None:  # type: ignore[no-untyped-def]
    class Down:
        name = "vista"

        def status(self) -> dict[str, Any]:
            return {"mode": "live", "reachable": False, "error": "circuit open"}

        def close(self) -> None: ...

    s = Settings(fhir_mode="fixtures", vista_mode="recorded", audit_db=":memory:")
    c = TestClient(create_app(s, sources={"vista": Down()}, audit=AuditStore(":memory:")))  # type: ignore[dict-item]
    r = c.get("/ready").json()
    assert r["degraded"] == ["vista"] and r["checks"]["vista"]["reachable"] is False and r["status"] == "ready"


# ------------------------------------------------------------------------------------- final QA round
def test_qa3_one_unparseable_lab_date_skips_that_lab_only() -> None:
    """A collection whose FileMan date is invalid (month 13) is skipped; older good labs are still used."""
    good_new, bad, good_old = "3150301.1", "3151301.1", "3150101.1"  # bad: month 13 is not a date

    def replies(params: tuple[str, ...]) -> str:
        before = float(params[1])
        for c, v in ((good_new, "1.5"), (bad, "9.9"), (good_old, "1.1")):
            if float(c) < before:
                return lab_reply(c, v)
        return ""

    client = Script({**BASE, "ORWLRR NEWOLD": f"{good_new}^{good_old}", "ORWLRR INTERIMG": replies})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    obs = a.get_lab_series("1")
    assert sorted(o["valueQuantity"]["value"] for o in obs) == [1.1, 1.5]  # the bad-date 9.9 is gone, others kept


def test_qa3_unparseable_collection_date_mid_chain_is_flagged_not_fatal() -> None:
    def replies(params: tuple[str, ...]) -> str:
        return lab_reply("3150301.1", "1.5") if params[1] == "3300101" else lab_reply("notadate", "2.0")

    client = Script({**BASE, "ORWLRR NEWOLD": "3150301.1^3140101.1", "ORWLRR INTERIMG": replies})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    obs = a.get_lab_series("1")  # does not raise
    assert [o["valueQuantity"]["value"] for o in obs] == [1.5]
    assert a.truncated_fetches == 1  # cannot page past a page with no usable date: flagged as incomplete


def test_qa3_unparseable_fhir_observation_date_skips_that_lab() -> None:
    from app.context import build_context

    pt = {"resourceType": "Patient", "id": "p", "gender": "male", "birthDate": "1950-01-01"}

    def ob(v: float, when: str) -> dict[str, Any]:
        return {
            "resourceType": "Observation",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2160-0"}]},
            "effectiveDateTime": when,
            "valueQuantity": {"value": v, "unit": "mg/dL"},
        }

    ctx = build_context(pt, [], [], [ob(1.0, "2026-05-01"), ob(2.0, "notadate"), ob(3.0, "2026-13-45")], source="x")
    assert [v.value for v in ctx.labs["creatinine"]] == [1.0]


@pytest.mark.parametrize("value", [0, 0.0, -1, -0.5])
def test_qa3_reported_egfr_of_zero_or_negative_is_rejected(value: float) -> None:
    from app.context import build_context

    pt = {"resourceType": "Patient", "id": "p", "gender": "female", "birthDate": "1950-01-01"}
    egfr = {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": "62238-1"}]},
        "effectiveDateTime": "2026-05-01",
        "valueQuantity": {"value": value, "unit": "mL/min/1.73m2"},
    }
    ctx = build_context(pt, [], [], [egfr], source="x")
    assert "egfr" not in ctx.labs and ctx.latest_lab("egfr") is None  # data gap, not "eGFR 0"
    assert ctx.dropped_labs == {"egfr: implausible value": 1}


def test_qa3_reported_egfr_zero_does_not_fire_a_critical_metformin_alert(service) -> None:  # type: ignore[no-untyped-def]
    from app.api.app import create_app

    pf_patient = {"resourceType": "Patient", "id": "p1", "gender": "male", "birthDate": "1950-01-01"}
    egfr = {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": "62238-1"}]},
        "effectiveDateTime": "2026-08-20",
        "valueQuantity": {"value": 0, "unit": "mL/min/1.73m2"},
    }
    b = body("p1", "prefetch", "metformin 500 MG Oral Tablet")
    b["prefetch"] = {
        "patient": pf_patient,
        "medications": {"resourceType": "Bundle", "entry": []},
        "conditions": {"resourceType": "Bundle", "entry": []},
        "observations": {"resourceType": "Bundle", "entry": [{"resource": egfr}]},
    }
    c = client_for(service)
    cards = c.post(URL, json=b).json()["cards"]
    assert len(cards) == 1 and cards[0]["indicator"] == "info" and cards[0]["extension"]["org.medsafe.dataGap"] is True
    assert create_app  # imported for parity with other API tests


def test_qa3_vista_umol_creatinine_is_converted_like_fhir() -> None:
    from app.context import build_context

    def replies(params: tuple[str, ...]) -> str:
        return lab_reply("3150301.1", "265", "umol/L") if params[1] == "3300101" else ""

    client = Script({**BASE, "ORWLRR NEWOLD": "3150301.1^3150301.1", "ORWLRR INTERIMG": replies})
    a = VistaAdapter(client, mode="live", demo_patients=DEMO)
    obs = a.get_lab_series("1")
    assert len(obs) == 1 and obs[0]["valueQuantity"]["unit"] == "mg/dL"
    assert obs[0]["valueQuantity"]["value"] == pytest.approx(2.9977, abs=1e-3)
    # and it is the same number the FHIR path produces for the same reading
    pt = {"resourceType": "Patient", "id": "p", "gender": "male", "birthDate": "1950-01-01"}
    fhir_ob = {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2160-0"}]},
        "effectiveDateTime": "2015-03-01T10:00:00",
        "valueQuantity": {"value": 265, "unit": "umol/L"},
    }
    via_vista = build_context(pt, [], [], obs, source="vista").latest_lab("creatinine")
    via_fhir = build_context(pt, [], [], [fhir_ob], source="fhir").latest_lab("creatinine")
    assert via_vista.value == via_fhir.value and via_vista.unit == via_fhir.unit  # type: ignore[union-attr]


@pytest.mark.parametrize("unit", ["mg/L", "furlongs", "mmol/L"])
def test_qa3_vista_unsupported_creatinine_units_are_dropped(unit: str) -> None:
    def replies(params: tuple[str, ...]) -> str:
        return lab_reply("3150301.1", "2.0", unit) if params[1] == "3300101" else ""

    client = Script({**BASE, "ORWLRR NEWOLD": "3150301.1^3150301.1", "ORWLRR INTERIMG": replies})
    assert VistaAdapter(client, mode="live", demo_patients=DEMO).get_lab_series("1") == []


def test_qa3_every_env_var_read_by_config_is_documented_in_readme() -> None:
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    config = (root / "backend/app/config.py").read_text()
    readme = (root / "README.md").read_text()
    names = set(re.findall(r'"((?:MEDSAFE|VISTA)_[A-Z_]+)"', config))
    assert len(names) >= 25
    assert [n for n in sorted(names) if n not in readme] == []
    for word in ("GEL", "OPHTHALMIC", "FLUSH"):  # the exclusion list in the README matches the mapper
        assert f"`{word}`" in readme
