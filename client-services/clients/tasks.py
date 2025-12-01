from celery import shared_task
from clients.models import AdvocateProfile, Case

@shared_task(name="client_service.tasks.get_advocates")
def get_advocates(name=None, city=None, specialization_id=None, min_rating=None, min_experience=None):
    qs = AdvocateProfile.objects.all()

    if name:
        qs = qs.filter(full_name__icontains=name)
    
    if city:
        qs = qs.filter(city__iexact=city)

    if specialization_id:
        qs = qs.filter(specializations__id=specialization_id)

    if min_rating:
        qs = qs.filter(rating__gte=float(min_rating))

    if min_experience:
        qs = qs.filter(experience_years__gte=int(min_experience))

    return list(qs)



@shared_task(name="client_service.tasks.get_advocate_detail")
def get_advocate_detail(advocate_id):
    try:
        advocate = AdvocateProfile.objects.get(id=advocate_id)
        return advocate
    except AdvocateProfile.DoesNotExist:
        return None


@shared_task(name="client_service.tasks.get_cases_by_client")
def get_cases_by_client(client_id):
    return list(Case.objects.filter(client_id=client_id))


@shared_task(name="client_service.tasks.get_case_detail")
def get_case_detail(case_id, client_id):
    try:
        case = Case.objects.get(id=case_id, client_id=client_id)
        return case
    except Case.DoesNotExist:
        return None
