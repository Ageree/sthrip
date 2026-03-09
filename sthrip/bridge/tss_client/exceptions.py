"""
TSS Client Exceptions
"""


class TSSClientError(Exception):
    """Base exception for TSS client"""
    pass


class TSSConnectionError(TSSClientError):
    """Raised when connection to TSS server fails"""
    pass


class TSSOperationError(TSSClientError):
    """Raised when TSS operation (keygen/sign) fails"""
    pass


class TSSInvalidInputError(TSSClientError):
    """Raised when input validation fails"""
    pass
