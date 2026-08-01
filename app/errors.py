"""統一的商業邏輯例外，由 main.py 轉成 HTTP 回應。"""


class MESError(Exception):
    """所有 MES 商業邏輯錯誤的基底。"""

    status_code = 400
    code = "MES_ERROR"

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFoundError(MESError):
    status_code = 404
    code = "NOT_FOUND"


class DuplicateError(MESError):
    status_code = 409
    code = "DUPLICATE"


class ValidationError(MESError):
    status_code = 422
    code = "VALIDATION_ERROR"


class StateError(MESError):
    """狀態不允許此操作，例如對 HOLD 中的批號進站。"""

    status_code = 409
    code = "INVALID_STATE"


class PermissionError_(MESError):
    status_code = 403
    code = "FORBIDDEN"
