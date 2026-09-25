"""Caravana — orquestrador resiliente de sessões de navegador isoladas.

Uso típico pela biblioteca::

    from caravana import Engine, EventBus, load_config

    config = load_config("caravana.toml")
    engine = Engine(config, bus=EventBus())
    result = engine.run()
    print(result.status, result.stats.completed, result.reports["html"])

Uso pela linha de comando::

    caravana run --config caravana.toml
    caravana show --json
    caravana run --resume <run-id>
"""

from __future__ import annotations

from ._version import __version__
from .config import Config, SessionConfig, load_config
from .engine import Engine, RunPlan, RunResult, TaskPlan
from .errors import (
    ActionError,
    CaravanaError,
    ConfigError,
    FlowValidationError,
    LedgerError,
    ResumeError,
    SessionError,
    StepFailed,
    TemplateError,
)
from .events import Counter, Event, EventBus
from .flow import Flow, Step, load_flow, parse_flow
from .ledger import Ledger, RunRecord, RunStats, StepRecord
from .report import (
    build_badge_svg,
    build_manifest,
    build_report_html,
    build_report_markdown,
    build_report_text,
)
from .session import ActionResult, SessionRunner, evaluate_when
from .templating import SecretStore, TemplateContext, render, render_value
from .util import (
    atomic_write_json,
    atomic_write_text,
    format_duration,
    human_bytes,
    new_run_id,
    redact,
    safe_filename,
    sha256_file,
    sha256_text,
    slugify,
)

__all__ = [
    "ActionError",
    "ActionResult",
    "CaravanaError",
    "Config",
    "ConfigError",
    "Counter",
    "Engine",
    "Event",
    "EventBus",
    "Flow",
    "FlowValidationError",
    "Ledger",
    "LedgerError",
    "ResumeError",
    "RunPlan",
    "RunRecord",
    "RunResult",
    "RunStats",
    "SecretStore",
    "SessionConfig",
    "SessionError",
    "SessionRunner",
    "Step",
    "StepFailed",
    "StepRecord",
    "TaskPlan",
    "TemplateContext",
    "TemplateError",
    "__version__",
    "atomic_write_json",
    "atomic_write_text",
    "build_badge_svg",
    "build_manifest",
    "build_report_html",
    "build_report_markdown",
    "build_report_text",
    "evaluate_when",
    "format_duration",
    "human_bytes",
    "load_config",
    "load_flow",
    "new_run_id",
    "parse_flow",
    "redact",
    "render",
    "render_value",
    "safe_filename",
    "sha256_file",
    "sha256_text",
    "slugify",
]
