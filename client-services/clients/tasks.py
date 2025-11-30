from celery import shared_task
from .models import Case

@shared_task(name="case_service.fetch_cases_for_client")
def fetch_cases_for_client(client_id):

    cases = Case.objects.filter(client_id=client_id).values(
        'id', 'title', 'description', 'advocate_id', 'client_id', 'status', 'created_at', 'updated_at'
    )
    return list(cases)


@shared_task(name="case_service.fetch_case_detail")
def fetch_case_detail(case_id):
    try:
        case = Case.objects.get(id=case_id)
        return {
            'id': case.id,
            'title': case.title,
            'description': case.description,
            'advocate_id': case.advocate_id,
            'client_id': case.client_id,
            'status': case.status,
            'created_at': case.created_at,
            'updated_at': case.updated_at
        }
    except Case.DoesNotExist:
        return {"error": "Case not found"}
