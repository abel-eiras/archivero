"""Generador del Dossier de Presentación en formato PDF maquetado (docs/Dossier_Presentacion_Archivero.pdf)."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_PDF = BASE_DIR / "docs" / "Dossier_Presentacion_Archivero.pdf"


def build_presentation_pdf(output_path: Path = OUTPUT_PDF) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title="Dossier de Presentación - Archivero (FarmaFacturas)",
        author="Abel Eiras",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DocTitle",
        parent=styles["Heading1"],
        fontSize=20,
        leading=24,
        textColor=colors.HexColor("#0F766E"),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        "DocSubtitle",
        parent=styles["Normal"],
        fontSize=11,
        leading=15,
        textColor=colors.HexColor("#475569"),
        spaceAfter=14,
    )
    h2_style = ParagraphStyle(
        "SectionH2",
        parent=styles["Heading2"],
        fontSize=13,
        leading=17,
        textColor=colors.HexColor("#0F172A"),
        spaceBefore=12,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=9.5,
        leading=13.5,
        textColor=colors.HexColor("#1E293B"),
        spaceAfter=6,
    )

    story = []

    story.append(Paragraph("Archivero (FarmaFacturas)", title_style))
    story.append(
        Paragraph(
            "Dossier de Presentación y Guía Completa · Gestor Inteligente de Facturas por Correo y Conciliación Bancaria para Farmacias",
            subtitle_style,
        )
    )

    story.append(Paragraph("1. Resumen Ejecutivo y Propuesta de Valor", h2_style))
    story.append(
        Paragraph(
            "Una oficina de farmacia recibe cada mes decenas de facturas en PDF desde múltiples buzones y proveedores: "
            "cooperativas de distribución diaria, laboratorios, suministros (luz, agua, fibra), alquiler del local con IRPF, "
            "seguros, gestoría y abonos por devoluciones. Archivero automatiza todo este proceso desde el propio ordenador "
            "de la farmacia, sin servidores externos, sin cuotas mensuales y pensado para personal sin conocimientos informáticos.",
            body_style,
        )
    )

    story.append(Paragraph("2. Flujo Automático en 5 Pasos", h2_style))
    flow_data = [
        ["Paso", "Módulo", "Qué hace por la farmacia"],
        ["1", "Vigilancia de Correo", "Revisa uno o varios buzones (IMAP, Gmail, Outlook) y descarga facturas nuevas sin borrar correos."],
        ["2", "Lectura IA y Validación", "Extrae proveedor, NIF, fechas, Base, IVA, Recargo de Equivalencia e IRPF y verifica la suma."],
        ["3", "Archivo para Gestoría", "Ordena los PDFs automáticamente en Año / Mes / Categoría / Proveedor con nombre estandarizado."],
        ["4", "Conciliación Bancaria", "Cruza extractos Norma 43, CAMT.053 XML y Excel/CSV (incluyendo remesas agrupadas y abonos)."],
        ["5", "Exportación a Excel", "Genera al instante el libro Excel (.xlsx) con 4 hojas contables listas para enviar a la gestoría."],
    ]
    t_flow = Table(flow_data, colWidths=[1.3 * cm, 4.2 * cm, 11.5 * cm])
    t_flow.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F766E")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t_flow)
    story.append(Spacer(1, 6))

    story.append(Paragraph("3. Los 6 Principios de Seguridad y Robustez", h2_style))
    principles = [
        "<b>Cada farmacia es una isla:</b> Base de datos SQLite propia, claves en el llavero seguro del sistema (Windows Credential Manager / macOS Keychain) y acceso restringido a localhost (127.0.0.1).",
        "<b>Carpeta a prueba de errores:</b> Si alguien mueve o renombra un PDF en la carpeta de la gestoría, el panel y las estadísticas siguen funcionando intactos porque todo se lee de SQLite.",
        "<b>Bandeja de Revisión inteligente:</b> Si una factura tiene un descuadre de IVA o baja confianza, nunca se confirma a ciegas; espera la confirmación del farmacéutico con el PDF al lado.",
        "<b>Nada se pierde:</b> Escritura primero en carpeta temporal con reintentos ante fallos de red/Drive y copia de seguridad automática de SQLite antes de cada migración o actualización.",
    ]
    for p in principles:
        story.append(Paragraph(f"• {p}", body_style))

    story.append(Paragraph("4. Proveedores de Inteligencia Artificial y Costes (API Key vs Suscripción)", h2_style))
    story.append(
        Paragraph(
            "El programa no requiere suscripciones mensuales de 20 €/mes (como ChatGPT Plus o Claude Pro, que son solo para chat web). "
            "Funciona mediante Claves API de pago por uso o gratuitas, con un coste prácticamente nulo para una farmacia:",
            body_style,
        )
    )
    ai_data = [
        ["Proveedor", "Coste para la farmacia", "Privacidad", "Facturas escaneadas"],
        ["Google AI Studio (Gratis)", "0,00 € (hasta 1.500/día)", "Estándar gratuita de Google", "Sí (Visión directa PDF)"],
        ["Google AI Studio (Con facturación)", "~0,02 € cada 100 facturas", "100% Privado por contrato", "Sí (Visión directa PDF)"],
        ["OpenAI API (gpt-4o-mini)", "~0,03 € cada 100 facturas", "100% Privado por contrato", "Sí (Texto / OCR)"],
        ["Anthropic Claude API", "~0,05 € cada 100 facturas", "100% Privado por contrato", "Sí (Lectura nativa PDF)"],
        ["Modo Básico sin Internet / Ollama", "0,00 € (Gratis)", "100% en el propio ordenador", "PDFs digitales / Local"],
    ]
    t_ai = Table(ai_data, colWidths=[4.8 * cm, 3.8 * cm, 4.4 * cm, 4.0 * cm])
    t_ai.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.2),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(t_ai)
    story.append(Spacer(1, 6))

    story.append(Paragraph("5. Pantallas Principales de la Aplicación", h2_style))
    screens = [
        "<b>1. Resumen General:</b> Tarjetas de gasto total, base e IVA, gráficos por mes, categoría y proveedor, alertas de vencimientos a 15 días y pendientes de pago.",
        "<b>2. Bandeja de Revisión:</b> Formulario con comprobación aritmética en vivo (Base + IVA + R.E. - IRPF = Total) junto al visor del PDF original.",
        "<b>3. Todas las Facturas:</b> Búsqueda rápida, filtros combinados y apertura del PDF archivado.",
        "<b>4. Cuadrar con el Banco:</b> Importación Norma 43, CAMT.053 y Excel/CSV, cruce automático de remesas agrupadas y abonos, y conciliación manual muchos-a-muchos.",
        "<b>5. Configuración Fácil y 6. Ayuda Paso a Paso:</b> Textos sin tecnicismos, botón para probar la clave en vivo y manual integrado.",
    ]
    for s in screens:
        story.append(Paragraph(f"• {s}", body_style))

    doc.build(story)
    return output_path


if __name__ == "__main__":
    path = build_presentation_pdf()
    print(f"PDF de presentación generado en: {path}")
