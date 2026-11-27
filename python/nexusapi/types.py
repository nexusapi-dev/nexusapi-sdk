"""Типы ответов API (TypedDict).

Во время выполнения это обычные dict — `reply["choices"][0]["message"]["content"]`
работает как раньше, а mypy / pyright знают поля. Все TypedDict объявлены с
`total=False`: сервер может не прислать необязательное поле и может добавить
новые, поэтому код не должен полагаться на полный набор ключей.
"""
from __future__ import annotations

from typing import Any, List, Literal, Optional, TypedDict

# ============================================================
# Чат: POST /v1/chat/completions
# ============================================================


class ChatCompletionMessage(TypedDict, total=False):
    role: str
    content: Optional[str]
    tool_calls: List[Any]
    reasoning_content: Optional[str]


class ChatCompletionChoice(TypedDict, total=False):
    index: int
    message: ChatCompletionMessage
    finish_reason: Optional[str]


class CompletionUsage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletion(TypedDict, total=False):
    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: CompletionUsage


class ChatCompletionChunkDelta(TypedDict, total=False):
    role: str
    content: Optional[str]
    tool_calls: List[Any]
    reasoning_content: Optional[str]


class ChatCompletionChunkChoice(TypedDict, total=False):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str]


class ChatCompletionChunk(TypedDict, total=False):
    id: str
    object: Literal["chat.completion.chunk"]
    created: int
    model: str
    choices: List[ChatCompletionChunkChoice]
    usage: Optional[CompletionUsage]


# ============================================================
# /v1/models, /v1/images/generations, /v1/videos
# ============================================================


class OpenAIModel(TypedDict, total=False):
    id: str
    object: Literal["model"]
    created: int
    owned_by: str


class ImageData(TypedDict, total=False):
    url: str


class ImagesResponse(TypedDict, total=False):
    created: int
    data: List[ImageData]


class VideoError(TypedDict, total=False):
    message: str
    type: str
    code: Optional[str]


class Video(TypedDict, total=False):
    id: str
    object: Literal["video"]
    model: str
    status: Literal["queued", "in_progress", "completed", "failed"]
    progress: int
    created_at: int
    completed_at: Optional[int]
    size: Optional[str]
    seconds: Optional[str]
    quality: Optional[str]
    error: Optional[VideoError]
    #: Ссылка на готовое видео (когда status = "completed").
    url: Optional[str]


# ============================================================
# Каталог GET /public/models
# ============================================================


class PricingOption(TypedDict, total=False):
    id: str
    label: str
    priceRub: float
    unit: Literal["sec", "shot", "request", "token"]
    resolution: str
    duration: int
    mode: str


class ModelInfo(TypedDict, total=False):
    id: str
    name: str
    provider: str
    kind: Literal["video", "image", "audio", "llm"]
    unit: Literal["sec", "shot", "request", "token"]
    priceRub: float
    pricingOptions: List[PricingOption]
    inputPriceRubPerMTokens: float
    outputPriceRubPerMTokens: float
    cacheReadPriceRubPerMTokens: float
    cacheCreationPriceRubPerMTokens: float
    supportsVision: bool
    defaultPricingOptionId: str


# ============================================================
# Вебхуки
# ============================================================


class WebhookEvent(TypedDict, total=False):
    """Тело вебхука о завершении задачи."""

    task_id: str
    status: Literal["completed", "failed"]
    result: Any
    error: Optional[str]


class WebhookSecret(TypedDict, total=False):
    """Ответ GET /webhooks/secret."""

    secret: str
    algorithm: str
    signature_header: str
    timestamp_header: str


__all__ = [
    "ChatCompletion",
    "ChatCompletionChoice",
    "ChatCompletionChunk",
    "ChatCompletionChunkChoice",
    "ChatCompletionChunkDelta",
    "ChatCompletionMessage",
    "CompletionUsage",
    "ImageData",
    "ImagesResponse",
    "ModelInfo",
    "OpenAIModel",
    "PricingOption",
    "Video",
    "VideoError",
    "WebhookEvent",
    "WebhookSecret",
]
