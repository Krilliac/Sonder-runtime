"""Private, byte-compatible policy/namespace identity shared by child stores."""

from hashlib import sha256

from ...application.subagents.child_migration import encode


def policy_identity(config, binding):
    values = binding.connection_kwargs(config)
    return sha256(
        encode(
            {
                "endpoint": {
                    key: values[key] for key in ("host", "port", "dbname", "user")
                },
                "policy": {
                    key: getattr(config, key)
                    for key in (
                        "owner_id",
                        "durability",
                        "required_standby",
                        "operation_timeout_seconds",
                        "cancel_timeout_seconds",
                    )
                },
                "binding": binding.private_closure_identity(),
            }
        )
    ).hexdigest()


def storage_identity(policy, namespace):
    if (
        type(namespace) is not str
        or len(namespace) != 32
        or any(c not in "0123456789abcdef" for c in namespace)
    ):
        raise ValueError("exact child namespace identity required")
    return sha256(encode({"endpoint": policy, "namespace": namespace})).hexdigest()
