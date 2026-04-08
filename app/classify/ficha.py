"""Construcción de fichas para el clasificador LLM.

Una **ficha** es la unidad mínima de información que el clasificador
necesita para asignarle un rol a una entidad detectada. Contiene:

- El texto exacto de la entidad.
- Una ventana de contexto a izquierda y derecha (±200 chars por default).
- Una pista de zona estructural (carátula/firma/cita) si aplica.
- El tipo NER que la detectó (PER/LOC/ORG) si vino de NER.

**Por qué fichas y no chunks**: el LLM no recibe el documento completo
ni siquiera oraciones completas. Recibe sólo lo necesario para
clasificar UNA entidad. Esto:

1. Hace **estructuralmente imposible** que el LLM altere o pierda texto
   no-PII (es la garantía central del enfoque B+C).
2. Reduce drásticamente los tokens por llamada — podemos batchear 20+
   fichas en una sola request.
3. Mantiene el contexto justo y necesario, sin distracciones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from app.detect.span import Span
from app.detect.structure import Zone, assign_zone


DEFAULT_CONTEXT_CHARS = 200


@dataclass
class Ficha:
    """Una entidad lista para ser clasificada por el LLM.

    Attributes:
        id: identificador interno usado para batching y matching de
            respuestas. NO se serializa al texto del prompt — el LLM
            recibe IDs separados.
        span: el `Span` original detectado por la capa regex/ner/structure.
            Mantenemos referencia para poder propagar el rol asignado de
            vuelta al span en `metadata`.
        text: texto de la entidad (igual a span.text, duplicado por
            comodidad y para no exponer todo el span al prompt).
        context_left: hasta `DEFAULT_CONTEXT_CHARS` caracteres antes del
            span en el texto fuente. Truncado al inicio del documento si
            el span está cerca del borde.
        context_right: idem hacia adelante.
        zone_hint: nombre de la zona estructural que contiene el span,
            si alguna (CARATULA, FIRMA, CITA_DOCTRINA, etc.).
        zone_default_role: rol pre-asignado por la zona (PARTE, JUEZ,
            AUTOR_DOCTRINA...). El LLM lo recibe como prior, pero puede
            sobreescribirlo si su confianza es alta.
        ner_type: tipo del detector NER (PER/LOC/ORG) si vino de ahí.
    """

    id: int
    span: Span
    text: str
    context_left: str
    context_right: str
    zone_hint: Optional[str] = None
    zone_default_role: Optional[str] = None
    ner_type: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_prompt_dict(self) -> dict:
        """Serializa la ficha en el formato que el prompt del LLM espera.

        Mantiene la representación compacta para minimizar tokens.
        """
        d = {
            "id": self.id,
            "entidad": self.text,
            "contexto_izq": self.context_left,
            "contexto_der": self.context_right,
        }
        if self.zone_hint:
            d["zona"] = self.zone_hint
        if self.zone_default_role:
            d["pista_rol"] = self.zone_default_role
        if self.ner_type:
            d["tipo_ner"] = self.ner_type
        return d


def build_fichas(
    spans: Sequence[Span],
    full_text: str,
    zones: Optional[Sequence[Zone]] = None,
    *,
    context_chars: int = DEFAULT_CONTEXT_CHARS,
) -> List[Ficha]:
    """Construye fichas para todos los spans clasificables.

    **Filtra automáticamente** los spans que NO necesitan clasificación:
    los de tipo regex (DNI, CUIT, CBU, EMAIL, TELEFONO, PATENTE, PASAPORTE)
    son identificadores numéricos y se anonimizan sin pasar por el LLM.
    Sólo las personas/lugares/organizaciones (NER + estructura) van a
    fichas.

    Args:
        spans: lista resuelta (sin solapamientos) de spans detectados.
        full_text: texto fuente completo, usado para extraer contexto.
        zones: zonas estructurales del documento (output de detect_zones).
        context_chars: tamaño de la ventana de contexto a cada lado.

    Returns:
        Lista de `Ficha`s, una por span clasificable, en el orden del
        texto. Los IDs son secuenciales empezando en 1.
    """
    if zones is None:
        zones = []
    fichas: List[Ficha] = []
    next_id = 1
    for span in spans:
        # Skip identificadores numéricos: van directo a anonimización.
        if span.source == "regex":
            continue

        ctx_start = max(0, span.start - context_chars)
        ctx_end = min(len(full_text), span.end + context_chars)
        context_left = full_text[ctx_start:span.start]
        context_right = full_text[span.end:ctx_end]

        # Asignar zona si hay
        zone = assign_zone(span.start, list(zones)) if zones else None

        ner_type = span.type if span.source == "ner" else None

        fichas.append(
            Ficha(
                id=next_id,
                span=span,
                text=span.text,
                context_left=context_left,
                context_right=context_right,
                zone_hint=zone.name if zone else None,
                zone_default_role=zone.default_role if zone else None,
                ner_type=ner_type,
            )
        )
        next_id += 1
    return fichas
