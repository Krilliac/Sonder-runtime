"""Security adapters that connect process/host facilities to application policy."""

from .credential_provider import BrokerCredentialProvider

__all__ = ["BrokerCredentialProvider"]
