import copy
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pdfplumber
import requests
import yaml
from docx import Document
from difflib import HtmlDiff, SequenceMatcher
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ==========================
# 1. CONFIGURACIÓN Y UTILIDADES
# ==========================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

DEFAULT_CONFIG: Dict[str, Any] = {
    "lm_api": {
        "base_url": "http://127.0.0.1:1234/v1",
        "api_key": "lm-studio",
        "model": "granite-3.1-8b-instruct",
    },
    "chunking": {
        "max_context_tokens": 2500,
        "overlap_tokens": 0,
        "safety_factor": 0.85,
    },
    "inference": {
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": 1,
        "max_tokens": 1024,
        "repeat_penalty": 1.0,
        "stop_sequences": ["</s>"],
    },
    "runtime": {
        "logs_dir": "logs",
        "debug": False,
        "max_retries": 2,
        "retry_backoff_seconds": 2.0,
        "abort_on_failure": False,
    },
}

ENV_OVERRIDE_MAP: Dict[str, Tuple[Sequence[str], Callable[[str], Any]]] = {
    "LM_API_BASE": (("lm_api", "base_url"), str),
    "LM_API_KEY": (("lm_api", "api_key"), str),
    "LM_API_MODEL": (("lm_api", "model"), str),
    "CHUNK_MAX_TOKENS": (("chunking", "max_context_tokens"), int),
    "CHUNK_OVERLAP_TOKENS": (("chunking", "overlap_tokens"), int),
    "CHUNK_SAFETY_FACTOR": (("chunking", "safety_factor"), float),
    "LM_TEMPERATURE": (("inference", "temperature"), float),
    "LM_TOP_P": (("inference", "top_p"), float),
    "LM_TOP_K": (("inference", "top_k"), int),
    "LM_MAX_TOKENS": (("inference", "max_tokens"), int),
    "LM_REPEAT_PENALTY": (("inference", "repeat_penalty"), float),
    "LOGS_DIR": (("runtime", "logs_dir"), str),
    "DEBUG_MODE": (("runtime", "debug"), lambda v: v.lower() in {"1", "true", "yes"}),
    "MAX_RETRIES": (("runtime", "max_retries"), int),
    "RETRY_BACKOFF_SECONDS": (("runtime", "retry_backoff_seconds"), float),
    "ABORT_ON_FAILURE": (("runtime", "abort_on_failure"), lambda v: v.lower() in {"1", "true", "yes"}),
}


