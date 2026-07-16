class LexwareError(Exception):
    """Base exception for all lexware-cli errors."""


class ConfigError(LexwareError):
    """Raised when configuration is missing or invalid."""


class LexwareAPIError(LexwareError):
    def __init__(self, status_code: int, message: str, body: object | None = None) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class RateLimitError(LexwareAPIError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(429, message)
        self.retry_after = retry_after
