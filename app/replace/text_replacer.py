"""Reemplazo determinista sobre texto plano.

Aplica una lista de spans con sus placeholders sobre un string fuente.
Garantías:

- Procesa spans en orden de aparición y verifica no-solapamiento.
- Conserva exactamente todo el texto NO cubierto por spans (byte por byte).
- Falla rápido si hay solapamientos: significa que `resolve_overlaps` no
  fue llamado río arriba. NO es seguro silenciar.

Esta función es la base de `docx_replacer` (que aplica el mismo algoritmo
sobre runs) y se usa también para anonimizar `.txt` o para debug.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class Replacement:
    """Una sustitución concreta: span + texto que va en su lugar."""

    start: int
    end: int
    replacement: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"Replacement inválido: [{self.start}, {self.end})")


def apply_replacements(text: str, replacements: Sequence[Replacement]) -> str:
    """Aplica los reemplazos sobre `text`. Determinista, sin solapamientos.

    Args:
        text: texto fuente.
        replacements: lista de Replacement. NO requiere estar pre-ordenada.

    Returns:
        Texto con los reemplazos aplicados.

    Raises:
        ValueError: si dos replacements se solapan.
        IndexError: si un replacement excede el largo del texto.
    """
    if not replacements:
        return text
    sorted_reps = sorted(replacements, key=lambda r: (r.start, r.end))
    out: List[str] = []
    cursor = 0
    for rep in sorted_reps:
        if rep.end > len(text):
            raise IndexError(
                f"Replacement [{rep.start},{rep.end}) excede el largo del texto ({len(text)})"
            )
        if rep.start < cursor:
            raise ValueError(
                f"Replacements solapados: [{rep.start},{rep.end}) "
                f"colisiona con el cursor en {cursor}"
            )
        out.append(text[cursor:rep.start])
        out.append(rep.replacement)
        cursor = rep.end
    out.append(text[cursor:])
    return "".join(out)
