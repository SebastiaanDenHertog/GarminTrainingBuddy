from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("garmin-embedded-ai")

_GIB = 1024**3
_MODEL_LOCK = threading.Lock()
_MODEL: Any | None = None
_TOKENIZER: Any | None = None
_INPUT_DEVICE: Any | None = None
_LOADED_MODEL_NAME: str | None = None


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _read_int(path: str | Path) -> int | None:
    raw = _read_text(path)
    if raw is None or raw.lower() == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _host_memory_bytes() -> tuple[int | None, int | None]:
    """Return Linux host total and currently available memory."""
    raw = _read_text("/proc/meminfo")
    if raw is None:
        return None, None

    values: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        match = re.search(r"(\d+)", value)
        if match:
            values[key] = int(match.group(1)) * 1024

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = sum(
            values.get(name, 0)
            for name in ("MemFree", "Buffers", "Cached", "SReclaimable")
        ) or None
    return total, available


def _cgroup_memory_bytes() -> tuple[int | None, int | None, str | None]:
    """Return cgroup limit, remaining bytes and detected cgroup version."""
    # cgroup v2
    limit = _read_int("/sys/fs/cgroup/memory.max")
    current = _read_int("/sys/fs/cgroup/memory.current")
    if current is not None:
        remaining = max(0, limit - current) if limit is not None else None
        return limit, remaining, "v2"

    # cgroup v1
    limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    current = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if current is not None:
        # Very large v1 limits generally mean that no real limit is set.
        if limit is not None and limit >= 1 << 60:
            limit = None
        remaining = max(0, limit - current) if limit is not None else None
        return limit, remaining, "v1"

    return None, None, None


def _to_gib(value: int | None) -> float | None:
    return round(value / _GIB, 2) if value is not None else None


def estimate_model_ram_gb(model_name: str, dtype: str = "auto") -> float:
    """Estimate model-loading RAM from the parameter count in the model name.

    This is intentionally conservative. It is a preflight check, not an exact
    memory profiler. LOCAL_AI_REQUIRED_RAM_GB can override the estimate.
    """
    match = re.search(r"(\d+(?:\.\d+)?)\s*[bB](?:\b|-)", model_name)
    parameters_billion = float(match.group(1)) if match else 30.0

    normalized = dtype.strip().lower()
    if normalized in {"float32", "fp32"}:
        bytes_per_parameter = 4.0
    elif normalized in {"int8", "8bit"}:
        bytes_per_parameter = 1.0
    elif normalized in {"int4", "4bit"}:
        bytes_per_parameter = 0.6
    else:
        # auto, float16 and bfloat16 are treated as two-byte weights.
        bytes_per_parameter = 2.0

    raw_gib = parameters_billion * 1_000_000_000 * bytes_per_parameter / _GIB
    # Account for loading overhead, tokenizer/runtime state and a modest KV cache.
    return round(raw_gib * 1.25 + 4.0, 1)


@dataclass
class RamCheck:
    enabled: bool
    mode: str
    model: str
    dtype: str
    host_total_gb: float | None
    host_available_gb: float | None
    cgroup_version: str | None
    cgroup_limit_gb: float | None
    cgroup_remaining_gb: float | None
    effective_available_gb: float | None
    estimated_model_ram_gb: float
    configured_required_ram_gb: float | None
    reserve_ram_gb: float
    required_including_reserve_gb: float
    enough_ram: bool
    load_allowed: bool
    message: str


