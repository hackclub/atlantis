from django.conf import settings


def mihi_mode(request):
    return {"mihi_mode": settings.MIHI_MODE}
