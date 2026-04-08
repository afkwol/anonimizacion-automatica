"""Taxonomía cerrada de roles para anonimización judicial.

**Diseño clave**: el enum es **cerrado**. El LLM clasificador SOLO puede
devolver uno de estos valores. Cualquier respuesta fuera del enum se
mapea a `DESCONOCIDO` y queda registrado como warning. Esto elimina por
construcción la creatividad del modelo, que es la fuente principal de
alucinaciones.

**Política `anonymize` por default**: refleja la doctrina argentina
(Acordadas CSJN 15/13 y 24/13, Ley 25.326) sobre publicación de
jurisprudencia:

- **Sí anonimizar**: partes, testigos, víctimas, menores, familiares,
  letrados de las partes, peritos de parte. Es decir, todo aquel cuya
  identidad NO debería quedar expuesta en un documento publicable.
- **NO anonimizar**: jueces, fiscales, defensores oficiales, secretarios,
  peritos oficiales, autores de doctrina/jurisprudencia citados,
  funcionarios públicos, entidades públicas. Estos son personas o
  instituciones en función pública o citas a obras públicas.
- **Configurable**: entidades privadas. Por default no se anonimizan
  (los bancos, compañías, etc. suelen aparecer públicamente en
  jurisprudencia), pero es regla configurable.
- **DESCONOCIDO**: ante la duda, **se anonimiza**. Fail-closed: el costo
  de filtrar PII es mucho mayor que el costo de un placeholder de más.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict


class Role(str, Enum):
    """Enum cerrado de roles. Hereda de str para serialización JSON directa."""

    # Partes y allegados — siempre anonimizar
    PARTE_ACTORA = "PARTE_ACTORA"
    PARTE_DEMANDADA = "PARTE_DEMANDADA"
    TERCERO_CITADO = "TERCERO_CITADO"
    TESTIGO = "TESTIGO"
    VICTIMA = "VICTIMA"
    MENOR = "MENOR"
    FAMILIAR_DE_PARTE = "FAMILIAR_DE_PARTE"

    # Letrados de las partes — anonimizar
    LETRADO_PATROCINANTE = "LETRADO_PATROCINANTE"
    LETRADO_APODERADO = "LETRADO_APODERADO"

    # Funcionarios judiciales — NO anonimizar
    JUEZ = "JUEZ"
    FISCAL = "FISCAL"
    DEFENSOR_OFICIAL = "DEFENSOR_OFICIAL"
    SECRETARIO = "SECRETARIO"

    # Peritos
    PERITO_OFICIAL = "PERITO_OFICIAL"  # NO anonimizar
    PERITO_DE_PARTE = "PERITO_DE_PARTE"  # anonimizar

    # Citas — NO anonimizar
    AUTOR_DOCTRINA = "AUTOR_DOCTRINA"
    AUTOR_JURISPRUDENCIA = "AUTOR_JURISPRUDENCIA"

    # Entes y funcionarios públicos
    FUNCIONARIO_PUBLICO = "FUNCIONARIO_PUBLICO"  # NO
    ENTIDAD_PUBLICA = "ENTIDAD_PUBLICA"  # NO
    ENTIDAD_PRIVADA = "ENTIDAD_PRIVADA"  # configurable

    # Fallback de seguridad
    DESCONOCIDO = "DESCONOCIDO"  # anonimizar (fail-closed)


# Default: True = se anonimiza, False = se preserva.
# Esta tabla es lo que el pipeline consulta tras la clasificación.
DEFAULT_ANONYMIZE_POLICY: Dict[Role, bool] = {
    # Partes y allegados
    Role.PARTE_ACTORA: True,
    Role.PARTE_DEMANDADA: True,
    Role.TERCERO_CITADO: True,
    Role.TESTIGO: True,
    Role.VICTIMA: True,
    Role.MENOR: True,
    Role.FAMILIAR_DE_PARTE: True,
    # Letrados
    Role.LETRADO_PATROCINANTE: True,
    Role.LETRADO_APODERADO: True,
    # Funcionarios judiciales
    Role.JUEZ: False,
    Role.FISCAL: False,
    Role.DEFENSOR_OFICIAL: False,
    Role.SECRETARIO: False,
    # Peritos
    Role.PERITO_OFICIAL: False,
    Role.PERITO_DE_PARTE: True,
    # Citas
    Role.AUTOR_DOCTRINA: False,
    Role.AUTOR_JURISPRUDENCIA: False,
    # Entes
    Role.FUNCIONARIO_PUBLICO: False,
    Role.ENTIDAD_PUBLICA: False,
    Role.ENTIDAD_PRIVADA: False,
    # Fail-closed
    Role.DESCONOCIDO: True,
}


# Prefijo de placeholder por rol. Cuando se anonimiza una entidad, el
# PR 8 (coreferencia) la reemplaza por `[<PREFIX>_<N>]` donde N es el
# índice del cluster de coreferencia. Roles que no se anonimizan no
# tienen prefijo (no se reemplazan).
PLACEHOLDER_PREFIX: Dict[Role, str] = {
    Role.PARTE_ACTORA: "ACTOR",
    Role.PARTE_DEMANDADA: "DEMANDADO",
    Role.TERCERO_CITADO: "TERCERO",
    Role.TESTIGO: "TESTIGO",
    Role.VICTIMA: "VICTIMA",
    Role.MENOR: "MENOR",
    Role.FAMILIAR_DE_PARTE: "FAMILIAR",
    Role.LETRADO_PATROCINANTE: "LETRADO",
    Role.LETRADO_APODERADO: "LETRADO",
    Role.PERITO_DE_PARTE: "PERITO",
    Role.DESCONOCIDO: "PERSONA",
}


@dataclass
class AnonymizationPolicy:
    """Política de anonimización. Permite override por rol vía config.yaml.

    Uso típico:
        pol = AnonymizationPolicy()  # defaults
        pol.set("ENTIDAD_PRIVADA", True)  # forzar anonimizar entidades privadas
        if pol.should_anonymize(Role.LETRADO_PATROCINANTE):
            ...
    """

    overrides: Dict[Role, bool] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.overrides is None:
            self.overrides = {}

    def should_anonymize(self, role: Role) -> bool:
        if role in self.overrides:
            return self.overrides[role]
        return DEFAULT_ANONYMIZE_POLICY[role]

    def set(self, role: str | Role, anonymize: bool) -> None:
        """Override por rol. Acepta string o Role."""
        r = Role(role) if isinstance(role, str) else role
        self.overrides[r] = anonymize

    @classmethod
    def from_dict(cls, mapping: Dict[str, bool]) -> "AnonymizationPolicy":
        """Construye desde un dict (típicamente cargado de config.yaml).

        Strings desconocidos se ignoran con KeyError explícito para que
        un typo en el config falle ruidoso en lugar de ser silencioso.
        """
        overrides: Dict[Role, bool] = {}
        for k, v in mapping.items():
            try:
                overrides[Role(k)] = bool(v)
            except ValueError as exc:
                raise KeyError(f"Rol desconocido en política: {k!r}") from exc
        return cls(overrides=overrides)


def parse_role_safe(value: str) -> Role:
    """Convierte un string del LLM a Role; cualquier valor desconocido → DESCONOCIDO.

    Esta es la función que blinda el pipeline contra hallucinations del
    clasificador. NUNCA lanza excepción.
    """
    if not value:
        return Role.DESCONOCIDO
    try:
        return Role(value.strip().upper())
    except ValueError:
        return Role.DESCONOCIDO
