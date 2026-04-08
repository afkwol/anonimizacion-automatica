"""Tests del clasificador LLM (PR 7).

Cubre los 4 archivos: taxonomy, ficha, lm_client (con mock), llm_classifier.

**Garantías críticas verificadas**:
1. Una respuesta del LLM con un rol inválido → DESCONOCIDO (no excepción).
2. JSON malformado → fallback DESCONOCIDO para todo el batch.
3. Fichas omitidas por el LLM → DESCONOCIDO.
4. La lista de salida tiene exactamente el mismo largo que la entrada.
5. Fail-closed: DESCONOCIDO siempre se anonimiza por default.
6. Spans regex se filtran y no van al LLM (van directo a anonimización).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.classify.ficha import build_fichas
from app.classify.llm_classifier import (
    LLMClassifier,
    SYSTEM_PROMPT,
    get_placeholder_for_role,
)
from app.classify.lm_client import LMStudioClient, LMStudioConfig
from app.classify.taxonomy import (
    DEFAULT_ANONYMIZE_POLICY,
    AnonymizationPolicy,
    Role,
    parse_role_safe,
)
from app.detect.span import Span
from app.detect.structure import Zone


# ============================ taxonomy ===================================
class TestTaxonomy:
    def test_enum_cerrado_completo(self) -> None:
        # Que todos los roles del enum tengan política definida.
        for role in Role:
            assert role in DEFAULT_ANONYMIZE_POLICY

    def test_partes_se_anonimizan(self) -> None:
        for r in [Role.PARTE_ACTORA, Role.PARTE_DEMANDADA, Role.TESTIGO,
                  Role.VICTIMA, Role.MENOR, Role.LETRADO_PATROCINANTE]:
            assert DEFAULT_ANONYMIZE_POLICY[r] is True

    def test_jueces_y_autores_no_se_anonimizan(self) -> None:
        for r in [Role.JUEZ, Role.FISCAL, Role.SECRETARIO,
                  Role.AUTOR_DOCTRINA, Role.AUTOR_JURISPRUDENCIA,
                  Role.PERITO_OFICIAL, Role.FUNCIONARIO_PUBLICO]:
            assert DEFAULT_ANONYMIZE_POLICY[r] is False

    def test_desconocido_es_failclosed(self) -> None:
        """DESCONOCIDO debe anonimizarse siempre por default."""
        assert DEFAULT_ANONYMIZE_POLICY[Role.DESCONOCIDO] is True

    def test_parse_role_safe_validos(self) -> None:
        assert parse_role_safe("PARTE_ACTORA") == Role.PARTE_ACTORA
        assert parse_role_safe("juez") == Role.JUEZ  # case-insensitive
        assert parse_role_safe(" TESTIGO ") == Role.TESTIGO  # con whitespace

    def test_parse_role_safe_invalidos(self) -> None:
        """Cualquier string fuera del enum → DESCONOCIDO, sin excepciones."""
        assert parse_role_safe("ROL_INVENTADO") == Role.DESCONOCIDO
        assert parse_role_safe("") == Role.DESCONOCIDO
        assert parse_role_safe("123") == Role.DESCONOCIDO

    def test_policy_override(self) -> None:
        pol = AnonymizationPolicy()
        assert pol.should_anonymize(Role.ENTIDAD_PRIVADA) is False
        pol.set("ENTIDAD_PRIVADA", True)
        assert pol.should_anonymize(Role.ENTIDAD_PRIVADA) is True

    def test_policy_from_dict(self) -> None:
        pol = AnonymizationPolicy.from_dict({"JUEZ": True})
        assert pol.should_anonymize(Role.JUEZ) is True

    def test_policy_from_dict_rol_invalido(self) -> None:
        with pytest.raises(KeyError):
            AnonymizationPolicy.from_dict({"NO_EXISTE": True})


# ============================ ficha ======================================
class TestFicha:
    def test_skip_spans_regex(self) -> None:
        """Los spans regex (DNI, CUIT, etc.) NO generan fichas."""
        text = "El Sr. Juan Pérez, DNI 20.123.456, comparece"
        spans = [
            Span(start=7, end=17, text="Juan Pérez", type="PER", source="ner", confidence=0.7),
            Span(start=23, end=33, text="20.123.456", type="DNI", source="regex", confidence=1.0),
        ]
        fichas = build_fichas(spans, text)
        assert len(fichas) == 1
        assert fichas[0].text == "Juan Pérez"
        assert fichas[0].ner_type == "PER"

    def test_contexto_con_borde(self) -> None:
        """Si el span está cerca del inicio, el contexto izq se trunca."""
        text = "Juan Pérez es actor"
        spans = [Span(start=0, end=10, text="Juan Pérez", type="PER", source="ner")]
        fichas = build_fichas(spans, text, context_chars=200)
        assert fichas[0].context_left == ""
        assert fichas[0].context_right == " es actor"

    def test_zone_hint(self) -> None:
        """Si hay zonas, el ficha hereda zone_hint y default_role."""
        text = "PROTOCOLICESE. Texto Firmado digitalmente por Juan Pérez, juez."
        spans = [Span(start=46, end=56, text="Juan Pérez", type="PER", source="ner")]
        zones = [Zone(name="FIRMA", start=0, end=len(text), default_role="JUEZ", evidence="x")]
        fichas = build_fichas(spans, text, zones=zones)
        assert fichas[0].zone_hint == "FIRMA"
        assert fichas[0].zone_default_role == "JUEZ"

    def test_to_prompt_dict_compacto(self) -> None:
        """to_prompt_dict no incluye claves None para minimizar tokens."""
        text = "X Juan Pérez Y"
        spans = [Span(start=2, end=12, text="Juan Pérez", type="PER", source="ner")]
        fichas = build_fichas(spans, text)
        d = fichas[0].to_prompt_dict()
        assert "id" in d and "entidad" in d and "tipo_ner" in d
        assert "zona" not in d  # no había zona
        assert "pista_rol" not in d


# ============================ lm_client (con mock HTTP) ==================
class TestLMClient:
    def test_chat_payload_correcto(self) -> None:
        client = LMStudioClient(LMStudioConfig(model="test-model"))
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"resultados": []}'}}]
        }
        mock_response.raise_for_status = MagicMock()
        with patch("app.classify.lm_client.requests.post", return_value=mock_response) as post:
            result = client.chat("sys", "user", force_json=True)
        assert result == '{"resultados": []}'
        # Verificar payload
        call = post.call_args
        body = call.kwargs["json"]
        assert body["temperature"] == 0.0
        assert body["top_p"] == 1.0
        assert body["top_k"] == 1
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0]["role"] == "system"

    def test_chat_response_vacia_falla(self) -> None:
        client = LMStudioClient(LMStudioConfig(model="x"))
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": ""}}]}
        mock_response.raise_for_status = MagicMock()
        with patch("app.classify.lm_client.requests.post", return_value=mock_response):
            with pytest.raises(RuntimeError, match="vacío"):
                client.chat("s", "u")

    def test_health_check_robusto(self) -> None:
        """health_check devuelve bool, jamás raise."""
        client = LMStudioClient(LMStudioConfig())
        with patch("app.classify.lm_client.requests.get", side_effect=Exception("boom")):
            assert client.health_check() is False


# ============================ llm_classifier =============================
def _make_classifier_with_mock(response_text: str) -> tuple[LLMClassifier, MagicMock]:
    """Crea un classifier cuyo cliente devuelve un string fijo."""
    client = MagicMock(spec=LMStudioClient)
    client.chat.return_value = response_text
    classifier = LLMClassifier(client=client, batch_size=10, max_retries=0)
    return classifier, client


def _make_ficha(id_: int, text: str = "Juan Pérez") -> object:
    span = Span(start=0, end=len(text), text=text, type="PER", source="ner", confidence=0.7)
    from app.classify.ficha import Ficha
    return Ficha(id=id_, span=span, text=text, context_left="", context_right="")


class TestLLMClassifier:
    def test_clasificacion_basica(self) -> None:
        response = json.dumps({
            "resultados": [
                {"id": 1, "rol": "PARTE_ACTORA", "confianza": 0.9},
                {"id": 2, "rol": "JUEZ", "confianza": 0.95},
            ]
        })
        classifier, _ = _make_classifier_with_mock(response)
        fichas = [_make_ficha(1, "A"), _make_ficha(2, "B")]
        results = classifier.classify(fichas)
        assert len(results) == 2
        assert results[0].role == Role.PARTE_ACTORA
        assert results[0].anonymize is True
        assert results[1].role == Role.JUEZ
        assert results[1].anonymize is False

    def test_rol_fuera_del_enum_va_a_desconocido(self) -> None:
        response = json.dumps({
            "resultados": [
                {"id": 1, "rol": "ROL_INVENTADO_POR_EL_LLM", "confianza": 0.8},
            ]
        })
        classifier, _ = _make_classifier_with_mock(response)
        results = classifier.classify([_make_ficha(1)])
        assert results[0].role == Role.DESCONOCIDO
        assert results[0].anonymize is True  # fail-closed

    def test_json_malformado_fallback_completo(self) -> None:
        classifier, _ = _make_classifier_with_mock("esto no es json {}{ malformed")
        fichas = [_make_ficha(1), _make_ficha(2)]
        results = classifier.classify(fichas)
        assert len(results) == 2
        assert all(r.role == Role.DESCONOCIDO for r in results)
        assert all(r.anonymize is True for r in results)
        assert all(r.source == "fallback" for r in results)

    def test_ficha_omitida_por_llm_va_a_desconocido(self) -> None:
        """El LLM devuelve sólo 1 de 2 fichas. La omitida → DESCONOCIDO."""
        response = json.dumps({"resultados": [{"id": 1, "rol": "JUEZ", "confianza": 0.9}]})
        classifier, _ = _make_classifier_with_mock(response)
        results = classifier.classify([_make_ficha(1), _make_ficha(2)])
        assert len(results) == 2
        assert results[0].role == Role.JUEZ
        assert results[1].role == Role.DESCONOCIDO

    def test_garantia_de_largo_de_salida(self) -> None:
        """Para N fichas → siempre N resultados, en el mismo orden."""
        response = json.dumps({"resultados": []})  # LLM devuelve nada
        classifier, _ = _make_classifier_with_mock(response)
        fichas = [_make_ficha(i) for i in range(1, 6)]
        results = classifier.classify(fichas)
        assert len(results) == 5
        assert [r.ficha.id for r in results] == [1, 2, 3, 4, 5]

    def test_batching(self) -> None:
        """Con batch_size=2 y 5 fichas → 3 llamadas al cliente."""
        response = json.dumps({"resultados": []})
        classifier, client = _make_classifier_with_mock(response)
        classifier.batch_size = 2
        fichas = [_make_ficha(i) for i in range(1, 6)]
        classifier.classify(fichas)
        assert client.chat.call_count == 3

    def test_confianza_fuera_de_rango_es_clampeada(self) -> None:
        """Pydantic debería rechazar confianza > 1.0; el batch cae a fallback."""
        response = json.dumps({
            "resultados": [{"id": 1, "rol": "JUEZ", "confianza": 1.5}]
        })
        classifier, _ = _make_classifier_with_mock(response)
        results = classifier.classify([_make_ficha(1)])
        # Confianza inválida invalida el batch entero (Pydantic raise → fallback).
        assert results[0].role == Role.DESCONOCIDO

    def test_retries(self) -> None:
        """Si las primeras llamadas fallan, el classifier reintenta."""
        client = MagicMock(spec=LMStudioClient)
        client.chat.side_effect = [
            RuntimeError("network"),
            RuntimeError("network"),
            json.dumps({"resultados": [{"id": 1, "rol": "JUEZ", "confianza": 0.9}]}),
        ]
        classifier = LLMClassifier(client=client, max_retries=2, backoff_seconds=0.001)
        results = classifier.classify([_make_ficha(1)])
        assert client.chat.call_count == 3
        assert results[0].role == Role.JUEZ

    def test_retries_agotados_va_a_fallback(self) -> None:
        client = MagicMock(spec=LMStudioClient)
        client.chat.side_effect = RuntimeError("siempre falla")
        classifier = LLMClassifier(client=client, max_retries=1, backoff_seconds=0.001)
        results = classifier.classify([_make_ficha(1)])
        assert client.chat.call_count == 2
        assert results[0].role == Role.DESCONOCIDO
        assert results[0].source == "fallback"


def test_get_placeholder_for_role() -> None:
    assert get_placeholder_for_role(Role.PARTE_ACTORA, 1) == "[ACTOR_1]"
    assert get_placeholder_for_role(Role.TESTIGO, 3) == "[TESTIGO_3]"
    assert get_placeholder_for_role(Role.JUEZ) is None  # no se anonimiza
    assert get_placeholder_for_role(Role.AUTOR_DOCTRINA) is None
    assert get_placeholder_for_role(Role.DESCONOCIDO, 7) == "[PERSONA_7]"


def test_system_prompt_contiene_taxonomia_completa() -> None:
    """Sanity: el prompt menciona todos los roles del enum."""
    for role in Role:
        assert role.value in SYSTEM_PROMPT, f"Rol {role.value} no está en el prompt"
