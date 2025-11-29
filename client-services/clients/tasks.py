from celery import shared_task

@shared_task
def notify_appointment_created(app_id, advocate_id, start_time):
    print(f"[EVENT] Appointment created -> Notify advocate-service later")


@shared_task
def send_payment_event(payment_id, case_id, amount):
    print(f"[EVENT] Payment completed -> Notify advocate/user-service later")
