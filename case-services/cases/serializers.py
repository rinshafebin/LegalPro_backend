from rest_framework import serializers
from .models import Case, CaseDocument, CaseNote, CaseTeamMember

class CaseDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = CaseDocument
        fields = ["id", "document", "uploaded_at", "visible_to_client", "visible_to_advocate"]

class CaseNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = CaseNote
        fields = ["id", "note", "created_by_id", "created_at"]

class CaseTeamMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = CaseTeamMember
        fields = ["id", "user_id", "role"]

class CaseListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Case
        fields = ["id", "title", "case_number", "status", "priority", "hearing_date", "created_at"]

class CaseDetailSerializer(serializers.ModelSerializer):
    team = CaseTeamMemberSerializer(many=True, read_only=True)
    documents = CaseDocumentSerializer(many=True, read_only=True)
    notes = CaseNoteSerializer(many=True, read_only=True)

    class Meta:
        model = Case
        fields = [
            "id", "title", "description", "case_number",
            "client_id", "advocate_id",
            "priority", "status", "result", "hearing_date",
            "created_at", "updated_at",
            "team", "documents", "notes",
        ]

