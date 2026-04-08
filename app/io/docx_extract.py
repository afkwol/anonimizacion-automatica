"""Extracción robusta de texto desde DOCX.

Un .docx es un ZIP de XMLs. Este módulo recorre TODAS las partes que pueden
contener texto visible (documento principal, headers, footers, footnotes,
endnotes, comments), no sólo los paragraphs del cuerpo como hace python-docx.

El resultado es una lista de `TextRun`: cada elemento representa un nodo
`<w:t>` del XML con su ubicación exacta (part + índice global del run dentro
de esa part). Esa ubicación permite, en PR 9, reemplazar texto preservando
el formato original (negritas, fuente, numeración, etc.) y volver a
empaquetar el .docx.

NO usa python-docx: necesitamos acceso a headers/footers/footnotes que
python-docx no expone uniformemente.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
NSMAP = {"w": W_NS}

# Partes del ZIP que pueden contener texto visible.
# Orden importa: así queda reflejado en el texto extraído.
TEXT_PARTS_ORDER = (
    "word/document.xml",
    # headers/footers tienen sufijo numérico: header1.xml, header2.xml, ...
    "word/header",
    "word/footer",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
)


@dataclass
class TextRun:
    """Un nodo <w:t> concreto del XML con su ubicación.

    Attributes:
        part: nombre de la part del ZIP (ej: 'word/document.xml').
        run_index: índice global del <w:t> dentro de la part (0-based).
        text: contenido textual del nodo.
        char_start: offset del primer carácter dentro del texto reconstruido
            de la part. Se llena durante extract_runs.
        char_end: offset exclusivo.
    """

    part: str
    run_index: int
    text: str
    char_start: int = 0
    char_end: int = 0


@dataclass
class DocxDocument:
    """Representación extraída de un .docx listo para detección/reemplazo.

    `runs` está ordenado por (orden de part, run_index). `full_text` es la
    concatenación de los .text de cada run, con un salto de línea entre
    parts distintas. Los offsets en `char_start`/`char_end` de cada run
    apuntan a posiciones dentro de `full_text`.
    """

    source_path: Path
    runs: List[TextRun]
    full_text: str


def _list_text_parts(zf: zipfile.ZipFile) -> List[str]:
    """Devuelve las parts con texto visible, en orden estable."""
    names = set(zf.namelist())
    result: List[str] = []
    for prefix in TEXT_PARTS_ORDER:
        if prefix.endswith(".xml"):
            if prefix in names:
                result.append(prefix)
        else:
            # header/footer con sufijo numérico
            matching = sorted(n for n in names if n.startswith(prefix) and n.endswith(".xml"))
            result.extend(matching)
    return result


def _iter_text_nodes(xml_bytes: bytes) -> Iterator[tuple[etree._Element, etree._Element | None]]:
    """Itera todos los <w:t> en orden de documento junto con su <w:p> ancestro.

    Importante: usamos iter() en el árbol completo. Eso incluye <w:t> dentro
    de tablas (<w:tbl>), text boxes, smartArt, etc. Por eso este método es
    estrictamente más cubriente que python-docx .paragraphs.

    Devolvemos el <w:p> ancestro más cercano para que el extractor pueda
    marcar cambios de párrafo con `\\n` y preservar la segmentación natural.
    """
    root = etree.fromstring(xml_bytes)
    p_tag = f"{W}p"
    for elem in root.iter(f"{W}t"):
        # Subir hasta el primer <w:p> ancestro (puede no existir si <w:t>
        # vive dentro de header/footer sin paragraph wrapper).
        ancestor = elem.getparent()
        while ancestor is not None and ancestor.tag != p_tag:
            ancestor = ancestor.getparent()
        yield elem, ancestor


def extract_runs(docx_path: Path) -> DocxDocument:
    """Extrae todos los <w:t> de un .docx como una lista de TextRun.

    El texto reconstruido (`full_text`) conserva el orden de documento y
    separa cada part con `\\n\\n` para que la segmentación semántica de PR 3
    no una contenido de, por ejemplo, el footer con el cuerpo.
    """
    docx_path = Path(docx_path)
    if not docx_path.exists():
        raise FileNotFoundError(f"No existe el archivo: {docx_path}")
    if docx_path.suffix.lower() != ".docx":
        raise ValueError(
            f"extract_runs sólo acepta .docx (recibido: {docx_path.suffix}). "
            "Para .doc legacy, convertir antes con soffice."
        )

    runs: List[TextRun] = []
    text_buffer: List[str] = []
    cursor = 0

    with zipfile.ZipFile(docx_path, "r") as zf:
        parts = _list_text_parts(zf)
        for part_index, part in enumerate(parts):
            xml_bytes = zf.read(part)
            try:
                nodes = list(_iter_text_nodes(xml_bytes))
            except etree.XMLSyntaxError as exc:
                # No abortamos por una part corrupta — seguimos con las demás
                # pero dejamos traza. Fail-closed lo maneja el pipeline arriba.
                raise ValueError(f"XML inválido en {part}: {exc}") from exc

            last_paragraph: etree._Element | None = None
            first_in_part = True
            for run_index, (node, paragraph) in enumerate(nodes):
                text = node.text or ""
                if not text:
                    continue

                # Cambio de párrafo → insertar "\n" (mismo separador que usa
                # python-docx). Así preservamos los límites naturales para
                # el segmenter del PR 3 sin hinchar artificialmente el texto.
                if paragraph is not None and paragraph is not last_paragraph and not first_in_part:
                    text_buffer.append("\n")
                    cursor += 1
                last_paragraph = paragraph
                first_in_part = False

                start = cursor
                end = cursor + len(text)
                runs.append(
                    TextRun(
                        part=part,
                        run_index=run_index,
                        text=text,
                        char_start=start,
                        char_end=end,
                    )
                )
                text_buffer.append(text)
                cursor = end

            # Separador entre parts: doble salto para que el segmenter
            # no fusione contenido de parts distintas.
            if part_index < len(parts) - 1 and runs:
                text_buffer.append("\n\n")
                cursor += 2

    full_text = "".join(text_buffer)
    return DocxDocument(source_path=docx_path, runs=runs, full_text=full_text)


def count_coverage(doc: DocxDocument) -> dict:
    """Métricas rápidas para comparar contra python-docx en los tests."""
    parts_seen = sorted({r.part for r in doc.runs})
    return {
        "num_runs": len(doc.runs),
        "num_chars": len(doc.full_text),
        "parts": parts_seen,
        "has_headers": any("header" in p for p in parts_seen),
        "has_footers": any("footer" in p for p in parts_seen),
        "has_footnotes": any("footnotes" in p for p in parts_seen),
    }
