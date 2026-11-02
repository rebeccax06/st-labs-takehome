"""Source-path errors suitable for a future HTTP exception handler."""


class InputError(ValueError):
    def __init__(self, message: str, *, path: str = "/", code: str = "invalid_input"):
        super().__init__(message)
        self.path = path
        self.code = code
