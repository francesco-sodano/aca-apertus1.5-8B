from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    model_endpoint: str
    model_id: str
    vllm_api_key: str
    content_safety_endpoint: str
    foundry_project_endpoint: str
    foundry_grounding_model: str
    content_safety_threshold: int
    max_output_tokens: int
    temperature: float
    model_timeout_seconds: float
    grounding_timeout_seconds: float
    health_token: str
    max_concurrent_requests: int
    requests_per_minute: int
    admission_queue_timeout_seconds: float

    def __post_init__(self) -> None:
        if not 0 <= self.content_safety_threshold <= 7:
            raise ValueError("CONTENT_SAFETY_THRESHOLD must be between 0 and 7.")
        if self.max_output_tokens <= 0:
            raise ValueError("MAX_OUTPUT_TOKENS must be positive.")
        if self.max_concurrent_requests <= 0:
            raise ValueError("MAX_CONCURRENT_REQUESTS must be positive.")
        if self.requests_per_minute <= 0:
            raise ValueError("REQUESTS_PER_MINUTE must be positive.")
        if self.admission_queue_timeout_seconds <= 0:
            raise ValueError("ADMISSION_QUEUE_TIMEOUT_SECONDS must be positive.")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            model_endpoint=_required("MODEL_ENDPOINT").rstrip("/"),
            model_id=os.getenv("MODEL_ID", "swiss-ai/Apertus-v1.5-8B"),
            vllm_api_key=_required("VLLM_API_KEY"),
            content_safety_endpoint=_required("CONTENT_SAFETY_ENDPOINT").rstrip("/"),
            foundry_project_endpoint=_required("FOUNDRY_PROJECT_ENDPOINT").rstrip("/"),
            foundry_grounding_model=os.getenv(
                "FOUNDRY_GROUNDING_MODEL", "gpt-5-mini"
            ),
            content_safety_threshold=int(
                os.getenv("CONTENT_SAFETY_THRESHOLD", "4")
            ),
            max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "2048")),
            temperature=float(os.getenv("TEMPERATURE", "0.2")),
            model_timeout_seconds=float(
                os.getenv("MODEL_TIMEOUT_SECONDS", "900")
            ),
            grounding_timeout_seconds=float(
                os.getenv("GROUNDING_TIMEOUT_SECONDS", "120")
            ),
            health_token=_required("MODEL_HEALTH_TOKEN"),
            max_concurrent_requests=int(
                os.getenv("MAX_CONCURRENT_REQUESTS", "4")
            ),
            requests_per_minute=int(os.getenv("REQUESTS_PER_MINUTE", "6")),
            admission_queue_timeout_seconds=float(
                os.getenv("ADMISSION_QUEUE_TIMEOUT_SECONDS", "5")
            ),
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is not set: {name}")
    return value