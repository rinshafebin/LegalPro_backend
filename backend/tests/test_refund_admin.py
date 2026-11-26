"""Admin handling of refunds that need a person, and admin alerts when refunds fail."""

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.gateway import PaymentGatewayError
from app.modules.payments.models import Payment, RefundStatus
from app.modules.users.models import UserRole
from app.workers import tasks
from tests.booking_support import World, session_factory, world
from tests.conftest import FakeGateway, FakeJobQueue
from tests.helpers import API, Actor, make_user, webhook_body_refund
from tests.test_refunds import _cancel, _paid_booking, _payment

__all__ = ["world"]


class _Job:
    retryable = False  # final attempt


async def _failed_refund(
    client: AsyncClient, world: World, db_session: AsyncSession, gateway: FakeGateway
) -> uuid.UUID:
    booking, payment_id = await _paid_booking(client, world, db_session, timedelta(hours=48))
    await _cancel(client, world.client.headers, booking["id"])
    gateway.refund_error = PaymentGatewayError("provider down")
    ctx = {"session_factory": session_factory(db_session), "job": _Job()}
    assert await tasks.issue_refund(ctx, payment_id=str(payment_id)) == "failed"
    gateway.refund_error = None
    return payment_id


@pytest.fixture
async def admin(db_session: AsyncSession) -> Actor:
    return await make_user(db_session, UserRole.ADMIN, "admin@example.com")


async def test_final_failure_alerts_admins_without_client_details(
    client: AsyncClient,
    world: World,
    db_session: AsyncSession,
    gateway: FakeGateway,
    jobs: FakeJobQueue,
    admin: Actor,
) -> None:
    payment_id = await _failed_refund(client, world, db_session, gateway)
    [email] = jobs.emails_to("admin@example.com")
    assert email["subject"] == "Refund needs attention"
    assert str(payment_id) in email["body"]
    assert "/admin/refunds" in email["body"]
    assert "c1@example.com" not in email["body"]
    assert "Asha" not in email["body"]
    notes = (await client.get(f"{API}/notifications", headers=admin.headers)).json()["items"]
    assert [n["type"] for n in notes] == ["refund.attention"]


async def test_webhook_refund_failure_alerts_admins_once(
    client: AsyncClient,
    world: World,
    db_session: AsyncSession,
    gateway: FakeGateway,
    jobs: FakeJobQueue,
    admin: Actor,
) -> None:
    booking, payment_id = await _paid_booking(client, world, db_session, timedelta(hours=48))
    await _cancel(client, world.client.headers, booking["id"])
    ctx = {"session_factory": session_factory(db_session)}
    assert await tasks.issue_refund(ctx, payment_id=str(payment_id)) == "pending"
    refund_id = gateway.refunds[0]["id"]
    for _ in range(2):  # second one is a separate event for the same refund
        body, headers = webhook_body_refund(refund_id, "pay_r1", 150000, event="refund.failed")
        await client.post(f"{API}/payments/webhook", content=body, headers=headers)
    assert (await _payment(db_session)).refund_status is RefundStatus.FAILED
    assert len(jobs.emails_to("admin@example.com")) == 1


async def test_list_retry_and_audit(
    client: AsyncClient,
    world: World,
    db_session: AsyncSession,
    gateway: FakeGateway,
    jobs: FakeJobQueue,
    admin: Actor,
) -> None:
    payment_id = await _failed_refund(client, world, db_session, gateway)
    listed = (await client.get(f"{API}/admin/refunds", headers=admin.headers)).json()
    [issue] = listed["items"]
    assert issue["payment_id"] == str(payment_id)
    assert issue["refund_status"] == "failed"
    assert issue["client_email"] == "c1@example.com"

    jobs.jobs.clear()
    resp = await client.post(f"{API}/admin/refunds/{payment_id}/retry", headers=admin.headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["refund_status"] == "requested"
    [(_name, kwargs)] = [(n, kw) for n, kw in jobs.jobs if n == "issue_refund"]
    assert kwargs["_job_id"].startswith(f"payment:{payment_id}:refund:retry:")

    ctx = {"session_factory": session_factory(db_session)}
    assert await tasks.issue_refund(ctx, payment_id=str(payment_id)) == "pending"
    assert (await client.get(f"{API}/admin/refunds", headers=admin.headers)).json()["items"] == []

    again = await client.post(f"{API}/admin/refunds/{payment_id}/retry", headers=admin.headers)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "refund_not_actionable"

    logs = (
        await client.get(
            f"{API}/admin/audit-logs",
            headers=admin.headers,
            params={"action": "payment.refund_retried"},
        )
    ).json()["items"]
    assert [(r["entity_id"], r["details"]) for r in logs] == [(str(payment_id), {"from": "failed"})]


async def test_mark_handled_closes_it_for_good(
    client: AsyncClient,
    world: World,
    db_session: AsyncSession,
    gateway: FakeGateway,
    admin: Actor,
) -> None:
    payment_id = await _failed_refund(client, world, db_session, gateway)
    url = f"{API}/admin/refunds/{payment_id}/mark-handled"
    assert (await client.post(url, headers=admin.headers, json={"note": ""})).status_code == 422
    resp = await client.post(
        url, headers=admin.headers, json={"note": "Refunded in Razorpay dashboard, rfnd_manual"}
    )
    assert resp.json()["refund_status"] == "resolved"
    payment = await _payment(db_session)
    assert payment.refund_resolved_by_id == admin.id
    assert payment.refund_resolution_note == "Refunded in Razorpay dashboard, rfnd_manual"

    # A late webhook doesn't reopen it.
    body, headers = webhook_body_refund("rfnd_late", "pay_r1", 150000)
    late = await client.post(f"{API}/payments/webhook", content=body, headers=headers)
    assert late.json()["status"] == "already_resolved"
    assert (await _payment(db_session)).refund_status is RefundStatus.RESOLVED


async def test_retry_needs_payment_ids_otherwise_manual(
    client: AsyncClient,
    world: World,
    db_session: AsyncSession,
    gateway: FakeGateway,
    admin: Actor,
) -> None:
    payment_id = await _failed_refund(client, world, db_session, gateway)
    await db_session.execute(
        update(Payment)
        .where(Payment.id == payment_id)
        .values(refund_status=RefundStatus.PENDING_REVIEW, refund_amount_paise=None)
    )
    await db_session.commit()
    resp = await client.post(f"{API}/admin/refunds/{payment_id}/retry", headers=admin.headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "refund_needs_manual_handling"
    filtered = await client.get(
        f"{API}/admin/refunds", headers=admin.headers, params={"refund_status": "pending_review"}
    )
    assert [i["payment_id"] for i in filtered.json()["items"]] == [str(payment_id)]
    assert await db_session.scalar(select(Payment.refund_status)) is RefundStatus.PENDING_REVIEW


async def test_refund_admin_endpoints_are_admin_only(client: AsyncClient, world: World) -> None:
    fake = uuid.uuid4()
    for actor in (world.client, world.advocate):
        assert (await client.get(f"{API}/admin/refunds", headers=actor.headers)).status_code == 403
        resp = await client.post(f"{API}/admin/refunds/{fake}/retry", headers=actor.headers)
        assert resp.status_code == 403
