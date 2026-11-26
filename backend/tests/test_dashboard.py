"""GET /advocates/me/dashboard."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bookings.models import Booking, BookingStatus
from app.modules.payments.models import Payment, PaymentStatus, RefundStatus
from tests.chat_support import ChatWorld, chat, make_booking
from tests.helpers import API

__all__ = ["chat"]
URL = f"{API}/advocates/me/dashboard"


async def test_dashboard_counts(
    client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    # A second case, closed.
    booking2 = await make_booking(
        db_session, chat.other_client.id, chat.lead.id, BookingStatus.COMPLETED, days=-3
    )
    case2 = (
        await client.post(
            f"{API}/cases",
            headers=chat.lead.headers,
            json={"booking_id": str(booking2), "title": "Old"},
        )
    ).json()
    await client.patch(
        f"{API}/cases/{case2['id']}", headers=chat.lead.headers, json={"status": "closed"}
    )

    hearings = f"{API}/cases/{chat.case_id}/hearings"
    soon = (
        await client.post(
            hearings,
            headers=chat.lead.headers,
            json={"scheduled_at": (now + timedelta(days=2)).isoformat(), "court": "HC"},
        )
    ).json()
    await client.post(
        hearings,
        headers=chat.lead.headers,
        json={"scheduled_at": (now + timedelta(days=10)).isoformat()},
    )

    upcoming = await make_booking(
        db_session, chat.client.id, chat.lead.id, BookingStatus.CONFIRMED, days=5
    )
    pending = Booking(
        client_id=chat.other_client.id,
        advocate_id=chat.lead.id,
        start_at=now + timedelta(days=6),
        end_at=now + timedelta(days=6, minutes=30),
        status=BookingStatus.PENDING_PAYMENT,
        fee_amount=Decimal("500.00"),
        currency="INR",
        expires_at=now + timedelta(minutes=10),
    )
    db_session.add(pending)
    await db_session.commit()
    db_session.add(
        Payment(
            booking_id=booking2,
            razorpay_order_id="order_dash",
            razorpay_payment_id="pay_dash",
            amount_paise=100000,
            currency="INR",
            status=PaymentStatus.CAPTURED,
            refund_status=RefundStatus.FAILED,
        )
    )
    await db_session.commit()

    lead = (await client.get(URL, headers=chat.lead.headers)).json()
    assert lead["cases_by_status"] == {"open": 1, "on_hold": 0, "closed": 1}
    assert [h["id"] for h in lead["upcoming_hearings"]] == [soon["id"]]  # 10-day one excluded
    assert lead["upcoming_hearings"][0]["case_number"].startswith("CASE-")
    # The booking the case came from is 3 days ahead (from the fixture) + the new one.
    starts = [b["start_at"] for b in lead["upcoming_bookings"]]
    assert starts == sorted(starts)
    assert str(upcoming) in {b["id"] for b in lead["upcoming_bookings"]}
    assert lead["upcoming_bookings_total"] == len(starts)
    assert lead["upcoming_bookings"][0]["client_name"] == "Owner"
    assert lead["pending_payments"] == 1
    assert lead["refunds"] == {"requested": 0, "failed": 1, "pending_review": 0}

    member = (await client.get(URL, headers=chat.member.headers)).json()
    assert member["cases_by_status"] == {"open": 1, "on_hold": 0, "closed": 0}
    assert len(member["upcoming_hearings"]) == 1
    assert member["upcoming_bookings"] == []
    assert member["pending_payments"] == 0

    outsider = (await client.get(URL, headers=chat.outsider.headers)).json()
    assert outsider["cases_by_status"] == {"open": 0, "on_hold": 0, "closed": 0}
    assert outsider["upcoming_hearings"] == []


async def test_dashboard_is_for_advocates(client: AsyncClient, chat: ChatWorld) -> None:
    assert (await client.get(URL, headers=chat.client.headers)).status_code == 403
    assert (await client.get(URL)).status_code == 401
