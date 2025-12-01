# case_service/views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

from .models import Case, CaseDocument, CaseNote, CaseTeamMember
from .serializers import (
    CaseDetailSerializer,
    CaseListSerializer,
    CaseTeamMemberSerializer,
    CaseDocumentSerializer,
    CaseNoteSerializer
)
from .tasks import (
    notify_client_new_case,
    notify_advocate_team,
    notify_case_update,
    notify_hearing_date_update,
    notify_new_note
)


# -------------------------------
# Case Views
# -------------------------------
class CaseCreateView(APIView):
    """Create a new case."""
    def post(self, request):
        serializer = CaseDetailSerializer(data=request.data)
        if serializer.is_valid():
            case = serializer.save()
            
            # Trigger async notifications
            notify_client_new_case.delay(case.id)
            notify_advocate_team.delay(case.id)
            
            return Response({"message": "Case created", "data": serializer.data}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CaseListView(APIView):
    """List cases for a given client or advocate."""
    def get(self, request):
        client_id = request.query_params.get("client_id")
        advocate_id = request.query_params.get("advocate_id")
        
        if client_id:
            cases = Case.objects.filter(client_id=client_id).order_by("-created_at")
        elif advocate_id:
            cases = Case.objects.filter(advocate_id=advocate_id).order_by("-created_at")
        else:
            return Response({"error": "client_id or advocate_id required"}, status=400)
        
        serializer = CaseListSerializer(cases, many=True)
        return Response(serializer.data, status=200)


class CaseDetailView(APIView):
    """Get full details of a case."""
    def get(self, request, case_id):
        case_obj = get_object_or_404(Case, id=case_id)
        serializer = CaseDetailSerializer(case_obj)
        return Response(serializer.data, status=200)


class CaseUpdateView(APIView):
    """Partial update of case info."""
    def patch(self, request, case_id):
        case_obj = get_object_or_404(Case, id=case_id)
        serializer = CaseDetailSerializer(case_obj, data=request.data, partial=True)
        if serializer.is_valid():
            updated_case = serializer.save()
            
            # Trigger async update notification
            notify_case_update.delay(updated_case.id)
            
            return Response({"message": "Case updated", "data": serializer.data}, status=200)
        return Response(serializer.errors, status=400)


# -------------------------------
# Case Team Views
# -------------------------------
class CaseTeamAddView(APIView):
    """Add a team member to a case."""
    def post(self, request, case_id):
        case_obj = get_object_or_404(Case, id=case_id)
        data = {
            "case": case_id,
            "user_id": request.data.get("user_id"),
            "role": request.data.get("role"),
        }
        serializer = CaseTeamMemberSerializer(data=data)
        if serializer.is_valid():
            team_member = serializer.save()
            
            # Notify team members via Celery
            notify_advocate_team.delay(case_obj.id)
            
            return Response({"message": "Team member added", "data": serializer.data}, status=201)
        return Response(serializer.errors, status=400)



class AddHearingDateView(APIView):
    """Add or update a hearing date."""
    def post(self, request, case_id):
        case_obj = get_object_or_404(Case, id=case_id)
        hearing_date = request.data.get("hearing_date")
        if not hearing_date:
            return Response({"error": "hearing_date required"}, status=400)
        
        case_obj.hearing_date = hearing_date
        case_obj.save()
        
        # Notify relevant services
        notify_hearing_date_update.delay(case_obj.id)
        
        return Response({"message": "Hearing date added"}, status=201)



class CaseDocumentAddView(APIView):
    """Add a document to a case."""
    def post(self, request, case_id):
        case_obj = get_object_or_404(Case, id=case_id)
        serializer = CaseDocumentSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(case=case_obj)
            return Response({"message": "Document added", "data": serializer.data}, status=201)
        return Response(serializer.errors, status=400)



class CaseNoteAddView(APIView):
    """Add a note to a case."""
    def post(self, request, case_id):
        case_obj = get_object_or_404(Case, id=case_id)
        serializer = CaseNoteSerializer(data=request.data)
        if serializer.is_valid():
            note = serializer.save(case=case_obj)
            
            # Notify client and advocate
            notify_new_note.delay(case_obj.id, note.id)
            
            return Response({"message": "Note added", "data": serializer.data}, status=201)
        return Response(serializer.errors, status=400)
