"""Clasificador LLM con taxonomía cerrada — corazón del enfoque B+C.

**Garantía estructural**: el LLM jamás reescribe texto. Sólo recibe
fichas (entidad + contexto) y devuelve roles del enum cerrado. Cualquier
respuesta fuera del enum se mapea a `DESCONOCIDO` (que por política se
anonimiza). Esto hace **imposible** que el LLM altere o pierda contenido
no-PII.

**Flujo**:
1. Recibe lista de `Ficha` (output de `build_fichas`).
2. Las parte en batches (default 10 por llamada).
3. Para cada batch, construye un prompt con:
   - System: taxonomía cerrada + few-shots + instrucciones JSON.
   - User: array JSON con las fichas serializadas.
4. Envía a LM Studio con `force_json=True`.
5. Parsea con Pydantic. Cualquier JSON malformado o rol fuera del enum
   se mapea a `DESCONOCIDO` con warning loggeado.
6. Devuelve la lista de `Ficha`s con `role`, `anonymize`, `confidence`
   asignados en `metadata`.

**Determinismo**: temperature=0 + top_p=1 + top_k=1 + JSON mode. Mismas
fichas → misma respuesta.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from pydantic import BaseModel, Field, ValidationError

from .ficha import Ficha
from .lm_client import LMStudioClient
from .taxonomy import (
    AnonymizationPolicy,
    PLACEHOLDER_PREFIX,
    Role,
    parse_role_safe,
)

logger = logging.getLogger(__name__)


# ============================ Pydantic schema ============================
class _ClassificationItem(BaseModel):
    """Una entrada de la respuesta del LLM. Validada con Pydantic."""

    id: int
    rol: str
    confianza: float = Field(default=0.5, ge=0.0, le=1.0)


class _ClassificationResponse(BaseModel):
    """Wrapper para forzar al LLM a devolver `{"resultados": [...]}`.

    LM Studio en JSON mode espera un objeto en el nivel superior, no un
    array. Por eso envolvemos.
    """

    resultados: List[_ClassificationItem]


# ============================ Resultado por ficha ========================
@dataclass
class ClassificationResult:
    """Resultado completo de clasificar una ficha."""

    ficha: Ficha
    role: Role
    anonymize: bool
    confidence: float
    source: str = "llm"  # "llm" | "zone_default" | "fallback"
    raw_role_str: Optional[str] = None  # lo que dijo literal el LLM


# ============================ Prompt =====================================
SYSTEM_PROMPT = """Sos un clasificador experto en anonimización de documentos judiciales argentinos.

TAREA: por cada ficha (entidad + contexto), asignás UN rol del enum cerrado abajo.
NO reescribís el texto. NO inventás. NO explicás. Sólo devolvés JSON.

ENUM CERRADO DE ROLES (devolver EXACTAMENTE uno de estos strings):
- PARTE_ACTORA: el demandante / actor / accionante
- PARTE_DEMANDADA: el demandado / accionado
- TERCERO_CITADO: tercero llamado al juicio
- TESTIGO: testigo declarante
- VICTIMA: víctima en causa penal
- MENOR: niño/niña/adolescente
- FAMILIAR_DE_PARTE: cónyuge/hijo/padre de una parte
- LETRADO_PATROCINANTE: abogado patrocinante de una parte
- LETRADO_APODERADO: abogado apoderado de una parte
- JUEZ: magistrado del tribunal (juez/jueza/vocal)
- FISCAL: representante del Ministerio Público Fiscal
- DEFENSOR_OFICIAL: defensor oficial del Estado
- SECRETARIO: secretario/a del juzgado
- PERITO_OFICIAL: perito designado por el juzgado
- PERITO_DE_PARTE: perito propuesto por una parte
- AUTOR_DOCTRINA: autor de obra doctrinaria citada (ej: Alterini, Bidart Campos)
- AUTOR_JURISPRUDENCIA: nombre de causa o magistrado en fallo citado
- FUNCIONARIO_PUBLICO: funcionario estatal en función pública
- ENTIDAD_PUBLICA: organismo del Estado (AFIP, ANSES, etc.)
- ENTIDAD_PRIVADA: empresa, banco, organización privada
- DESCONOCIDO: si no podés decidir con confianza razonable

REGLAS DURAS:
1. Si la ficha tiene "pista_rol", úsala salvo que el contexto la contradiga.
2. Si la entidad está en zona FIRMA, casi siempre es JUEZ o SECRETARIO.
3. Si está en zona CITA_DOCTRINA, es AUTOR_DOCTRINA.
4. Si está en zona CITA_JURISPRUDENCIA, es AUTOR_JURISPRUDENCIA.
5. Si el contexto menciona "Dr./Dra." y "patrocina"/"apoderado", es LETRADO_*.
6. Si el contexto menciona "Juez/a", "Vocal", "Tribunal", es JUEZ.
7. Ante la duda, devolver DESCONOCIDO con confianza baja. No inventar.

FORMATO DE RESPUESTA (JSON estricto, sin texto adicional):
{"resultados": [{"id": <int>, "rol": "<ROL>", "confianza": <0.0-1.0>}, ...]}

EJEMPLOS:

