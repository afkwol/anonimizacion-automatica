"""Reemplazo in-place sobre .docx preservando formato.

**Cómo funciona**:
1. Recibe un `DocxDocument` (de PR 1) y una lista de `Replacement` con
   offsets que apuntan a `doc.full_text`.
2. Para cada replacement, encuentra los `TextRun` que intersecta:
   - Si toca un único run → split del texto del run en before+rep+after.
   - Si cruza N runs → primer run = before+rep, intermedios = "",
     último run = after.
3. Construye un mapping `(part, run_index) → nuevo_texto`.
4. Re-empaqueta el .docx: copia byte-por-byte el ZIP original salvo las
   parts modificadas, en las que reescribe sólo el `.text` de los `<w:t>`
   afectados (preservando atributos, rPr, formato, etc.).

**Garantías**:
- Si no hay reemplazos en una part, esa part se copia literal del ZIP
  original (no se reparseay no se re-serializa).
- Si una part tiene reemplazos, se reparsea con lxml, se modifican sólo
  los nodos `<w:t>` afectados (in-place), y se serializa con la misma
  declaración XML que tenía.
- El formato (negrita, itálica, fuente, color, numeración) se preserva
  porque vive en `<w:rPr>` (hermano de `<w:t>`), nunca tocado.
- `xml:space="preserve"` se agrega automáticamente si el nuevo texto
  empieza/termina con whitespace, para que Word no lo colapse.
"""
from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from lxml import etree

from app.io.docx_extract import (
    NSMAP,
    W,
    DocxDocument,
    TextRun,
    _iter_text_nodes,
)

from .text_replacer import Replacement, apply_replacements

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass
class _RunEdit:
    """Texto nuevo para un run particular. None = sin cambio."""

    part: str
    run_index: int
    new_text: str


def _compute_run_edits(
    doc: DocxDocument, replacements: Sequence[Replacement]
) -> Dict[Tuple[str, int], str]:
    """Para cada run afectado por replacements, calcula su nuevo texto.

    Estrategia: agrupar runs por part, y dentro de cada part aplicar el
    algoritmo de `apply_replacements` pero a nivel de run, no de string
    monolítico. Como `full_text` es la concatenación de runs separados
    por '\\n' (cambio de párrafo) o '\\n\\n' (cambio de part) y esos
    separadores NO pertenecen a ningún run, los offsets dentro de un run
    son `[run.char_start, run.char_end)`.

    Spans que cruzan separadores (entre párrafos o parts) son raros pero
    posibles si el detector detectó una entidad partida por whitespace
    de párrafo. En ese caso: primer run-tocado recibe before+replacement,
    runs intermedios pierden su contenido (texto entre ellos también),
    último run recibe after. El separador en sí NO se preserva en la
    salida del run intermedio porque ya fue subsumido por el span (esto
    es por diseño: si vos decís "este span es PII", todo lo que está
    adentro del span se borra).
    """
    if not replacements:
        return {}

    # Agrupar runs por part en orden de aparición.
    runs_by_part: Dict[str, List[TextRun]] = {}
    for r in doc.runs:
        runs_by_part.setdefault(r.part, []).append(r)

    edits: Dict[Tuple[str, int], str] = {}
    # Procesar en orden REVERSO de offset para que cada edit no desalinee los
    # offsets de los siguientes (que se calculan contra `run.text` original).
    sorted_reps = sorted(replacements, key=lambda r: (r.start, r.end), reverse=True)

    # Para cada replacement, encontrar runs intersectados.
    for rep in sorted_reps:
        # Localizar la part que contiene rep.start.
        # Si el span cruza parts, lo procesamos por la part del start.
        affected: List[TextRun] = []
        for r in doc.runs:
            if r.char_end <= rep.start:
                continue
            if r.char_start >= rep.end:
                break
            affected.append(r)

        if not affected:
            continue

        if len(affected) == 1:
            run = affected[0]
            local_start = max(0, rep.start - run.char_start)
            local_end = min(len(run.text), rep.end - run.char_start)
            current = edits.get((run.part, run.run_index), run.text)
            new = current[:local_start] + rep.replacement + current[local_end:]
            edits[(run.part, run.run_index)] = new
        else:
            first = affected[0]
            last = affected[-1]
            first_local = max(0, rep.start - first.char_start)
            last_local = min(len(last.text), rep.end - last.char_start)

            current_first = edits.get((first.part, first.run_index), first.text)
            edits[(first.part, first.run_index)] = (
                current_first[:first_local] + rep.replacement
            )
            for mid in affected[1:-1]:
                edits[(mid.part, mid.run_index)] = ""
            current_last = edits.get((last.part, last.run_index), last.text)
            edits[(last.part, last.run_index)] = current_last[last_local:]

    return edits


def _rewrite_part_xml(xml_bytes: bytes, edits_in_part: Dict[int, str]) -> bytes:
    """Reescribe los <w:t> de una part con el texto nuevo.

    Sólo toca los nodos cuyos run_index están en `edits_in_part`. Si el
    nuevo texto empieza/termina con whitespace, agrega `xml:space="preserve"`
    para que Word no lo colapse.
    """
    root = etree.fromstring(xml_bytes)
    p_tag = f"{W}p"
    run_index = 0
    for node in root.iter(f"{W}t"):
        # Mantener consistencia con extract_runs: contamos sólo nodos con .text
        original = node.text or ""
        if not original:
            continue
        if run_index in edits_in_part:
            new_text = edits_in_part[run_index]
            node.text = new_text
            if new_text != new_text.strip():
                node.set(XML_SPACE, "preserve")
        run_index += 1
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def write_anonymized_docx(
    doc: DocxDocument,
    replacements: Sequence[Replacement],
    output_path: Path,
) -> None:
    """Escribe un nuevo .docx con los reemplazos aplicados.

    El archivo de salida es un ZIP idéntico al input salvo en las parts
    que tuvieron reemplazos. Esto preserva todo el formato, imágenes,
    estilos, numeración, etc.

    Args:
        doc: DocxDocument extraído con `extract_runs`.
        replacements: spans con sus textos de reemplazo (offsets en
            `doc.full_text`).
        output_path: destino. Se sobreescribe si existe.
    """
    output_path = Path(output_path)
    edits = _compute_run_edits(doc, replacements)

    # Agrupar edits por part para reescribir cada part una sola vez.
    edits_by_part: Dict[str, Dict[int, str]] = {}
    for (part, run_index), new_text in edits.items():
        edits_by_part.setdefault(part, {})[run_index] = new_text

    # Si no hay edits, simplemente copiamos el archivo.
    if not edits:
        shutil.copyfile(doc.source_path, output_path)
        return

    with zipfile.ZipFile(doc.source_path, "r") as zin:
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename in edits_by_part:
                    data = _rewrite_part_xml(data, edits_by_part[item.filename])
                zout.writestr(item, data)


def build_replacements_from_spans(
    spans_with_placeholders: Sequence[Tuple[int, int, str]],
) -> List[Replacement]:
    """Helper: convierte tuplas (start, end, placeholder) en Replacements."""
    return [Replacement(start=s, end=e, replacement=p) for s, e, p in spans_with_placeholders]
