"""Alert notification: email the admin when a new active anomaly is opened.

The :class:`~alerting.engine.AlertEngine` owns the lifecycle; it is the only stage
that knows a *new* incident has just opened (deduplication means one continuous
anomaly is one incident, so this fires once per incident -- not once per window).
The engine takes an optional ``notifier`` callback and invokes it on every newly
opened alert; this module provides the email implementation of that callback::

    AlertEngine(notifier=EmailNotifier.from_env())  # None when email is disabled

Boundary. Like the rest of ``alerting``, this imports no torch / FastAPI /
``ml.src`` / ``simulator`` -- only the stdlib and this package's :class:`Alert`.
It is **opt-in**: :meth:`EmailNotifier.from_env` returns ``None`` unless
``ALERT_EMAIL_ENABLED`` is truthy *and* the required SMTP settings are present, so
the default (and every test / CI run) behaves exactly as before -- no SMTP, no
network. Sending is best-effort and off the request path: each email goes out on a
short-lived daemon thread and any failure is logged and swallowed, so a flaky mail
server can never stall ``/score`` or break the alert lifecycle.
"""

from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from email.utils import formatdate

from .alert import Alert, Severity

__all__ = ["EmailNotifier"]

logger = logging.getLogger("alerting.notifier")

# Environment keys (documented in .env.example). Email is OFF unless ENABLED is
# truthy and host/sender/recipients are all set.
_ENV_ENABLED = "ALERT_EMAIL_ENABLED"
_ENV_HOST = "ALERT_EMAIL_SMTP_HOST"
_ENV_PORT = "ALERT_EMAIL_SMTP_PORT"
_ENV_USERNAME = "ALERT_EMAIL_USERNAME"
_ENV_PASSWORD = "ALERT_EMAIL_PASSWORD"  # noqa: S105 - env var NAME, not a secret
_ENV_FROM = "ALERT_EMAIL_FROM"
_ENV_TO = "ALERT_EMAIL_TO"
_ENV_USE_TLS = "ALERT_EMAIL_USE_TLS"  # STARTTLS on a plain port (default: on)
_ENV_USE_SSL = "ALERT_EMAIL_USE_SSL"  # implicit TLS (SMTPS, usually port 465)
_ENV_TIMEOUT = "ALERT_EMAIL_TIMEOUT"
_ENV_MIN_SEVERITY = "ALERT_EMAIL_MIN_SEVERITY"  # LOW (default) | MEDIUM | HIGH

# Severity ordering for the optional floor filter (LOW < MEDIUM < HIGH).
_SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}

_TRUTHY = {"1", "true", "yes", "on"}


def _is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _split_recipients(raw: str) -> list[str]:
    """Parse a comma/semicolon/whitespace-separated recipient list, order-preserving."""
    out: list[str] = []
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        addr = chunk.strip()
        if addr and addr not in out:
            out.append(addr)
    return out


