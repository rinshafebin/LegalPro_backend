import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bookings.models import Booking, BookingStatus
from app.modules.payments.gateway import checkout_signature
from app.modules.payments.models import Payment, RefundStatus, WebhookEvent
from app.workers import tasks
from tests.booking_support import World, book, free_slots, session_factory, webhook_request, world
from tests.conftest import FakeGateway, FakeJobQueue
from tests.helpers import API

__all__ = ["world"]


def _rejected(caplog: pytest.LogCaptureFixture, reason: str) -> bool:
    return any(
        isinstance(r.msg, dict)
        and r.msg.get("event") == "webhook_rejected"
        and r.msg.get("reason") == reason
        for r in caplog.records
    )


async def _pending_booking(client: AsyncClient, world: World, index: int = 0) -> dict[str, Any]:
    start = (await free_slots(client, world.advocate.id, days=5))[index]
    return await book(client, world.client, world.advocate.id, start)


async def _order(client: AsyncClient, world: World, booking_id: str) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/payments/orders", headers=world.client.headers, json={"booking_id": booking_id}
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def _status(client: AsyncClient, world: World, booking_id: str) -> str:
    resp = await client.get(f"{API}/bookings/{booking_id}", headers=world.client.headers)
    status: str = resp.json()["status"]
    return status


async def _send_webhook(client: AsyncClient, body: bytes, headers: dict[str, str]) -> Any:
    return await client.post(f"{API}/payments/webhook", content=body, headers=headers)


# ---------------------------------------------------------------- orders


async def test_order_amount_is_computed_on_the_server(
    client: AsyncClient, world: World, gateway: FakeGateway
) -> None:
    booking = await _pending_booking(client, world)
    resp = await client.post(
        f"{API}/payments/orders",
        headers=world.client.headers,
        json={"booking_id": booking["id"], "amount": 1, "currency": "USD"},  # ignored
    )
    assert resp.status_code == 201
    order = resp.json()
    assert order["amount"] == 150000  # ₹1500.00 in paise, from the booking
    assert order["currency"] == "INR"
    assert order["key_id"] == "rzp_test_fake"
    assert gateway.orders == [
        {"id": order["order_id"], "amount": 150000, "currency": "INR", "receipt": booking["id"]}
    ]


async def test_order_creation_is_idempotent(
    client: AsyncClient, world: World, gateway: FakeGateway
) -> None:
    booking = await _pending_booking(client, world)
    first = await _order(client, world, booking["id"])
    second = await _order(client, world, booking["id"])
    assert first["order_id"] == second["order_id"]
    assert len(gateway.orders) == 1


