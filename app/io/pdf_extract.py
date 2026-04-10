"""Extracción de texto desde PDF con mapeo de offsets a posiciones de página.

Usa PyMuPDF (fitz) para extraer texto preservando la información de
posición (página + bounding box) de cada fragmento. Esto permite luego
aplicar redacciones quirúrgicas sobre el PDF original, preservando el
layout, imágenes, sellos, firmas digitales, etc.

**Modelo de datos**: análogo a DocxDocument / TextRun del módulo DOCX:

- `PdfSpan`: un fragmento de texto con su página y bbox (equivale a TextRun).
- `PdfDocument`: contiene la lista de PdfSpans y el `full_text` concatenado.
  Los offsets `char_start`/`char_end` de cada PdfSpan apuntan a posiciones
  dentro de `full_text`, igual que en DOCX.

**OCR**: si una página tiene muy poco texto extraíble (< umbral), se
marca `needs_ocr=True`. El pipeline puede decidir aplicar OCR con
ocrmypdf antes de re-extraer. Por ahora no lo implementamos inline —
es un paso previo opcional.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import fitz  # PyMuPDF


# Umbral de caracteres por página para considerar que necesita OCR.
_OCR_CHAR_THRESHOLD = 30


@dataclass
class PdfSpan:
    """Un fragmento de texto extraído del PDF con posición exacta.

    Attributes:
        page: número de página (0-based).
        bbox: bounding box (x0, y0, x1, y1) en coordenadas del PDF.
        text: contenido textual del fragmento.
        char_start: offset del primer carácter dentro de `full_text`.
        char_end: offset exclusivo.
        block_no: número de bloque dentro de la página (para ordenamiento).
        line_no: número de línea dentro del bloque.
        span_no: número de span dentro de la línea.
    """

    page: int
    bbox: tuple[float, float, float, float]
    text: str
    char_start: int = 0
    char_end: int = 0
    block_no: int = 0
    line_no: int = 0
    span_no: int = 0


@dataclass
class PdfDocument:
    """Representación extraída de un PDF lista para detección/reemplazo.

    Análogo a DocxDocument: `full_text` es el texto plano concatenado,
    y `spans` contiene la información de posición para volver a mapear
    los offsets de detección a posiciones del PDF original.
    """

    source_path: Path
    spans: List[PdfSpan]
    full_text: str
    page_count: int = 0
    needs_ocr_pages: List[int] = field(default_factory=list)


def extract_pdf(pdf_path: Path) -> PdfDocument:
    """Extrae texto de un PDF con información de posición por span.

    Usa `page.get_text("dict")` de PyMuPDF para obtener bloques →
    líneas → spans con bounding boxes. Concatena todo en `full_text`
    con separadores de línea (\\n) y página (\\n\\n).

    Args:
        pdf_path: ruta al archivo PDF.

    Returns:
        PdfDocument con full_text y spans mapeados.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"No existe el archivo: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    all_spans: List[PdfSpan] = []
    text_buffer: List[str] = []
    cursor = 0
    needs_ocr: List[int] = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        page_char_count = 0
        first_in_page = True

        for block in page_dict.get("blocks", []):
            # Solo bloques de texto (type 0), ignorar imágenes (type 1).
            if block.get("type", 0) != 0:
                continue

            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text:
                        continue

                    page_char_count += len(text)

                    if not first_in_page:
                        # No agregamos separador antes del primer span de
                        # la página — el separador de página lo puso la
                        # página anterior.
                        pass

                    start = cursor
                    end = cursor + len(text)

                    all_spans.append(
                        PdfSpan(
                            page=page_idx,
                            bbox=(
                                span["bbox"][0],
                                span["bbox"][1],
                                span["bbox"][2],
                                span["bbox"][3],
                            ),
                            text=text,
                            char_start=start,
                            char_end=end,
                            block_no=block.get("number", 0),
                            line_no=line.get("spans", []).index(span),
                            span_no=0,
                        ),
                    )
                    text_buffer.append(text)
                    cursor = end
                    first_in_page = False

                # Separador de línea tras cada línea del bloque.
                if not first_in_page:
                    text_buffer.append("\n")
                    cursor += 1

        if page_char_count < _OCR_CHAR_THRESHOLD:
            needs_ocr.append(page_idx)

        # Separador de página.
        if page_idx < len(doc) - 1:
            text_buffer.append("\n")
            cursor += 1

    full_text = "".join(text_buffer)
    page_count = len(doc)
    doc.close()

    return PdfDocument(
        source_path=pdf_path,
        spans=all_spans,
        full_text=full_text,
        page_count=page_count,
        needs_ocr_pages=needs_ocr,
    )
