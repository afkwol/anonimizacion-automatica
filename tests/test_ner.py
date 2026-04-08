"""Tests del detector NER.

Si spaCy o el modelo `es_core_news_md` no están instalados, los tests
end-to-end se skipean. Hay además un test con mock que valida el camino
principal sin necesidad del modelo real.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.detect import ner
from app.detect.span import Span, resolve_overlaps

NER_AVAILABLE = ner.is_available()
requires_ner = pytest.mark.skipif(not NER_AVAILABLE, reason="spaCy es_core_news_md no disponible")


SAMPLE_LEGAL = (
    "En la ciudad de Córdoba, comparece el Sr. Juan Carlos Pérez, "
    "DNI 20.123.456, lo patrocina el Dr. Roberto Gómez. "
    "Se presenta ante el Juzgado Civil N° 5 a cargo de la "
    "Dra. María Elena Fernández. Cita doctrina de Atilio Aníbal Alterini."
)


@requires_ner
def test_detecta_personas_principales() -> None:
    spans = ner.detect_entities(SAMPLE_LEGAL)
    persons = [s.text for s in spans if s.type == "PER"]
    # Esperamos al menos 2 de los 4 nombres del fixture (modelo md no es perfecto).
    expected = ["Juan Carlos Pérez", "Roberto Gómez", "María Elena Fernández", "Atilio Aníbal Alterini"]
    found = sum(1 for name in expected if any(name in p for p in persons))
    assert found >= 2, f"NER detectó muy pocas personas: {persons}"


@requires_ner
def test_offsets_round_trip() -> None:
    spans = ner.detect_entities(SAMPLE_LEGAL)
    for s in spans:
        assert SAMPLE_LEGAL[s.start:s.end] == s.text


@requires_ner
def test_filtro_de_tipos() -> None:
    """Por default sólo devuelve PER. Pidiendo PER+LOC se obtiene más."""
    only_per = ner.detect_entities(SAMPLE_LEGAL, types=("PER",))
    per_loc = ner.detect_entities(SAMPLE_LEGAL, types=("PER", "LOC"))
    assert all(s.type == "PER" for s in only_per)
    assert len(per_loc) >= len(only_per)


@requires_ner
def test_no_devuelve_misc_ni_descartados() -> None:
    spans = ner.detect_entities(SAMPLE_LEGAL, types=("PER", "LOC", "ORG"))
    assert all(s.type in {"PER", "LOC", "ORG"} for s in spans)


@requires_ner
def test_texto_vacio() -> None:
    assert ner.detect_entities("") == []
    assert ner.detect_entities("   \n  ") == []


@requires_ner
def test_resolucion_de_solapamiento_con_regex() -> None:
    """Si NER detecta 'Juan Pérez' y regex detecta un DNI dentro,
    el resultado final no debe contener spans solapados."""
    from app.detect.regex_detectors import detect_all as detect_all_regex

    text = "El Sr. Juan Pérez, DNI 20.123.456, comparece."
    regex_spans = detect_all_regex(text)
    ner_spans = ner.detect_entities(text)
    combined = resolve_overlaps([*regex_spans, *ner_spans])
    # No deben quedar pares solapados.
    for i, a in enumerate(combined):
        for b in combined[i + 1:]:
            assert not a.overlaps(b)


# ============================ Path con mock ===============================
def test_detect_entities_con_mock_spacy() -> None:
    """Validación del path de mapeo PER/LOC/ORG sin necesidad del modelo real."""
    # Simulamos un Doc de spaCy con dos entities.
    mock_ent1 = MagicMock()
    mock_ent1.label_ = "PER"
    mock_ent1.text = "Juan Pérez"
    mock_ent1.start_char = 7
    mock_ent1.end_char = 17

    mock_ent2 = MagicMock()
    mock_ent2.label_ = "MISC"  # debe ser descartado
    mock_ent2.text = "algo"
    mock_ent2.start_char = 20
    mock_ent2.end_char = 24

    mock_doc = MagicMock()
    mock_doc.ents = [mock_ent1, mock_ent2]

    mock_nlp = MagicMock()
    mock_nlp.return_value = mock_doc
    # select_pipes context manager
    mock_nlp.select_pipes.return_value.__enter__ = MagicMock(return_value=None)
    mock_nlp.select_pipes.return_value.__exit__ = MagicMock(return_value=None)

    with patch.object(ner, "_load_model", return_value=mock_nlp):
        spans = ner.detect_entities("El Sr. Juan Pérez está")

    assert len(spans) == 1
    assert spans[0].type == "PER"
    assert spans[0].source == "ner"
    assert spans[0].text == "Juan Pérez"


def test_is_available_no_explota() -> None:
    """is_available nunca debe lanzar una excepción."""
    result = ner.is_available()
    assert isinstance(result, bool)


def test_import_error_si_spacy_falta() -> None:
    """Si spaCy no se puede importar, _load_model lanza ImportError claro."""
    ner._reset_model()
    with patch.dict("sys.modules", {"spacy": None}):
        with pytest.raises((ImportError, OSError)):
            ner._load_model("modelo_inexistente_xyz")
