"""Atomic, redacted JSON reports for evaluation runs."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..governance import redact_sensitive

if TYPE_CHECKING:
    from .models import EvaluationRunResult


class JSONEvaluationReporter:
    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)

    def path_for(self, run_id: str) -> Path:
        safe_id = "".join(character for character in run_id if character.isalnum() or character in "-_")
        if not safe_id:
            raise ValueError("evaluation run ID has no safe filename characters")
        return self.output_dir / f"{safe_id}.json"

    def write(self, result: EvaluationRunResult) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.path_for(result.id)
        payload = self._sanitize(asdict(result))
        handle, temporary = tempfile.mkstemp(prefix=".evaluation-", suffix=".json", dir=self.output_dir)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        return target

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if any(marker in key.lower() for marker in ("password", "token", "secret"))
                else cls._sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [cls._sanitize(item) for item in value]
        if isinstance(value, str):
            return redact_sensitive(value)
        return value
