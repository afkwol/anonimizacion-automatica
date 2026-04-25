from __future__ import annotations

from pathlib import Path

from app.pipeline.safe_output import (
    build_public_audit_path,
    build_public_output_path,
    find_obvious_leaks,
    redact_public_audit,
)
from app.replace.text_replacer import Replacement


def test_public_paths_no_filtran_stem(tmp_path: Path) -> None:
    src = tmp_path / "Pérez c. Gómez.pdf"
    src.write_bytes(b"contenido")

    out = build_public_output_path(src)
    audit = build_public_audit_path(src)

    assert "Pérez" not in out.name
    assert "Gómez" not in out.name
    assert out.suffix == ".pdf"
    assert audit.name.endswith("_audit_publico.json")


def test_redact_public_audit_saca_nombres_y_paths() -> None:
    audit = {
        "input": "C:/algo/Perez.pdf",
        "output": "C:/algo/Perez_anonimizado.pdf",
        "parties": [
            {
                "nombre": "PEREZ, JUAN",
                "rol": "actor",
                "placeholder": "P., J.",
                "from_caratula": True,
            }
        ],
        "n_replacements": 5,
    }

    safe = redact_public_audit(audit, document_id="abc123", output_name="documento_abc123_anonimizado.pdf")
    assert safe["input"] is None
    assert safe["output"] == "documento_abc123_anonimizado.pdf"
    assert safe["parties"][0]["placeholder"] == "P., J."
    assert "nombre" not in safe["parties"][0]


def test_find_obvious_leaks_detecta_texto_original() -> None:
    source = "Autos caratulados PEREZ, JUAN contra ACME SA."
    reps = [Replacement(start=17, end=29, replacement="P., J.")]

    leaks = find_obvious_leaks(source, reps, "Autos caratulados PEREZ, JUAN contra ACME SA.")
    assert leaks == ["PEREZ, JUAN"]


def test_find_obvious_leaks_no_marca_si_ya_no_esta() -> None:
    source = "Autos caratulados PEREZ, JUAN contra ACME SA."
    reps = [Replacement(start=17, end=29, replacement="P., J.")]

    leaks = find_obvious_leaks(source, reps, "Autos caratulados P., J. contra ACME SA.")
    assert leaks == []
