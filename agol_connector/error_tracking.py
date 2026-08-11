"""
error_tracking.py — Bugsink/Sentry error reporting for AGOL Connector
======================================================================
Initialises the Sentry SDK pointed at the project's Bugsink instance.

Only captures unhandled exceptions and explicit error reports.
No personal data, no performance tracing.

Call at plugin startup:
    from .error_tracking import init_error_tracking
    init_error_tracking()

Report a caught exception explicitly:
    from .error_tracking import report_error
    report_error(exc, context={"layer": layer.name()})
"""

from __future__ import annotations

_DSN = "https://8528b1e5c512447391e576d7868c4300@mutantkiwi.bugsink.com/3"
_initialised = False


def init_error_tracking() -> bool:
    """
    Initialise Sentry SDK targeting the Bugsink instance.
    Returns True if successful, False if sentry_sdk is unavailable.
    """
    global _initialised
    if _initialised:
        return True
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        import logging

        # Only capture ERROR+ from logging — not every INFO message
        logging_integration = LoggingIntegration(
            level=logging.ERROR,
            event_level=logging.ERROR,
        )

        sentry_sdk.init(
            dsn=_DSN,
            integrations=[logging_integration],
            # No performance tracing — issues only
            traces_sample_rate=0.0,
            # Scrub common PII patterns from breadcrumbs/events
            send_default_pii=False,
            # Tag every event with plugin version and QGIS version
            before_send=_before_send,
            # Don't send if user has opted out
            environment="production",
            release=_get_version(),
        )

        # Add QGIS context tags
        _set_context_tags()

        _initialised = True
        return True

    except ImportError:
        # sentry_sdk not available in this QGIS install — silent degradation
        return False
    except Exception:
        return False


def report_error(exc: Exception, context: dict | None = None) -> None:
    """
    Explicitly capture a caught exception and send to Bugsink.
    Attaches optional context dict as extra data.
    """
    if not _initialised:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            if context:
                for k, v in context.items():
                    scope.set_extra(k, str(v))
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass


def _before_send(event: dict, hint: dict) -> dict | None:
    """
    Scrub sensitive data before sending.
    Removes tokens, passwords, and file paths from event data.
    """
    import re
    _SCRUB = re.compile(
        r'(token|password|secret|key|Authorization)[=:][^\s&"\']+',
        re.IGNORECASE
    )

    def _clean(obj):
        if isinstance(obj, str):
            return _SCRUB.sub(r'\1=***', obj)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(i) for i in obj]
        return obj

    return _clean(event)


def _get_version() -> str:
    try:
        import os
        meta = os.path.join(os.path.dirname(__file__), "metadata.txt")
        for line in open(meta):
            if line.startswith("version="):
                return "agol-connector@" + line.split("=", 1)[1].strip()
    except Exception:
        pass
    return "agol-connector@unknown"


def _set_context_tags() -> None:
    try:
        import sentry_sdk
        from qgis.core import Qgis
        sentry_sdk.set_tag("qgis.version", Qgis.QGIS_VERSION)
    except Exception:
        pass
