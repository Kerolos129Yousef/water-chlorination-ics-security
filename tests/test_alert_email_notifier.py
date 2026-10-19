"""Tests for the email-on-new-anomaly notification path.

Two seams are covered:

* the :class:`~alerting.engine.AlertEngine` ``notifier`` hook -- it fires exactly
  once per *newly opened* incident (not on extend, not on normal), fires only
  after the alert is persisted, and a raising notifier never breaks the lifecycle;
* :class:`~alerting.notifier.EmailNotifier` -- env configuration (opt-in),
  the severity floor, and message formatting, all without touching real SMTP
  (``_send`` is injected / dispatch is synchronous in tests).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from alerting import AlertEngine, EmailNotifier, Severity
from alerting.notifier import format_alert
from backend.app import create_app

THR = 9.269035945180804e-05
FEATURES = (("AIT201", 0.9), ("P204", 0.5), ("MV304", 0.3))


@dataclass
class FakeResult:
    is_anomaly: bool
    anomaly_score: float
    threshold: float = THR
    window_start: str | None = None
    window_end: str | None = None
    features: tuple = FEATURES

    def top_features(self, n: int) -> list[tuple[str, float]]:
        return list(self.features[:n])


def _anomaly(score: float = 1.0e-3, **kw) -> FakeResult:
    return FakeResult(is_anomaly=True, anomaly_score=score, **kw)


def _normal(**kw) -> FakeResult:
    return FakeResult(is_anomaly=False, anomaly_score=1.0e-5, **kw)


# --------------------------------------------------------------- engine hook

def test_notifier_fires_once_per_incident_not_per_window():
    seen = []
    engine = AlertEngine(notifier=seen.append)

    engine.process(_anomaly(window_end="t1"))   # OPEN  -> notify
    engine.process(_anomaly(window_end="t2"))   # extend -> NO notify
    engine.process(_anomaly(window_end="t3"))   # extend -> NO notify

    assert len(seen) == 1
    assert seen[0].alert_id == engine.active_alert.alert_id


def test_normal_result_never_notifies():
    seen = []
    engine = AlertEngine(notifier=seen.append)
    engine.process(_normal())
    assert seen == []


def test_second_independent_incident_notifies_again():
    seen = []
    engine = AlertEngine(notifier=seen.append)

    engine.process(_anomaly(window_end="t1"))   # OPEN #1 -> notify
    engine.process(_normal(window_end="t2"))    # CLOSE  -> NO notify
    engine.process(_anomaly(window_end="t3"))   # OPEN #2 -> notify

    assert len(seen) == 2
    assert seen[0].alert_id != seen[1].alert_id


def test_notifier_receives_persisted_alert():
    """The notified alert is the one in the store (fired after persist)."""
    seen = []
    engine = AlertEngine(notifier=seen.append)
    engine.process(_anomaly(window_end="t1"))
    assert seen[0] in engine.alerts


def test_raising_notifier_does_not_break_lifecycle():
    def boom(_alert):
        raise RuntimeError("smtp down")

    engine = AlertEngine(notifier=boom)
    alert = engine.process(_anomaly(window_end="t1"))   # must not raise
    assert alert is not None
    assert engine.active_alert is alert                 # lifecycle intact
    assert len(engine.alerts) == 1                      # alert still persisted


# ---------------------------------------------------- EmailNotifier.from_env

def test_from_env_disabled_by_default_returns_none():
    assert EmailNotifier.from_env({}) is None
    assert EmailNotifier.from_env({"ALERT_EMAIL_ENABLED": "false"}) is None


def test_from_env_enabled_but_missing_required_returns_none():
    # Enabled but no host/from/to -> degrade to no-email rather than crash.
    assert EmailNotifier.from_env({"ALERT_EMAIL_ENABLED": "true"}) is None


def test_from_env_full_config():
    n = EmailNotifier.from_env({
        "ALERT_EMAIL_ENABLED": "1",
        "ALERT_EMAIL_SMTP_HOST": "smtp.example.com",
        "ALERT_EMAIL_SMTP_PORT": "465",
        "ALERT_EMAIL_FROM": "ics@example.com",
        "ALERT_EMAIL_TO": "admin@example.com, ops@example.com",
        "ALERT_EMAIL_USE_SSL": "true",
        "ALERT_EMAIL_MIN_SEVERITY": "HIGH",
    })
    assert n is not None
    assert n.host == "smtp.example.com"
    assert n.port == 465
    assert n.recipients == ["admin@example.com", "ops@example.com"]
    assert n.use_ssl is True
    assert n.use_tls is False            # SSL forces STARTTLS off
    assert n.min_severity is Severity.HIGH


# --------------------------------------------------------- severity floor

def test_severity_floor_suppresses_below_threshold():
    sent = []
    n = EmailNotifier(
        host="h", sender="a@b", recipients=["x@y"],
        min_severity=Severity.HIGH, async_send=False, send=sent.append,
    )
    engine = AlertEngine(notifier=n)
    engine.process(_anomaly(score=THR * 1.5, window_end="t1"))  # LOW ratio -> skip
    assert sent == []


def test_severity_floor_allows_at_or_above():
    sent = []
    n = EmailNotifier(
        host="h", sender="a@b", recipients=["x@y"],
        min_severity=Severity.HIGH, async_send=False, send=sent.append,
    )
    engine = AlertEngine(notifier=n)
    engine.process(_anomaly(score=THR * 20, window_end="t1"))   # HIGH -> send
    assert len(sent) == 1


# ------------------------------------------------------------ message build

def test_build_message_has_recipients_subject_and_body():
    captured = []
    n = EmailNotifier(
        host="h", sender="ics@example.com", recipients=["admin@example.com"],
        async_send=False, send=lambda alert: captured.append(n._build_message(alert)),
    )
    engine = AlertEngine(notifier=n)
    engine.process(_anomaly(score=THR * 20, window_end="2015-12-28T10:00:00"))

    msg = captured[0]
    assert msg["To"] == "admin@example.com"
    assert msg["From"] == "ics@example.com"
    assert "PROCESS_ANOMALY" in msg["Subject"]
    body = msg.get_content()
    assert "AIT201" in body                      # top feature present
    assert "2015-12-28T10:00:00" in body         # window timestamp present


def test_format_alert_is_pure_and_includes_core_fields():
    engine = AlertEngine()
    alert = engine.process(_anomaly(score=THR * 20, window_end="t9"))
    subject, body = format_alert(alert)
    assert alert.alert_id in subject
    assert alert.severity.value in subject
    assert "threshold" in body.lower()


# ------------------------------------------------------ end-to-end via /score

def test_score_endpoint_emails_admin_on_real_attack_window(detector, feature_names, attack_window):
    """The full live path: POST /score (real TranAD anomaly) -> admin email sent."""
    sent = []
    notifier = EmailNotifier(
        host="smtp.example.com", sender="ics@example.com",
        recipients=["admin@example.com"], async_send=False,
        send=lambda alert: sent.append(notifier._build_message(alert)),
    )
    engine = AlertEngine(notifier=notifier)
    client = TestClient(create_app(detector=detector, alert_engine=engine))

    body = {"feature_names": list(feature_names), "window": attack_window.tolist()}
    resp = client.post("/score", json=body)
    assert resp.status_code == 200
    assert resp.json()["is_anomaly"] is True

    assert len(sent) == 1
    assert sent[0]["To"] == "admin@example.com"
    assert "PROCESS_ANOMALY" in sent[0]["Subject"]


def test_score_endpoint_does_not_email_on_normal_window(detector, feature_names, normal_window):
    sent = []
    notifier = EmailNotifier(
        host="h", sender="ics@example.com", recipients=["admin@example.com"],
        async_send=False, send=sent.append,
    )
    client = TestClient(create_app(detector=detector, alert_engine=AlertEngine(notifier=notifier)))

    body = {"feature_names": list(feature_names), "window": normal_window.tolist()}
    resp = client.post("/score", json=body)
    assert resp.status_code == 200
    assert resp.json()["is_anomaly"] is False
    assert sent == []
