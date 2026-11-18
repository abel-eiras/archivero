"""
Generador de muestra de Fase 0:
Crea facturas PDF realistas de proveedores de farmacia (incluyendo recargo de equivalencia,
retención de IRPF, un abono/rectificativa y una factura con descuadre para la bandeja de revisión)
y extractos bancarios en Norma 43, CAMT.053 (XML) y CSV.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.db.database import Database
from app.services.credentials import CredentialManager
from app.services.pipeline import InvoiceIngestionPipeline
from app.services.reconciliation import BankReconciliationService


def build_invoice_pdf_bytes(lines: List[str]) -> bytes:
    """Genera un archivo PDF válido en memoria con capa de texto nativa."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 11)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = 800
    c.save()
    return buf.getvalue()


def get_sample_invoices_specs() -> List[Dict[str, Any]]:
    """Devuelve las especificaciones de las facturas PDF de muestra para Fase 0."""
    return [
        {
            "filename": "factura_distribuidora_norte_2026_11.pdf",
            "sender": "facturacion@distribuidoranorte.es",
            "lines": [
                "Proveedor: Distribuidora Farmaceutica Norte S.Coop.",
                "NIF: F28012345",
                "Factura Nº: FRA-2026-1001",
                "Fecha de emisión: 2026-11-02",
                "Fecha de vencimiento: 2026-11-15",
                "Cliente: Farmacia Central",
                "Concepto: Suministro de medicamentos y especialidades farmacéuticas",
                "Base Imponible: 1200,00 EUR",
                "IVA 4%: 48,00 EUR",
                "Recargo de Equivalencia: 6,00 EUR",
                "Total Factura: 1254,00 EUR",
                "Forma de pago: Domiciliación bancaria SEPA ES9121000418450200051332",
            ],
        },
        {
            "filename": "factura_cooperativa_sur_2026_11.pdf",
            "sender": "facturas@cooperativasur.es",
            "lines": [
                "Proveedor: Cooperativa Farmaceutica del Sur S.Coop.",
                "NIF: F41987654",
                "Factura Nº: FRA-2026-1002",
                "Fecha de emisión: 2026-11-05",
                "Fecha de vencimiento: 2026-11-20",
                "Concepto: Pedido diario medicamentos y parafarmacia",
                "Base Imponible: 850,00 EUR",
                "IVA 4%: 34,00 EUR",
                "Recargo de Equivalencia: 4,25 EUR",
                "Total Factura: 888,25 EUR",
                "Forma de pago: Domiciliación bancaria",
            ],
        },
        {
            "filename": "abono_cooperativa_sur_2026_11.pdf",
            "sender": "facturas@cooperativasur.es",
            "lines": [
                "Proveedor: Cooperativa Farmaceutica del Sur S.Coop.",
                "NIF: F41987654",
                "Abono Nº: AB-2026-0045",
                "Factura rectificativa por devolución de mercancía",
                "Fecha de emisión: 2026-11-08",
                "Fecha de vencimiento: 2026-11-20",
                "Base Imponible: -100,00 EUR",
                "IVA 4%: -4,00 EUR",
                "Recargo de Equivalencia: -0,50 EUR",
                "Total Factura: -104,50 EUR",
                "Forma de pago: Domiciliación bancaria",
            ],
        },
        {
            "filename": "factura_electricidad_2026_11.pdf",
            "sender": "clientes@energiapeninsular.es",
            "lines": [
                "Proveedor: Comercializadora Electrica Peninsular S.A.",
                "NIF: A95758389",
                "Factura Nº: FRA-2026-2050",
                "Fecha de emisión: 2026-11-10",
                "Fecha de vencimiento: 2026-11-18",
                "Concepto: Suministro eléctrico local farmacia noviembre",
                "Base Imponible: 210,00 EUR",
                "IVA 21%: 44,10 EUR",
                "Total Factura: 254,10 EUR",
                "Forma de pago: Domiciliación bancaria",
            ],
        },
        {
            "filename": "recibo_alquiler_local_2026_11.pdf",
            "sender": "administracion@inmobiliariagoya.es",
            "lines": [
                "Proveedor: Arrendamientos Urbanos Goya S.L.",
                "NIF: B28445566",
                "Factura Nº: ALQ-2026-011",
                "Fecha de emisión: 2026-11-01",
                "Fecha de vencimiento: 2026-11-05",
                "Concepto: Arrendamiento y alquiler de local comercial farmacia",
                "Base Imponible: 1500,00 EUR",
                "IVA 21%: 315,00 EUR",
                "Retención IRPF 19%: 285,00 EUR",
                "Total Factura: 1530,00 EUR",
                "Forma de pago: Transferencia bancaria",
            ],
        },
        {
            "filename": "factura_asesoria_2026_11.pdf",
            "sender": "facturacion@asesoriafarma.es",
            "lines": [
                "Proveedor: Asesoría Farmacéutica Integral S.L.",
                "NIF: B28998877",
                "Factura Nº: FRA-2026-3099",
                "Fecha de emisión: 2026-11-12",
                "Fecha de vencimiento: 2026-11-25",
                "Concepto: Servicios profesionales de gestoría laboral y fiscal",
                "Base Imponible: 200,00 EUR",
                "IVA 21%: 42,00 EUR",
                "Total Factura: 242,00 EUR",
                "Forma de pago: Domiciliación bancaria",
            ],
        },
        {
            "filename": "factura_dudosa_embalajes.pdf",
            "sender": "ventas@embalajesfarma.es",
            "lines": [
                "Proveedor: Bolsas y Etiquetas FarmaPack S.L.",
                "NIF: B12345678",
                "Factura Nº: FRA-2026-4012",
                "Fecha de emisión: 2026-11-14",
                "Fecha de vencimiento: 2026-11-28",
                "Concepto: Bolsas biodegradables y papel térmico para mostrador",
                "Base Imponible: 100,00 EUR",
                "IVA 21%: 21,00 EUR",
                "Total Factura: 135,00 EUR",  # Descuadre intencionado (100+21 != 135) -> Bandeja de revisión
            ],
        },
    ]


