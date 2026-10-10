"""Typed failures for structured LLM requests; no provider credentials or prose."""


class TransientStructuredError(ValueError):
    """A transport/service failure that may be retried with backoff."""
