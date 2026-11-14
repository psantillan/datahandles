"""Typed protocol errors. The codes are the enum in spec/protocol/errors.schema.json."""


class HandleError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.retryable, self.details = code, message, retryable, details or {}

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details:
            d["details"] = self.details
        return d
