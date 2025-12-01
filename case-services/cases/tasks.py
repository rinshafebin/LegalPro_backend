# case_service/tasks.py
from celery import shared_task
from .models import Case, CaseTeamMember
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

# ----------------- Case Creation/Update Tasks -----------------
@shared_task
def notify_client_new_case(case_id):
    """
    Notify client-service about a new case.
    Instead of API calls, just mark a 'notification' table or log.
    Client-service can poll or subscribe to these tasks.
    """
    try:
        case = Case.objects.get(id=case_id)
        logger.info(f"Notify client-service: New case {case.id} for client {case.client_id}")
        # Example: write to a shared notification table if needed
        # Client-service can query Case table directly since shared DB
    except Case.DoesNotExist:
        logger.error(f"Case {case_id} not found")


@shared_task
def notify_advocate_team(case_id):
    """
    Notify all team members about the case creation/update.
    No API call: just log or update a notification table.
    """
    try:
        case = Case.objects.get(id=case_id)
        team_members = CaseTeamMember.objects.filter(case=case)
        for member in team_members:
            logger.info(f"Notify advocate {member.user_id} about case {case.id}")
            # Could create a Notification model entry here
    except Case.DoesNotExist:
        logger.error(f"Case {case_id} not found")


@shared_task
def notify_case_update(case_id, message):
    """Generic task for notifying changes related to a case"""
    logger.info(f"Case {case_id} update: {message}")


# ----------------- Case Listing / Details Tasks -----------------
@shared_task
def get_cases_for_client(client_id):
    """Return all cases for a client (pollable by client-service)"""
    cases = Case.objects.filter(client_id=client_id).order_by("-created_at")
    return [{"id": c.id, "title": c.title, "status": c.status, "advocate_id": c.advocate_id} for c in cases]


@shared_task
def get_cases_for_advocate(advocate_id):
    """Return all cases for an advocate (pollable by advocate-service)"""
    cases = Case.objects.filter(advocate_id=advocate_id).order_by("-created_at")
    return [{"id": c.id, "title": c.title, "status": c.status, "client_id": c.client_id} for c in cases]


@shared_task
def get_case_details(case_id):
    """Return full details for a single case (shared DB)"""
    try:
        case = Case.objects.prefetch_related("team", "documents", "notes").get(id=case_id)
        team = [{"user_id": t.user_id, "role": t.role} for t in case.team.all()]
        documents = [{"id": d.id, "document": str(d.document)} for d in case.documents.all()]
        notes = [{"id": n.id, "note": n.note, "created_by_id": n.created_by_id} for n in case.notes.all()]

        return {
            "id": case.id,
            "title": case.title,
            "description": case.description,
            "case_number": case.case_number,
            "advocate_id": case.advocate_id,
            "client_id": case.client_id,
            "status": case.status,
            "priority": case.priority,
            "result": case.result,
            "hearing_date": case.hearing_date.isoformat() if case.hearing_date else None,
            "team": team,
            "documents": documents,
            "notes": notes,
        }
    except Case.DoesNotExist:
        logger.error(f"Case {case_id} not found")
        return {}
