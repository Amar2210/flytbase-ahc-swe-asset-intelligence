"""Local configuration. Secrets are never written to the database."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_local_env(path: str | Path | None = None) -> dict[str, str]:
    """Read fresh local settings without changing the process environment.

    No shell expansion, variable interpolation or Python evaluation occurs.
    Keeping file values separate means editing/removing .env is reflected on the
    next Streamlit rerun; genuine environment variables still take precedence.
    """
    path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if value.startswith(("\"", "'")):
            closing = value.find(value[0], 1)
            if closing < 0 or (value[closing + 1:].strip() and not value[closing + 1:].lstrip().startswith("#")):
                continue
            value = value[1:closing]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    db_path: str = str(PROJECT_ROOT / "data" / "copilot.sqlite3")
    dataset_path: str = str(PROJECT_ROOT / "challenge-dataset" / "sample-data")
    # Informational only: provider selection is no longer configurable.
    provider: str = field(default="openrouter", init=False)
    model: str = "google/gemma-4-31b-it:free"
    api_key: str = field(default="", repr=False)
    as_of_date: str = "2026-10-16"
    field_timezone: str = "UTC"
    request_timeout: int = 90
    max_tool_rounds: int = 3
    max_output_tokens: int = 4096
    max_input_tokens: int = 12000
    report_date: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        environment = {**load_local_env(), **os.environ}
        # Keep the existing OpenRouter key/model names. Legacy provider and TPM
        # variables are ignored; they cannot select a different request path.
        default_model = cls.model
        key = environment.get("OPENROUTER_API_KEY", "").strip()
        return cls(
            db_path=environment.get("COPILOT_DB_PATH", cls.db_path),
            dataset_path=environment.get("COPILOT_DATASET_PATH", cls.dataset_path),
            model=environment.get("COPILOT_MODEL", default_model).strip() or default_model,
            api_key=key,
            as_of_date=environment.get("COPILOT_AS_OF_DATE", "2026-10-16"),
            field_timezone=environment.get("COPILOT_FIELD_TIMEZONE", "UTC"),
            request_timeout=int(environment.get("COPILOT_REQUEST_TIMEOUT", "90")),
            max_tool_rounds=max(1, min(3, int(environment.get("COPILOT_MAX_TOOL_ROUNDS", "3")))),
            max_output_tokens=int(environment.get("COPILOT_MAX_OUTPUT_TOKENS", "4096")),
            max_input_tokens=max(256, int(environment.get("COPILOT_MAX_INPUT_TOKENS", "12000"))),
            report_date=environment.get("COPILOT_REPORT_DATE", "").strip(),
        )
