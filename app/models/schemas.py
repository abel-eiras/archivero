"""Esquemas Pydantic para validación y contratos de la API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class InvoiceExtraction(BaseModel):
    """Datos estructurados extraídos de un PDF de factura."""

    es_factura: bool = Field(default=True, description="Indica si el documento es una factura o abono válido")
    proveedor: str = Field(default="", description="Nombre o razón social del proveedor")
    nif: str = Field(default="", description="NIF/CIF del proveedor")
    numero_factura: str = Field(default="", description="Número o código de la factura")
    fecha_emision: Optional[str] = Field(default=None, description="Fecha de emisión en formato YYYY-MM-DD")
    fecha_vencimiento: Optional[str] = Field(default=None, description="Fecha de vencimiento en formato YYYY-MM-DD")
    base_imponible: float = Field(default=0.0, description="Base imponible total sin impuestos")
    iva: float = Field(default=0.0, description="Importe total de IVA")
    total: float = Field(default=0.0, description="Importe total de la factura")
    moneda: str = Field(default="EUR", description="Código ISO de moneda")
    tipo_documento: str = Field(default="factura_mercancia", description="Tipo de documento según catálogo")
    categoria: str = Field(default="compras_mercancia", description="Categoría de gasto/compra")
    nivel_confianza: float = Field(default=0.0, ge=0.0, le=1.0, description="Confianza de la extracción (0 a 1)")
    datos_extra: Dict[str, Any] = Field(
        default_factory=dict,
        description="Campos adicionales flexibles: recargo_equivalencia, irpf, forma_pago, iban, etc.",
    )


class ValidationResult(BaseModel):
    """Resultado de la validación aritmética, temporal y de campos obligatorios."""

    valido: bool = True
    cuadra_importes: bool = True
    fechas_coherentes: bool = True
    campos_obligatorios_completos: bool = True
    errores: List[str] = Field(default_factory=list)
    advertencias: List[str] = Field(default_factory=list)
    confianza_ajustada: float = 0.0
    estado_revision: str = "pendiente_revision"  # "confirmada" | "pendiente_revision"


class ProcessedPDFResult(BaseModel):
    """Resultado completo tras procesar un archivo PDF en el núcleo (Fase 1)."""

    hash_pdf: str
    metodo_extraccion_texto: str  # "texto_nativo" | "ocr_local" | "vision_llm"
    texto_extraido_longitud: int
    extraccion: InvoiceExtraction
    validacion: ValidationResult
    proveedor_llm_usado: str


class InvoiceUpdatePayload(BaseModel):
    """Payload para editar y/o confirmar una factura desde la bandeja de revisión o listado."""

    proveedor_nombre: Optional[str] = None
    nif: Optional[str] = None
    numero_factura: Optional[str] = None
    fecha_emision: Optional[str] = None
    fecha_vencimiento: Optional[str] = None
    base_imponible: Optional[float] = None
    iva: Optional[float] = None
    total: Optional[float] = None
    moneda: Optional[str] = None
    tipo_documento: Optional[str] = None
    categoria: Optional[str] = None
    estado_revision: Optional[str] = None
    estado_pago: Optional[str] = None
    notas: Optional[str] = None
    datos_extra: Optional[Dict[str, Any]] = None
    aprender_regla_proveedor: bool = False


class MailAccountCreate(BaseModel):
    """Configuración de una cuenta de correo (sin guardar secretos en SQLite)."""

    nombre: str
    email: str
    tipo_conexion: str = "imap"  # "imap" | "oauth_gmail" | "oauth_outlook"
    servidor_imap: str = ""
    puerto_imap: int = 993
    usar_ssl: bool = True
    remitentes_permitidos: List[str] = Field(default_factory=list)
    intervalo_minutos: int = 15
    activa: bool = True
    password_o_token: Optional[str] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    oauth_refresh_token: Optional[str] = None


class RuleCreate(BaseModel):
    """Regla configurable o aprendida para clasificar facturas y conciliar pagos."""

    tipo_regla: str  # "remitente_proveedor" | "remitente_categoria" | "concepto_proveedor" | "nif_categoria"
    patron: str
    proveedor_id: Optional[int] = None
    proveedor_nombre: Optional[str] = None
    categoria: Optional[str] = None
    tipo_documento: Optional[str] = None
    prioridad: int = 10
    aprendida: bool = False
    activa: bool = True


class ManualReconciliationRequest(BaseModel):
    """Petición de conciliación manual muchos-a-muchos entre un movimiento y una o varias facturas."""

    movimiento_id: int
    asignaciones: List[Dict[str, Any]] = Field(
        ...,
        description="Lista de {'factura_id': int, 'importe_asignado': float}",
    )
    notas: str = ""
    aprender_regla: bool = True


class CSVMappingConfig(BaseModel):
    """Mapeo de columnas para importación de extractos CSV/Excel por entidad bancaria."""

    banco: str
    columna_fecha: str
    columna_concepto: str
    columna_importe: str
    columna_cargo: Optional[str] = None
    columna_abono: Optional[str] = None
    columna_fecha_valor: Optional[str] = None
    columna_referencia: Optional[str] = None
    separador: str = ";"
    formato_fecha: str = "%d/%m/%Y"
    filas_cabecera: int = 1
