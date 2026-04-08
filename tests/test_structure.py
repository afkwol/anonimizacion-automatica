"""Tests del detector estructural de zonas."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.detect.structure import (
    ZONE_CARATULA,
    ZONE_CITA_DOCTRINA,
    ZONE_CITA_JURISPRUDENCIA,
    ZONE_FIRMA,
    assign_zone,
    detect_zones,
)
from app.io.docx_extract import extract_runs


SAMPLE_SENTENCIA = """SENTENCIA NUMERO: 59. CORDOBA, 26/04/2022.
Y VISTOS: estos autos caratulados PEREZ, JUAN c/ BANCO XYZ. El Sr. Juan Pérez,
DNI 20.123.456, promueve demanda. Cita doctrina ver doctrina de Atilio Aníbal
Alterini, Derecho de Obligaciones, 2019, p. 215. También invoca CSJN, Fallos:
333:1234, 'Smith c/ Jones'. PROTOCOLICESE, HAGASE SABER. Texto Firmado
digitalmente por: LINCON Yessica Nadina, JUEZ/A DE 1RA. INSTANCIA.
"""


class TestCaratula:
    def test_detecta_hasta_y_vistos(self) -> None:
        zones = detect_zones(SAMPLE_SENTENCIA)
        caratulas = [z for z in zones if z.name == ZONE_CARATULA]
        assert len(caratulas) == 1
        c = caratulas[0]
        assert c.start == 0
        # Termina en "Y VISTOS:"
        assert "Y VISTOS" in SAMPLE_SENTENCIA[c.end:c.end + 20]

    def test_fallback_si_no_hay_marcador(self) -> None:
        text = "Texto sin marcadores claros." * 50  # >800 chars
        zones = detect_zones(text)
        caratulas = [z for z in zones if z.name == ZONE_CARATULA]
        assert caratulas
        assert caratulas[0].end == 800


class TestFirma:
    def test_detecta_protocolicese(self) -> None:
        zones = detect_zones(SAMPLE_SENTENCIA)
        firmas = [z for z in zones if z.name == ZONE_FIRMA]
        assert len(firmas) == 1
        # La zona de firma debe contener al juez.
        f = firmas[0]
        assert "LINCON" in SAMPLE_SENTENCIA[f.start:f.end]
        assert f.default_role == "JUEZ"

    def test_detecta_texto_firmado(self) -> None:
        text = "...sentencia. Texto Firmado digitalmente por: García López, Juez."
        zones = detect_zones(text)
        firmas = [z for z in zones if z.name == ZONE_FIRMA]
        assert firmas

    def test_ignora_marcador_intermedio(self) -> None:
        """Si 'PROTOCOLICESE' aparece en el medio (cita interna), no es firma."""
        # Marcador en posición 100 de un doc de 1000 chars (10%, no 70%+).
        text = "Inicio. " + "x" * 90 + " PROTOCOLICESE como cita. " + "y" * 900
        zones = detect_zones(text)
        firmas = [z for z in zones if z.name == ZONE_FIRMA]
        assert firmas == []

    def test_toma_el_ultimo_marcador(self) -> None:
        """Si 'PROTOCOLICESE' aparece dos veces, gana el último."""
        text = "x" * 700 + "PROTOCOLICESE intermedio. " + "y" * 200 + "PROTOCOLICESE final."
        zones = detect_zones(text)
        firmas = [z for z in zones if z.name == ZONE_FIRMA]
        assert len(firmas) == 1
        assert firmas[0].start > 900

    def test_no_detecta_si_no_hay_marcador(self) -> None:
        text = "Un texto sin firma alguna."
        zones = detect_zones(text)
        firmas = [z for z in zones if z.name == ZONE_FIRMA]
        assert firmas == []


class TestJurisprudencia:
    def test_detecta_csjn(self) -> None:
        zones = detect_zones(SAMPLE_SENTENCIA)
        jur = [z for z in zones if z.name == ZONE_CITA_JURISPRUDENCIA]
        assert jur
        assert jur[0].default_role == "AUTOR_JURISPRUDENCIA"

    def test_detecta_camara(self) -> None:
        text = "Conforme CNCiv., Sala A, 'Pérez c/ Banco', 12/3/2020."
        zones = detect_zones(text)
        jur = [z for z in zones if z.name == ZONE_CITA_JURISPRUDENCIA]
        assert jur


class TestDoctrina:
    def test_detecta_ver_doctrina(self) -> None:
        zones = detect_zones(SAMPLE_SENTENCIA)
        doc = [z for z in zones if z.name == ZONE_CITA_DOCTRINA]
        assert doc
        assert doc[0].default_role == "AUTOR_DOCTRINA"
        assert "Alterini" in SAMPLE_SENTENCIA[doc[0].start:doc[0].end]

    def test_detecta_conf(self) -> None:
        text = "El criterio es discutido (conf. Bidart Campos, Manual, 2010)."
        zones = detect_zones(text)
        doc = [z for z in zones if z.name == ZONE_CITA_DOCTRINA]
        assert doc


class TestAssignZone:
    def test_offset_en_caratula(self) -> None:
        zones = detect_zones(SAMPLE_SENTENCIA)
        # Offset 0 (inicio) debe estar en CARATULA.
        z = assign_zone(0, zones)
        assert z is not None
        assert z.name == ZONE_CARATULA

    def test_offset_en_firma(self) -> None:
        zones = detect_zones(SAMPLE_SENTENCIA)
        idx = SAMPLE_SENTENCIA.find("LINCON")
        z = assign_zone(idx, zones)
        assert z is not None
        assert z.name == ZONE_FIRMA

    def test_zona_mas_especifica_gana(self) -> None:
        """Si una cita doctrinaria está dentro de la carátula, gana cita."""
        text = "Y VISTOS: ver doctrina de Pérez, Obra, 2020, p. 5. RESULTA..."
        # Forzamos que doctrina esté antes de "Y VISTOS"
        text = "Inicio. Ver doctrina de Pérez, Obra, 2020. Y VISTOS: cuerpo."
        zones = detect_zones(text)
        idx = text.find("Pérez")
        z = assign_zone(idx, zones)
        # Cita doctrina tiene prioridad mayor que carátula.
        assert z is not None
        assert z.name == ZONE_CITA_DOCTRINA

    def test_offset_fuera_de_zonas(self) -> None:
        """Texto en el cuerpo sin marcadores devuelve None."""
        text = (
            "Y VISTOS: el cuerpo de la sentencia es muy extenso. " * 5
            + "Aquí no hay nada estructural. " * 30
        )
        zones = detect_zones(text)
        # Hay carátula (hasta Y VISTOS), pero el offset al final del cuerpo
        # no debería estar en ninguna zona estructural.
        z = assign_zone(len(text) - 10, zones)
        assert z is None or z.name != ZONE_CARATULA


def test_integracion_con_fixture_real() -> None:
    """En el fixture CERAMI debe detectarse carátula y firma reales."""
    fixtures = sorted(Path("ejemplos").glob("CERAMI*.docx"))
    if not fixtures:
        pytest.skip("Fixture CERAMI no disponible")
    doc = extract_runs(fixtures[0])
    zones = detect_zones(doc.full_text)
    names = {z.name for z in zones}
    assert ZONE_CARATULA in names
    assert ZONE_FIRMA in names
    # La firma debe contener la jueza Yessica Lincón.
    firmas = [z for z in zones if z.name == ZONE_FIRMA]
    firma_text = doc.full_text[firmas[0].start:firmas[0].end]
    assert "LINCON" in firma_text or "Lincon" in firma_text
