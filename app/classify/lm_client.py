"""Cliente minimal para LM Studio (OpenAI-compatible).

Reescritura limpia del cliente del legacy monolith. Diferencias:

- **Sin estado**: cada llamada construye su propio payload. Reentrante.
- **Determinismo forzado**: temperature=0, top_p=1, top_k=1 (los hardcoded
  del legacy se mantienen — son la garantía de reproducibilidad).
- **JSON mode opcional**: cuando está disponible, fuerza la respuesta a
  JSON válido del lado del servidor (LM Studio lo soporta vía
  `response_format`).
- **Timeout y reintentos por separado**: la lógica de retry queda en el
  caller (clasificador) porque la decisión de reintentar depende del
  tipo de fallo, no sólo del HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests


@dataclass
class LMStudioConfig:
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = "lm-studio"
    model: str = ""
    timeout_seconds: float = 120.0
    # Hiperparámetros — fijos por diseño para garantizar determinismo.
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 1
    max_tokens: int = 2048


class LMStudioClient:
    def __init__(self, config: LMStudioConfig) -> None:
        self.config = config
        self.headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

    def health_check(self) -> bool:
        """Verifica que el servidor responde. Devuelve bool, NUNCA raise.

        Captura `Exception` ancho a propósito: este método es usado como
        precondición de UI/CLI y no debe nunca tirar el programa.
        """
        try:
            r = requests.get(
                f"{self.config.base_url.rstrip('/')}/models",
                headers=self.headers,
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        force_json: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Envía un chat completion y devuelve el `content` de la respuesta.

        Args:
            system_prompt: instrucciones del sistema (taxonomía + reglas).
            user_prompt: payload del usuario (las fichas serializadas).
            force_json: si el servidor soporta `response_format=json_object`,
                lo activa para forzar JSON parseable.
            max_tokens: override del default.

        Returns:
            String con el contenido del mensaje del asistente. Lanza
            `RuntimeError` si la respuesta está vacía o malformada.
        """
        cfg = self.config
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "top_k": cfg.top_k,
            "max_tokens": max_tokens or cfg.max_tokens,
        }
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        try:
            r = requests.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                headers=self.headers,
                json=payload,
                timeout=cfg.timeout_seconds,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"Fallo HTTP contra LM Studio: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Respuesta no-JSON de LM Studio: {exc}") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Estructura de respuesta inesperada: {data}") from exc

        if not content or not content.strip():
            raise RuntimeError("LM Studio devolvió contenido vacío.")
        return content
