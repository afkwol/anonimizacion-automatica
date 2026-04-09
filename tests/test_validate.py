"""Tests de validación post-hoc (PR 10)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.io.docx_extract import extract_runs
from app.replace.docx_replacer import write_anonymized_docx
from app.replace.text_replacer import Replacement
from app.validate.post_checks import (
    check_docx_structure,
    check_length_ratio,
    check_placeholders_present,
    check_regex_leak,
    validate_docx_output,
    validate_text_output,
)


FIXTURES = Path(__file__).resolve().parent.parent / "ejemplos"
DOCX_FILES = sorted(p for p in FIXTURES.glob("*.docx") if not p.name.startswith("~$"))


class TestRegexLeak:
    def test_dni_filtrado_es_blocker(self) -> None:
        text = "El DNI 24.123.456 corresponde al actor"
        issues = check_regex_leak(text)
        assert any(i.code == "REGEX_LEAK" for i in issues)
        assert all(i.severity == "blocker" for i in issues)

    def test_texto_limpio_no_genera_issues(self) -> None:
        text = "El [ACTOR_1] interpuso demanda contra [DEMANDADO_1]"
        assert check_regex_leak(text) == []


class TestLengthRatio:
    def test_dentro_de_rango_ok(self) -> None:
        assert check_length_ratio("a" * 100, "a" * 95) == []

    def test_colapso_es_blocker(self) -> None:
        issues = check_length_ratio("a" * 100, "a" * 10)
        assert len(issues) == 1
        assert issues[0].code == "LENGTH_RATIO"

    def test_inflado_es_blocker(self) -> None:
        issues = check_length_ratio("a" * 100, "a" * 200)
        assert issues and issues[0].code == "LENGTH_RATIO"

    def test_input_vacio_no_falla(self) -> None:
        assert check_length_ratio("", "") == []


class TestPlaceholders:
    def test_todos_presentes(self) -> None:
        text = "El [ACTOR_1] vs [DEMANDADO_1]"
        assert check_placeholders_present(text, ["[ACTOR_1]", "[DEMANDADO_1]"]) == []

    def test_falta_uno(self) -> None:
        text = "El [ACTOR_1] solo"
        issues = check_placeholders_present(text, ["[ACTOR_1]", "[TESTIGO_1]"])
        assert len(issues) == 1
        assert "[TESTIGO_1]" in issues[0].message


class TestValidateText:
    def test_pipeline_feliz(self) -> None:
        original = "Juan Pérez DNI 24.123.456 demanda a Roberto Gómez"
        output = "[ACTOR_1] [DNI_1] demanda a [DEMANDADO_1]"
        report = validate_text_output(
            original, output, expected_placeholders=["[ACTOR_1]", "[DEMANDADO_1]"]
        )
        assert report.passed

    def test_dni_filtrado_falla(self) -> None:
        original = "Juan Pérez DNI 24.123.456"
        output = "[ACTOR_1] DNI 24.123.456"  # ¡PII filtrada!
        report = validate_text_output(original, output)
        assert not report.passed
        assert any(b.code == "REGEX_LEAK" for b in report.blockers)


@pytest.mark.skipif(not DOCX_FILES, reason="No hay docx fixtures")
class TestValidateDocx:
    def test_roundtrip_sin_cambios_estructura_ok(self, tmp_path: Path) -> None:
        """Sin reemplazos: la estructura del DOCX se conserva (parts iguales).

        Nota: las fixtures contienen DNIs/CUITs reales, así que el chequeo
        de regex_leak fallaría sobre el texto. Lo que importa acá es que
        la estructura del ZIP se preserva.
        """
        src = DOCX_FILES[0]
        doc = extract_runs(src)
        out = tmp_path / "out.docx"
        write_anonymized_docx(doc, [], out)
        issues = check_docx_structure(src, out)
        assert not any(i.severity == "blocker" for i in issues)

    def test_estructura_rota_blocker(self, tmp_path: Path) -> None:
        src = DOCX_FILES[0]
        # Crear un docx "roto" sin la part principal
        import zipfile
        broken = tmp_path / "broken.docx"
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(broken, "w") as zout:
            for item in zin.infolist():
                if item.filename != "word/document.xml":
                    zout.writestr(item, zin.read(item.filename))
        issues = check_docx_structure(src, broken)
        assert any(i.code == "DOCX_MISSING_PARTS" for i in issues)
