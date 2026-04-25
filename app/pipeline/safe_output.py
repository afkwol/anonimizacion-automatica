"""Helpers para exportación segura de documentos anonimizados.

Objetivos:

1. Evitar nombres de archivo de salida que filtren identidades.
2. Permitir auditorías públicas sin nombres sensibles.
3. Hacer un post-check simple sobre el output para detectar fugas obvias.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

from app.replace.text_replacer import Replacement


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def public_document_id(input_path: Path) -> str:
    """ID estable, no derivado del nombre visible del archivo."""
    return _sha1_file(Path(input_path))[:10]


def build_public_output_path(input_path: Path) -> Path:
    input_path = Path(input_path)
    doc_id = public_document_id(input_path)
    return input_path.with_name(f"documento_{doc_id}_anonimizado{input_path.suffix.lower()}")


def build_public_audit_path(input_path: Path) -> Path:
    input_path = Path(input_path)
    doc_id = public_document_id(input_path)
    return input_path.with_name(f"documento_{doc_id}_audit_publico.json")


def build_review_hold_path(output_path: Path) -> Path:
    output_path = Path(output_path)
    return output_path.with_name(output_path.stem + "_REVISAR_NO_PUBLICAR" + output_path.suffix.lower())


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def find_obvious_leaks(
    source_text: str,
    replacements: Sequence[Replacement],
    output_text: str,
) -> List[str]:
    """Busca fugas obvias del texto que se intentó reemplazar.

    No pretende demostrar ausencia total de leaks; sólo detectar casos
    evidentes en el texto extraído del documento final.
    """
    output_raw = output_text.casefold()
    output_norm = _normalize_ws(output_text)
    seen: set[str] = set()
    leaks: List[str] = []

    for rep in sorted(replacements, key=lambda r: (r.start, r.end)):
        original = source_text[rep.start:rep.end].strip()
        if len(original) < 3:
            continue
        key = original.casefold()
        if key in seen:
            continue
        seen.add(key)
        if original.casefold() in output_raw or _normalize_ws(original) in output_norm:
            leaks.append(original)

    return leaks


def redact_public_audit(
    audit: Mapping[str, object],
    *,
    document_id: str,
    output_name: Optional[str],
) -> dict:
    """Devuelve una versión publicable del audit, sin nombres ni paths sensibles."""
    safe = {k: v for k, v in audit.items() if k not in {"input", "output", "coreference", "parties"}}
    safe["document_id"] = document_id
    safe["input"] = None
    safe["output"] = output_name

    parties = audit.get("parties")
    if isinstance(parties, list):
        safe["parties"] = [
            {
                "rol": p.get("rol"),
                "placeholder": p.get("placeholder"),
                "from_caratula": p.get("from_caratula", False),
            }
            for p in parties
            if isinstance(p, Mapping)
        ]

    validation = audit.get("validation")
    if isinstance(validation, Mapping):
        safe["validation"] = validation

    return safe

