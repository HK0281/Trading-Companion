class InvalidRequestError(ValueError):
    """The analysis request is invalid and must be corrected by the caller."""


class DataSourceError(Exception):
    """The source could not be read or contacted."""


class DataValidationError(ValueError):
    """The source returned malformed or inconsistent data."""
