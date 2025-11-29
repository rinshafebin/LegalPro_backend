# client_service/celery_app.py
import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "client_service.settings")

app = Celery("client_service")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
