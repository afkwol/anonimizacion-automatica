"""Tests E2E con golden expectations (PR 14).

Corre el pipeline completo sobre cada fixture en `ejemplos/`, con el
LLM mockeado, y verifica:

- Conteos mínimos de detección regex (DNI/CUIT) por fixture.
- Que la validación fail-closed pasa (no quedan PII filtradas).
- Que los placeholders esperados aparecen en el output.
- Métricas de tiempo razonables (smoke; sin asserts duros).

Los thresholds están en `tests/golden/expectations.json` y son
MÍNIMOS — el pipeline puede atrapar más, nunca menos.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from app.classify.lm_client import LMStudioClient
from app.io.docx_extract import extract_runs
from app.pipeline.run import PipelineConfig, run_pipeline


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "ejemplos"
GOLDEN_FILE = Path(__file__).resolve().parent / "golden" / "expectations.json"


def _load_expectations() -> dict:
    if not GOLDEN_FILE.exists():
        return {"fixtures": {}}
    return json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))


_EXPECTATIONS = _load_expectations()
_FIXTURE_NAMES = sorted(_EXPECTATIONS.get("fixtures", {}).keys())


class _FakeChat:
    """Mock determinista del LLM: devuelve siempre `resultados: []`."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, system_prompt: str, user_prompt: str, **kw) -> str:
        self.calls += 1
        return json.dumps({"resultados": []})


@pytest.mark.skipif(not _FIXTURE_NAMES, reason="No hay golden expectations")
@pytest.mark.parametrize("fixture_name", _FIXTURE_NAMES)
def test_pipeline_e2e_fixture(fixture_name: str, tmp_path: Path) -> None:
    src = FIXTURES_DIR / fixture_name
    if not src.exists():
        pytest.skip(f"Fixture no presente en disco: {fixture_name}")

    expected = _EXPECTATIONS["fixtures"][fixture_name]

    # 1. Sanity check del input
    doc = extract_runs(src)
    assert len(doc.full_text) >= expected["min_chars"], (
        f"Fixture {fixture_name} más corto de lo esperado: "
        f"{len(doc.full_text)} < {expected['min_chars']}"
    )

    # 2. Copiar a tmp para no ensuciar el repo y correr el pipeline
    work = tmp_path / src.name
    shutil.copyfile(src, work)

    config = PipelineConfig(use_ner=False, dry_run=False)
    with patch.object(LMStudioClient, "chat", new=_FakeChat()):
        result = run_pipeline(work, config)

    # 3. Conteos mínimos de regex por tipo
    by_type: dict[str, int] = {}
    for s in result.spans:
        if s.source == "regex":
            by_type[s.type] = by_type.get(s.type, 0) + 1
    assert by_type.get("DNI", 0) >= expected["min_regex_dni"], (
        f"DNIs detectados ({by_type.get('DNI', 0)}) < esperado "
        f"({expected['min_regex_dni']}) en {fixture_name}"
    )
    assert by_type.get("CUIT", 0) >= expected["min_regex_cuit"], (
        f"CUITs detectados ({by_type.get('CUIT', 0)}) < esperado "
        f"({expected['min_regex_cuit']}) en {fixture_name}"
    )

    # 4. Validación fail-closed
    assert result.validation is not None
    if expected.get("validation_must_pass", True):
        assert result.validation.passed, (
            f"Validación falló en {fixture_name}: "
            f"{[i.message for i in result.validation.blockers]}"
        )

    # 5. Placeholders esperados presentes en el output
    if expected.get("must_contain_placeholders"):
        out_doc = extract_runs(result.output_path)
        for ph in expected["must_contain_placeholders"]:
            assert ph in out_doc.full_text, (
                f"Placeholder {ph} ausente del output de {fixture_name}"
            )


def test_golden_file_existe_y_es_valido() -> None:
    assert GOLDEN_FILE.exists()
    data = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    assert "fixtures" in data
    for name, exp in data["fixtures"].items():
        assert "min_chars" in exp
        assert "min_regex_dni" in exp
        assert "min_regex_cuit" in exp
