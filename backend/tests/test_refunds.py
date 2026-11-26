"""Refund policy (agreed): client >=24h -> full, client <24h -> none, advocate -> full,
payment that can't be honoured -> full (automatic). Plus reschedule cutoff of 24h."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bookings.models import Booking
from app.modules.payments.gateway import PaymentGatewayError
from app.modules.payments.models import Payment, RefundStatus
from app.modules.payments.refunds import Canceller, refundable_paise
from app.workers import tasks
from tests.booking_support import World, book, free_slots, session_factory, webhook_request, world
from tests.conftest import FakeGateway, FakeJobQueue
from tests.helpers import API, webhook_body_refund

__all__ = ["world"]


# ---------------------------------------------------------------- policy (pure)


@pytest.mark.parametrize(
    ("notice", "by", "expected"),
    [
        (timedelta(hours=48), Canceller.CLIENT, 1000),
        (timedelta(hours=24), Canceller.CLIENT, 1000),  # boundary is inclusive
        (timedelta(hours=23, minutes=59), Canceller.CLIENT, 0),
        (timedelta(hours=1), Canceller.ADVOCATE, 1000),
        (timedelta(minutes=-30), Canceller.SYSTEM, 1000),
    ],
)
def test_refund_policy(notice: timedelta, by: Canceller, expected: int) -> None:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    assert refundable_paise(1000, now + notice, now, by) == expected


# ---------------------------------------------------------------- helpers


async def _paid_booking(
    client: AsyncClient, world: World, db_session: AsyncSession, starts_in: timedelta
) -> tuple[dict[str, Any], uuid.UUID]:
    start = (await free_slots(client, world.advocate.id, days=5))[-1]
    booking = await book(client, world.client, world.advocate.id, start)
    order = (
        await client.post(
            f"{API}/payments/orders",
            headers=world.client.headers,
            json={"booking_id": booking["id"]},
        )
    ).json()
    body, headers = webhook_request(order["order_id"], order["amount"], payment_id="pay_r1")
    assert (await client.post(f"{API}/payments/webhook", content=body, headers=headers)).json()[
        "status"
    ] == "confirmed"

    # Move the booking so it starts `starts_in` from now (policy depends on notice period).
    new_start = datetime.now(UTC).replace(microsecond=0) + starts_in
    await db_session.execute(
        update(Booking).values(start_at=new_start, end_at=new_start + timedelta(minutes=30))
    )
    await db_session.commit()
    payment_id = await db_session.scalar(select(Payment.id))
    assert payment_id is not None
    return booking, payment_id


async def _cancel(client: AsyncClient, actor_headers: dict[str, str], booking_id: str) -> None:
    resp = await client.post(f"{API}/bookings/{booking_id}/cancel", headers=actor_headers, json={})
    assert resp.status_code == 200, resp.text


async def _payment(db_session: AsyncSession) -> Payment:
    db_session.expire_all()
    payment: Payment | None = await db_session.scalar(select(Payment))
    assert payment is not None
    return payment


# ---------------------------------------------------------------- end to end


async def test_client_cancel_with_notice_gets_full_refund(
    client: AsyncClient,
    world: World,
    db_session: AsyncSession,
    jobs: FakeJobQueue,
    gateway: FakeGateway,
) -> None:
    booking, payment_id = await _paid_booking(client, world, db_session, timedelta(hours=48))
    await _cancel(client, world.client.headers, booking["id"])

    payment = await _payment(db_session)
    assert payment.refund_status is RefundStatus.REQUESTED
    assert payment.refund_amount_paise == 150000
    refund_jobs = [kw for n, kw in jobs.jobs if n == "issue_refund"]
    assert refund_jobs == [
        {
            "payment_id": str(payment.id),
            "_job_id": f"payment:{payment.id}:refund",
            "_defer_until": None,
        }
    ]

    # The worker sends it to Razorpay...
    ctx = {"session_factory": session_factory(db_session)}
    assert await tasks.issue_refund(ctx, payment_id=str(payment_id)) == "pending"
    assert gateway.refunds == [{"id": "rfnd_fake_1", "payment_id": "pay_r1", "amount": 150000}]
    # ...and a retry of the same job doesn't refund twice.
    assert await tasks.issue_refund(ctx, payment_id=str(payment_id)) == "skipped"
    assert len(gateway.refunds) == 1

    # Razorpay confirms via webhook; redelivery changes nothing.
    body, headers = webhook_body_refund("rfnd_fake_1", "pay_r1", 150000, event_id="evt_rf1")
    first = await client.post(f"{API}/payments/webhook", content=body, headers=headers)
    assert first.json()["status"] == "refund_processed"
    again = await client.post(f"{API}/payments/webhook", content=body, headers=headers)
    assert again.json()["status"] == "duplicate"
    payment = await _payment(db_session)
    assert payment.refund_status is RefundStatus.PROCESSED
    assert payment.refunded_at is not None


async def test_late_client_cancel_gets_no_refund(
    client: AsyncClient, world: World, db_session: AsyncSession, jobs: FakeJobQueue
) -> None:
    booking, _ = await _paid_booking(client, world, db_session, timedelta(hours=3))
    await _cancel(client, world.client.headers, booking["id"])

    payment = await _payment(db_session)
    assert payment.refund_status is RefundStatus.NOT_ELIGIBLE
    assert payment.refund_amount_paise is None
    assert not [n for n, _ in jobs.jobs if n == "issue_refund"]


async def test_late_advocate_cancel_still_refunds_in_full(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    booking, _ = await _paid_booking(client, world, db_session, timedelta(hours=1))
    await _cancel(client, world.advocate.headers, booking["id"])
    payment = await _payment(db_session)
    assert payment.refund_status is RefundStatus.REQUESTED
    assert payment.refund_amount_paise == payment.amount_paise


async def test_refund_marked_processed_immediately_when_gateway_says_so(
    client: AsyncClient, world: World, db_session: AsyncSession, gateway: FakeGateway
) -> None:
    gateway.refund_status = "processed"
    booking, payment_id = await _paid_booking(client, world, db_session, timedelta(hours=48))
    await _cancel(client, world.client.headers, booking["id"])
    ctx = {"session_factory": session_factory(db_session)}
    assert await tasks.issue_refund(ctx, payment_id=str(payment_id)) == "processed"
    assert (await _payment(db_session)).refund_status is RefundStatus.PROCESSED


async def test_refund_job_retries_then_marks_failed(
    client: AsyncClient, world: World, db_session: AsyncSession, gateway: FakeGateway
) -> None:
    booking, payment_id = await _paid_booking(client, world, db_session, timedelta(hours=48))
    await _cancel(client, world.client.headers, booking["id"])
    gateway.refund_error = PaymentGatewayError("provider down")

    class _Job:
        def __init__(self, retryable: bool) -> None:
            self.retryable = retryable

    factory = session_factory(db_session)
    with pytest.raises(PaymentGatewayError):  # not the last attempt: let SAQ retry
        await tasks.issue_refund(
            {"session_factory": factory, "job": _Job(True)}, payment_id=str(payment_id)
        )
    assert (await _payment(db_session)).refund_status is RefundStatus.REQUESTED

    result = await tasks.issue_refund(
        {"session_factory": factory, "job": _Job(False)}, payment_id=str(payment_id)
    )
    assert result == "failed"
    assert (await _payment(db_session)).refund_status is RefundStatus.FAILED


async def test_refund_webhook_rejects_wrong_amount(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    booking, _payment_id = await _paid_booking(client, world, db_session, timedelta(hours=48))
    await _cancel(client, world.client.headers, booking["id"])
    body, headers = webhook_body_refund("rfnd_x", "pay_r1", 1)
    resp = await client.post(f"{API}/payments/webhook", content=body, headers=headers)
    assert resp.json()["status"] == "rejected_refund_amount_mismatch"
    assert (await _payment(db_session)).refund_status is RefundStatus.REQUESTED


# ---------------------------------------------------------------- reschedule cutoff


async def test_reschedule_blocked_within_24_hours(
    client: AsyncClient, world: World, db_session: AsyncSession
) -> None:
    booking, _ = await _paid_booking(client, world, db_session, timedelta(hours=23))
    new_start = (await free_slots(client, world.advocate.id, days=5))[-1]
    resp = await client.post(
        f"{API}/bookings/{booking['id']}/reschedule",
        headers=world.client.headers,
        json={"start_at": new_start},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "reschedule_window_closed"


async def test_second_booking_unaffected(client: AsyncClient, world: World) -> None:
    """Sanity: refund changes don't affect normal unpaid cancellation."""
    start = (await free_slots(client, world.advocate.id))[0]
    data = await book(client, world.client, world.advocate.id, start)
    await _cancel(client, world.client.headers, data["id"])
