"""Coreferencia y asignación de placeholders estables.

**Objetivo**: que dos menciones de la misma entidad (ej: "Juan Pérez" y
"Pérez") reciban el MISMO placeholder a lo largo del documento. Esto:

1. Mantiene la legibilidad del documento anonimizado.
2. Permite al lector seguir quién es quién sin nombres reales.
3. Evita inconsistencias del LLM (que puede clasificar diferente la
   misma entidad si el contexto cambia).

**Estrategia**: agrupar por apellido normalizado (lowercase, sin
acentos, sin títulos). Si dos menciones del mismo cluster reciben
roles distintos del LLM, gana el de mayor confianza.

**Placeholder**: `[PREFIX_N]` donde PREFIX viene de la taxonomía
(ACTOR/DEMANDADO/TESTIGO/etc) y N es un contador estable por rol,
asignado en orden de primera aparición.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .llm_classifier import ClassificationResult, get_placeholder_for_role
from .taxonomy import PLACEHOLDER_PREFIX, Role

logger = logging.getLogger(__name__)


_TITLE_RE = re.compile(
    r"\b(?:dr|dra|sr|sra|srta|don|do[ñn]a|lic|ing|prof|cdor|cra)\.?\s+",
    re.IGNORECASE,
)
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_name(name: str) -> str:
    """Normaliza un nombre para clustering: lowercase, sin acentos, sin títulos."""
    if not name:
        return ""
    s = name.strip()
    s = _TITLE_RE.sub("", s)
    # Quitar acentos
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _NON_WORD_RE.sub(" ", s)
    s = " ".join(s.split())
    return s


def cluster_key(normalized: str) -> str:
    """Devuelve la clave de cluster: el último token (apellido) si hay >=2,
    o el string completo si es un solo token."""
    if not normalized:
        return ""
    tokens = normalized.split()
    if len(tokens) >= 2:
        return tokens[-1]
    return tokens[0]


@dataclass
class EntityCluster:
    """Un cluster de menciones que refieren a la misma persona/entidad."""

    key: str
    role: Role
    confidence: float
    placeholder: Optional[str]
    members: List[ClassificationResult] = field(default_factory=list)


@dataclass
class CoreferenceResolution:
    """Resultado completo de la resolución de coreferencia."""

    clusters: List[EntityCluster]
    # Mapping ficha.id -> placeholder (None si no se anonimiza)
    placeholder_by_ficha_id: Dict[int, Optional[str]] = field(default_factory=dict)
    # Mapping cluster_key -> placeholder (para auditoría)
    placeholder_by_key: Dict[str, Optional[str]] = field(default_factory=dict)


def resolve_coreference(
    results: Sequence[ClassificationResult],
) -> CoreferenceResolution:
    """Agrupa resultados por apellido normalizado y asigna placeholders estables.

    **Reglas**:
    - Las menciones se agrupan por `cluster_key(normalize_name(text))`.
    - Si dentro de un cluster los roles del LLM difieren, gana el de
      mayor `confidence`. Empates se resuelven por orden de aparición.
    - Los placeholders son `[PREFIX_N]` con N contado por rol en orden
      de primera aparición del cluster.
    - Las entidades cuyo rol no se anonimiza (ej: JUEZ, AUTOR_DOCTRINA)
      reciben placeholder=None y se preservan en el output.

    Args:
        results: lista de ClassificationResult del LLM (en orden del texto).

    Returns:
        CoreferenceResolution con clusters y mappings.
    """
    # Paso 1: agrupar por cluster_key, manteniendo orden de aparición.
    clusters_by_key: Dict[str, EntityCluster] = {}
    cluster_order: List[str] = []

    for r in results:
        norm = normalize_name(r.ficha.text)
        key = cluster_key(norm) or f"__singleton_{r.ficha.id}"
        if key not in clusters_by_key:
            clusters_by_key[key] = EntityCluster(
                key=key,
                role=r.role,
                confidence=r.confidence,
                placeholder=None,
            )
            cluster_order.append(key)
        clusters_by_key[key].members.append(r)

    # Paso 2: dentro de cada cluster, elegir el rol de mayor confianza.
    for cluster in clusters_by_key.values():
        if len(cluster.members) > 1:
            roles_seen = {m.role for m in cluster.members}
            if len(roles_seen) > 1:
                best = max(cluster.members, key=lambda m: m.confidence)
                if best.role != cluster.members[0].role:
                    logger.info(
                        "Coreference: cluster '%s' tiene roles distintos %s; "
                        "usando %s (confianza %.2f)",
                        cluster.key,
                        roles_seen,
                        best.role.value,
                        best.confidence,
                    )
                cluster.role = best.role
                cluster.confidence = best.confidence

    # Paso 3: asignar placeholders. Contadores por rol, en orden de aparición.
    counters: Dict[Role, int] = {}
    for key in cluster_order:
        cluster = clusters_by_key[key]
        prefix = PLACEHOLDER_PREFIX.get(cluster.role)
        if prefix is None:
            cluster.placeholder = None
            continue
        counters[cluster.role] = counters.get(cluster.role, 0) + 1
        cluster.placeholder = get_placeholder_for_role(
            cluster.role, counters[cluster.role]
        )

    # Paso 4: armar mappings.
    placeholder_by_ficha_id: Dict[int, Optional[str]] = {}
    placeholder_by_key: Dict[str, Optional[str]] = {}
    for key in cluster_order:
        cluster = clusters_by_key[key]
        placeholder_by_key[key] = cluster.placeholder
        for m in cluster.members:
            placeholder_by_ficha_id[m.ficha.id] = cluster.placeholder

    return CoreferenceResolution(
        clusters=[clusters_by_key[k] for k in cluster_order],
        placeholder_by_ficha_id=placeholder_by_ficha_id,
        placeholder_by_key=placeholder_by_key,
    )


def audit_log(resolution: CoreferenceResolution) -> List[Dict[str, object]]:
    """Genera el log de auditoría: una entrada por cluster.

    Cada entrada incluye: cluster_key, role, placeholder, confidence,
    n_menciones, sample (primer texto). NO incluye los textos completos
    de todas las menciones (eso quedaría en el log de detección).
    """
    log: List[Dict[str, object]] = []
    for c in resolution.clusters:
        sample = c.members[0].ficha.text if c.members else ""
        log.append(
            {
                "cluster_key": c.key,
                "role": c.role.value,
                "placeholder": c.placeholder,
                "confidence": round(c.confidence, 3),
                "n_menciones": len(c.members),
                "sample": sample,
            }
        )
    return log
