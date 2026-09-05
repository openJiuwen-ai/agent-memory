# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared status mapping and redacted errors for HTTP and CLI."""

from jiuwen_memory.api import AgentMemoryError, safe_error_message

_RETRY_AFTER_SECONDS = 1
_HTTP_ERROR_POLICIES = {
    "BadRequest": (400, False, None),
    "ValidationError": (400, False, None),
    "PolicyError": (400, False, None),
    "AuthenticationError": (401, False, "authentication failed"),
    "PermissionDeniedError": (403, False, "permission denied"),
    "NotFound": (404, False, "resource not found"),
    "NotFoundError": (404, False, "resource not found"),
    "UnknownVerb": (404, False, "resource not found"),
    "MethodNotAllowed": (405, False, "method not allowed"),
    "ConflictError": (409, False, "resource conflict"),
    "PartialFailureError": (409, False, None),
    "RateLimitedError": (429, True, "too many requests"),
    "BackendError": (503, True, "service temporarily unavailable"),
    "HealthCheckError": (503, True, "service temporarily unavailable"),
    "SecurityUnavailable": (503, False, "HTTP authentication is not configured"),
    "PayloadTooLarge": (413, False, "request body is too large"),
    "InternalError": (500, False, "internal server error"),
    "UnsupportedStorageCapabilityError": (400, False, None),
    "StorageRetrievalError": (400, False, None),
}
_PARTIAL_FAILURE_FIELDS = ("completed", "failed", "retry_action")
_UNSET = object()


def _error_name(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, type):
        return value.__name__
    return type(value).__name__


def error_response(error: object, detail: object = "") -> tuple[int, dict[str, object], int | None]:
    """Translate an internal error name into the stable HTTP error envelope."""
    name = _error_name(error)
    policy = _HTTP_ERROR_POLICIES.get(name)
    if policy is None:
        if isinstance(error, AgentMemoryError) or (
            isinstance(error, type) and issubclass(error, AgentMemoryError)
        ):
            status, retryable, fixed_message = 400, False, None
        else:
            name = "InternalError"
            status, retryable, fixed_message = _HTTP_ERROR_POLICIES[name]
    else:
        status, retryable, fixed_message = policy
    if fixed_message is None:
        message = safe_error_message(Exception(str(detail)))
    else:
        message = fixed_message
    body: dict[str, object] = {
        "error": name or "InternalError",
        "message": message,
        "retryable": retryable,
    }
    if name == "PartialFailureError":
        for field in _PARTIAL_FAILURE_FIELDS:
            value = getattr(detail, field, _UNSET)
            if value is not _UNSET:
                body[field] = value
    retry_after = _RETRY_AFTER_SECONDS if retryable and status == 429 else None
    return status, body, retry_after
