from celery import shared_task
from clients.models import AdvocateProfile, Case
from django.db.models import Prefetch

@shared_task(name="client_service.tasks.get_advocates")
def get_advocates(name=None, city=None, specialization_id=None):

    qs = AdvocateProfile.objects.prefetch_related("specializations").all()

    if name:
        qs = qs.filter(full_name__icontains=name)
    if city:
        qs = qs.filter(city__iexact=city)
    if specialization_id:
        qs = qs.filter(specializations__id=specialization_id)

    result = []
    for a in qs:
        result.append({
            "id": a.id,
            "full_name": a.full_name,
            "phone": a.phone,
            "gender": a.gender,
            "dob": str(a.dob) if a.dob else None,
            "bar_council_id": a.bar_council_id,
            "enrollment_year": a.enrollment_year,
            "experience_years": a.experience_years,
            "languages": a.languages,
            "specializations": [{"id": s.id, "name": s.name} for s in a.specializations.all()],
            "city": a.city,
            "state": a.state,
            "pincode": a.pincode,
            "profile_image": a.profile_image,
            "is_verified": a.is_verified,
            "rating": a.rating,
            "cases_count": a.cases_count,
            "wins_count": a.wins_count,
            "created_at": a.created_at.isoformat(),
            "updated_at": a.updated_at.isoformat(),
        })
    return result



@shared_task(name="client_service.tasks.get_advocate_detail")
def get_advocate_detail(advocate_id):
    try:
        a = AdvocateProfile.objects.prefetch_related("specializations").get(id=advocate_id)
        return {
            "id": a.id,
            "full_name": a.full_name,
            "phone": a.phone,
            "gender": a.gender,
            "dob": str(a.dob) if a.dob else None,
            "bar_council_id": a.bar_council_id,
            "experience_years": a.experience_years,
            "languages": a.languages,
            "specializations": [{"id": s.id, "name": s.name} for s in a.specializations.all()],
            "city": a.city,
            "state": a.state,
            "pincode": a.pincode,
            "profile_image": a.profile_image,
            "is_verified": a.is_verified,
            "rating": a.rating,
            "cases_count": a.cases_count,
            "wins_count": a.wins_count,
        }
    except AdvocateProfile.DoesNotExist:
        return None



@shared_task(name="client_service.tasks.get_cases_by_client")
def get_cases_by_client(client_id):
    cases = Case.objects.filter(client_id=client_id)

    return [
        {
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "case_number": c.case_number,
            "advocate_id": c.advocate_id,
            "status": c.status,
            "result": c.result,
            "hearing_date": str(c.hearing_date) if c.hearing_date else None,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        }
        for c in cases
    ]



@shared_task(name="client_service.tasks.get_case_detail")
def get_case_detail(case_id, client_id):
    try:
        c = Case.objects.get(id=case_id, client_id=client_id)
        return {
            "id": c.id,
            "title": c.title,
            "description": c.description,
            "case_number": c.case_number,
            "advocate_id": c.advocate_id,
            "status": c.status,
            "result": c.result,
            "hearing_date": str(c.hearing_date) if c.hearing_date else None,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        }
    except Case.DoesNotExist:
        return None
