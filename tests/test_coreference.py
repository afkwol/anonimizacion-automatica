"""Tests de coreferencia y placeholders estables (PR 8)."""
from __future__ import annotations

from app.classify.coreference import (
    audit_log,
    cluster_key,
    normalize_name,
    resolve_coreference,
)
from app.classify.ficha import Ficha
from app.classify.llm_classifier import ClassificationResult
from app.classify.taxonomy import Role
from app.detect.span import Span


def _result(id_: int, text: str, role: Role, conf: float = 0.9) -> ClassificationResult:
    span = Span(start=0, end=len(text), text=text, type="PER", source="ner", confidence=0.7)
    ficha = Ficha(id=id_, span=span, text=text, context_left="", context_right="")
    return ClassificationResult(
        ficha=ficha,
        role=role,
        anonymize=role != Role.JUEZ,
        confidence=conf,
        source="llm",
    )


class TestNormalize:
    def test_normaliza_acentos_y_titulos(self) -> None:
        assert normalize_name("Dr. Juan Pérez") == "juan perez"
        assert normalize_name("Sra. María Ñandú") == "maria nandu"
        assert normalize_name("DON JOSÉ") == "jose"

    def test_quita_puntuacion(self) -> None:
        assert normalize_name("J. Pérez,") == "j perez"

    def test_string_vacio(self) -> None:
        assert normalize_name("") == ""

    def test_cluster_key_apellido(self) -> None:
        assert cluster_key("juan perez") == "perez"
        assert cluster_key("juan carlos perez") == "perez"
        assert cluster_key("madonna") == "madonna"


class TestResolveCoreference:
    def test_mismo_apellido_mismo_placeholder(self) -> None:
        results = [
            _result(1, "Juan Pérez", Role.PARTE_ACTORA),
            _result(2, "Pérez", Role.PARTE_ACTORA),
            _result(3, "Sr. Pérez", Role.PARTE_ACTORA),
        ]
        res = resolve_coreference(results)
        ph = res.placeholder_by_ficha_id
        assert ph[1] == ph[2] == ph[3] == "[ACTOR_1]"
        assert len(res.clusters) == 1

    def test_diferentes_personas_diferentes_placeholders(self) -> None:
        results = [
            _result(1, "Juan Pérez", Role.PARTE_ACTORA),
            _result(2, "Roberto Gómez", Role.PARTE_DEMANDADA),
            _result(3, "Pérez", Role.PARTE_ACTORA),
        ]
        res = resolve_coreference(results)
        ph = res.placeholder_by_ficha_id
        assert ph[1] == ph[3] == "[ACTOR_1]"
        assert ph[2] == "[DEMANDADO_1]"

    def test_juez_no_recibe_placeholder(self) -> None:
        results = [_result(1, "Yessica Lincón", Role.JUEZ)]
        res = resolve_coreference(results)
        assert res.placeholder_by_ficha_id[1] is None

    def test_consistencia_gana_mayor_confianza(self) -> None:
        # Mismo apellido, dos roles distintos: gana el de mayor confianza.
        results = [
            _result(1, "Pérez", Role.TESTIGO, conf=0.4),
            _result(2, "Juan Pérez", Role.PARTE_ACTORA, conf=0.95),
        ]
        res = resolve_coreference(results)
        assert res.clusters[0].role == Role.PARTE_ACTORA
        assert res.placeholder_by_ficha_id[1] == "[ACTOR_1]"
        assert res.placeholder_by_ficha_id[2] == "[ACTOR_1]"

    def test_contadores_por_rol_independientes(self) -> None:
        results = [
            _result(1, "Pérez", Role.PARTE_ACTORA),
            _result(2, "Gómez", Role.PARTE_ACTORA),
            _result(3, "López", Role.TESTIGO),
            _result(4, "Díaz", Role.TESTIGO),
        ]
        res = resolve_coreference(results)
        ph = res.placeholder_by_ficha_id
        assert ph[1] == "[ACTOR_1]"
        assert ph[2] == "[ACTOR_2]"
        assert ph[3] == "[TESTIGO_1]"
        assert ph[4] == "[TESTIGO_2]"

    def test_audit_log(self) -> None:
        results = [
            _result(1, "Juan Pérez", Role.PARTE_ACTORA),
            _result(2, "Pérez", Role.PARTE_ACTORA),
        ]
        res = resolve_coreference(results)
        log = audit_log(res)
        assert len(log) == 1
        entry = log[0]
        assert entry["role"] == "PARTE_ACTORA"
        assert entry["placeholder"] == "[ACTOR_1]"
        assert entry["n_menciones"] == 2

    def test_desconocido_recibe_placeholder_persona(self) -> None:
        results = [_result(1, "X Y", Role.DESCONOCIDO)]
        res = resolve_coreference(results)
        assert res.placeholder_by_ficha_id[1] == "[PERSONA_1]"
