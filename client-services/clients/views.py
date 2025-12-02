from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from client_service.celery import app

class AdvocateSearchView(APIView):
    def get(self, request):
        task = app.send_task(
            "client_service.tasks.get_advocates",
            kwargs={
                "name": request.GET.get("name"),
                "city": request.GET.get("city"),
                "specialization_id": request.GET.get("specialization_id"),
                "min_rating": request.GET.get("min_rating"),
                "min_experience": request.GET.get("min_experience")
            }
        )

        result = task.get(timeout=20)
        return Response({"advocates": result}, status=status.HTTP_200_OK)



class AdvocateDetailView(APIView):
    def get(self, request, advocate_id):
        task = app.send_task(
            "client_service.tasks.get_advocate_detail",
            kwargs={"advocate_id": advocate_id}
        )

        result = task.get(timeout=20)
        if not result:
            return Response({"error": "Advocate not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response({"advocate": result}, status=status.HTTP_200_OK)



class CaseListView(APIView):
    def get(self, request):
        client_id = request.GET.get("client_id")

        task = app.send_task(
            "client_service.tasks.get_cases_by_client",
            kwargs={"client_id": int(client_id)}
        )

        cases = task.get(timeout=20)
        return Response({"cases": cases}, status=status.HTTP_200_OK)



class CaseDetailView(APIView):
    def get(self, request, case_id):
        client_id = request.GET.get("client_id")

        task = app.send_task(
            "client_service.tasks.get_case_detail",
            kwargs={"case_id": int(case_id), "client_id": int(client_id)}
        )

        case = task.get(timeout=20)
        if not case:
            return Response({"error": "Case not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response({"case": case}, status=status.HTTP_200_OK)
