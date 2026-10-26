from prediction_trader.domain.models import OrderIntent


class PaperExecutionAdapter:
    def submit(self, intent: OrderIntent) -> str:
        return f"paper-{intent.client_order_id}"

