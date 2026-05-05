from typing import Any

class SignatureListItem:
    """A pending cross-signing signature."""

    signing_key_id: str
    """ Full key ID of the signing key, e.g. `"ed25519:ABCDEF"`."""

    target_user_id: str
    """User whose key was signed."""

    target_device_id: str
    """Device ID (or master-key ID) that the signature targets."""

    signature: Any
    """Raw signature value."""

    def __init__(
        self,
        signing_key_id: str,
        target_user_id: str,
        target_device_id: str,
        signature: Any,
    ) -> None: ...
