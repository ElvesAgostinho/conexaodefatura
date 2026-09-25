"""API v1: sistemas externos enviam documentos no formato canónico e consultam o estado.

  GET  /api/v1/ping/                          verifica a chave (empresa e origem)
  POST /api/v1/documents/                     envia um documento
  GET  /api/v1/documents/?status=&page=       lista os documentos desta origem
  GET  /api/v1/documents/<source_document_id>/ estado de um documento

Respostas do POST:
  201 criado · 200 já existia igual (idempotente) ou atualizado · 400 estrutura inválida
  413 pedido demasiado grande
  409 conflito (documento comunicado alterado, ou número fiscal de outra origem)
"""

from django.core.exceptions import RequestDataTooBig
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.services import audit_event, log_integration
from invoices.canonical import CanonicalError
from invoices.models import Invoice, InvoiceStatus
from invoices.services import DocumentConflict, import_document

from .authentication import ApiKeyAuthentication, HasApiKey
from .throttling import ApiKeyRateThrottle

PAGE_SIZE = 100


def document_status(invoice: Invoice) -> dict:
    return {
        "id": invoice.pk,
        "source": invoice.source_system,
        "source_document_id": invoice.source_document_id,
        "document_type": invoice.document_type,
        "series": invoice.series,
        "document_number": invoice.document_number,
        "document_date": invoice.document_date.isoformat(),
        "total": str(invoice.total),
        "status": invoice.status,
        "status_label": invoice.get_status_display(),
        "validation_errors": invoice.validation_errors,
        "agt": {
            "request_id": invoice.agt_request_id,
            "message": invoice.agt_message,
            "simulated": invoice.simulated,
            "sent_at": invoice.sent_at.isoformat() if invoice.sent_at else None,
            "confirmed_at": invoice.confirmed_at.isoformat() if invoice.confirmed_at else None,
        },
        "updated_at": invoice.updated_at.isoformat(),
    }


class ApiView(APIView):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [HasApiKey]
    throttle_classes = [ApiKeyRateThrottle]

    def source_invoices(self, request):
        return Invoice.objects.filter(company=request.user.company, source=request.user.source)


class PingView(ApiView):
    def get(self, request):
        return Response({
            "company": {"name": request.user.company.name, "nif": request.user.company.nif},
            "source": {"code": request.user.source.code, "name": request.user.source.name},
        })


class DocumentListView(ApiView):
    def get(self, request):
        invoices = self.source_invoices(request).order_by("-id")
        wanted = request.query_params.get("status")
        if wanted:
            if wanted not in InvoiceStatus.values:
                return Response({"errors": [f"status inválido; use um de {', '.join(InvoiceStatus.values)}."]},
                                status=status.HTTP_400_BAD_REQUEST)
            invoices = invoices.filter(status=wanted)
        paginator = Paginator(invoices, PAGE_SIZE)
        try:
            page = paginator.page(request.query_params.get("page") or 1)
        except PageNotAnInteger:
            return Response({"errors": ["Página inválida."]}, status=status.HTTP_400_BAD_REQUEST)
        except EmptyPage:
            return Response({"errors": ["Página inexistente."]}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "count": paginator.count,
            "page": page.number,
            "pages": paginator.num_pages,
            "results": [document_status(invoice) for invoice in page.object_list],
        })

    def post(self, request):
        client = request.user
        try:
            data = request.data
        except RequestDataTooBig:
            return Response({"errors": ["Pedido demasiado grande."]}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        try:
            result = import_document(client.source, data)
        except CanonicalError as exc:
            log_integration("API_RECEIVE", company=client.company, status="INVALID",
                            error_message="; ".join(exc.errors))
            return Response({"errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST)
        except DocumentConflict as exc:
            log_integration("API_RECEIVE", company=client.company, invoice=exc.invoice, status="CONFLICT",
                            error_message=str(exc))
            body = {"errors": [str(exc)]}
            if exc.invoice is not None and exc.invoice.source_id == client.source.pk:
                body["document"] = document_status(exc.invoice)
            return Response(body, status=status.HTTP_409_CONFLICT)

        if not result.duplicate:
            audit_event("API_DOCUMENT_UPDATED" if result.updated else "API_DOCUMENT_CREATED",
                        request=request._request, actor=str(client), company=client.company, obj=result.invoice,
                        details={"source": client.source.code, "document_number": result.invoice.document_number})
        body = document_status(result.invoice)
        body["result"] = "created" if result.created else "updated" if result.updated else "duplicate"
        return Response(body, status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK)


class DocumentDetailView(ApiView):
    def get(self, request, source_document_id):
        invoice = self.source_invoices(request).filter(source_document_id=source_document_id).first()
        if invoice is None:
            return Response({"errors": ["Documento não encontrado nesta origem."]}, status=status.HTTP_404_NOT_FOUND)
        return Response(document_status(invoice))