async def test_order_only_for_own_payable_booking(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    booking = await _pending_booking(client, world)
    other = await client.post(
        f"{API}/payments/orders",
        headers=world.other_client.headers,
        json={"booking_id": booking["id"]},
    )
    assert other.status_code == 404

    await db_session.execute(
        update(Booking).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()
    expired = await client.post(
        f"{API}/payments/orders", headers=world.client.headers, json={"booking_id": booking["id"]}
    )
    assert expired.status_code == 409


# ---------------------------------------------------------------- checkout verification


async def test_checkout_verify_confirms_booking(
    client: AsyncClient, world: World, jobs: FakeJobQueue
) -> None:
    booking = await _pending_booking(client, world, index=-1)
    order = await _order(client, world, booking["id"])
    payload = {
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_123",
        "razorpay_signature": checkout_signature(order["order_id"], "pay_123"),
    }
    resp = await client.post(f"{API}/payments/verify", headers=world.client.headers, json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "captured"
    assert resp.json()["booking_status"] == "confirmed"
    assert await _status(client, world, booking["id"]) == "confirmed"

    assert f"booking:{booking['id']}:expire" in jobs.aborted
    start_ts = int(datetime.fromisoformat(booking["start_at"]).timestamp())
    reminder_ids = {kw["_job_id"] for n, kw in jobs.jobs if n == "send_booking_reminder"}
    assert f"booking:{booking['id']}:reminder:1h:{start_ts}" in reminder_ids
    assert jobs.emails_to("c1@example.com")[-1]["subject"] == "Consultation confirmed"


async def test_checkout_verify_rejects_bad_signature(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    payload = {
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_123",
        "razorpay_signature": "0" * 64,
    }
    resp = await client.post(f"{API}/payments/verify", headers=world.client.headers, json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_signature"
    assert await _status(client, world, booking["id"]) == "pending_payment"


# ---------------------------------------------------------------- webhook


async def test_webhook_confirms_booking(client: AsyncClient, world: World) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    body, headers = webhook_request(order["order_id"], order["amount"])

    resp = await _send_webhook(client, body, headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"
    assert await _status(client, world, booking["id"]) == "confirmed"


async def test_webhook_redelivery_changes_nothing(
    client: AsyncClient, world: World, jobs: FakeJobQueue, db_session: AsyncSession
) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    body, headers = webhook_request(order["order_id"], order["amount"], event_id="evt_same")

    assert (await _send_webhook(client, body, headers)).json()["status"] == "confirmed"
    emails_after_first = len(jobs.emails())

    again = await _send_webhook(client, body, headers)
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert len(jobs.emails()) == emails_after_first

    # Razorpay also sends order.paid for the same payment under another event id.
    body2, headers2 = webhook_request(order["order_id"], order["amount"], event="order.paid")
    assert (await _send_webhook(client, body2, headers2)).json()["status"] == "already_captured"
    assert len(jobs.emails()) == emails_after_first

    events = (await db_session.scalars(select(WebhookEvent.outcome))).all()
    assert sorted(events) == ["already_captured", "confirmed"]


async def test_webhook_rejects_bad_signature(
    client: AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    body, headers = webhook_request(order["order_id"], order["amount"])
    headers["X-Razorpay-Signature"] = "f" * 64

    with caplog.at_level(logging.WARNING):
        resp = await _send_webhook(client, body, headers)
    assert resp.status_code == 400
    assert _rejected(caplog, "bad_signature")
    assert await _status(client, world, booking["id"]) == "pending_payment"


async def test_webhook_tampered_body_is_rejected(client: AsyncClient, world: World) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    body, headers = webhook_request(order["order_id"], order["amount"])
    tampered = body.replace(b'"amount": 150000', b'"amount": 100')
    assert (await _send_webhook(client, tampered, headers)).status_code == 400


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"amount": 100}, "amount_mismatch"),
        ({"currency": "USD"}, "amount_mismatch"),
        ({"order_id": "order_unknown"}, "unknown_order"),
    ],
)
async def test_webhook_rejects_mismatched_payments(
    client: AsyncClient,
    world: World,
    caplog: pytest.LogCaptureFixture,
    change: dict[str, Any],
    reason: str,
) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    args: dict[str, Any] = {"order_id": order["order_id"], "amount": order["amount"], **change}
    body, headers = webhook_request(
        args.pop("order_id"), args.pop("amount"), currency=args.pop("currency", "INR")
    )
    with caplog.at_level(logging.WARNING):
        resp = await _send_webhook(client, body, headers)
    assert resp.status_code == 200  # acknowledged so Razorpay doesn't retry...
    assert resp.json()["status"] == f"rejected_{reason}"  # ...but not applied
    assert _rejected(caplog, reason)
    assert await _status(client, world, booking["id"]) == "pending_payment"


async def test_payment_after_expiry_revives_booking_if_slot_is_free(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    await db_session.execute(
        update(Booking).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()
    await tasks.expire_booking(
        {"session_factory": session_factory(db_session)}, booking_id=booking["id"]
    )
    assert await _status(client, world, booking["id"]) == "expired"

    body, headers = webhook_request(order["order_id"], order["amount"])
    assert (await _send_webhook(client, body, headers)).json()["status"] == "confirmed"
    assert await _status(client, world, booking["id"]) == "confirmed"


async def test_payment_after_slot_was_taken_is_refunded_automatically(
    client: AsyncClient,
    world: World,
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    jobs: FakeJobQueue,
) -> None:
    booking = await _pending_booking(client, world)
    order = await _order(client, world, booking["id"])
    await db_session.execute(
        update(Booking).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()
    await tasks.expire_booking(
        {"session_factory": session_factory(db_session)}, booking_id=booking["id"]
    )
    await book(client, world.other_client, world.advocate.id, booking["start_at"])

    body, headers = webhook_request(order["order_id"], order["amount"])
    with caplog.at_level(logging.WARNING):
        resp = await _send_webhook(client, body, headers)
    assert resp.json()["status"] == "slot_lost"
    assert await _status(client, world, booking["id"]) == "expired"
    payment = await db_session.scalar(select(Payment))
    assert payment is not None
    assert payment.refund_status is RefundStatus.REQUESTED
    assert payment.refund_amount_paise == order["amount"]  # full refund
    assert ("issue_refund", f"payment:{payment.id}:refund") in [
        (n, kw["_job_id"]) for n, kw in jobs.jobs
    ]


# ---------------------------------------------------------------- cancel / reschedule / complete


async def _confirmed_booking(client: AsyncClient, world: World, index: int = -1) -> dict[str, Any]:
    booking = await _pending_booking(client, world, index=index)
    order = await _order(client, world, booking["id"])
    body, headers = webhook_request(order["order_id"], order["amount"])
    assert (await _send_webhook(client, body, headers)).json()["status"] == "confirmed"
    return booking


async def test_advocate_cancelling_paid_booking_refunds_and_aborts_reminders(
    client: AsyncClient, world: World, jobs: FakeJobQueue, db_session: AsyncSession
) -> None:
    booking = await _confirmed_booking(client, world)
    start_ts = int(datetime.fromisoformat(booking["start_at"]).timestamp())

    resp = await client.post(
        f"{API}/bookings/{booking['id']}/cancel",
        headers=world.advocate.headers,
        json={"reason": "Court hearing"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    for kind in ("24h", "1h"):
        assert f"booking:{booking['id']}:reminder:{kind}:{start_ts}" in jobs.aborted
    assert jobs.emails_to("c1@example.com")[-1]["subject"] == "Consultation cancelled"

    payment = await db_session.scalar(select(Payment))
    assert payment is not None
    assert payment.refund_status is RefundStatus.REQUESTED  # advocate cancels -> full refund
    assert payment.refund_amount_paise == payment.amount_paise


async def test_reschedule_moves_booking_and_reminders(
    client: AsyncClient, world: World, jobs: FakeJobQueue
) -> None:
    booking = await _confirmed_booking(client, world, index=-1)
    old_ts = int(datetime.fromisoformat(booking["start_at"]).timestamp())
    new_start = (await free_slots(client, world.advocate.id, days=5))[-1]

    resp = await client.post(
        f"{API}/bookings/{booking['id']}/reschedule",
        headers=world.client.headers,
        json={"start_at": new_start},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["start_at"] == new_start
    new_ts = int(datetime.fromisoformat(new_start).timestamp())
    assert f"booking:{booking['id']}:reminder:1h:{old_ts}" in jobs.aborted
    scheduled = {kw["_job_id"] for n, kw in jobs.jobs if n == "send_booking_reminder"}
    assert f"booking:{booking['id']}:reminder:1h:{new_ts}" in scheduled
    assert jobs.emails_to("adv@example.com")[-1]["subject"] == "Consultation rescheduled"


async def test_reschedule_into_taken_slot_conflicts(client: AsyncClient, world: World) -> None:
    booking = await _confirmed_booking(client, world, index=-1)
    taken = (await free_slots(client, world.advocate.id, days=5))[0]
    await book(client, world.other_client, world.advocate.id, taken)
    resp = await client.post(
        f"{API}/bookings/{booking['id']}/reschedule",
        headers=world.client.headers,
        json={"start_at": taken},
    )
    assert resp.status_code == 409


async def test_pending_booking_cannot_be_rescheduled(client: AsyncClient, world: World) -> None:
    booking = await _pending_booking(client, world)
    new_start = (await free_slots(client, world.advocate.id, days=5))[-1]
    resp = await client.post(
        f"{API}/bookings/{booking['id']}/reschedule",
        headers=world.client.headers,
        json={"start_at": new_start},
    )
    assert resp.status_code == 409


async def test_complete_booking(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    booking = await _confirmed_booking(client, world)
    url = f"{API}/bookings/{booking['id']}/complete"
    assert (await client.post(url, headers=world.advocate.headers)).status_code == 409  # not ended
    assert (await client.post(url, headers=world.client.headers)).status_code == 403

    past = datetime.now(UTC) - timedelta(hours=2)
    await db_session.execute(
        update(Booking).values(start_at=past, end_at=past + timedelta(minutes=30))
    )
    await db_session.commit()
    resp = await client.post(url, headers=world.advocate.headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


# ---------------------------------------------------------------- reminder job


async def test_reminder_job_skips_cancelled_or_moved_bookings(
    client: AsyncClient, world: World, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    booking = await _confirmed_booking(client, world)
    start_ts = int(datetime.fromisoformat(booking["start_at"]).timestamp())
    ctx = {"session_factory": session_factory(db_session)}

    before = len(jobs.emails())
    assert await tasks.send_booking_reminder(
        ctx, booking_id=booking["id"], kind="1h", start_ts=start_ts
    )
    assert len(jobs.emails()) == before + 2  # client + advocate
    assert jobs.emails_to("c1@example.com")[-1]["subject"] == "Reminder: consultation in 1h"

    stale = await tasks.send_booking_reminder(
        ctx, booking_id=booking["id"], kind="1h", start_ts=start_ts - 3600
    )
    assert stale is False

    await db_session.execute(update(Booking).values(status=BookingStatus.CANCELLED))
    await db_session.commit()
    cancelled = await tasks.send_booking_reminder(
        ctx, booking_id=booking["id"], kind="1h", start_ts=start_ts
    )
    assert cancelled is False


async def test_gateway_failure_returns_502_and_creates_no_payment(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    from app.modules.payments.gateway import PaymentGatewayError, set_gateway

    class BrokenGateway(FakeGateway):
        async def create_order(self, **_: Any) -> str:
            raise PaymentGatewayError("The payment provider is unavailable; try again")

    booking = await _pending_booking(client, world)
    set_gateway(BrokenGateway())
    resp = await client.post(
        f"{API}/payments/orders", headers=world.client.headers, json={"booking_id": booking["id"]}
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "payment_gateway_error"
    assert await db_session.scalar(select(Payment)) is None


async def test_real_razorpay_gateway_wraps_sdk_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.payments.gateway import PaymentGatewayError, RazorpayGateway

    gw = RazorpayGateway("rzp_test_x", "secret")

    def _boom(**_: Any) -> dict[str, Any]:
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(gw._client.order, "create", _boom)
    with pytest.raises(PaymentGatewayError):
        await gw.create_order(amount_paise=100, currency="INR", receipt="r", notes={})
