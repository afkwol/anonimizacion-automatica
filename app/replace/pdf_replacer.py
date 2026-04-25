"""Reemplazo sobre PDF usando redacciones de PyMuPDF.

**Estrategia v6**: placeholder + guiones hasta completar el ancho original.

1. Buscar texto original con search_for → agrupar rects de misma línea.
2. Calcular el ancho total del área original.
3. Medir el ancho del placeholder en la fuente/tamaño del original.
4. Rellenar con guiones hasta completar el ancho.
5. Blanquear (redact) sin texto, luego insertar con insert_text en baseline.

Resultado: "[ACTOR_1]----------" ocupando exactamente el mismo espacio
que "SONZINI, NAHUEL ANTONIO", en la misma fuente y tamaño.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

import fitz  # PyMuPDF

from app.io.pdf_extract import PdfDocument
from app.replace.text_replacer import Replacement

# Mapeo de familias de fuentes PDF comunes a fuentes base de PyMuPDF.
# PyMuPDF tiene 14 fuentes base disponibles siempre.
_FONT_MAP = {
    "times": "tiro",      # Times-Roman → tiro (serif)
    "serif": "tiro",
    "courier": "cobi",    # Courier → courier
    "mono": "cobi",
    "helvetica": "helv",  # Helvetica → helv (sans-serif)
    "arial": "helv",
    "sans": "helv",
}


def _map_font(pdf_font_name: str) -> str:
    """Mapea el nombre de fuente del PDF a una fuente base de PyMuPDF."""
    lower = pdf_font_name.lower()
    for key, value in _FONT_MAP.items():
        if key in lower:
            return value
    # Default: serif (más común en documentos judiciales).
    return "tiro"


def _group_rects(rects: List[fitz.Rect]) -> List[List[fitz.Rect]]:
    """Agrupa rects de una misma ocurrencia (misma línea o línea siguiente).

    search_for devuelve múltiples rects cuando el texto cruza un salto
    de línea. Los agrupamos si están en la misma línea (adyacentes) O
    en la línea inmediatamente siguiente (wrap de renglón).
    """
    if not rects:
        return []
    groups: List[List[fitz.Rect]] = []
    current: List[fitz.Rect] = [rects[0]]
    for rect in rects[1:]:
        prev = current[-1]
        height = prev.y1 - prev.y0
        same_line = abs(rect.y0 - prev.y0) < height * 0.5
        adjacent = (rect.x0 - prev.x1) < height * 2
        # Línea siguiente: el rect empieza donde termina el anterior (±margen).
        next_line = (rect.y0 >= prev.y0 + height * 0.5 and
                     rect.y0 < prev.y1 + height * 1.5)
        if (same_line and adjacent) or next_line:
            current.append(rect)
        else:
            groups.append(current)
            current = [rect]
    groups.append(current)
    return groups


def _get_span_info_at(page, x: float, y: float) -> dict:
    """Obtiene info completa del span en la posición (x, y).

    Cuando varios spans solapan el punto, elige el que tiene el centro
    vertical más cercano a y (evita tomar la línea anterior/siguiente).
    """
    page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    best = None
    best_dist = float("inf")

    for block in page_dict.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span["bbox"]
                if (bbox[0] - 2 <= x <= bbox[2] + 2 and
                        bbox[1] - 2 <= y <= bbox[3] + 2):
                    center_y = (bbox[1] + bbox[3]) / 2
                    dist = abs(center_y - y)
                    if dist < best_dist:
                        best_dist = dist
                        origin = span.get("origin", (x, bbox[1] + span["size"]))
                        best = {
                            "size": span["size"],
                            "font": span.get("font", "Times"),
                            "baseline_y": origin[1],
                            "flags": span.get("flags", 0),
                        }

    if best is not None:
        return best

    # Fallback: estimar baseline desde la geometría del rect.
    return {"size": 12.0, "font": "Times", "baseline_y": y, "flags": 0, "fallback": True}


def _pad_with_dashes(placeholder: str, target_width: float,
                     fontname: str, fontsize: float) -> tuple[str, float]:
    """Agrega guiones o achica fuente para que el placeholder quepa en target_width.

    Returns:
        (texto_final, fontsize_final) — fontsize puede ser menor si el
        placeholder no entraba en el tamaño original.
    """
    current_width = fitz.get_text_length(placeholder, fontname=fontname, fontsize=fontsize)

    # Si el placeholder es más ancho que el espacio, achicar fuente.
    if current_width > target_width:
        min_size = max(5.0, fontsize * 0.45)
        adjusted = fontsize
        while adjusted > min_size:
            adjusted -= 0.5
            w = fitz.get_text_length(placeholder, fontname=fontname, fontsize=adjusted)
            if w <= target_width:
                break
        return placeholder, adjusted

    dash_width = fitz.get_text_length("-", fontname=fontname, fontsize=fontsize)
    if dash_width <= 0:
        return placeholder, fontsize

    remaining = target_width - current_width
    n_dashes = int(remaining / dash_width)
    if n_dashes <= 0:
        return placeholder, fontsize

    # Centrar el placeholder entre guiones.
    left = n_dashes // 2
    right = n_dashes - left
    return "-" * left + placeholder + "-" * right, fontsize


def write_anonymized_pdf(
    doc: PdfDocument,
    replacements: Sequence[Replacement],
    output_path: Path,
    *,
    scrub_metadata: bool = False,
) -> None:
    """Escribe un nuevo PDF con redacciones + placeholders rellenados."""
    output_path = Path(output_path)

    if not replacements:
        shutil.copyfile(doc.source_path, output_path)
        return

    text_to_placeholder: Dict[str, str] = {}
    for rep in replacements:
        original = doc.full_text[rep.start:rep.end]
        if original not in text_to_placeholder:
            text_to_placeholder[original] = rep.replacement

    pdf = fitz.open(str(doc.source_path))

    for page_idx in range(len(pdf)):
        page = pdf[page_idx]
        rects_to_blank: List[fitz.Rect] = []
        insertions: List[Tuple[fitz.Point, str, float, str]] = []
        covered: Set[Tuple[int, int, int, int]] = set()

        # Procesar textos más largos primero: así la redacción más amplia
        # cubre el área y las variantes cortas se detectan como solapadas.
        sorted_items = sorted(text_to_placeholder.items(),
                              key=lambda kv: len(kv[0]), reverse=True)
        for original_text, placeholder in sorted_items:
            hits = page.search_for(original_text)
            if not hits:
                continue

            groups = _group_rects(hits)

            for group in groups:
                # Deduplicar: si CUALQUIER rect del grupo se solapa con
                # alguna redacción ya registrada, saltar todo el grupo.
                already = False
                for rect in group:
                    for cr in covered:
                        if (rect.x0 < cr[2] and rect.x1 > cr[0] and
                                rect.y0 < cr[3] and rect.y1 > cr[1]):
                            already = True
                            break
                    if already:
                        break
                if already:
                    continue
                for rect in group:
                    covered.add((rect.x0, rect.y0, rect.x1, rect.y1))

                for rect in group:
                    rects_to_blank.append(rect)

                # Info de fuente del primer rect.
                first_rect = group[0]
                info = _get_span_info_at(page, first_rect.x0, first_rect.y0)
                fontsize = info["size"]
                fontname = _map_font(info["font"])
                baseline_y = info["baseline_y"]

                # Si usó fallback, estimar baseline desde la geometría del rect.
                if info.get("fallback"):
                    rect_height = first_rect.y1 - first_rect.y0
                    fontsize = rect_height * 0.85
                    baseline_y = first_rect.y1 - rect_height * 0.15

                # Ancho del área para el placeholder.
                # Para grupos multi-línea, usar solo la primera línea
                # (el placeholder se inserta ahí).
                first_line_rects = [r for r in group
                                    if abs(r.y0 - first_rect.y0) < (first_rect.y1 - first_rect.y0) * 0.5]
                total_width = first_line_rects[-1].x1 - first_line_rects[0].x0

                # Placeholder + guiones para llenar (o fuente achicada).
                padded, final_fontsize = _pad_with_dashes(
                    placeholder, total_width, fontname, fontsize,
                )

                insertions.append((
                    fitz.Point(first_rect.x0, baseline_y),
                    padded,
                    final_fontsize,
                    fontname,
                ))

        if not rects_to_blank:
            continue

        # Blanquear.
        for rect in rects_to_blank:
            page.add_redact_annot(rect, text="", fill=(1, 1, 1))
        page.apply_redactions()

        # Insertar.
        for point, text, fontsize, fontname in insertions:
            page.insert_text(
                point,
                text,
                fontsize=fontsize,
                fontname=fontname,
                color=(0, 0, 0),
            )

    if scrub_metadata:
        pdf.set_metadata(
            {
                "title": "",
                "author": "",
                "subject": "",
                "keywords": "",
                "creator": "",
                "producer": "",
                "creationDate": "",
                "modDate": "",
                "trapped": "",
            }
        )
        try:
            pdf.del_xml_metadata()
        except Exception:
            # Algunos PDFs no traen paquete XMP o PyMuPDF puede no soportarlo
            # según la versión; no bloqueamos la exportación por eso.
            pass

    pdf.save(str(output_path), garbage=3, deflate=True)
    pdf.close()
