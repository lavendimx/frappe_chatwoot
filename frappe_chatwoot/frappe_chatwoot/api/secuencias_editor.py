# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Editor y constructor de secuencias desde el panel de Sofía CRM.

Complementa al motor (`utils/secuencias.py`), que solo ejecuta. Aquí vive el
CRUD que antes no existía: entrar a una secuencia, ver sus pasos, editarlos,
elegir la plantilla del catálogo, cambiar la espera y crear/duplicar/archivar.

Decisiones que no son obvias leyendo el código
    - La plantilla es un **Link** (`Secuencia Paso.plantilla`), no una copia:
      corregir el texto en `Plantilla` actualiza todos los pasos que la usan.
      El motor resuelve el contenido en `secuencias.contenido_paso()`.
    - La espera sigue guardándose en `espera_minutos`. La UI ofrece minutos,
      horas y días y convierte aquí; así no hay dos fuentes de verdad ni
      migración de datos.
    - Escribir exige el mismo gate que las acciones manuales del motor
      (`_ROLES_EDICION`): System Manager, Sales Manager y Administrator. Un
      Sales User ve todo, pero no puede reescribir una automatización.
    - Archivar = `activa=0`. Una secuencia con inscripciones no se borra:
      se archiva y se puede reactivar.
"""

import frappe
from frappe import _
from frappe.utils import cint

from frappe_chatwoot.utils import plan
from frappe_chatwoot.utils import secuencias as motor

TIPOS = [
    "Esperar",
    "WhatsApp",
    "Email",
    "Nota interna",
    "Aviso al equipo",
    "Tarea",
    "Actualizar oportunidad",
    "Etiqueta",
]
TIPOS_ENVIO = ("WhatsApp", "Email")


def _exigir_lectura():
    plan.exigir_no_lite()


def _exigir_edicion():
    motor._exigir_edicion()


def _puede_editar() -> bool:
    roles = frappe.get_roles(frappe.session.user)
    return (
        frappe.session.user == "Administrator"
        or any(r in roles for r in motor._ROLES_EDICION)
    )


def _secuencia(name: str):
    if not name or not frappe.db.exists("Secuencia", name):
        frappe.throw(_("La secuencia {0} no existe").format(name), frappe.DoesNotExistError)
    return frappe.get_doc("Secuencia", name)


def _plantillas_de(pasos: list[dict]) -> dict:
    nombres = sorted({p.get("plantilla") for p in pasos if p.get("plantilla")})
    if not nombres:
        return {}
    return {
        p.name: p
        for p in frappe.get_all(
            "Plantilla",
            filters={"name": ["in", nombres]},
            fields=["name", "titulo", "canal", "activa", "texto", "adjunto"],
            limit_page_length=0,
        )
    }


def _paso_dict(row, plantillas: dict) -> dict:
    """Fila de `Secuencia Paso` + el estado de su plantilla, para la UI."""
    p = plantillas.get(row.get("plantilla")) if row.get("plantilla") else None
    return {
        "idx": cint(row.get("idx")),
        "tipo": row.get("tipo"),
        "nombre_ghl": row.get("nombre_ghl"),
        "espera_minutos": cint(row.get("espera_minutos")),
        "mensaje": row.get("mensaje") or "",
        "asunto": row.get("asunto") or "",
        "html": row.get("html") or "",
        "adjunto_url": row.get("adjunto_url") or "",
        "campo_destino": row.get("campo_destino") or "",
        "valor_destino": row.get("valor_destino") or "",
        "condicion_campo": row.get("condicion_campo") or "",
        "condicion_valor": row.get("condicion_valor") or "",
        "plantilla": row.get("plantilla") or "",
        "plantilla_titulo": (p or {}).get("titulo") or "",
        "plantilla_canal": (p or {}).get("canal") or "",
        "plantilla_activa": bool((p or {}).get("activa")),
        "plantilla_texto": (p or {}).get("texto") or "",
        "plantilla_adjunto": (p or {}).get("adjunto") or "",
        "plantilla_faltante": bool(row.get("plantilla")) and not p,
    }


def _detalle(doc) -> dict:
    filas = [r.as_dict() for r in doc.pasos]
    plantillas = _plantillas_de(filas)
    pasos = [_paso_dict(r, plantillas) for r in filas]

    estados = dict(frappe.db.sql(
        "SELECT estado, COUNT(*) FROM `tabSecuencia Inscripcion` "
        "WHERE secuencia=%s GROUP BY estado", doc.name,
    ))
    return {
        "name": doc.name,
        "titulo": doc.titulo,
        "activa": bool(doc.activa),
        "disparador": doc.disparador,
        "disparador_valor": doc.disparador_valor or "",
        "parar_si_responde": bool(doc.parar_si_responde),
        "permitir_reingreso": bool(doc.permitir_reingreso),
        "horario_habil": bool(doc.horario_habil),
        "producto": doc.get("producto") or "",
        "max_por_corrida": cint(doc.get("max_por_corrida")),
        "pasos": pasos,
        "total_pasos": len(pasos),
        "inscritos": estados,
        "inscritos_activos": estados.get("Activa", 0),
        "inscritos_total": sum(estados.values()),
        "puede_editar": _puede_editar(),
    }


def _minutos(datos: dict) -> int:
    """Espera en minutos. Acepta `espera_minutos` o `espera_valor`+`espera_unidad`."""
    if datos.get("espera_valor") not in (None, ""):
        valor = cint(datos.get("espera_valor"))
        unidad = (datos.get("espera_unidad") or "minutos").lower()
        factor = {"minutos": 1, "horas": 60, "dias": 1440, "días": 1440}.get(unidad, 1)
        return max(0, valor * factor)
    return max(0, cint(datos.get("espera_minutos")))


def _validar_paso(datos: dict) -> None:
    tipo = (datos.get("tipo") or "").strip()
    if tipo not in TIPOS:
        frappe.throw(_("Tipo de paso inválido: {0}").format(tipo))

    plantilla = (datos.get("plantilla") or "").strip()
    if plantilla and tipo not in TIPOS_ENVIO:
        frappe.throw(_("Solo los pasos de WhatsApp o Email pueden llevar plantilla."))
    if plantilla:
        p = frappe.db.get_value(
            "Plantilla", plantilla, ["canal", "activa", "titulo"], as_dict=True
        )
        if not p:
            frappe.throw(_("La plantilla {0} no existe.").format(plantilla))
        esperado = "Email" if tipo == "Email" else "WhatsApp"
        if p.canal != esperado:
            frappe.throw(_("La plantilla «{0}» es de {1}, no de {2}.").format(
                p.titulo or plantilla, p.canal, esperado))

    if tipo in TIPOS_ENVIO and not plantilla:
        if tipo == "Email" and not (datos.get("html") or datos.get("mensaje")):
            frappe.throw(_("Un paso de Email necesita plantilla o cuerpo."))
        if tipo == "WhatsApp" and not datos.get("mensaje"):
            frappe.throw(_("Un paso de WhatsApp necesita plantilla o mensaje."))
    if tipo in ("Nota interna", "Aviso al equipo", "Tarea") and not datos.get("mensaje"):
        frappe.throw(_("Este tipo de paso necesita un mensaje."))
    if tipo == "Actualizar oportunidad" and not datos.get("campo_destino"):
        frappe.throw(_("Indica qué campo de la oportunidad se va a actualizar."))
    if tipo in ("Actualizar oportunidad", "Etiqueta") and not datos.get("valor_destino"):
        frappe.throw(_("Indica el valor a aplicar."))


def _asignar(row, datos: dict) -> None:
    for campo in (
        "tipo", "nombre_ghl", "mensaje", "asunto", "html", "adjunto_url",
        "campo_destino", "valor_destino", "condicion_campo", "condicion_valor",
        "plantilla",
    ):
        if campo in datos:
            row.set(campo, datos.get(campo) or None)
    if "espera_minutos" in datos or "espera_valor" in datos:
        row.espera_minutos = _minutos(datos)


@frappe.whitelist()
def listar() -> list:
    """Secuencias con conteos, para la lista del panel."""
    _exigir_lectura()
    activos = dict(frappe.db.sql(
        "SELECT secuencia, COUNT(*) FROM `tabSecuencia Inscripcion` "
        "WHERE estado='Activa' GROUP BY secuencia"))
    totales = dict(frappe.db.sql(
        "SELECT secuencia, COUNT(*) FROM `tabSecuencia Inscripcion` GROUP BY secuencia"))
    pasos = dict(frappe.db.sql(
        "SELECT parent, COUNT(*) FROM `tabSecuencia Paso` GROUP BY parent"))
    filas = frappe.get_all(
        "Secuencia",
        fields=["name", "titulo", "activa", "disparador", "disparador_valor", "producto"],
        order_by="activa desc, titulo asc",
        limit_page_length=0,
    )
    for s in filas:
        s["inscritos"] = activos.get(s["name"], 0)
        s["inscritos_total"] = totales.get(s["name"], 0)
        s["total_pasos"] = pasos.get(s["name"], 0)
    return filas


@frappe.whitelist()
def detalle(name: str) -> dict:
    _exigir_lectura()
    return _detalle(_secuencia(name))


@frappe.whitelist()
def crear(titulo: str, disparador: str = "Manual", disparador_valor: str = "",
          activa: int = 0) -> dict:
    _exigir_edicion()
    if not (titulo or "").strip():
        frappe.throw(_("El nombre de la secuencia es obligatorio."))
    doc = frappe.get_doc({
        "doctype": "Secuencia",
        "titulo": titulo.strip(),
        "disparador": disparador or "Manual",
        "disparador_valor": disparador_valor or "",
        "activa": cint(activa),
        "parar_si_responde": 1,
        "permitir_reingreso": 0,
        "horario_habil": 1,
    })
    doc.insert()
    return _detalle(doc)


@frappe.whitelist()
def guardar_datos(name: str, datos=None) -> dict:
    """Título, disparador y reglas de la secuencia (no toca los pasos)."""
    _exigir_edicion()
    datos = frappe.parse_json(datos) or {}
    doc = _secuencia(name)
    for campo in ("titulo", "disparador", "disparador_valor"):
        if campo in datos:
            doc.set(campo, datos.get(campo))
    for campo in ("parar_si_responde", "permitir_reingreso", "horario_habil", "activa"):
        if campo in datos:
            doc.set(campo, 1 if cint(datos.get(campo)) else 0)
    doc.save()
    return _detalle(doc)


@frappe.whitelist()
def duplicar(name: str) -> dict:
    _exigir_edicion()
    origen = _secuencia(name)
    doc = frappe.get_doc({
        "doctype": "Secuencia",
        "titulo": f"{origen.titulo or origen.name} (copia)",
        "disparador": origen.disparador,
        "disparador_valor": origen.disparador_valor,
        "activa": 0,
        "parar_si_responde": origen.parar_si_responde,
        "permitir_reingreso": origen.permitir_reingreso,
        "horario_habil": origen.horario_habil,
        "producto": origen.get("producto"),
        "max_por_corrida": origen.get("max_por_corrida"),
    })
    for paso in origen.pasos:
        doc.append("pasos", {
            "tipo": paso.tipo,
            "nombre_ghl": paso.nombre_ghl,
            "espera_minutos": paso.espera_minutos,
            "mensaje": paso.mensaje,
            "asunto": paso.asunto,
            "html": paso.html,
            "adjunto_url": paso.adjunto_url,
            "campo_destino": paso.campo_destino,
            "valor_destino": paso.valor_destino,
            "condicion_campo": paso.condicion_campo,
            "condicion_valor": paso.condicion_valor,
            "plantilla": paso.get("plantilla"),
        })
    doc.insert()
    return _detalle(doc)


@frappe.whitelist()
def archivar(name: str, activa: int = 0) -> dict:
    _exigir_edicion()
    doc = _secuencia(name)
    doc.activa = 1 if cint(activa) else 0
    doc.save()
    return _detalle(doc)


@frappe.whitelist()
def eliminar(name: str) -> None:
    """Solo si no tiene ninguna inscripción (ni terminada). Si no, archivar."""
    _exigir_edicion()
    doc = _secuencia(name)
    if frappe.db.exists("Secuencia Inscripcion", {"secuencia": name}):
        frappe.throw(_("La secuencia tiene inscripciones: archívala en vez de borrarla."))
    frappe.delete_doc("Secuencia", name)


@frappe.whitelist()
def guardar_paso(name: str, datos=None, idx=None) -> dict:
    """Crea (idx vacío) o actualiza un paso (idx 1-based)."""
    _exigir_edicion()
    datos = frappe.parse_json(datos) or {}
    _validar_paso(datos)
    doc = _secuencia(name)

    if idx in (None, "", 0):
        row = doc.append("pasos", {})
    else:
        fila = next((r for r in doc.pasos if cint(r.idx) == cint(idx)), None)
        if not fila:
            frappe.throw(_("El paso {0} no existe.").format(idx))
        row = fila

    _asignar(row, datos)
    doc.save()
    return _detalle(doc)


@frappe.whitelist()
def borrar_paso(name: str, idx: int) -> dict:
    _exigir_edicion()
    doc = _secuencia(name)
    fila = next((r for r in doc.pasos if cint(r.idx) == cint(idx)), None)
    if not fila:
        frappe.throw(_("El paso {0} no existe.").format(idx))
    doc.pasos.remove(fila)
    doc.save()
    return _detalle(doc)


@frappe.whitelist()
def reordenar(name: str, orden=None) -> dict:
    """`orden` = lista de idx actuales en el orden nuevo."""
    _exigir_edicion()
    orden = frappe.parse_json(orden) or []
    doc = _secuencia(name)
    filas = {cint(r.idx): r for r in doc.pasos}
    if sorted(cint(x) for x in orden) != sorted(filas):
        frappe.throw(_("El orden no cubre todos los pasos."))
    doc.set("pasos", [filas[cint(i)] for i in orden])
    doc.save()
    return _detalle(doc)


def _contexto_demo() -> dict:
    return {
        "contact.first_name": "María",
        "contact.last_name": "Ejemplo",
        "contact.name": "María Ejemplo",
        "contact.company_name": "Empresa de ejemplo",
        "contact.email": "maria@ejemplo.com",
    }


@frappe.whitelist()
def vista_previa(datos=None, deal: str | None = None) -> dict:
    """Renderiza un paso (guardado o en edición) sin enviar nada."""
    _exigir_lectura()
    datos = frappe.parse_json(datos) or {}
    paso = {
        "tipo": datos.get("tipo"),
        "mensaje": datos.get("mensaje"),
        "asunto": datos.get("asunto"),
        "html": datos.get("html"),
        "adjunto_url": datos.get("adjunto_url"),
        "plantilla": datos.get("plantilla"),
    }
    mensaje, asunto, html, adjunto, error = motor.contenido_paso(paso)

    ctx = _contexto_demo()
    if deal:
        contacto = frappe.db.get_value("CRM Deal", deal, "contact")
        if contacto:
            ctx = motor._contexto({"deal": deal, "contacto": contacto})

    return {
        "tipo": paso["tipo"],
        "asunto": motor._resolver(asunto, ctx),
        "texto": motor._resolver(mensaje, ctx),
        "html": motor._resolver(html, ctx),
        "adjunto": adjunto or "",
        "error": error,
    }


@frappe.whitelist()
def enviar_prueba(name: str, idx: int, deal: str, confirmar: int = 0) -> dict:
    """Manda el paso a un inscrito real. Gate: requiere `confirmar=1`.

    No mueve el estado del motor: reusa `_ejecutar_paso`, que solo envía. La UI
    pide confirmación explícita y muestra el contacto destino.
    """
    _exigir_edicion()
    if not cint(confirmar):
        frappe.throw(_("Falta la confirmación explícita para enviar la prueba."))
    doc = _secuencia(name)
    fila = next((r for r in doc.pasos if cint(r.idx) == cint(idx)), None)
    if not fila:
        frappe.throw(_("El paso {0} no existe.").format(idx))
    ins = frappe.db.get_value(
        "Secuencia Inscripcion",
        {"secuencia": name, "deal": deal},
        ["name", "secuencia", "deal", "contacto", "conversation_id", "paso_actual"],
        as_dict=True,
    )
    if not ins:
        frappe.throw(_("Ese contacto no está inscrito en esta secuencia."))
    nota = motor._ejecutar_paso(ins, fila.as_dict(), doc.as_dict())
    return {"nota": nota}
