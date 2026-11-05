class ContractError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class AccessDenied(ContractError):
    def __init__(self):
        # Deliberately identical for missing, forged, inaccessible or revoked refs.
        super().__init__("reference_unavailable")


class Conflict(ContractError):
    pass
