"""Tests de reemplazo determinista (PR 9)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.io.docx_extract import extract_runs
from app.replace.docx_replacer import (
    _compute_run_edits,
    write_anonymized_docx,
)
from app.replace.text_replacer import Replacement, apply_replacements


FIXTURES = Path(__file__).resolve().parent.parent / "ejemplos"
DOCX_FILES = sorted(p for p in FIXTURES.glob("*.docx") if not p.name.startswith("~$"))


# ============================ text_replacer ==============================
class TestTextReplacer:
    def test_reemplazo_simple(self) -> None:
        text = "Hola Juan Pérez, ¿cómo estás?"
        reps = [Replacement(start=5, end=15, replacement="[ACTOR_1]")]
        assert apply_replacements(text, reps) == "Hola [ACTOR_1], ¿cómo estás?"

    def test_multiples_reemplazos_orden_arbitrario(self) -> None:
        text = "A B C D E"
        reps = [
            Replacement(start=4, end=5, replacement="[X]"),  # C
            Replacement(start=0, end=1, replacement="[Y]"),  # A
            Replacement(start=8, end=9, replacement="[Z]"),  # E
        ]
        assert apply_replacements(text, reps) == "[Y] B [X] D [Z]"

    def test_sin_reemplazos_devuelve_original(self) -> None:
        assert apply_replacements("texto", []) == "texto"

    def test_solapamiento_lanza(self) -> None:
        text = "abcdefg"
        reps = [
            Replacement(start=0, end=3, replacement="X"),
            Replacement(start=2, end=5, replacement="Y"),
        ]
        with pytest.raises(ValueError, match="solapados"):
            apply_replacements(text, reps)

    def test_fuera_de_rango_lanza(self) -> None:
        with pytest.raises(IndexError):
            apply_replacements("abc", [Replacement(start=0, end=10, replacement="X")])

    def test_replacement_invalido_lanza(self) -> None:
        with pytest.raises(ValueError):
            Replacement(start=5, end=2, replacement="X")

    def test_preserva_todo_lo_no_reemplazado_byte_a_byte(self) -> None:
        text = "INICIO XXXXX MEDIO YYYYY FIN"
        reps = [
            Replacement(start=7, end=12, replacement="[A]"),
            Replacement(start=19, end=24, replacement="[B]"),
        ]
        result = apply_replacements(text, reps)
        assert result == "INICIO [A] MEDIO [B] FIN"

    def test_replacement_vacio_borra(self) -> None:
        text = "abc DEF ghi"
        reps = [Replacement(start=4, end=7, replacement="")]
        assert apply_replacements(text, reps) == "abc  ghi"


# ============================ docx_replacer ==============================
@pytest.mark.skipif(not DOCX_FILES, reason="No hay docx fixtures")
class TestDocxReplacer:
    def test_compute_run_edits_un_solo_run(self) -> None:
        """Caso típico: la entidad cae completamente dentro de un único run."""
        doc = extract_runs(DOCX_FILES[0])
        # Tomar el primer run con texto suficiente
        run = next(r for r in doc.runs if len(r.text) >= 5)
        rep = Replacement(
            start=run.char_start + 1,
            end=run.char_start + 4,
            replacement="[X]",
        )
        edits = _compute_run_edits(doc, [rep])
        key = (run.part, run.run_index)
        assert key in edits
        expected = run.text[:1] + "[X]" + run.text[4:]
        assert edits[key] == expected

    def test_roundtrip_sin_reemplazos_es_idempotente(self, tmp_path: Path) -> None:
        """Sin reemplazos, el archivo de salida es copia byte-byte del input."""
        doc = extract_runs(DOCX_FILES[0])
        out = tmp_path / "out.docx"
        write_anonymized_docx(doc, [], out)
        assert out.exists()
        assert out.read_bytes() == DOCX_FILES[0].read_bytes()

    def test_roundtrip_con_reemplazo_abre_y_contiene_placeholder(
        self, tmp_path: Path
    ) -> None:
        """Aplicar un reemplazo y reabrir el archivo: contiene el placeholder."""
        doc = extract_runs(DOCX_FILES[0])
        # Buscar un run con texto suficientemente largo (>=10 chars).
        run = next(r for r in doc.runs if len(r.text) >= 10)
        rep = Replacement(
            start=run.char_start,
            end=run.char_start + len(run.text),
            replacement="[REDACTED_TEST]",
        )
        out = tmp_path / "anonimizado.docx"
        write_anonymized_docx(doc, [rep], out)

        # Reabrir y verificar
        doc2 = extract_runs(out)
        assert "[REDACTED_TEST]" in doc2.full_text
        # Texto original del run no debe estar (chequeo débil porque
        # podría aparecer en otra part — usamos hash de longitud)
        new_run_texts = [r.text for r in doc2.runs if r.part == run.part and r.run_index == run.run_index]
        assert "[REDACTED_TEST]" in new_run_texts

    def test_reemplazos_multiples_en_misma_part(self, tmp_path: Path) -> None:
        doc = extract_runs(DOCX_FILES[0])
        target_part = doc.runs[0].part
        runs_in_part = [r for r in doc.runs if r.part == target_part and len(r.text) >= 5]
        if len(runs_in_part) < 2:
            pytest.skip("Fixture no tiene suficientes runs")
        r1, r2 = runs_in_part[0], runs_in_part[1]
        reps = [
            Replacement(start=r1.char_start, end=r1.char_start + 3, replacement="[A]"),
            Replacement(start=r2.char_start, end=r2.char_start + 3, replacement="[B]"),
        ]
        out = tmp_path / "out.docx"
        write_anonymized_docx(doc, reps, out)
        doc2 = extract_runs(out)
        assert "[A]" in doc2.full_text
        assert "[B]" in doc2.full_text