def deep_update(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_config(config_path: Path) -> Dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as cfg_file:
            user_config = yaml.safe_load(cfg_file) or {}
        deep_update(config, user_config)

    for env_key, (path, caster) in ENV_OVERRIDE_MAP.items():
        if env_key not in os.environ:
            continue
        raw_value = os.environ[env_key]
        try:
            cast_value = caster(raw_value)
        except Exception as exc:
            raise ValueError(f"No se pudo interpretar la variable de entorno {env_key}: {exc}") from exc

        target = config
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = cast_value

    return config


def save_config(config: Dict[str, Any], config_path: Path) -> None:
    with open(config_path, "w", encoding="utf-8") as cfg_file:
        yaml.safe_dump(config, cfg_file, sort_keys=False, allow_unicode=False)


def ensure_positive(value: int, name: str) -> int:
    if value <= 0:
        raise ValueError(f"{name} debe ser mayor que cero. Valor recibido: {value}")
    return value


def timestamp_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def resolve_logs_dir(runtime_cfg: Dict[str, Any]) -> Path:
    logs_dir = Path(runtime_cfg.get("logs_dir", "logs"))
    if not logs_dir.is_absolute():
        logs_dir = BASE_DIR / logs_dir
    return logs_dir


class RunLogger:
    def __init__(
        self,
        logs_dir: Path,
        run_id: str,
        ui_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.log_file = self.logs_dir / f"run_{run_id}.jsonl"
        self.summary_file = self.logs_dir / f"run_summary_{run_id}.json"
        self._chunk_entries: List[Dict[str, Any]] = []
        self.ui_callback = ui_callback

    def log_console(self, message: str, level: str = "INFO") -> None:
        line = f"[{timestamp_now()}] [{level}] {message}"
        print(line)
        if self.ui_callback:
            self.ui_callback(level, line)

    def log_chunk(self, entry: Dict[str, Any]) -> None:
        entry_with_ts = {"timestamp": datetime.now().isoformat(), **entry}
        self._chunk_entries.append(entry_with_ts)
        with open(self.log_file, "a", encoding="utf-8") as logf:
            logf.write(json.dumps(entry_with_ts, ensure_ascii=False) + "\n")

    def finalize(self, summary: Dict[str, Any]) -> None:
        summary_with_ts = {
            "timestamp": datetime.now().isoformat(),
            "chunks": self._chunk_entries,
            **summary,
        }
        with open(self.summary_file, "w", encoding="utf-8") as summary_file:
            json.dump(summary_with_ts, summary_file, ensure_ascii=False, indent=2)


class LMStudioClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        inference_params: Dict[str, Any],
        logger: RunLogger,
        max_retries: int,
        backoff_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.inference_params = inference_params.copy()
        self.inference_params["temperature"] = 0.0
        self.inference_params["top_p"] = 1.0
        self.inference_params["top_k"] = 1
        if "repeat_penalty" not in self.inference_params:
            self.inference_params["repeat_penalty"] = 1.0
        self.logger = logger
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _chat_payload(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        payload.update(self.inference_params)
        if "stop_sequences" in payload:
            payload["stop"] = payload.pop("stop_sequences")
        return payload

    def check_health(self) -> None:
        try:
            response = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectionError(
                "No se pudo contactar el servidor de LM Studio. "
                "Asegúrate de que está en ejecución y el endpoint es correcto."
            ) from exc

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        payload = self._chat_payload(system_prompt, user_prompt)
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                data = response.json()

                if not data.get("choices"):
                    raise ValueError("La respuesta del servidor no contiene 'choices'.")

                content = data["choices"][0]["message"]["content"]
                if content and content.strip():
                    return content
                raise ValueError("La respuesta del modelo está vacía o sólo contiene espacios.")

            except (requests.RequestException, ValueError, KeyError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    wait_time = self.backoff_seconds * (attempt + 1)
                    self.logger.log_console(
                        f"Intento {attempt + 1}/{self.max_retries} fallido. Reintentando en {wait_time:.1f}s. Error: {exc}",
                        level="WARN",
                    )
                    time.sleep(wait_time)
                else:
                    break

        assert last_error is not None
        raise RuntimeError(f"No se pudo obtener respuesta del modelo tras varios intentos: {last_error}") from last_error


@dataclass
class TokenSpan:
    token: str
    start: int
    end: int


@dataclass
class Chunk:
    index: int
    total: int
    text: str
    char_start: int
    char_end: int
    token_start: int
    token_end: int

    @property
    def token_length(self) -> int:
        return self.token_end - self.token_start

    def preview(self, max_chars: int = 120) -> str:
        preview_text = self.text[:max_chars].replace("\n", " ").strip()
        return preview_text + ("..." if len(self.text) > max_chars else "")


# ==========================
# 2. EXTRACCIÓN DE TEXTO
# ==========================
def extract_text_from_pdf(file_path: Path) -> str:
    with pdfplumber.open(str(file_path)) as pdf:
        pages_text = []
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                pages_text.append(page_text)
    return "\n".join(pages_text)


def extract_text_from_docx(file_path: Path) -> str:
    doc = Document(str(file_path))
    return "\n".join(para.text for para in doc.paragraphs)


# ==========================
# 3. TOKENIZACIÓN Y TROCEO
# ==========================
TOKEN_PATTERN = re.compile(r"\S+\s*")


def tokenize_with_spans(text: str) -> List[TokenSpan]:
    spans: List[TokenSpan] = []
    cursor = 0

    for match in TOKEN_PATTERN.finditer(text):
        if match.start() > cursor:
            whitespace_chunk = text[cursor:match.start()]
            spans.append(TokenSpan(token=whitespace_chunk, start=cursor, end=match.start()))
        spans.append(TokenSpan(token=match.group(0), start=match.start(), end=match.end()))
        cursor = match.end()

    if cursor < len(text):
        spans.append(TokenSpan(token=text[cursor:], start=cursor, end=len(text)))

    return spans


def build_chunks(text: str, max_tokens: int, overlap_tokens: int, safety_factor: float) -> List[Chunk]:
    token_spans = tokenize_with_spans(text)
    total_tokens = len(token_spans)

    effective_chunk_tokens = max(1, int(max_tokens * safety_factor))
    if overlap_tokens > 0:
        raise ValueError("El solapamiento entre chunks no está soportado actualmente. Configura overlap_tokens en 0.")

    ensure_positive(effective_chunk_tokens, "max_context_tokens ajustado")

    chunks: List[Chunk] = []
    token_start = 0
    chunk_index = 1

    while token_start < total_tokens:
        token_end = min(token_start + effective_chunk_tokens, total_tokens)
        chunk_spans = token_spans[token_start:token_end]
        char_start = chunk_spans[0].start
        char_end = chunk_spans[-1].end
        chunk_text = text[char_start:char_end]

        chunks.append(
            Chunk(
                index=chunk_index,
                total=0,
                text=chunk_text,
                char_start=char_start,
                char_end=char_end,
                token_start=token_start,
                token_end=token_end,
            )
        )

        if token_end >= total_tokens:
            break

        token_start = token_end

        chunk_index += 1

    total_chunks = len(chunks)
    for chunk in chunks:
        chunk.total = total_chunks

    validate_chunk_sequence(chunks, total_tokens)
    return chunks


def validate_chunk_sequence(chunks: Sequence[Chunk], total_tokens: int) -> None:
    if not chunks:
        raise ValueError("No se generaron chunks para el documento.")

    if chunks[0].token_start != 0:
        raise ValueError("El primer chunk no comienza en el inicio del documento.")

    if chunks[-1].token_end != total_tokens:
        raise ValueError("El último chunk no cubre hasta el final del documento.")

    previous_end = 0
    for chunk in chunks:
        if chunk.token_start > previous_end:
            raise ValueError("Se detectó un hueco entre chunks.")
        previous_end = max(previous_end, chunk.token_end)


# ==========================
# 4. PROCESAMIENTO DE CHUNKS
# ==========================
SYSTEM_PROMPT = (
    "Eres un asistente especializado en anonimizar documentos legales.\n"
    "Actúa con precisión absoluta: copia el texto íntegro y solo modifica los datos sensibles."
    "\n\n"
    "Reemplazos obligatorios (aplica a cada ocurrencia, respetando mayúsculas/minúsculas del resto del texto):\n"
    "- Nombres completos de personas (demandantes, demandados, testigos, letrados, funcionarios) -> [NOMBRE APELLIDO]\n"
    "- Domicilios o direcciones -> [DOMICILIO]\n"
    "- Identificadores personales (DNI, CUIL, CUIT, pasaportes, etc.) -> [DOCUMENTO]\n"
    "- Teléfonos -> [TELEFONO]\n"
    "- Correos electrónicos -> [EMAIL]\n"
    "- Cuentas bancarias, CBU, alias, números de tarjeta -> [CUENTA BANCARIA]\n"
    "\n"
    "Reglas estrictas:\n"
    "1. No borres ni agregues contenido distinto de los placeholders anteriores.\n"
    "2. Conserva el orden de oraciones, cifras, signos, saltos de línea y formato del texto original.\n"
    "3. Mantén todos los montos, fechas, números de expediente u otros valores numéricos que no sean identificadores personales.\n"
    "4. No reformules ni resumas. Cada palabra que no sea dato sensible debe permanecer igual.\n"
    "5. Si dudas, deja el texto tal cual. No inventes, no completes, no expliques.\n"
    "6. Si el fragmento no contiene datos sensibles, devuélvelo idéntico.\n"
    "\n"
    "Ejemplo:\n"
    "Original: \"Juan Pérez, DNI 30.123.456, vive en Av. Siempre Viva 742.\"\n"
    "Anonimizado: \"[NOMBRE APELLIDO], [DOCUMENTO], vive en [DOMICILIO].\"\n"
    "\n"
    "Devuelve solamente el texto anonimizado sin comentarios adicionales."
)

PLACEHOLDER_TOKENS = [
    "[NOMBRE APELLIDO]",
    "[DOMICILIO]",
    "[DOCUMENTO]",
    "[TELEFONO]",
    "[EMAIL]",
    "[CUENTA BANCARIA]",
]


def build_user_prompt(chunk: Chunk) -> str:
    return chunk.text


def process_chunks(
    chunks: Sequence[Chunk],
    client: LMStudioClient,
    logger: RunLogger,
    runtime_config: Dict[str, Any],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[str], List[int]]:
    results: List[str] = [""] * len(chunks)
    failed_chunks: List[int] = []
    abort_on_failure = runtime_config.get("abort_on_failure", False)
    debug_mode = runtime_config.get("debug", False)

    total = len(chunks)
    if progress_callback:
        progress_callback(0, total)

    for chunk in chunks:
        logger.log_console(
            f"Procesando chunk {chunk.index}/{chunk.total} "
            f"(caracteres: {len(chunk.text)}, tokens ~ {chunk.token_length})."
        )

        user_prompt = build_user_prompt(chunk)
        start_time = time.time()
        response = ""

        try:
            response = client.generate(SYSTEM_PROMPT, user_prompt)
            duration = time.time() - start_time
            snippet = response[:120].replace("\n", " ").strip()

            logger.log_chunk(
                {
                    "chunk_index": chunk.index,
                    "total_chunks": chunk.total,
                    "char_length": len(chunk.text),
                    "output_char_length": len(response),
                    "char_delta": len(response) - len(chunk.text),
                    "length_ratio": round(len(response) / len(chunk.text), 4) if len(chunk.text) else None,
                    "token_length": chunk.token_length,
                    "duration_seconds": round(duration, 3),
                    "status": "ok",
                    "input_preview": chunk.preview(),
                    "output_preview": snippet + ("..." if len(response) > 120 else ""),
                }
            )

            if debug_mode:
                logger.log_chunk(
                    {
                        "chunk_index": chunk.index,
                        "total_chunks": chunk.total,
                        "status": "debug",
                        "raw_input": chunk.text,
                        "raw_output": response,
                    }
                )

            results[chunk.index - 1] = response

        except Exception as exc:
            duration = time.time() - start_time
            logger.log_console(f"Error en chunk {chunk.index}: {exc}", level="ERROR")
            logger.log_chunk(
                {
                    "chunk_index": chunk.index,
                    "total_chunks": chunk.total,
                    "char_length": len(chunk.text),
                    "output_char_length": len(response),
                    "char_delta": len(response) - len(chunk.text),
                    "length_ratio": round(len(response) / len(chunk.text), 4) if len(chunk.text) else None,
                    "token_length": chunk.token_length,
                    "duration_seconds": round(duration, 3),
                    "status": "error",
                    "error_message": str(exc),
                }
            )
            failed_chunks.append(chunk.index)
            if abort_on_failure:
                if progress_callback:
                    progress_callback(chunk.index, total)
                raise

        if progress_callback:
            progress_callback(chunk.index, total)

    return results, failed_chunks


# ==========================
# 5. FUSIÓN DE RESULTADOS
# ==========================
def merge_chunks(chunks: Sequence[Chunk], processed_chunks: Sequence[str]) -> str:
    if not chunks or not processed_chunks:
        return ""

    merged_parts: List[str] = []
    covered_ranges: List[Tuple[int, int]] = []

    for chunk, processed in zip(chunks, processed_chunks):
        start = chunk.char_start
        end = chunk.char_end

        if any(not (end <= existing_start or start >= existing_end) for existing_start, existing_end in covered_ranges):
            continue

        covered_ranges.append((start, end))
        merged_parts.append(processed if processed else chunk.text)

    return "".join(merged_parts)


# ==========================
# 6. PIPELINE DE ANONIMIZACIÓN
# ==========================
def calculate_length_metrics(original_text: str, anonymized_text: str) -> Dict[str, Any]:
    original_length = len(original_text)
    anonymized_length = len(anonymized_text)
    ratio = anonymized_length / original_length if original_length else None
    delta = anonymized_length - original_length
    metrics = {
        "original_length": original_length,
        "anonymized_length": anonymized_length,
        "delta": delta,
        "ratio": ratio,
    }
    return metrics


def generate_diff_report(original_text: str, anonymized_text: str, output_path: Path) -> None:
    html_diff = HtmlDiff(wrapcolumn=80)
    original_lines = original_text.splitlines()
    anonymized_lines = anonymized_text.splitlines()
    diff_html = html_diff.make_file(original_lines, anonymized_lines, "Original", "Anonimizado")
    with open(output_path, "w", encoding="utf-8") as diff_file:
        diff_file.write(diff_html)


def remove_placeholders(text: str) -> str:
    cleaned = text
    for token in PLACEHOLDER_TOKENS:
        cleaned = cleaned.replace(token, "")
    return cleaned


def contains_placeholder(text: str) -> bool:
    return any(token in text for token in PLACEHOLDER_TOKENS)


def detect_suspicious_edits(
    original_text: str,
    anonymized_text: str,
    max_items: int = 10,
) -> List[Dict[str, str]]:
    matcher = SequenceMatcher(None, original_text, anonymized_text)
    suspicious: List[Dict[str, str]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        original_segment = original_text[i1:i2]
        anonymized_segment = anonymized_text[j1:j2]

        if not original_segment.strip() and not anonymized_segment.strip():
            continue

        if tag in {"replace", "insert"}:
            if contains_placeholder(anonymized_segment) and not remove_placeholders(anonymized_segment).strip():
                continue

        if tag == "delete":
            if not original_segment.strip():
                continue

        suspicious.append(
            {
                "type": tag,
                "original": original_segment.strip(),
                "anon": anonymized_segment.strip(),
            }
        )

        if len(suspicious) >= max_items:
            break

    return suspicious


# ==========================
# 6. PIPELINE DE ANONIMIZACIÓN
# ==========================
def run_anonymization(
    file_path: Path,
    config: Dict[str, Any],
    logger: RunLogger,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Any]:
    logger.log_console(f"Iniciando anonimización para: {file_path}")

    runtime_cfg = config.get("runtime", {})
    chunk_cfg = config.get("chunking", {})
    inference_cfg = config.get("inference", {})
    lm_cfg = config.get("lm_api", {})

    start_time = time.time()
    text = ""
    chunks: List[Chunk] = []
    processed_chunks: List[str] = []
    failed_chunks: List[int] = []
    final_document = ""
    output_file = file_path.parent / f"{file_path.stem}_anonimizado.txt"
    diff_report_file = file_path.parent / f"{file_path.stem}_comparacion.html"

    summary: Dict[str, Any] = {
        "run_id": logger.run_id,
        "source_file": str(file_path),
        "output_file": None,
        "diff_report_file": None,
        "total_chunks": 0,
        "failed_chunks": [],
        "processing_seconds": 0.0,
        "status": "pending",
        "length_metrics": {},
        "validation": {"status": "pending", "issues": []},
    }

    try:
        client = LMStudioClient(
            base_url=lm_cfg["base_url"],
            api_key=lm_cfg["api_key"],
            model=lm_cfg["model"],
            inference_params=inference_cfg.copy(),
            logger=logger,
            max_retries=runtime_cfg.get("max_retries", 2),
            backoff_seconds=runtime_cfg.get("retry_backoff_seconds", 2.0),
        )
        logger.log_console("Verificando conexión con LM Studio...")
        client.check_health()
        logger.log_console("Conexión con LM Studio verificada.")

        ext = file_path.suffix.lower()
        logger.log_console("Extrayendo texto del documento...")
        if ext == ".pdf":
            text = extract_text_from_pdf(file_path)
        elif ext in {".doc", ".docx"}:
            text = extract_text_from_docx(file_path)
        else:
            raise ValueError(f"Formato de archivo no soportado: {ext}")

        if not text.strip():
            raise ValueError("El archivo está vacío o no se pudo extraer texto legible.")

        logger.log_console(f"Caracteres extraídos: {len(text)}")

        chunks = build_chunks(
            text=text,
            max_tokens=ensure_positive(chunk_cfg["max_context_tokens"], "max_context_tokens"),
            overlap_tokens=max(0, chunk_cfg["overlap_tokens"]),
            safety_factor=float(chunk_cfg.get("safety_factor", 0.85)),
        )
        logger.log_console(f"Documento dividido en {len(chunks)} chunks.")

        processed_chunks, failed_chunks = process_chunks(
            chunks=chunks,
            client=client,
            logger=logger,
            runtime_config=runtime_cfg,
            progress_callback=progress_callback,
        )

        logger.log_console("Unificando chunks procesados...")
        final_document = merge_chunks(chunks, processed_chunks)
        logger.log_console(f"Documento final: {len(final_document)} caracteres.")

        with open(output_file, "w", encoding="utf-8") as output:
            output.write(final_document)
        logger.log_console(f"Documento anonimizado guardado en: {output_file}")

        length_metrics = calculate_length_metrics(text, final_document)
        summary["length_metrics"] = length_metrics
        ratio_msg = (
            f"Relación de longitud anonimizado/original: {length_metrics['ratio']:.2%}"
            if length_metrics["ratio"] is not None
            else "Relación de longitud no disponible."
        )
        logger.log_console(
            f"Caracteres originales: {length_metrics['original_length']}, "
            f"anonimizados: {length_metrics['anonymized_length']}, "
            f"diferencia: {length_metrics['delta']}. {ratio_msg}"
        )

        try:
            generate_diff_report(text, final_document, diff_report_file)
            summary["diff_report_file"] = str(diff_report_file)
            logger.log_console(f"Reporte de comparación generado en: {diff_report_file}")
        except Exception as diff_exc:
            logger.log_console(f"No se pudo generar el reporte de comparación: {diff_exc}", level="WARN")

        suspicious_edits = detect_suspicious_edits(text, final_document)
        if suspicious_edits:
            summary["validation"] = {"status": "warn", "issues": suspicious_edits}
            logger.log_console(
                f"Validación automática: se detectaron {len(suspicious_edits)} diferencias no esperadas.",
                level="WARN",
            )
            for issue in suspicious_edits[:3]:
                logger.log_console(
                    " - "
                    f"tipo={issue['type']}, original='{issue['original'][:80]}', "
                    f"anonimizado='{issue['anon'][:80]}'",
                    level="WARN",
                )
        else:
            summary["validation"] = {"status": "ok", "issues": []}
            logger.log_console("Validación automática: sin diferencias no permitidas detectadas.")

        summary.update(
            {
                "output_file": str(output_file),
                "diff_report_file": summary["diff_report_file"],
                "total_chunks": len(chunks),
                "failed_chunks": failed_chunks,
                "processing_seconds": round(time.time() - start_time, 2),
                "status": "success",
            }
        )

    except Exception as exc:
        logger.log_console(f"Proceso interrumpido: {exc}", level="ERROR")
        summary.update(
            {
                "output_file": summary["output_file"],
                "total_chunks": len(chunks),
                "failed_chunks": failed_chunks,
                "processing_seconds": round(time.time() - start_time, 2),
                "status": "error",
                "error_message": str(exc),
            }
        )
        if not summary.get("validation"):
            summary["validation"] = {"status": "error", "issues": []}
    finally:
        logger.finalize(summary)

    return summary


# ==========================
# 7. INTERFAZ GRÁFICA
# ==========================
class AnonymizerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Anonimizador de Documentos Legales")
        self.geometry("980x720")
        self.minsize(920, 660)

        self.config_data = load_config(CONFIG_PATH)
        self.processing_thread: Optional[threading.Thread] = None
        self.log_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.run_in_progress = False

        self.file_path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Selecciona un archivo y ajusta la configuración si es necesario.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_label_var = tk.StringVar(value="Progreso: 0%")
        self.summary_var = tk.StringVar(value="")

        self.config_controls: Dict[Tuple[str, ...], Dict[str, Any]] = {}

        self._build_ui()
        self.after(200, self._process_log_queue)

    # --- Construcción de UI ---
    def _build_ui(self) -> None:
        self.style = ttk.Style(self)
        self.style.configure("TButton", padding=6)
        self.style.configure("TLabel", padding=4)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.notebook = notebook

        self.tab_processing = ttk.Frame(notebook)
        self.tab_config = ttk.Frame(notebook)
        self.tab_logs = ttk.Frame(notebook)
        self.tab_about = ttk.Frame(notebook)

        notebook.add(self.tab_processing, text="Procesamiento")
        notebook.add(self.tab_config, text="Configuración")
        notebook.add(self.tab_logs, text="Registros")
        notebook.add(self.tab_about, text="Acerca de")

        self._build_processing_tab()
        self._build_config_tab()
        self._build_logs_tab()
        self._build_about_tab()

    def _build_processing_tab(self) -> None:
        frame = self.tab_processing
        frame.columnconfigure(0, weight=1)

        file_frame = ttk.LabelFrame(frame, text="Archivo de entrada")
        file_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        file_frame.columnconfigure(1, weight=1)

        ttk.Label(file_frame, text="Ruta del archivo:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        entry = ttk.Entry(file_frame, textvariable=self.file_path_var, state="readonly")
        entry.grid(row=0, column=1, sticky="ew", padx=6, pady=6)

        ttk.Button(file_frame, text="Examinar...", command=self._select_file).grid(row=0, column=2, padx=6, pady=6)

        control_frame = ttk.LabelFrame(frame, text="Ejecución")
        control_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=10)
        control_frame.columnconfigure(0, weight=1)

        ttk.Label(control_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w", padx=6, pady=6)

        progress = ttk.Progressbar(
            control_frame, maximum=100, variable=self.progress_var, mode="determinate"
        )
        progress.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        self.progress_bar = progress

        ttk.Label(control_frame, textvariable=self.progress_label_var).grid(row=2, column=0, sticky="w", padx=6, pady=2)

        buttons_frame = ttk.Frame(control_frame)
        buttons_frame.grid(row=3, column=0, sticky="ew", padx=6, pady=6)
        buttons_frame.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(buttons_frame, text="Iniciar anonimización", command=self._start_processing)
        self.start_button.grid(row=0, column=0, padx=6, pady=4)

        ttk.Button(buttons_frame, text="Limpiar resumen", command=self._clear_summary).grid(
            row=0, column=1, sticky="e", padx=6, pady=4
        )

        summary_frame = ttk.LabelFrame(frame, text="Resultado")
        summary_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.rowconfigure(0, weight=1)

        summary_text = tk.Text(summary_frame, height=6, wrap="word", state="disabled")
        summary_text.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        summary_scroll = ttk.Scrollbar(summary_frame, orient="vertical", command=summary_text.yview)
        summary_scroll.grid(row=0, column=1, sticky="ns")
        summary_text.configure(yscrollcommand=summary_scroll.set)
        self.summary_text = summary_text

    def _build_config_tab(self) -> None:
        canvas = tk.Canvas(self.tab_config, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(self.tab_config, orient="vertical", command=canvas.yview)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        config_frame = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=config_frame, anchor="nw")

        config_frame.bind(
            "<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        sections = [
            (
                "LM Studio",
                [
                    (("lm_api", "base_url"), "URL base", str),
                    (("lm_api", "api_key"), "API key", str),
                    (("lm_api", "model"), "Modelo", str),
                ],
            ),
            (
                "Troceo",
                [
                    (("chunking", "max_context_tokens"), "Tokens máximos por chunk", int),
                    (("chunking", "overlap_tokens"), "Tokens de solapamiento", int),
                    (("chunking", "safety_factor"), "Factor de seguridad", float),
                ],
            ),
            (
                "Parámetros del modelo",
                [
                    (("inference", "temperature"), "Temperatura", float),
                    (("inference", "top_p"), "Top P", float),
                    (("inference", "top_k"), "Top K", int),
                    (("inference", "max_tokens"), "Max tokens de salida", int),
                    (("inference", "repeat_penalty"), "Penalización repetición", float),
                    (("inference", "stop_sequences"), "Secuencias de parada (una por línea)", list),
                ],
            ),
            (
                "Tiempo de ejecución",
                [
                    (("runtime", "logs_dir"), "Directorio de logs", str),
                    (("runtime", "debug"), "Modo debug", bool),
                    (("runtime", "max_retries"), "Reintentos", int),
                    (("runtime", "retry_backoff_seconds"), "Backoff entre reintentos (s)", float),
                    (("runtime", "abort_on_failure"), "Abortar ante fallos", bool),
                ],
            ),
        ]

        for section_title, fields in sections:
            section_frame = ttk.LabelFrame(config_frame, text=section_title)
            section_frame.pack(fill="x", padx=10, pady=10)
            section_frame.columnconfigure(1, weight=1)

            for row, (path, label, field_type) in enumerate(fields):
                ttk.Label(section_frame, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=6)

                if field_type == bool:
                    var = tk.BooleanVar()
                    widget = ttk.Checkbutton(section_frame, variable=var)
                    widget.grid(row=row, column=1, sticky="w", padx=6, pady=6)
                    self.config_controls[path] = {"type": field_type, "var": var}
                elif field_type == list:
                    text_widget = tk.Text(section_frame, height=4, wrap="word")
                    text_widget.grid(row=row, column=1, sticky="ew", padx=6, pady=6)
                    scroll = ttk.Scrollbar(section_frame, orient="vertical", command=text_widget.yview)
                    scroll.grid(row=row, column=2, sticky="ns")
                    text_widget.configure(yscrollcommand=scroll.set)
                    self.config_controls[path] = {"type": field_type, "widget": text_widget}
                else:
                    var = tk.StringVar()
                    entry = ttk.Entry(section_frame, textvariable=var)
                    entry.grid(row=row, column=1, sticky="ew", padx=6, pady=6)
                    self.config_controls[path] = {"type": field_type, "var": var}

        button_frame = ttk.Frame(config_frame)
        button_frame.pack(fill="x", padx=10, pady=10)

        ttk.Button(button_frame, text="Recargar desde archivo", command=self._reload_config).pack(
            side="left", padx=6
        )
        ttk.Button(button_frame, text="Guardar configuración", command=self._save_config).pack(
            side="right", padx=6
        )

        self._populate_config_form()

    def _build_logs_tab(self) -> None:
        frame = self.tab_logs
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=6)
        button_frame.columnconfigure(1, weight=1)

        ttk.Button(button_frame, text="Abrir carpeta de logs", command=self._open_logs_dir).grid(
            row=0, column=0, padx=6
        )
        ttk.Button(button_frame, text="Limpiar vista", command=self._clear_log_view).grid(
            row=0, column=1, sticky="e", padx=6
        )

        text = tk.Text(frame, wrap="none", state="disabled")
        text.grid(row=1, column=0, sticky="nsew", padx=10, pady=6)

        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        y_scroll.grid(row=1, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        x_scroll.grid(row=2, column=0, sticky="ew", padx=10)
        text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.log_text = text

    def _build_about_tab(self) -> None:
        frame = self.tab_about
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        about_text = (
            "Anonimizador de documentos legales\n\n"
            "Este aplicativo procesa archivos PDF y Word de forma local, dividiéndolos en fragmentos "
            "para enviarlos a un modelo de lenguaje alojado en LM Studio. Cada fragmento se anonimiza "
            "siguiendo reglas estrictas y luego se recompone para generar un documento final.\n\n"
            "Características destacadas:\n"
            "- Configuración editable desde archivo YAML o interfaz gráfica.\n"
            "- Procesamiento con seguimiento en tiempo real y registros persistentes.\n"
            "- Integración con LM Studio mediante API compatible con OpenAI.\n"
            "- Diseño modular pensado para ser extensible y auditable.\n\n"
            "Recuerda mantener LM Studio en ejecución y con el servidor local habilitado antes de iniciar un proceso."
        )

        text_widget = tk.Text(frame, wrap="word", state="disabled")
        text_widget.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        text_widget.configure(state="normal")
        text_widget.insert("1.0", about_text)
        text_widget.configure(state="disabled")

        scroll = ttk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text_widget.configure(yscrollcommand=scroll.set)

    # --- Configuración ---
    def _get_nested_value(self, data: Dict[str, Any], path: Tuple[str, ...]) -> Any:
        node = data
        for key in path:
            node = node[key]
        return node

    def _populate_config_form(self) -> None:
        for path, meta in self.config_controls.items():
            value = self._get_nested_value(self.config_data, path)
            if meta["type"] == bool:
                meta["var"].set(bool(value))
            elif meta["type"] == list:
                widget = meta["widget"]
                widget.delete("1.0", "end")
                if value:
                    widget.insert("1.0", "\n".join(str(item) for item in value))
            else:
                meta["var"].set(str(value))

    def _build_config_from_form(self) -> Dict[str, Any]:
        new_config = copy.deepcopy(self.config_data)

        for path, meta in self.config_controls.items():
            target = new_config
            for key in path[:-1]:
                target = target.setdefault(key, {})
            field_type = meta["type"]

            try:
                if field_type == bool:
                    target[path[-1]] = bool(meta["var"].get())
                elif field_type == list:
                    raw = meta["widget"].get("1.0", "end").strip()
                    if not raw:
                        target[path[-1]] = []
                    else:
                        parts = [item.strip() for item in re.split(r"[,\n]+", raw) if item.strip()]
                        target[path[-1]] = parts
                elif field_type == int:
                    target[path[-1]] = int(meta["var"].get())
                elif field_type == float:
                    target[path[-1]] = float(meta["var"].get())
                else:
                    target[path[-1]] = meta["var"].get().strip()
            except ValueError:
                raise ValueError(f"Revisa el campo {' > '.join(path)}: el valor debe ser de tipo {field_type.__name__}.")

        return new_config

    def _save_config(self) -> None:
        try:
            updated_config = self._build_config_from_form()
        except ValueError as exc:
            messagebox.showerror("Configuración inválida", str(exc))
            return

        save_config(updated_config, CONFIG_PATH)
        self.config_data = load_config(CONFIG_PATH)
        self._populate_config_form()
        messagebox.showinfo("Configuración guardada", "La configuración se guardó correctamente.")

    def _reload_config(self) -> None:
        self.config_data = load_config(CONFIG_PATH)
        self._populate_config_form()
        messagebox.showinfo("Configuración recargada", "Se recargaron los valores desde el archivo.")

    # --- Acciones de procesamiento ---
    def _select_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Selecciona un archivo",
            filetypes=[("Documentos", "*.pdf *.doc *.docx")],
        )
        if file_path:
            self.file_path_var.set(file_path)
            self.status_var.set("Archivo listo para procesar.")

    def _start_processing(self) -> None:
        if self.run_in_progress:
            return

        file_path_str = self.file_path_var.get()
        if not file_path_str:
            messagebox.showwarning("Archivo requerido", "Selecciona un archivo antes de iniciar el proceso.")
            return

        if not Path(file_path_str).exists():
            messagebox.showerror("Archivo no encontrado", "La ruta seleccionada no existe.")
            return

        try:
            current_config = self._build_config_from_form()
        except ValueError as exc:
            messagebox.showerror("Configuración inválida", str(exc))
            return

        self.config_data = current_config
        self._prepare_run_ui()

        self.run_in_progress = True
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_dir = resolve_logs_dir(self.config_data.get("runtime", {}))
        logger = RunLogger(
            logs_dir=logs_dir,
            run_id=run_id,
            ui_callback=self._enqueue_log_message,
        )

        def worker() -> None:
            try:
                summary = run_anonymization(
                    file_path=Path(file_path_str),
                    config=self.config_data,
                    logger=logger,
                    progress_callback=self._enqueue_progress,
                )
            except Exception as exc:  # pragma: no cover
                summary = {
                    "run_id": run_id,
                    "source_file": file_path_str,
                    "status": "error",
                    "error_message": str(exc),
                }
            self.log_queue.put(("done", summary))

        self.processing_thread = threading.Thread(target=worker, daemon=True)
        self.processing_thread.start()

    def _prepare_run_ui(self) -> None:
        self.progress_var.set(0.0)
        self.progress_label_var.set("Progreso: 0%")
        self.status_var.set("Procesando...")
        self.start_button.configure(state="disabled")
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.configure(state="disabled")

    def _clear_summary(self) -> None:
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.configure(state="disabled")
        self.status_var.set("Resumen limpiado.")

    # --- Gestores de logs y progreso ---
    def _enqueue_log_message(self, level: str, message: str) -> None:
        self.log_queue.put(("log", level, message))

    def _enqueue_progress(self, current: int, total: int) -> None:
        self.log_queue.put(("progress", current, total))

    def _process_log_queue(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                kind = item[0]

                if kind == "log":
                    _, level, message = item
                    self._append_log(message)
                elif kind == "progress":
                    _, current, total = item
                    self._update_progress(current, total)
                elif kind == "done":
                    _, summary = item
                    self._handle_completion(summary)
        except queue.Empty:
            pass
        finally:
            self.after(200, self._process_log_queue)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log_view(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _update_progress(self, current: int, total: int) -> None:
        if total <= 0:
            return
        percentage = min(100.0, max(0.0, (current / total) * 100.0))
        self.progress_var.set(percentage)
        self.progress_label_var.set(f"Progreso: {percentage:.1f}% ({current}/{total} chunks)")

    def _handle_completion(self, summary: Dict[str, Any]) -> None:
        self.start_button.configure(state="normal")
        self.run_in_progress = False
        status = summary.get("status", "error")

        if status == "success":
            self.status_var.set("Proceso completado correctamente.")
            self._update_progress(summary.get("total_chunks", 0), summary.get("total_chunks", 1))
        else:
            self.status_var.set("Proceso finalizado con errores.")

        lines = [
            f"Run ID: {summary.get('run_id', '-') }",
            f"Archivo origen: {summary.get('source_file', '-') }",
            f"Estado: {summary.get('status', '-') }",
            f"Chunks procesados: {summary.get('total_chunks', 0)}",
            f"Chunks con error: {', '.join(map(str, summary.get('failed_chunks', []))) or 'Ninguno'}",
            f"Tiempo total (s): {summary.get('processing_seconds', 0)}",
            f"Archivo de salida: {summary.get('output_file', '-') }",
        ]

        metrics = summary.get("length_metrics") or {}
        if metrics:
            ratio = metrics.get("ratio")
            ratio_text = f"{ratio:.2%}" if isinstance(ratio, (float, int)) and ratio is not None else "N/D"
            lines.extend(
                [
                    f"Caracteres originales: {metrics.get('original_length', 0)}",
                    f"Caracteres anonimizados: {metrics.get('anonymized_length', 0)}",
                    f"Diferencia: {metrics.get('delta', 0)}",
                    f"Relación anon/orig: {ratio_text}",
                ]
            )

        validation = summary.get("validation") or {}
        if validation:
            lines.append(f"Validación automática: {validation.get('status', 'N/D')}")
            issues = validation.get("issues") or []
            for issue in issues[:3]:
                original_snippet = issue.get("original", "")[:80]
                anonymized_snippet = issue.get("anon", "")[:80]
                lines.append(
                    " - "
                    f"tipo={issue.get('type', '-')}, original='{original_snippet}', "
                    f"anon='{anonymized_snippet}'"
                )
            if len(issues) > 3:
                lines.append(f" - ... {len(issues) - 3} diferencias adicionales")

        diff_report_path = summary.get("diff_report_file")
        if diff_report_path:
            lines.append(f"Reporte de comparación: {diff_report_path}")

        if "error_message" in summary:
            lines.append(f"Error: {summary['error_message']}")

        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", "\n".join(lines))
        self.summary_text.configure(state="disabled")

        if status != "success":
            messagebox.showerror("Anonimización con errores", summary.get("error_message", "Error no especificado."))
        elif validation.get("status") == "warn":
            messagebox.showwarning(
                "Validación con observaciones",
                "Se detectaron diferencias no esperadas. Revisa el reporte de comparación.",
            )

    # --- Utilidades de GUI ---
    def _open_logs_dir(self) -> None:
        logs_dir = resolve_logs_dir(self.config_data.get("runtime", {}))
        try:
            if sys.platform.startswith("win"):
                os.startfile(logs_dir)  # type: ignore[attr-defined]
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(logs_dir)])
            else:
                subprocess.Popen(["xdg-open", str(logs_dir)])
        except Exception as exc:
            messagebox.showerror("No se pudo abrir la carpeta", str(exc))


# ==========================
# 8. EJECUCIÓN DE LA APLICACIÓN
# ==========================
def main() -> None:
    app = AnonymizerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
