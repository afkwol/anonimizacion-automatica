"""Capa 3: heurística estructural — detección de zonas del documento.

Los escritos judiciales tienen una estructura muy predecible. Detectar
las zonas obvias antes de invocar al LLM clasificador (PR 7) tiene dos
beneficios:

1. **Ahorra llamadas al LLM** en casos obvios (toda persona dentro del
   bloque de firma es juez/secretario; toda persona dentro de una cita
   doctrinaria es autor de doctrina).

2. **Mejora la precisión final** porque el LLM recibe la zona como una
   pista contextual fuerte en la ficha, en vez de adivinar por el
   contexto plano de ±200 caracteres.

**Importante**: las zonas son **priors** que el LLM puede sobrescribir
cuando tiene alta confianza. El default_role es informativo, no
imperativo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


# Roles canónicos pre-asignables por estructura. El enum completo de
# roles vive en PR 7 (`app/classify/taxonomy.py`); acá usamos strings
# para no acoplar el detector a la taxonomía aún inexistente.
ZONE_CARATULA = "CARATULA"
ZONE_FIRMA = "FIRMA"
ZONE_CITA_DOCTRINA = "CITA_DOCTRINA"
ZONE_CITA_JURISPRUDENCIA = "CITA_JURISPRUDENCIA"


@dataclass
class Zone:
    """Una región del documento con un rol estructural identificado.

    Attributes:
        name: identificador de zona (CARATULA, FIRMA, CITA_DOCTRINA, ...).
        start: offset inicial absoluto.
        end: offset final exclusivo.
        default_role: rol sugerido para entidades dentro de la zona.
            El clasificador LLM (PR 7) lo recibe como prior.
        evidence: substring que disparó la detección (para auditoría).
    """

    name: str
    start: int
    end: int
    default_role: str
    evidence: str = ""

    def contains(self, offset: int) -> bool:
        return self.start <= offset < self.end


# ============================ CARÁTULA ===================================
# La carátula va del inicio del documento hasta el primer marcador que
# señala el cuerpo de la sentencia/escrito: "Y VISTOS:", "VISTO:",
# "RESULTA:", "I.- DEMANDA", etc. Si no encontramos ningún marcador,
# tomamos los primeros 800 caracteres como aproximación conservadora.
_CARATULA_END_MARKERS = re.compile(
    r"(?im)^\s*(?:Y\s+VISTOS?|VISTO|RESULTA|RESULTANDO|"
    r"I\s*[\.\-]\s*DEMANDA|I\s*[\.\-]\s*ANTECEDENTES|"
    r"AUTOS\s+Y\s+VISTOS|CONSIDERANDO)\s*[:.]",
)
_CARATULA_FALLBACK_CHARS = 800


def _detect_caratula(text: str) -> Optional[Zone]:
    if not text:
        return None
    match = _CARATULA_END_MARKERS.search(text)
    end = match.start() if match else min(_CARATULA_FALLBACK_CHARS, len(text))
    if end <= 0:
        return None
    return Zone(
        name=ZONE_CARATULA,
        start=0,
        end=end,
        default_role="PARTE",  # personas en carátula son partes (actor/demandado)
        evidence=match.group(0) if match else "(fallback 800 chars)",
    )


# ============================ FIRMA ======================================
# El bloque de firma final empieza con marcadores muy estables y va
# hasta el fin del documento. Los marcadores son:
#   - "PROTOCOLICESE", "PROTOCOLÍCESE"
#   - "Es copia"
#   - "Texto Firmado digitalmente por"
#   - "Firmado:"
#   - "Notif[ií]quese y arch[ií]vese"
_FIRMA_START_MARKERS = re.compile(
    r"(?i)("
    r"PROTOCOL[IÍ]CESE"
    r"|Es\s+copia(?:\s+fiel)?"
    r"|Texto\s+Firmado\s+digitalmente\s+por"
    r"|Firmado(?:\s+digitalmente)?\s*:"
    r"|Notif[ií]quese\s+y\s+arch[ií]vese"
    r"|REG[IÍ]STRESE\s+Y\s+NOTIF[IÍ]QUESE"
    r")",
)


def _detect_firma(text: str) -> Optional[Zone]:
    """Detecta el bloque de firma final.

    Importante: tomamos el ÚLTIMO match del marcador y exigimos que esté
    en el último 30% del documento. Esto evita falsos positivos cuando
    una sentencia menciona "PROTOCOLICESE" como cita interna o referencia
    a otro fallo (caso real visto en fixture NANZER).
    """
    matches = list(_FIRMA_START_MARKERS.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    # Restricción: el marcador de firma debe estar en el último 30% del
    # documento. Para docs muy cortos (<300 chars) no aplicamos la regla.
    threshold = int(len(text) * 0.7)
    if len(text) > 300 and last.start() < threshold:
        return None
    return Zone(
        name=ZONE_FIRMA,
        start=last.start(),
        end=len(text),
        default_role="JUEZ",  # personas firmantes son jueces/secretarios
        evidence=last.group(0),
    )


# ============================ CITAS DE JURISPRUDENCIA ====================
# Patrones típicos en escritos argentinos:
#   "CSJN, Fallos: 333:1234"
#   "CSJN, 'NOMBRE c/ NOMBRE', 12/3/2020"
#   "CNCiv., Sala A, ..."
#   "Cám. Civ. ...", "C. Nac. ..."
#   "TSJ Cba., Sala ..."
# Cada match genera una zona PEQUEÑA (~200 chars hacia adelante) porque
# la cita real raramente supera una oración.
_JURISPRUDENCIA_PATTERNS = re.compile(
    r"(?i)("
    r"CSJN[,\s]"
    r"|Fallos:\s*\d+"
    r"|CNCiv\."
    r"|CNCom\."
    r"|CNCrim\."
    r"|CNCAF\."
    r"|TSJ\s+\w+"
    r"|C[aá]m\.\s*(?:Civ|Com|Crim|Apel)"
    r"|C\.\s*Nac\.\s*(?:Civ|Com|Crim)"
    r")",
)
_JURISPRUDENCIA_WINDOW = 250  # caracteres hacia adelante desde el match


def _detect_jurisprudencia(text: str) -> List[Zone]:
    zones: List[Zone] = []
    for match in _JURISPRUDENCIA_PATTERNS.finditer(text):
        start = match.start()
        end = min(start + _JURISPRUDENCIA_WINDOW, len(text))
        # Truncar al primer "." que termine oración (si lo hay) para no
        # absorber el párrafo siguiente.
        dot = text.find(". ", start + 30, end)
        if dot != -1:
            end = dot + 1
        zones.append(
            Zone(
                name=ZONE_CITA_JURISPRUDENCIA,
                start=start,
                end=end,
                default_role="AUTOR_JURISPRUDENCIA",
                evidence=match.group(0),
            )
        )
    return zones


# ============================ CITAS DE DOCTRINA ==========================
# Patrones doctrinarios típicos:
#   "AUTOR, A., Obra, Editorial, año, p. NN"
#   "ver doctrina de AUTOR, ..."
#   "(conf. AUTOR, ...)"
# Y citas entre comillas tipográficas: "AUTOR" sostiene que...
# Aquí usamos heurísticas conservadoras: una coma seguida de un año
# entre 1900 y el actual, en una oración con un APELLIDO en mayúsculas
# o "ver doctrina"/"conf."/"cf.".
_DOCTRINA_TRIGGERS = re.compile(
    r"(?i)("
    r"ver\s+doctrina\s+de\s+"
    r"|conf\.\s+"
    r"|cf(?:r)?\.\s+"
    r"|seg[uú]n\s+ense[ñn]a\s+"
    r"|en\s+palabras\s+de\s+"
    r")",
)
_DOCTRINA_WINDOW = 200


def _detect_doctrina(text: str) -> List[Zone]:
    zones: List[Zone] = []
    for match in _DOCTRINA_TRIGGERS.finditer(text):
        start = match.start()
        end = min(start + _DOCTRINA_WINDOW, len(text))
        dot = text.find(". ", start + 20, end)
        if dot != -1:
            end = dot + 1
        zones.append(
            Zone(
                name=ZONE_CITA_DOCTRINA,
                start=start,
                end=end,
                default_role="AUTOR_DOCTRINA",
                evidence=match.group(0),
            )
        )
    return zones


# ============================ Orquestador =================================
def detect_zones(text: str) -> List[Zone]:
    """Devuelve todas las zonas estructurales detectadas en `text`.

    Las zonas pueden solaparse entre sí (ej: una cita doctrinaria dentro
    de la carátula). El consumidor (`assign_zone`) elige la más
    específica para un offset dado.
    """
    if not text:
        return []
    zones: List[Zone] = []
    caratula = _detect_caratula(text)
    if caratula is not None:
        zones.append(caratula)
    firma = _detect_firma(text)
    if firma is not None:
        zones.append(firma)
    zones.extend(_detect_jurisprudencia(text))
    zones.extend(_detect_doctrina(text))
    return zones


# Prioridad de zonas para resolución: las citas son MÁS específicas
# que la carátula o firma, así que ganan cuando hay solapamiento.
_ZONE_PRIORITY = {
    ZONE_CITA_DOCTRINA: 100,
    ZONE_CITA_JURISPRUDENCIA: 100,
    ZONE_FIRMA: 50,
    ZONE_CARATULA: 50,
}


def assign_zone(offset: int, zones: List[Zone]) -> Optional[Zone]:
    """Para un offset dado, devuelve la zona más específica que lo contiene.

    Si ninguna zona lo contiene, devuelve `None` (el span está en el
    cuerpo libre del documento y debe pasar al clasificador LLM normal).
    """
    candidates = [z for z in zones if z.contains(offset)]
    if not candidates:
        return None
    # Mayor prioridad gana; si empate, la zona más corta (más específica).
    candidates.sort(
        key=lambda z: (-_ZONE_PRIORITY.get(z.name, 0), z.end - z.start),
    )
    return candidates[0]
