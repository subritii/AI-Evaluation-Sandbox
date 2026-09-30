"""Errors shared by every model client."""


class ModelServerError(RuntimeError):
    """A model server (Ollama or an OpenAI-compatible endpoint) returned an error.

    Messages carry the server's own error text and never the request body or
    credentials, so they are safe to log by type and to show as HTTP 502.
    """