def check_available_ram() -> RamCheck:
    model_name = os.getenv("AI_MODEL", "Qwen/Qwen3-30B-A3B")
    dtype = os.getenv("LOCAL_AI_DTYPE", "auto")
    mode = os.getenv("LOCAL_AI_RAM_CHECK_MODE", "strict").strip().lower()
    if mode not in {"strict", "warn", "off"}:
        mode = "strict"

    host_total, host_available = _host_memory_bytes()
    cgroup_limit, cgroup_remaining, cgroup_version = _cgroup_memory_bytes()

    candidates = [value for value in (host_available, cgroup_remaining) if value is not None]
    effective_available = min(candidates) if candidates else None

    estimate = estimate_model_ram_gb(model_name, dtype)
    configured = _env_float("LOCAL_AI_REQUIRED_RAM_GB", 0.0)
    required = configured if configured > 0 else estimate
    reserve = max(0.0, _env_float("LOCAL_AI_RAM_RESERVE_GB", 8.0))
    required_with_reserve = round(required + reserve, 1)

    enough = (
            effective_available is not None
            and effective_available / _GIB >= required_with_reserve
    )
    load_allowed = enough or mode in {"warn", "off"}

    if mode == "off":
        message = (
                "RAM preflight enforcement is disabled. "
                + (
                    "The measured RAM is above the recommendation."
                    if enough
                    else "The measured RAM is below or unavailable for the recommendation."
                )
        )
    elif effective_available is None:
        message = (
            "Could not determine available RAM. Embedded AI is blocked in strict mode."
            if mode == "strict"
            else "Could not determine available RAM; loading is allowed in warn mode."
        )
    else:
        available_gb = effective_available / _GIB
        if enough:
            message = (
                f"Enough RAM is available: {available_gb:.1f} GiB effective, "
                f"{required_with_reserve:.1f} GiB required including reserve."
            )
        elif mode == "warn":
            message = (
                f"Only {available_gb:.1f} GiB is effectively available while "
                f"{required_with_reserve:.1f} GiB is recommended; loading is allowed in warn mode."
            )
        else:
            message = (
                f"Only {available_gb:.1f} GiB is effectively available while "
                f"{required_with_reserve:.1f} GiB is required including reserve."
            )

    return RamCheck(
        enabled=env_bool("AI_ENABLED", False),
        mode=mode,
        model=model_name,
        dtype=dtype,
        host_total_gb=_to_gib(host_total),
        host_available_gb=_to_gib(host_available),
        cgroup_version=cgroup_version,
        cgroup_limit_gb=_to_gib(cgroup_limit),
        cgroup_remaining_gb=_to_gib(cgroup_remaining),
        effective_available_gb=_to_gib(effective_available),
        estimated_model_ram_gb=estimate,
        configured_required_ram_gb=round(configured, 1) if configured > 0 else None,
        reserve_ram_gb=reserve,
        required_including_reserve_gb=required_with_reserve,
        enough_ram=enough,
        load_allowed=load_allowed,
        message=message,
    )


def ram_check_payload() -> dict[str, Any]:
    return asdict(check_available_ram())


