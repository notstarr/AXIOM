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


class StorageDiskFull(DomainError):
    """The data volume cannot accept another write.

    503 rather than 500: nothing is wrong with the request or with the worker
    that sent it, and the correct response is to retry once an operator frees
    space.  Without a distinct type this surfaces as an opaque ASGI 500 on every
    work input at once, which is indistinguishable from every backend breaking.
    """

    status_code = 503
    code = "storage_disk_full"
