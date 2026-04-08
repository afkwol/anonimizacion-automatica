"""Tests del detector de citas (PR 13)."""
from __future__ import annotations

from app.detect.citations import (
    detect_citations,
    detect_doctrina_authors,
    detect_jurisprudencia_authors,
)


class TestDoctrina:
    def test_trigger_ver_doctrina_de(self) -> None:
        text = "Como surge de la opinión, ver doctrina de Alterini, Atilio, Derecho de Daños, 2009."
        spans = detect_doctrina_authors(text)
        names = {s.text for s in spans}
        assert any("Alterini" in n for n in names)
        # Todos preserve
        assert all(s.metadata.get("preserve") for s in spans)
        assert all(s.type == "AUTOR_DOCTRINA" for s in spans)

    def test_trigger_conf(self) -> None:
        text = "El criterio se mantiene (conf. Bidart Campos, Manual, 1998)."
        spans = detect_doctrina_authors(text)
        assert any("Bidart" in s.text for s in spans)

    def test_trigger_segun_ensena(self) -> None:
        text = "según enseña Lorenzetti, el contrato es ley para las partes."
        spans = detect_doctrina_authors(text)
        assert any("Lorenzetti" in s.text for s in spans)

    def test_doctrina_formal_apellido_anio(self) -> None:
        text = "ALTERINI, Derecho de Obligaciones, La Ley, 2019, p. 234."
        spans = detect_doctrina_authors(text)
        assert any("ALTERINI" in s.text for s in spans)

    def test_no_falsos_positivos_en_texto_normal(self) -> None:
        text = "El actor solicita la prueba pericial."
        assert detect_doctrina_authors(text) == []


class TestJurisprudencia:
    def test_caratula_csjn(self) -> None:
        text = 'CSJN, "Pérez Juan c/ ANSES s/ jubilación", 12/3/2020.'
        spans = detect_jurisprudencia_authors(text)
        assert len(spans) == 1
        assert "Pérez Juan" in spans[0].text
        assert spans[0].metadata.get("preserve") is True
        assert spans[0].type == "AUTOR_JURISPRUDENCIA"

    def test_no_match_sin_comillas(self) -> None:
        text = "El caso es relevante para la jurisprudencia."
        assert detect_jurisprudencia_authors(text) == []


class TestDetectCitations:
    def test_devuelve_doctrina_y_jurisprudencia(self) -> None:
        text = (
            'Como sostiene CSJN, "García c/ Estado", 1/1/2020. '
            "Asimismo, ver doctrina de Lorenzetti, Tratado, 2010."
        )
        spans = detect_citations(text)
        types = {s.type for s in spans}
        assert "AUTOR_DOCTRINA" in types
        assert "AUTOR_JURISPRUDENCIA" in types

    def test_spans_tienen_source_structure(self) -> None:
        """Para que ganen el resolve_overlaps contra NER."""
        text = "ver doctrina de Alterini, 2019."
        for s in detect_citations(text):
            assert s.source == "structure"
