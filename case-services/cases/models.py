from django.db import models
from django.utils import timezone

class Case(models.Model):
    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Pending', 'Pending'),
        ('Closed', 'Closed'),
    ]

    PRIORITY_CHOICES = [
        ('High', 'High'),
        ('Medium', 'Medium'),
        ('Low', 'Low'),
    ]

    RESULT_CHOICES = [
        ('Won', 'Won'),
        ('Lost', 'Lost'),
        ('Pending', 'Pending'),
    ]

    id = models.AutoField(primary_key=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    case_number = models.CharField(max_length=50, unique=True)

    # External service references
    client_id = models.IntegerField(null=True, db_index=True)
    advocate_id = models.IntegerField(null=True, db_index=True)

    # Workflow
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='Medium')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    result = models.CharField(max_length=20, choices=RESULT_CHOICES, default='Pending')
    hearing_date = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "case"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.case_number})"


class CaseTeamMember(models.Model):
    ROLE_CHOICES = [
        ('Lead', 'Lead'),
        ('Junior', 'Junior'),
        ('Reviewer', 'Reviewer'),
    ]

    id = models.AutoField(primary_key=True)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='team')
    user_id = models.IntegerField(db_index=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='Junior')

    class Meta:
        db_table = "case_team_member"
        unique_together = ("case", "user_id")

    def __str__(self):
        return f"{self.user_id} -> {self.case.case_number}"


class CaseDocument(models.Model):
    id = models.AutoField(primary_key=True)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='documents')
    document = models.FileField(upload_to="case_documents/")
    uploaded_at = models.DateTimeField(default=timezone.now)
    visible_to_client = models.BooleanField(default=True)
    visible_to_advocate = models.BooleanField(default=True)

    class Meta:
        db_table = "case_document"

    def __str__(self):
        return f"Document for Case {self.case.case_number}"


class CaseNote(models.Model):
    id = models.AutoField(primary_key=True)
    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name='notes')
    note = models.TextField()
    created_by_id = models.IntegerField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "case_note"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Note({self.created_by_id}) for Case {self.case.case_number}"

