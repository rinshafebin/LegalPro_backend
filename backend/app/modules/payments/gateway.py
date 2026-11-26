"""Payment gateway interface + Razorpay implementation, and signature checks.

Signatures are verified locally with HMAC-SHA256 (what Razorpay's SDK utility does too),
using constant-time comparison.
"""

import hashlib
import hmac
from typing import Any, Protocol

import razorpay
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)


class PaymentGatewayError(AppError):
    status_code = 502
    code = "payment_gateway_error"


class PaymentGateway(Protocol):
    @property
    def key_id(self) -> str: ...

    async def create_order(
        self, *, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> str:
        """Create an order and return its id."""
        ...

    async def refund(
        self, *, payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> tuple[str, str]:
        """Refund a captured payment. Returns (refund_id, status)."""
        ...


class RazorpayGateway:
    def __init__(self, key_id: str, key_secret: str) -> None:
        self._key_id = key_id
        self._client = razorpay.Client(auth=(key_id, key_secret))

    @property
    def key_id(self) -> str:
        return self._key_id

    async def create_order(
        self, *, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> str:
        data = {"amount": amount_paise, "currency": currency, "receipt": receipt, "notes": notes}
        try:
            order: dict[str, Any] = await run_in_threadpool(self._client.order.create, data=data)
            return str(order["id"])
        except Exception as exc:  # SDK raises several unrelated exception types
            logger.exception("razorpay_order_failed", receipt=receipt)
            raise PaymentGatewayError("The payment provider is unavailable; try again") from exc

    async def refund(
        self, *, payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> tuple[str, str]:
        data = {"amount": amount_paise, "speed": "normal", "notes": notes}
        try:
            refund: dict[str, Any] = await run_in_threadpool(
                self._client.payment.refund, payment_id, data
            )
            return str(refund["id"]), str(refund.get("status", "pending"))
        except Exception as exc:
            logger.exception("razorpay_refund_failed", payment_id=payment_id)
            raise PaymentGatewayError("Refund request to the payment provider failed") from exc


_gateway: PaymentGateway | None = None


def get_gateway() -> PaymentGateway:
    global _gateway
    if _gateway is None:
        s = get_settings()
        _gateway = RazorpayGateway(s.razorpay_key_id, s.razorpay_key_secret.get_secret_value())
    return _gateway


def set_gateway(gateway: PaymentGateway | None) -> None:
    """Swap the gateway (tests use a fake)."""
    global _gateway
    _gateway = gateway


# ---------------------------------------------------------------- signatures


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def checkout_signature(order_id: str, payment_id: str) -> str:
    secret = get_settings().razorpay_key_secret.get_secret_value()
    return _hmac_hex(secret, f"{order_id}|{payment_id}".encode())


def webhook_signature(body: bytes) -> str:
    return _hmac_hex(get_settings().razorpay_webhook_secret.get_secret_value(), body)


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not get_settings().razorpay_key_secret.get_secret_value():
        return False  # never accept signatures made with an empty key
    return hmac.compare_digest(checkout_signature(order_id, payment_id), signature)


def verify_webhook_signature(body: bytes, signature: str | None) -> bool:
    if not signature or not get_settings().razorpay_webhook_secret.get_secret_value():
        return False
    return hmac.compare_digest(webhook_signature(body), signature)
