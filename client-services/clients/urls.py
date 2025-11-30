from django.urls import path
from .views import AdvocateDetailView, AdvocateSearchView

urlpatterns = [
    path("advocates/search/", AdvocateSearchView.as_view(),name="advocate-search"),
    path("advocates/<int:advocate_id>/", AdvocateDetailView.as_view(), name="advocate-detail"),

]

