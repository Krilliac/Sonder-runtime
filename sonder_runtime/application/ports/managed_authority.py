"""Private ordered authority hooks; never infer a connection from ambient state."""

from typing import Protocol, Any, ContextManager
from ..context import OperationContext
from .lane_continuation import HostContinuationGrant


class ManagedAuthority(Protocol):
    def admit(self, subject: Any, context: OperationContext) -> ContextManager[Any]: ...
    def authorize_host(
        self,
        admission: Any,
        context: OperationContext,
        host_conversation_id: str,
        *,
        connection: Any
    ) -> HostContinuationGrant: ...
    def authorize_lane(
        self, admission: Any, lane: dict, context: OperationContext, *, connection: Any
    ) -> None: ...

    def require_bound(
        self,
        admission: Any,
        bound: Any,
        record: dict,
        context: OperationContext,
        *,
        connection: Any
    ) -> None: ...
    def register_parent(
        self, bound: Any, record: dict, context: OperationContext
    ) -> Any: ...
    def release_parent(self, bound: Any) -> None: ...
    def bind_worker(
        self,
        lane: dict,
        parent_context: OperationContext,
        worker_context: OperationContext,
        *,
        issuer: object
    ) -> None: ...
    def release_worker(self, context: OperationContext, *, issuer: object) -> None: ...
