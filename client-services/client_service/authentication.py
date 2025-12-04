# client_service/authentication.py
from rest_framework_simplejwt.authentication import JWTAuthentication

class RemoteJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user_id = validated_token.get("user_id")
        email = validated_token.get("email", "remote_user@example.com")

        # Create a simple user-like object dynamically
        class RemoteUser:
            def __init__(self, user_id, email):
                self.id = user_id
                self.email = email
                self.is_authenticated = True
                
            def __str__(self):
                return self.email

        return RemoteUser(user_id, email)