class EmailNotifier:
    """Send an email to the admin when a new alert opens.

    Used as the ``notifier`` callback on :class:`~alerting.engine.AlertEngine`: the
    engine calls ``notifier(alert)`` once, synchronously, for each newly opened
    incident. This class makes that non-blocking -- :meth:`__call__` applies the
    optional severity floor and then hands the actual SMTP send to a daemon thread,
    so neither detection nor the alert lifecycle ever waits on the mail server.

    Prefer :meth:`from_env` to construct one from environment configuration; the
    direct constructor exists for tests and explicit wiring. ``_send`` is injectable
    (``send=...``) so tests exercise the formatting + dispatch without real SMTP.
    """

    def __init__(
        self,
        *,
        host: str,
        sender: str,
        recipients: list[str],
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 10.0,
        min_severity: Severity | None = None,
        async_send: bool = True,
        send=None,
    ) -> None:
        if not host:
            raise ValueError("EmailNotifier requires a non-empty SMTP host")
        if not sender:
            raise ValueError("EmailNotifier requires a non-empty sender (from) address")
        if not recipients:
            raise ValueError("EmailNotifier requires at least one recipient")
        if use_ssl and use_tls:
            # SMTPS (implicit TLS) and STARTTLS are mutually exclusive; SSL wins and
            # STARTTLS is ignored. Surface it rather than silently picking one.
            logger.warning(
                "%s and %s are both set; using implicit TLS (SSL) and ignoring STARTTLS",
                _ENV_USE_SSL, _ENV_USE_TLS,
            )
        self.host = host
        self.port = port
        self.sender = sender
        self.recipients = recipients
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.use_tls = use_tls and not use_ssl
        self.timeout = timeout
        self.min_severity = min_severity
        self.async_send = async_send
        self._send_impl = send or self._smtp_send

    # ------------------------------------------------------------------ factory

    @classmethod
    def from_env(cls, env: dict | None = None) -> "EmailNotifier | None":
        """Build a notifier from environment variables, or ``None`` when disabled.

        Returns ``None`` (email off) unless ``ALERT_EMAIL_ENABLED`` is truthy. When
        enabled, ``ALERT_EMAIL_SMTP_HOST``, ``ALERT_EMAIL_FROM`` and
        ``ALERT_EMAIL_TO`` are required; a missing one is logged and also yields
        ``None`` (degrade to no-email rather than crash the app at startup). See
        ``.env.example`` for the full list.
        """
        env = os.environ if env is None else env
        if not _is_truthy(env.get(_ENV_ENABLED)):
            return None

        host = (env.get(_ENV_HOST) or "").strip()
        sender = (env.get(_ENV_FROM) or "").strip()
        recipients = _split_recipients(env.get(_ENV_TO) or "")
        missing = [
            name for name, val in (
                (_ENV_HOST, host), (_ENV_FROM, sender), (_ENV_TO, recipients),
            ) if not val
        ]
        if missing:
            logger.error(
                "%s is set but email is not configured: missing %s; alerts will NOT be emailed",
                _ENV_ENABLED, ", ".join(missing),
            )
            return None

        try:
            port = int((env.get(_ENV_PORT) or "587").strip())
        except ValueError:
            logger.error("invalid %s=%r; defaulting to 587", _ENV_PORT, env.get(_ENV_PORT))
            port = 587
        try:
            timeout = float((env.get(_ENV_TIMEOUT) or "10").strip())
        except ValueError:
            timeout = 10.0

        use_ssl = _is_truthy(env.get(_ENV_USE_SSL))
        # STARTTLS defaults ON (safe default for the common 587 submission port);
        # it is force-disabled when implicit TLS (SSL) is selected.
        use_tls = env.get(_ENV_USE_TLS) is None or _is_truthy(env.get(_ENV_USE_TLS))

        min_sev_raw = (env.get(_ENV_MIN_SEVERITY) or "").strip().upper()
        min_severity: Severity | None = None
        if min_sev_raw:
            try:
                min_severity = Severity(min_sev_raw)
            except ValueError:
                logger.error(
                    "invalid %s=%r; expected one of LOW/MEDIUM/HIGH; emailing all severities",
                    _ENV_MIN_SEVERITY, min_sev_raw,
                )

        notifier = cls(
            host=host,
            port=port,
            sender=sender,
            recipients=recipients,
            username=(env.get(_ENV_USERNAME) or "").strip() or None,
            password=env.get(_ENV_PASSWORD) or None,
            use_ssl=use_ssl,
            use_tls=use_tls,
            timeout=timeout,
            min_severity=min_severity,
        )
        logger.info(
            "email alerts ENABLED: %s:%d -> %s (ssl=%s, starttls=%s, min_severity=%s)",
            host, port, ", ".join(recipients), use_ssl, notifier.use_tls,
            min_severity.value if min_severity else "LOW",
        )
        return notifier

    # ------------------------------------------------------------- callback API

    def __call__(self, alert: Alert) -> None:
        """Notifier callback: filter by severity, then dispatch the send (async)."""
        if self.min_severity is not None and (
            _SEVERITY_RANK.get(alert.severity, 0) < _SEVERITY_RANK[self.min_severity]
        ):
            logger.debug(
                "alert %s severity %s below floor %s; not emailing",
                alert.alert_id, alert.severity.value, self.min_severity.value,
            )
            return
        if not self.async_send:
            self._guarded_send(alert)
            return
        threading.Thread(
            target=self._guarded_send,
            args=(alert,),
            name=f"alert-email-{alert.alert_id}",
            daemon=True,
        ).start()

    def _guarded_send(self, alert: Alert) -> None:
        """Run the send, swallowing every error so a bad mail server is harmless."""
        try:
            self._send_impl(alert)
            logger.info("emailed alert %s to %s", alert.alert_id, ", ".join(self.recipients))
        except Exception:  # noqa: BLE001 - notification must never raise to the caller
            logger.exception("failed to email alert %s; detection/lifecycle unaffected", alert.alert_id)

    # ------------------------------------------------------------- message + SMTP

    def _build_message(self, alert: Alert) -> EmailMessage:
        subject, body = format_alert(alert)
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(body)
        return msg

    def _smtp_send(self, alert: Alert) -> None:
        msg = self._build_message(alert)
        if self.use_ssl:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as smtp:
                self._login_and_send(smtp, msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls()
                self._login_and_send(smtp, msg)

    def _login_and_send(self, smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if self.username and self.password:
            smtp.login(self.username, self.password)
        smtp.send_message(msg)


def format_alert(alert: Alert) -> tuple[str, str]:
    """Render a newly-opened alert into an (subject, plain-text body) email pair.

    Module-level + pure so it is trivially unit-testable and reusable by any other
    transport (e.g. a future Slack/webhook notifier) without an SMTP object.
    """
    subject = (
        f"[ICS ALERT] {alert.severity.value} {alert.category} — {alert.alert_id}"
    )
    lines = [
        "A new active anomaly has been detected by the Water Chlorination ICS monitor.",
        "",
        f"  Alert ID   : {alert.alert_id}",
        f"  Category   : {alert.category}",
        f"  Severity   : {alert.severity.value}  (heuristic — score/threshold band)",
        f"  Status     : {alert.status.value}",
        f"  Detected   : {alert.detected_at or 'n/a'}",
        f"  Window     : {alert.window_start or 'n/a'} .. {alert.window_end or 'n/a'}",
        f"  Score      : {alert.anomaly_score:.6g}  (threshold {alert.threshold:.6g})",
    ]
    if alert.reason:
        lines.append(f"  Reason     : {alert.reason}")
    if alert.affected_channels:
        lines.append(f"  Channels   : {', '.join(alert.affected_channels)}")
    if alert.top_features:
        lines.append("")
        lines.append("  Top contributing sensors (reconstruction error):")
        for name, err in alert.top_features:
            lines.append(f"    - {name}: {float(err):.6g}")
    lines += [
        "",
        "This is an automated notification. Severity is an engineering heuristic, "
        "not a calibrated risk score. Review the operator dashboard for context.",
    ]
    return subject, "\n".join(lines)
