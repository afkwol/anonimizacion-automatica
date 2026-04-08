"""Tests para los detectores regex deterministas.

Dos invariantes críticas que se verifican:
  - PRECISION: ningún detector produce falsos positivos sobre texto
    legal típico (número de expediente, fechas, montos).
  - RECALL: todos los casos positivos del fixture son detectados.
Los tests usan ejemplos reales del dominio judicial argentino.
"""
from __future__ import annotations

import pytest

from app.detect.regex_detectors import (
    _cbu_is_valid,
    _cuit_checksum_ok,
    detect_all,
    detect_cbu,
    detect_cuit,
    detect_dni,
    detect_email,
    detect_pasaporte,
    detect_patente,
    detect_phone,
)
from app.detect.span import Span, resolve_overlaps


# ============================ DNI ========================================
class TestDNI:
    def test_con_keyword_y_puntos(self) -> None:
        text = "Juan Pérez, DNI 20.123.456, domiciliado en..."
        spans = detect_dni(text)
        assert len(spans) == 1
        assert spans[0].text == "20.123.456"
        assert spans[0].metadata["normalized"] == "20123456"

    def test_con_keyword_sin_puntos(self) -> None:
        spans = detect_dni("actor DNI 20123456, demanda...")
        assert len(spans) == 1
        assert spans[0].metadata["normalized"] == "20123456"

    def test_variantes_keyword(self) -> None:
        for kw in ("DNI", "D.N.I.", "D.N.I", "documento", "L.E.", "LE", "LC"):
            text = f"{kw} 15.678.901 se presenta"
            spans = detect_dni(text)
            assert spans, f"No detectó DNI con keyword '{kw}'"

    def test_rechaza_numeros_sin_keyword(self) -> None:
        # Un número de expediente NO debe ser detectado como DNI.
        text = "Expediente 12.345.678, año 2024"
        spans = detect_dni(text)
        assert spans == []

    def test_rechaza_fuera_de_rango(self) -> None:
        # Número demasiado chico.
        assert detect_dni("DNI 123") == []
        # Sólo 7 u 8 dígitos válidos (7 es admitido: DNIs viejos).
        assert detect_dni("DNI 1.234.567") != []


# ============================ CUIT =======================================
class TestCUIT:
    def test_checksum_valido(self) -> None:
        # 20-20123456-X: calcular X válido
        # Usamos un CUIT real de ejemplo: 20-12345678-9 (verificamos algoritmo)
        # El checksum real para "2012345678" es 3, no 9. Probamos con uno correcto:
        # AFIP: 30-50001091-2 (CUIT ficticio validado manualmente).
        assert _cuit_checksum_ok("30500010912")

    def test_rechaza_checksum_invalido(self) -> None:
        assert not _cuit_checksum_ok("20123456780")  # dígito inválido

    def test_detect_con_guiones(self) -> None:
        text = "La empresa CUIT 30-50001091-2 se presenta"
        spans = detect_cuit(text)
        assert len(spans) == 1

    def test_detect_sin_separadores(self) -> None:
        spans = detect_cuit("CUIT 30500010912")
        assert len(spans) == 1

    def test_rechaza_checksum_invalido_en_texto(self) -> None:
        # 11 dígitos con último inválido (20-12345678-0 cuando debería ser 3).
        spans = detect_cuit("el CUIT 20-12345678-0 es inválido")
        assert spans == []


# ============================ CBU ========================================
class TestCBU:
    def test_checksum_conocido(self) -> None:
        # CBU de ejemplo del BCRA: 0170035040000002373188
        assert _cbu_is_valid("0170035040000002373188")

    def test_rechaza_invalido(self) -> None:
        assert not _cbu_is_valid("0170035040000002373189")
        assert not _cbu_is_valid("1234567890123456789012")

    def test_detecta_en_texto(self) -> None:
        spans = detect_cbu("Transferir al CBU 0170035040000002373188 urgente")
        assert len(spans) == 1


# ============================ Email ======================================
class TestEmail:
    def test_basico(self) -> None:
        spans = detect_email("Contactar a juan.perez@estudio-legal.com.ar")
        assert len(spans) == 1
        assert spans[0].text == "juan.perez@estudio-legal.com.ar"

    def test_multiple(self) -> None:
        spans = detect_email("a@b.com, c.d@e.f.ar y x_y@z.org")
        assert len(spans) == 3

    def test_no_falsos_positivos(self) -> None:
        assert detect_email("arroba@ sin dominio") == []


# ============================ Teléfono ===================================
class TestTelefono:
    def test_con_keyword(self) -> None:
        spans = detect_phone("Tel. 0351-4567890")
        assert len(spans) == 1

    def test_varias_formas(self) -> None:
        cases = [
            "teléfono +54 11 4567-8900",
            "Celular 351 555 1234",
            "Tel: 43218765",
        ]
        for c in cases:
            assert detect_phone(c), f"No detectó: {c}"

    def test_sin_keyword_no_detecta(self) -> None:
        """Números sueltos sin contexto no son teléfonos."""
        assert detect_phone("expediente 1234567890") == []


# ============================ Patente ====================================
class TestPatente:
    def test_mercosur(self) -> None:
        spans = detect_patente("Vehículo AB123CD involucrado")
        assert len(spans) == 1
        assert spans[0].metadata["formato"] == "mercosur"

    def test_viejo(self) -> None:
        spans = detect_patente("Vehículo ABC 123 involucrado")
        assert len(spans) == 1
        assert spans[0].metadata["formato"] == "viejo"


# ============================ Pasaporte ==================================
def test_pasaporte() -> None:
    spans = detect_pasaporte("Pasaporte AAA123456")
    assert len(spans) == 1


# ============================ detect_all + overlaps ======================
def test_detect_all_sin_duplicados() -> None:
    text = (
        "Juan Pérez, DNI 20.123.456, CUIT 30-50001091-2, "
        "email juan@perez.com, Tel. 11-4567-8900. "
        "CBU 0170035040000002373188."
    )
    spans = detect_all(text)
    types = {s.type for s in spans}
    assert types == {"DNI", "CUIT", "EMAIL", "TELEFONO", "CBU"}
    # Ningún par se solapa.
    for i, a in enumerate(spans):
        for b in spans[i + 1 :]:
            assert not a.overlaps(b)


def test_resolve_overlaps_prioridad_regex() -> None:
    """Si un span regex y uno LLM se solapan, gana regex."""
    regex_span = Span(start=0, end=10, text="DNI 123456", type="DNI", source="regex", confidence=1.0)
    llm_span = Span(start=5, end=15, text="123456 etc", type="PER", source="llm", confidence=0.7)
    resolved = resolve_overlaps([llm_span, regex_span])
    assert len(resolved) == 1
    assert resolved[0].source == "regex"


def test_no_falsos_positivos_sobre_expediente() -> None:
    """Texto legal real no debe disparar detecciones espurias."""
    text = (
        "En los autos 'Pérez c/ Banco s/ cobro' Expte. 12345/2024, "
        "cuaderno de prueba N° 789, el Juzgado Civil N° 5 dispone "
        "que en fecha 15/03/2024 se realice la audiencia a las 10:30hs."
    )
    spans = detect_all(text)
    # No debería detectar nada: no hay DNIs, CUITs, CBUs, emails, ni
    # teléfonos con keyword.
    assert spans == [], f"Falsos positivos: {[(s.type, s.text) for s in spans]}"
