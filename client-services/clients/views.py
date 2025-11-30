from rest_framework.views import APIView
from rest_framework.response import Response
from .models import AdvocateProfile
from .serializers import AdvocateSerializer,AdvocateDetailSerializer
from django.db.models import Q
from rest_framework import status

class AdvocateSearchView(APIView):
    def get(self, request):
        q = request.GET.get("q", "")
        city = request.GET.get("city")
        specialization = request.GET.get("specialization")
        min_exp = request.GET.get("min_exp")
        max_exp = request.GET.get("max_exp")

        advocates = AdvocateProfile.objects.all()

        # ---------------- FILTERS ----------------
        if q:
            advocates = advocates.filter(
                Q(full_name__icontains=q)
            )

        if city:
            advocates = advocates.filter(city__icontains=city)

        if specialization:
            advocates = advocates.filter(
                specializations__name__icontains=specialization
            )

        if min_exp:
            advocates = advocates.filter(experience_years__gte=min_exp)

        if max_exp:
            advocates = advocates.filter(experience_years__lte=max_exp)

        advocates = advocates.distinct()

        serializer = AdvocateSerializer(advocates, many=True)
        return Response(serializer.data)



class AdvocateDetailView(APIView):
    def get(self, request, advocate_id):
        try:
            advocate = AdvocateProfile.objects.get(id=advocate_id)
        except AdvocateProfile.DoesNotExist:
            return Response({"error": "Advocate not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = AdvocateDetailSerializer(advocate)
        return Response(serializer.data)