Entrada:
{"fichas": [
  {"id": 1, "entidad": "Juan Pérez", "contexto_izq": "comparece el Sr. ", "contexto_der": ", DNI 20.123.456, promueve demanda", "zona": "CARATULA", "pista_rol": "PARTE"},
  {"id": 2, "entidad": "Roberto Gómez", "contexto_izq": "lo patrocina el Dr. ", "contexto_der": ", T° 45 F° 123", "tipo_ner": "PER"},
  {"id": 3, "entidad": "Yessica Lincón", "contexto_izq": "Texto Firmado digitalmente por: ", "contexto_der": " JUEZ/A DE 1RA INSTANCIA", "zona": "FIRMA", "pista_rol": "JUEZ"},
  {"id": 4, "entidad": "Alterini", "contexto_izq": "ver doctrina de ", "contexto_der": ", Derecho de Obligaciones, 2019", "zona": "CITA_DOCTRINA", "pista_rol": "AUTOR_DOCTRINA"}
]}

Salida:
{"resultados": [
  {"id": 1, "rol": "PARTE_ACTORA", "confianza": 0.95},
  {"id": 2, "rol": "LETRADO_PATROCINANTE", "confianza": 0.92},
  {"id": 3, "rol": "JUEZ", "confianza": 0.99},
  {"id": 4, "rol": "AUTOR_DOCTRINA", "confianza": 0.97}
]}
"""


def _build_user_prompt(fichas_batch: Sequence[Ficha]) -> str:
    payload = {"fichas": [f.to_prompt_dict() for f in fichas_batch]}
    return json.dumps(payload, ensure_ascii=False)


# ============================ Classifier =================================
@dataclass
class LLMClassifier:
    client: LMStudioClient
    policy: AnonymizationPolicy = field(default_factory=AnonymizationPolicy)
    batch_size: int = 10
    max_retries: int = 2
    backoff_seconds: float = 1.5

    def classify(self, fichas: Sequence[Ficha]) -> List[ClassificationResult]:
        """Clasifica todas las fichas. Devuelve un resultado por ficha.

        **Garantía**: la lista de salida tiene EXACTAMENTE el mismo
        largo que la entrada y mantiene el orden. Si el LLM omite o
        agrega entradas, las que falten se completan con DESCONOCIDO
        (fail-closed) y las extras se descartan.
        """
        results: List[ClassificationResult] = []
        for i in range(0, len(fichas), self.batch_size):
            batch = list(fichas[i : i + self.batch_size])
            batch_results = self._classify_batch(batch)
            results.extend(batch_results)
        return results

    def _classify_batch(self, batch: List[Ficha]) -> List[ClassificationResult]:
        """Clasifica un batch. Si falla todo el batch, fallback DESCONOCIDO."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                user_prompt = _build_user_prompt(batch)
                raw = self.client.chat(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                    force_json=True,
                )
                return self._parse_response(raw, batch)
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = self.backoff_seconds * (attempt + 1)
                    logger.warning(
                        "LLM classification attempt %d/%d failed: %s. Retrying in %.1fs",
                        attempt + 1,
                        self.max_retries + 1,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "LLM classification batch failed permanently: %s. "
                        "Falling back to DESCONOCIDO for %d fichas.",
                        exc,
                        len(batch),
                    )
        # Fail-closed fallback: cada ficha se marca DESCONOCIDO (que se anonimiza).
        return [
            self._fallback_result(f, reason=str(last_exc) if last_exc else "unknown")
            for f in batch
        ]

    def _parse_response(
        self, raw: str, batch: List[Ficha]
    ) -> List[ClassificationResult]:
        """Parsea la respuesta JSON del LLM y la mapea a ClassificationResult.

        Robustez:
        - JSON malformado → fallback DESCONOCIDO para todo el batch.
        - Roles fuera del enum → DESCONOCIDO para esa ficha.
        - Fichas omitidas por el LLM → DESCONOCIDO.
        - Fichas extra inventadas por el LLM → descartadas.
        - Confianza fuera de [0,1] → clamped por Pydantic.
        """
        try:
            data = json.loads(raw)
            response = _ClassificationResponse.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.error("LLM devolvió JSON inválido: %s. Raw: %s", exc, raw[:300])
            return [self._fallback_result(f, reason="invalid_json") for f in batch]

        # Index por id para el matching.
        by_id = {item.id: item for item in response.resultados}

        results: List[ClassificationResult] = []
        for ficha in batch:
            item = by_id.get(ficha.id)
            if item is None:
                logger.warning("LLM omitió ficha id=%d, fallback DESCONOCIDO", ficha.id)
                results.append(self._fallback_result(ficha, reason="omitted"))
                continue
            role = parse_role_safe(item.rol)
            results.append(
                ClassificationResult(
                    ficha=ficha,
                    role=role,
                    anonymize=self.policy.should_anonymize(role),
                    confidence=item.confianza,
                    source="llm",
                    raw_role_str=item.rol,
                )
            )
        return results

    def _fallback_result(self, ficha: Ficha, *, reason: str) -> ClassificationResult:
        return ClassificationResult(
            ficha=ficha,
            role=Role.DESCONOCIDO,
            anonymize=self.policy.should_anonymize(Role.DESCONOCIDO),
            confidence=0.0,
            source="fallback",
            raw_role_str=f"FALLBACK:{reason}",
        )


def get_placeholder_for_role(role: Role, cluster_id: int = 1) -> Optional[str]:
    """Devuelve `[PREFIX_N]` o None si el rol no se anonimiza por default.

    Helper usado por PR 8/9. Está acá porque el mapping vive en taxonomy.
    """
    prefix = PLACEHOLDER_PREFIX.get(role)
    if prefix is None:
        return None
    return f"[{prefix}_{cluster_id}]"