def _load_embedded_model() -> tuple[Any, Any, Any]:
    global _MODEL, _TOKENIZER, _INPUT_DEVICE, _LOADED_MODEL_NAME

    model_name = os.getenv("AI_MODEL", "Qwen/Qwen3-30B-A3B")
    with _MODEL_LOCK:
        if _MODEL is not None and _TOKENIZER is not None:
            if _LOADED_MODEL_NAME != model_name:
                raise RuntimeError(
                    "A different embedded model is already loaded in this process. "
                    "Restart the service after changing AI_MODEL."
                )
            return _MODEL, _TOKENIZER, _INPUT_DEVICE

        check = check_available_ram()
        LOG.info("Embedded AI RAM check: %s", check.message)
        if not check.load_allowed:
            raise RuntimeError(
                "Embedded AI was not loaded because the RAM preflight failed. "
                f"{check.message} Adjust LOCAL_AI_REQUIRED_RAM_GB only after "
                "confirming the model's real memory use."
            )

        # These imports are intentionally lazy. With AI_ENABLED=false the normal
        # Garmin advisor never initializes PyTorch or Transformers.
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Embedded AI dependencies are missing. Rebuild the V4 Docker image."
            ) from exc

        cpu_threads = int(_env_float("LOCAL_AI_CPU_THREADS", 0))
        if cpu_threads > 0:
            torch.set_num_threads(cpu_threads)

        cache_dir = Path(os.getenv("HF_HOME", "/models/huggingface"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        offload_dir = Path(os.getenv("LOCAL_AI_OFFLOAD_DIR", "/models/offload"))
        offload_dir.mkdir(parents=True, exist_ok=True)

        trust_remote_code = env_bool("LOCAL_AI_TRUST_REMOTE_CODE", False)
        dtype = os.getenv("LOCAL_AI_DTYPE", "auto").strip().lower()
        torch_dtype: Any = "auto"
        if dtype in {"float32", "fp32"}:
            torch_dtype = torch.float32
        elif dtype in {"float16", "fp16"}:
            torch_dtype = torch.float16
        elif dtype in {"bfloat16", "bf16"}:
            torch_dtype = torch.bfloat16

        LOG.info("Loading embedded model %s. This can take several minutes.", model_name)
        tokenizer = AutoTokenizer.from_pretrained(
            model_name,
            cache_dir=str(cache_dir),
            use_fast=True,
            trust_remote_code=trust_remote_code,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            cache_dir=str(cache_dir),
            torch_dtype=torch_dtype,
            device_map=os.getenv("LOCAL_AI_DEVICE_MAP", "auto"),
            low_cpu_mem_usage=True,
            trust_remote_code=trust_remote_code,
            offload_folder=str(offload_dir),
        )
        model.eval()

        input_device = model.get_input_embeddings().weight.device
        _MODEL = model
        _TOKENIZER = tokenizer
        _INPUT_DEVICE = input_device
        _LOADED_MODEL_NAME = model_name
        LOG.info("Embedded model loaded. Prompt input device: %s", input_device)
        return model, tokenizer, input_device


def generate_chat_completion(
        messages: list[dict[str, str]],
        *,
        max_new_tokens: int | None = None,
        temperature: float | None = None,
) -> str:
    model, tokenizer, input_device = _load_embedded_model()

    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("PyTorch is unavailable after model initialization.") from exc

    token_limit = max_new_tokens or int(
        _env_float("LOCAL_AI_MAX_NEW_TOKENS", 1400)
    )
    token_limit = max(64, min(token_limit, 4096))
    configured_temperature = (
        temperature
        if temperature is not None
        else _env_float("LOCAL_AI_TEMPERATURE", 0.0)
    )

    template_args: dict[str, Any] = {
        "conversation": messages,
        "tokenize": True,
        "add_generation_prompt": True,
        "return_tensors": "pt",
        "return_dict": True,
    }
    if not env_bool("LOCAL_AI_ENABLE_THINKING", False):
        template_args["enable_thinking"] = False

    try:
        model_inputs = tokenizer.apply_chat_template(**template_args)
    except TypeError:
        # Keep compatibility with tokenizers that do not expose enable_thinking.
        template_args.pop("enable_thinking", None)
        model_inputs = tokenizer.apply_chat_template(**template_args)

    model_inputs = model_inputs.to(input_device)
    input_tokens = int(model_inputs["input_ids"].shape[1])

    generation_args: dict[str, Any] = {
        "max_new_tokens": token_limit,
        "pad_token_id": tokenizer.eos_token_id,
    }
    if configured_temperature > 0:
        generation_args.update(
            {
                "do_sample": True,
                "temperature": max(0.05, configured_temperature),
                "top_p": _env_float("LOCAL_AI_TOP_P", 0.8),
                "top_k": int(_env_float("LOCAL_AI_TOP_K", 20)),
            }
        )
    else:
        generation_args["do_sample"] = False

    with _MODEL_LOCK:
        with torch.inference_mode():
            generated = model.generate(**model_inputs, **generation_args)

    output_tokens = generated[0, input_tokens:]
    return tokenizer.decode(output_tokens, skip_special_tokens=True).strip()
