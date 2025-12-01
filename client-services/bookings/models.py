from django.db import models
from django.utils import timezone

class Booking(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Confirmed', 'Confirmed'),
        ('Cancelled', 'Cancelled'),
        ('Completed', 'Completed'),
    ]

    id = models.AutoField(primary_key=True)
    client_id = models.IntegerField()       # from user-service
    advocate_id = models.IntegerField()     # from advocate/user-service
    appointment_datetime = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = 'booking'
        ordering = ['-appointment_datetime']

    def __str__(self):
        return f"Booking {self.id} for Client {self.client_id} with Advocate {self.advocate_id}"
