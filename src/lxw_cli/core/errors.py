import re

# A translated (German) validation message needs no "HTTP 406:" in front.
_GERMAN = re.compile(
    r"[äöüßÄÖÜ„“]|\b(?:der|die|das|bitte|nicht|ist|für|kein|keine|und|oder|eine?)\b", re.I)


class LexwareError(Exception):
    """Base exception for all lxw-cli errors."""


class ConfigError(LexwareError):
    """Raised when configuration is missing or invalid."""


class LexwareAPIError(LexwareError):
    def __init__(self, status_code: int, message: str, body: object | None = None) -> None:
        german = status_code in (400, 406, 409, 422) and bool(_GERMAN.search(message or ""))
        super().__init__(message if german else f"HTTP {status_code}: {message}")
        self.message = message
        self.status_code = status_code
        self.body = body


class RateLimitError(LexwareAPIError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(429, message)
        self.retry_after = retry_after
