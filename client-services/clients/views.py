from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from clients.serializers import AdvocateProfileSerializer, CaseSerializer
from client_service.celery import app

class AdvocateSearchView(APIView):
    def get(self, request):
        # Query params
        name = request.GET.get("name")
        city = request.GET.get("city")
        specialization_id = request.GET.get("specialization_id")
        min_rating = request.GET.get("min_rating")
        min_experience = request.GET.get("min_experience")

        # Call Celery task
        task = app.send_task(
            "client_service.tasks.get_advocates",
            kwargs={
                "name": name,
                "city": city,
                "specialization_id": specialization_id,
                "min_rating": min_rating,
                "min_experience": min_experience
            }
        )

        result = task.get(timeout=20)  # Wait for Celery response

        if not result:
            return Response({"advocates": []}, status=status.HTTP_200_OK)

        serializer = AdvocateProfileSerializer(result, many=True)
        return Response({"advocates": serializer.data}, status=status.HTTP_200_OK)



class AdvocateDetailView(APIView):
    def get(self, request, advocate_id):
        # Call Celery task to fetch advocate
        task = app.send_task(
            "client_service.tasks.get_advocate_detail",
            kwargs={"advocate_id": advocate_id}
        )
        advocate = task.get(timeout=20)

        if not advocate:
            return Response({"error": "Advocate not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = AdvocateProfileSerializer(advocate)
        return Response({"advocate": serializer.data}, status=status.HTTP_200_OK)
    
    


class CaseListView(APIView):
    def get(self, request):
        client_id = request.GET.get("client_id")  # could get from auth in future
        task = app.send_task("client_service.tasks.get_cases_by_client", kwargs={"client_id": int(client_id)})
        cases = task.get(timeout=20)
        serializer = CaseSerializer(cases, many=True)
        return Response({"cases": serializer.data}, status=status.HTTP_200_OK)


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
        serializer = CaseSerializer(case)
        return Response({"case": serializer.data}, status=status.HTTP_200_OK)