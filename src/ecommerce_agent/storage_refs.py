from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit


APPROVED_OBJECT_STORAGE_SCHEMES = frozenset({"cos", "oss", "s3"})
SENSITIVE_STORAGE_QUERY_KEYS = frozenset(
    {
        "access_key",
        "access_token",
        "credential",
        "secret",
        "signature",
        "token",
        "x-amz-credential",
        "x-amz-signature",
    }
)


def validate_controlled_storage_ref(
    value: str,
    *,
    local_prefix: str = "objects",
    required_subpath: str | None = None,
) -> str:
    parsed = urlsplit(value)
    query_keys = {key.lower() for key, _value in parse_qsl(parsed.query)}
    if (
        parsed.username is not None
        or parsed.password is not None
        or query_keys & SENSITIVE_STORAGE_QUERY_KEYS
    ):
        raise ValueError("storage_ref_credentials_forbidden")
    if value != value.strip() or parsed.query or parsed.fragment:
        raise ValueError("storage_ref_not_approved")
    if parsed.scheme:
        path_parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme.lower() not in APPROVED_OBJECT_STORAGE_SCHEMES
            or not parsed.netloc
            or not parsed.path.strip("/")
            or (
                required_subpath is not None
                and (
                    any(part in {"", ".", ".."} for part in path_parts)
                    or path_parts[0] != required_subpath
                )
            )
        ):
            raise ValueError("storage_ref_not_approved")
        return value
    parts = value.split("/")
    if (
        parsed.netloc
        or "\\" in value
        or len(parts) < 2
        or parts[0] != local_prefix
        or any(part in {"", ".", ".."} for part in parts)
        or (required_subpath is not None and (len(parts) < 3 or parts[1] != required_subpath))
    ):
        raise ValueError("storage_ref_not_approved")
    return value
