from typing import Protocol

from prediction_trader.domain.models import OrderIntent


class ExecutionAdapter(Protocol):
    def submit(self, intent: OrderIntent) -> str: ...


class OrderManager:
    def __init__(self, adapter: ExecutionAdapter) -> None:
        self._adapter = adapter
        self._submitted: dict[str, str] = {}

    def submit(self, intent: OrderIntent) -> str:
        key = str(intent.client_order_id)
        if key in self._submitted:
            return self._submitted[key]
        remote_id = self._adapter.submit(intent)
        self._submitted[key] = remote_id
        return remote_id

