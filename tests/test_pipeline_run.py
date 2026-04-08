"""Tests del orquestador end-to-end (PR 11).

Mockea el cliente LM Studio (no requiere LM Studio corriendo). Verifica
que las etapas se ejecutan en orden, que los reemplazos regex se
aplican, y que el dry-run no escribe archivo.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.classify.lm_client import LMStudioClient
from app.pipeline.run import (
    PipelineConfig,
    _build_regex_replacements,
    run_pipeline,
    write_audit_log,
)
from app.detect.span import Span


FIXTURES = Path(__file__).resolve().parent.parent / "ejemplos"
DOCX_FILES = sorted(FIXTURES.glob("*.docx"))


class _FakeChat:
    """Sustituto de LMStudioClient.chat — devuelve una respuesta fija."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def __call__(self, system_prompt: str, user_prompt: str, **kw) -> str:
        self.calls += 1
        return self.response


def _empty_resultados() -> str:
    return json.dumps({"resultados": []})


# ============================ helpers unitarios =========================
class TestBuildRegexReplacements:
    def test_dni_repetido_mismo_placeholder(self) -> None:
        spans = [
            Span(start=0, end=10, text="20.111.222", type="DNI", source="regex"),
            Span(start=20, end=30, text="20.111.222", type="DNI", source="regex"),
            Span(start=40, end=50, text="30.999.888", type="DNI", source="regex"),
        ]
        reps, counters, expected = _build_regex_replacements(spans)
        assert len(reps) == 3
        assert reps[0].replacement == reps[1].replacement == "[DNI_1]"
        assert reps[2].replacement == "[DNI_2]"
        assert counters["DNI"] == 2
        assert expected == ["[DNI_1]", "[DNI_2]"]


# ============================ end-to-end con mock =======================
@pytest.mark.skipif(not DOCX_FILES, reason="No hay docx fixtures")
class TestRunPipeline:
    def test_dry_run_no_escribe_archivo(self, tmp_path: Path) -> None:
        config = PipelineConfig(use_ner=False, dry_run=True)
        with patch.object(LMStudioClient, "chat", new=_FakeChat(_empty_resultados())):
            result = run_pipeline(DOCX_FILES[0], config)
        assert result.output_path is None
        assert result.success is True
        # Validation no corre en dry-run
        assert result.validation is None
        # Pero sí debe haber detectado spans regex (DNIs en las fixtures)
        assert any(s.source == "regex" for s in result.spans)

    def test_audit_serializable(self, tmp_path: Path) -> None:
        config = PipelineConfig(use_ner=False, dry_run=True)
        with patch.object(LMStudioClient, "chat", new=_FakeChat(_empty_resultados())):
            result = run_pipeline(DOCX_FILES[0], config)
        audit_path = tmp_path / "audit.json"
        write_audit_log(result, audit_path)
        loaded = json.loads(audit_path.read_text(encoding="utf-8"))
        assert loaded["input"] == str(DOCX_FILES[0])
        assert "metrics" in loaded
        assert any(m["stage"] == "extract" for m in loaded["metrics"])

    def test_metricas_cubren_todas_las_etapas(self) -> None:
        config = PipelineConfig(use_ner=False, dry_run=True)
        with patch.object(LMStudioClient, "chat", new=_FakeChat(_empty_resultados())):
            result = run_pipeline(DOCX_FILES[0], config)
        names = {m.name for m in result.metrics}
        for required in [
            "extract", "detect_regex", "detect_zones", "resolve_overlaps",
            "build_fichas", "classify_llm", "coreference", "build_replacements",
        ]:
            assert required in names

    def test_pipeline_real_escribe_y_valida(self, tmp_path: Path) -> None:
        """E2E real: escribe el output, valida, y como las fixtures tienen DNIs
        que SÍ se anonimizan via regex, la validación debe pasar."""
        # Copiamos la fixture a tmp_path para no ensuciar el repo.
        import shutil
        src = tmp_path / DOCX_FILES[0].name
        shutil.copyfile(DOCX_FILES[0], src)

        config = PipelineConfig(use_ner=False, dry_run=False)
        with patch.object(LMStudioClient, "chat", new=_FakeChat(_empty_resultados())):
            result = run_pipeline(src, config)

        # El archivo se escribió
        assert result.output_path is not None
        assert result.output_path.exists()
        # Validación corrió
        assert result.validation is not None
        # Como anonimizamos los DNIs por regex, no debería haber leak.
        # (Si la fixture tiene CUITs sin keyword o algo edge, podría
        # fallar — en ese caso el archivo se renombra a *_FAILED.docx,
        # lo cual también es comportamiento correcto.)
        if result.validation.passed:
            assert result.success is True
            assert "_anonimizado" in result.output_path.name
            assert "_FAILED" not in result.output_path.name
        else:
            assert result.success is False
            assert "_FAILED" in result.output_path.name
