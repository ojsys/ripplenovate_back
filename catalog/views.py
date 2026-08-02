from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import ProductLine
from .serializers import ProductLineSerializer


@api_view(["GET"])
@permission_classes([AllowAny])
def product_lines(request):
    """The live catalogue: active lines and their active services.

    Public so the landing page and the brief form are driven by the same data
    an admin edits — marketing and the product can't drift apart.
    """
    lines = (ProductLine.objects.filter(is_active=True)
             .prefetch_related("services"))
    return Response(ProductLineSerializer(lines, many=True).data)
