from django.db import models
from users.models import User  # user-service User model
from cases.models import Case  # case-service Case model

class ClientSearchHistory(models.Model):
    client = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'client'})
    query = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)


class ClientCaseDocument(models.Model):
    client = models.ForeignKey(User, on_delete=models.CASCADE, limit_choices_to={'role': 'client'})
    case = models.ForeignKey(Case, on_delete=models.CASCADE)
    file = models.FileField(upload_to="client_documents/")
    description = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
