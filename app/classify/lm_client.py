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
    model: str = "qwen3.5-9b"
    timeout_seconds: float = 120.0
    # Hiperparámetros — fijos por diseño para garantizar determinismo.
    temperature: float = 0.0
    top_p: float = 1.0
    top_k: int = 1
    # Default alto para modelos reasoning (qwen3, deepseek-r1, etc.) que
    # gastan tokens en `reasoning_content` antes de llegar al `content`.
    max_tokens: int = 8192


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
            # LM Studio rechaza `json_object`; sólo acepta `json_schema` o `text`.
            # Usamos `text` y confiamos en el parser Pydantic del clasificador
            # (con el SYSTEM_PROMPT explícito que pide JSON puro). El few-shot
            # del prompt + temperature=0 son suficientes en la práctica.
            payload["response_format"] = {"type": "text"}

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
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Estructura de respuesta inesperada: {data}") from exc

        if not content.strip():
            # Caso típico: modelo reasoning (qwen3, deepseek-r1) que gastó
            # todos los tokens pensando antes de emitir contenido.
            reasoning = message.get("reasoning_content") or ""
            if reasoning and finish_reason == "length":
                raise RuntimeError(
                    "LM Studio devolvió contenido vacío: el modelo cargado es "
                    "un reasoning model y se quedó sin tokens en la fase de "
                    "razonamiento. Subí `max_tokens` (>= 16384) o cargá un "
                    "modelo no-reasoning (instruct) en LM Studio."
                )
            raise RuntimeError(
                f"LM Studio devolvió contenido vacío (finish_reason={finish_reason})."
            )
        return content