def build_sample_norma43_statement() -> str:
    """
    Construye un fichero Norma 43 válido de muestra con:
    1. Pago a Distribuidora Farmacéutica Norte: -1254,00 € (FRA-2026-1001)
    2. Pago agrupado a Cooperativa del Sur: -783,75 € (cubre FRA-2026-1002 de 888,25 € + Abono AB-2026-0045 de -104,50 €)
    3. Pago a Comercializadora Eléctrica: -254,10 € (FRA-2026-2050)
    """
    lines = [
        # Registro 11: Cabecera de cuenta
        "112100041812345678902611012611302000001500000009783FARMACIA CENTRAL             ",
        # Movimiento 1: Distribuidora Norte -1254.00 EUR el 2026-11-15
        "22    0418261115261115030001000000001254000000001001FRA-2026-1001   DIST. NORTE ",
        "2301RECIBO DOMICILIADO DISTRIBUIDORA NORTE  FACTURA FRA-2026-1001               ",
        # Movimiento 2: Pago agrupado Cooperativa del Sur -783.75 EUR (888.25 - 104.50) el 2026-11-20
        "22    0418261120261120030001000000000783750000001002REMESA          COOP. SUR   ",
        "2301ADEUDO SEPA COOPERATIVA FARMACEUTICA    LIQUIDACION FACTURA Y ABONO         ",
        # Movimiento 3: Comercializadora Eléctrica -254.10 EUR el 2026-11-18
        "22    0418261118261118030001000000000254100000002050FRA-2026-2050   ELECTRICA   ",
        "2301RECIBO COMERCIALIZADORA ELECTRICA       SUMINISTRO ELECTRICO                ",
        # Registro 33 y 88: Cierre
        "33210004181234567890000030000002291850000000000000000002000012708150978         ",
        "88999999999999999999000008                                                      ",
    ]
    return "\n".join(lines) + "\n"


def build_sample_camt053_xml() -> str:
    """Construye un extracto CAMT.053 ISO 20022 XML con el pago del alquiler (-1530,00 €)."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Id>STMT-2026-11</Id>
      <Acct>
        <Id><IBAN>ES9121000418450200051332</IBAN></Id>
        <Svcr><FinInstnId><Nm>Banco Principal Farmacia</Nm></FinInstnId></Svcr>
      </Acct>
      <Ntry>
        <Amt Ccy="EUR">1530.00</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-11-05</Dt></BookgDt>
        <ValDt><Dt>2026-11-05</Dt></ValDt>
        <AcctSvcrRef>REF-ALQ-2026-011</AcctSvcrRef>
        <AddtlNtryInf>TRANSFERENCIA ARRENDAMIENTOS URBANOS GOYA S.L. ALQ-2026-011</AddtlNtryInf>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def build_sample_csv_statement() -> str:
    """Construye un extracto CSV con un cargo de Asesoría pendiente de conciliar manualmente."""
    return (
        "Fecha;Fecha Valor;Concepto;Importe;Referencia\n"
        "25/11/2026;25/11/2026;RECIBO SEPA GESTORIA MENSUAL;-242,00;REC-3099\n"
    )


def generate_and_ingest_sample_dataset(
    db: Optional[Database] = None,
    creds: Optional[CredentialManager] = None,
) -> Dict[str, Any]:
    """Genera los PDFs y extractos bancarios de Fase 0 y los ingiere en la base de datos."""
    database = db or Database()
    credentials = creds or CredentialManager()
    pipeline = InvoiceIngestionPipeline(db=database, llm_api_key=credentials.get_llm_api_key("google_ai_studio"))

    insertadas = 0
    duplicadas = 0
    for spec in get_sample_invoices_specs():
        pdf_bytes = build_invoice_pdf_bytes(spec["lines"])
        res = pipeline.ingest_pdf(
            pdf_source=pdf_bytes,
            filename=spec["filename"],
            sender_email=spec["sender"],
        )
        if res.status == "insertada":
            insertadas += 1
        elif res.status == "duplicada":
            duplicadas += 1

    reconciler = BankReconciliationService(db=database)
    r_n43 = reconciler.import_statement(
        content=build_sample_norma43_statement(),
        filename="extracto_noviembre.n43",
        formato="norma43",
        banco="Banco Principal Farmacia",
        auto_reconcile=True,
    )
    r_camt = reconciler.import_statement(
        content=build_sample_camt053_xml(),
        filename="extracto_camt053.xml",
        formato="camt053",
        banco="Banco Principal Farmacia",
        auto_reconcile=True,
    )
    r_csv = reconciler.import_statement(
        content=build_sample_csv_statement(),
        filename="extracto_asesoria.csv",
        formato="csv",
        banco="Cuenta Secundaria Farmacia",
        auto_reconcile=True,
    )

    total_movs = (
        r_n43["nuevos_insertados"] + r_camt["nuevos_insertados"] + r_csv["nuevos_insertados"]
    )
    return {
        "facturas_insertadas": insertadas,
        "facturas_duplicadas": duplicadas,
        "movimientos_insertados": total_movs,
        "detalle_bancos": {"norma43": r_n43, "camt053": r_camt, "csv": r_csv},
    }


if __name__ == "__main__":
    summary = generate_and_ingest_sample_dataset()
    print("Muestra de Fase 0 generada e ingerida con éxito:", summary)
