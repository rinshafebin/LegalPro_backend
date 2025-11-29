from django.db import models

class Appointment(models.Model):
    client_id = models.IntegerField()
    advocate_id = models.IntegerField()
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()

    STATUS = (
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    )

    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Appointment {self.id}"
    

class Payment(models.Model):
    STATUS = (
        ("created", "Created"),
        ("paid", "Paid"),
        ("failed", "Failed")
    )

    order_id = models.CharField(max_length=200, unique=True)
    razorpay_payment_id = models.CharField(max_length=200, null=True, blank=True)
    amount = models.IntegerField()
    currency = models.CharField(max_length=10, default="INR")
    status = models.CharField(max_length=20, choices=STATUS, default="created")
    user_id = models.IntegerField()
    case_id = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Payment {self.order_id} - {self.status}"   


class Transaction(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE)
    razorpay_payment_id = models.CharField(max_length=200)
    razorpay_signature = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Transaction for Payment {self.payment.order_id}"


class ExternalCase(models.Model):
    external_id = models.CharField(max_length=100, unique=True)
    title = models.CharField(max_length=200, null=True, blank=True)
    advocate_id = models.IntegerField(null=True, blank=True)
    client_id = models.IntegerField(null=True, blank=True)
    last_synced = models.DateTimeField(auto_now=True)
    
    def __str__(self):  
        return f"ExternalCase {self.external_id}"
