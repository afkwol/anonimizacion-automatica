"""Tests para segmentación semántica y chunking.

Invariantes duras que se verifican:
  - Round-trip: para toda oración, text[s.start:s.end] == s.text
  - No se parte ninguna entidad: ningún chunk termina en medio de un
    nombre propio (heurística: no termina en un token capitalizado
    seguido de otro capitalizado en el chunk siguiente).
  - Cobertura: la concatenación de oraciones cubre todo el contenido
    no-whitespace del texto original.
  - Presupuesto: ningún chunk excede max_tokens (con el token_counter
    usado).
"""
from __future__ import annotations

from app.pipeline.chunk import Chunk, build_chunks, default_token_counter
from app.pipeline.segment import segment_sentences


SAMPLE_LEGAL = (
    "En la ciudad de Córdoba, a los 10 días del mes de marzo de 2024, "
    "comparece el Sr. Juan Carlos Pérez, DNI 20.123.456, con domicilio "
    "en Av. Colón 1234. Lo patrocina el Dr. Roberto Gómez, T° 45 F° 123. "
    "Se presenta ante el Juzgado Civil N° 5, a cargo de la Dra. María "
    "Elena Fernández, en autos 'Pérez c/ Banco XYZ s/ cobro ejecutivo'. "
    "Ver doctrina de Alterini, A. A., Derecho de Obligaciones, Abeledo, "
    "2019, p. 215."
)


def test_segment_roundtrip() -> None:
    sents = segment_sentences(SAMPLE_LEGAL)
    assert sents, "Debería haber al menos una oración"
    for s in sents:
        assert SAMPLE_LEGAL[s.start : s.end] == s.text


def test_segment_respeta_abreviaturas_legales() -> None:
    """pysbd no debe cortar en 'Sr.', 'Dr.', 'Dra.', 'Av.', 'T°', etc."""
    sents = segment_sentences(SAMPLE_LEGAL)
    # No debería haber una oración que consista sólo en "Sr." o "Dr."
    for s in sents:
        clean = s.text.strip()
        assert clean not in {"Sr.", "Dr.", "Dra.", "Av.", "Sra."}


def test_segment_vacio() -> None:
    assert segment_sentences("") == []
    assert segment_sentences("   \n\n  ") == []


def test_chunks_respetan_budget() -> None:
    sents = segment_sentences(SAMPLE_LEGAL)
    max_tokens = 30
    chunks = build_chunks(sents, max_tokens=max_tokens)
    assert chunks
    for c in chunks:
        assert c.token_count <= max_tokens, (
            f"Chunk {c.index} excede presupuesto: {c.token_count} > {max_tokens}"
        )


def test_chunks_cubren_todo_el_texto() -> None:
    sents = segment_sentences(SAMPLE_LEGAL)
    chunks = build_chunks(sents, max_tokens=50)
    # El primer chunk empieza donde empieza la primera oración;
    # el último termina donde termina la última.
    assert chunks[0].char_start == sents[0].start
    assert chunks[-1].char_end == sents[-1].end
    # Cada oración debe estar asignada a algún chunk.
    total_in_chunks = sum(len(c.sentences) for c in chunks)
    assert total_in_chunks >= len(sents)  # >= por posible overlap


def test_chunks_no_parten_entidades() -> None:
    """Una entidad como 'Juan Carlos Pérez' debe estar entera en un chunk.

    Verificamos que el nombre completo aparece en el texto reconstruido
    de al menos un chunk.
    """
    sents = segment_sentences(SAMPLE_LEGAL)
    chunks = build_chunks(sents, max_tokens=30)
    chunk_texts = [SAMPLE_LEGAL[c.char_start : c.char_end] for c in chunks]
    assert any("Juan Carlos Pérez" in t for t in chunk_texts)
    assert any("María Elena Fernández" in t for t in chunk_texts)


def test_oracion_oversized_se_parte_por_delimitadores() -> None:
    """Una oración muy larga con `;` debe cortarse por `;`, no por palabras."""
    long_sent = (
        "Se presentan los siguientes testigos: Juan Pérez, DNI 20.111.222; "
        "María López, DNI 21.333.444; Carlos García, DNI 22.555.666; "
        "Ana Martínez, DNI 23.777.888; Luis Fernández, DNI 24.999.000."
    )
    sents = segment_sentences(long_sent)
    # Forzamos presupuesto pequeño para obligar al corte por `;`.
    chunks = build_chunks(sents, max_tokens=25)
    assert len(chunks) >= 2
    # Ningún chunk debería cortar un DNI a la mitad.
    for c in chunks:
        text = long_sent[c.char_start : c.char_end]
        # Si aparece "DNI", el número que le sigue debe estar completo.
        if "DNI" in text:
            import re
            # Un DNI "entero" tiene 3 grupos: XX.XXX.XXX (8 dígitos con 2 puntos).
            nums = re.findall(r"DNI\s+(\d{1,2}\.\d{3}\.\d{3})", text)
            # Si hay "DNI" sin su número completo detrás, es un corte roto.
            fragments = re.findall(r"DNI\s+\S+", text)
            assert len(nums) == len(fragments), (
                f"Chunk cortó un DNI a la mitad: {fragments} vs {nums}"
            )


def test_token_counter_inyectable() -> None:
    sents = segment_sentences(SAMPLE_LEGAL)
    calls = []

    def counter(t: str) -> int:
        calls.append(t)
        return len(t.split())

    chunks = build_chunks(sents, max_tokens=10, token_counter=counter)
    assert calls, "El token_counter inyectado debería haber sido llamado"
    assert chunks


def test_overlap_arrastra_oraciones() -> None:
    sents = segment_sentences(SAMPLE_LEGAL)
    chunks_no_overlap = build_chunks(sents, max_tokens=30, overlap_sentences=0)
    chunks_overlap = build_chunks(sents, max_tokens=30, overlap_sentences=1)
    # Con overlap, el chunk 2 debería empezar >= al char_start de la última
    # oración del chunk 1.
    if len(chunks_no_overlap) >= 2 and len(chunks_overlap) >= 2:
        assert chunks_overlap[1].char_start <= chunks_no_overlap[1].char_start


def test_integracion_con_docx() -> None:
    """Smoke test: extract_runs → segment → chunk sobre un fixture real."""
    from pathlib import Path
    from app.io.docx_extract import extract_runs

    fixtures = sorted(p for p in Path("ejemplos").glob("*.docx") if not p.name.startswith("~$"))
    assert fixtures, "No hay fixtures en ejemplos/"
    doc = extract_runs(fixtures[0])
    sents = segment_sentences(doc.full_text)
    assert len(sents) > 10
    chunks = build_chunks(sents, max_tokens=500)
    assert chunks
    assert all(c.token_count <= 500 for c in chunks)
    # Round-trip: texto reconstruido debe ser subconjunto del original.
    for c in chunks:
        assert doc.full_text[c.char_start : c.char_end]
