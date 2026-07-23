from __future__ import annotations


class DomainError(ValueError):
    status_code = 422
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class LeaseInactiveError(ConflictError):
    code = "lease_inactive"


class InvalidOriginError(DomainError):
    status_code = 422
    code = "invalid_origin"
