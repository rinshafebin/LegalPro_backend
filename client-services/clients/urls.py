from django.urls import path
from .views import AdvocateDetailView, AdvocateSearchView, CaseListView, CaseDetailView

urlpatterns = [
    path('advocates/search/', AdvocateSearchView.as_view(), name='advocate-search'),
    path('advocates/<int:advocate_id>/', AdvocateDetailView.as_view(), name='advocate-detail'),
    path('cases/', CaseListView.as_view(), name='case-list'),
    path('cases/<int:case_id>/', CaseDetailView.as_view(), name='case-detail'),
]

