# Copyright (c) 2026, lavendi.mx
"""Renderizador propio para los Web Form de Frappe — el módulo de formularios de Sofía.

POR QUÉ EXISTE
--------------
Frappe ya trae el equivalente del builder de formularios de GHL: el doctype
`Web Form` deja definir campos, etiquetas y obligatoriedad sin escribir código.
Lo que no trae es un resultado presentable: su plantilla pinta un formulario de
backoffice (label arriba, campo, checkbox tras checkbox) y la única palanca es
`custom_css`. Hasta hoy el formulario de captación de lavendi.mx vivía de ~100
líneas de `!important` imitando al de GHL, con una tarjeta clara pegada sobre
una landing oscura y altura fija de 780 px.

Esto sustituye la **plantilla**, no el motor. La definición del formulario, la
validación, los permisos y la escritura siguen siendo de Frappe: el envío viaja
por su propio `accept()` whitelisted. Es decir, cualquier formulario que
cualquier cliente cree en el Desk hereda el diseño sin tocar código — que es lo
que convierte esto en módulo vendible y no en una página a la medida.

TRES DECISIONES QUE VALE LA PENA REGISTRAR
------------------------------------------
1. **Ruta propia `/f/<ruta>`, no reemplazo de la ruta original.** Se evaluó
   sobrescribir la clase `Web Form` (`override_doctype_class`) para que la ruta
   de siempre usara este renderer. Se descartó: el formulario de captación es
   hoy la única vía viva de leads del sitio, y un renderer propio encima de un
   doctype central significa que una actualización de Frappe puede romperlo.
   Con ruta aparte, `/solicitar-cotizacion` sigue intacto y es el rollback:
   cambiar el `src` del iframe de vuelta y ya.

2. **El envío llama a `accept()`, no escribe el doctype.** `accept()` corre la
   validación del servidor, respeta `login_required`/`anonymous`, aplica el
   rate limit de Frappe (10/min por IP) y solo acepta los campos declarados en
   el formulario. Escribir el doctype por nuestra cuenta duplicaría todas esas
   reglas y las dejaría desincronizadas en la primera actualización.

3. **El tema se resuelve por petición, no por formulario.** El mismo formulario
   de captación se embebe en cuatro landings con cuatro sistemas visuales
   distintos. Un tema por documento obligaría a cuatro formularios duplicados
   —cuatro sitios donde editar un campo— así que el `?tema=` de la URL manda, y
   lo que el documento guarda es el default de ese formulario.
"""

import frappe
from frappe import _

# Campos que el visitante nunca ve pero que traen la atribución. Se rellenan
# desde el query string en el navegador (ver la plantilla), no aquí: dentro de
# un iframe los parámetros los propaga `attr-iframe.js` de lavendi.mx.
CAMPOS_ATRIBUCION = (
    "pagina_origen", "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "gclid", "gbraid", "wbraid",
)

# Trampa para bots. No se marca como `hidden` en el HTML —un bot decente ignora
# lo oculto por atributo—: se saca de la vista por CSS y se deja etiquetado como
# un campo legítimo. Si viene lleno, `captacion.py` ya sabe qué hacer.
#
# ⚠ El nombre importa (2026-09-16): antes era `empresa_web`, etiquetado
# "Empresa", y el autofill del navegador lo rellenó en un lead REAL (el campo
# "empresa" del perfil → "LG Electronics"). El autofill ignora
# `autocomplete="off"`, así que el campo se llama con un token que ningún
# autofill reconoce y la etiqueta ya no dice "Empresa".
CAMPO_TRAMPA = "referencia_adicional"

# ---------------------------------------------------------------------------
# Temas
# ---------------------------------------------------------------------------
# Cada preset son las variables CSS que la plantilla vuelca en `:root`. Los
# valores NO son inventados: salen de los `:root` reales de cada landing
# (verificados contra el CSS que sirve lavendi.mx el 2026-09-14), para que el
# formulario no "combine" con la página sino que sea tipográficamente la misma.
#
# `fuentes` es lo que se pide a Google Fonts. Dentro del iframe no se hereda
# nada de la página anfitriona: si no se carga aquí, no existe.
TEMAS = {
    # lavendi.mx/curso-taller-de-ventas-estrategicas/ — fondo #0A0E17, oro
    "curso": {
        "fuentes": "family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Manrope:wght@400;500;600;700;800",
        "vars": {
            "fondo-pagina": "#0A0E17",
            "ink": "#EDEFF4", "ink-soft": "#9AA3B8",
            "superficie": "rgba(237,239,244,0.04)",
            "superficie-alta": "rgba(237,239,244,0.07)",
            "linea": "rgba(237,239,244,0.16)",
            "linea-fuerte": "rgba(237,239,244,0.30)",
            "acento": "#E8B054", "acento-alto": "#F2C475", "acento-ink": "#1A1406",
            "acento-tenue": "rgba(232,176,84,0.12)",
            "error": "#F0846B",
            "display": '"Fraunces", Georgia, serif',
            "body": '"Manrope", system-ui, sans-serif',
            "radio": "6px", "radio-lg": "10px",
            "peso-cta": "700", "caja-cta": "0.04em", "mayus-cta": "uppercase",
        },
    },
    # lavendi.mx (home) — fondo #0f1424, violeta
    "home": {
        "fuentes": "family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700&family=Manrope:wght@400;500;600;700;800",
        "vars": {
            "fondo-pagina": "#0F1424",
            "ink": "#EEF0F7", "ink-soft": "#B7BCD4",
            "superficie": "rgba(238,240,247,0.04)",
            "superficie-alta": "rgba(238,240,247,0.07)",
            "linea": "#212A42", "linea-fuerte": "#36415f",
            "acento": "#6D4AFF", "acento-alto": "#A894FF", "acento-ink": "#FFFFFF",
            "acento-tenue": "rgba(109,74,255,0.16)",
            "error": "#FF8A7A",
            "display": '"Bricolage Grotesque", system-ui, sans-serif',
            "body": '"Manrope", system-ui, sans-serif',
            "radio": "10px", "radio-lg": "14px",
            "peso-cta": "700", "caja-cta": "0.02em", "mayus-cta": "none",
        },
    },
    # lavendi.mx/proyectos/ — fondo #0d1015, azul
    "proyectos": {
        "fuentes": "family=Fraunces:opsz,wght@9..144,300;9..144,600&family=IBM+Plex+Sans:wght@400;500;600",
        "vars": {
            "fondo-pagina": "#0D1015",
            "ink": "#EDEFF2", "ink-soft": "#838D9C",
            "superficie": "rgba(237,239,242,0.04)",
            "superficie-alta": "rgba(237,239,242,0.07)",
            "linea": "#232833", "linea-fuerte": "#3a4152",
            "acento": "#1E73BE", "acento-alto": "#5FA8EA", "acento-ink": "#FFFFFF",
            "acento-tenue": "rgba(30,115,190,0.16)",
            "error": "#F0846B",
            "display": '"Fraunces", Georgia, serif',
            "body": '"IBM Plex Sans", system-ui, sans-serif',
            "radio": "4px", "radio-lg": "6px",
            "peso-cta": "600", "caja-cta": "0.02em", "mayus-cta": "none",
        },
    },
    # lavendi.mx/ia/ — la sección va sobre un degradado violeta→cyan y el
    # formulario vive dentro de una tarjeta de vidrio. El CTA va en blanco
    # porque cualquier color de marca sobre ese degradado pierde contraste.
    "ia": {
        "fuentes": "family=Inter:wght@400;500;600;700;800",
        "vars": {
            "fondo-pagina": "linear-gradient(135deg, #6C3AED 0%, #06B6D4 100%)",
            "ink": "#FFFFFF", "ink-soft": "rgba(255,255,255,0.78)",
            "superficie": "rgba(255,255,255,0.10)",
            "superficie-alta": "rgba(255,255,255,0.18)",
            "linea": "rgba(255,255,255,0.28)",
            "linea-fuerte": "rgba(255,255,255,0.55)",
            "acento": "#FFFFFF", "acento-alto": "#F1F5F9", "acento-ink": "#5B21B6",
            "acento-tenue": "rgba(255,255,255,0.16)",
            "error": "#FFD3CB",
            "display": '"Inter", system-ui, sans-serif',
            "body": '"Inter", system-ui, sans-serif',
            "radio": "12px", "radio-lg": "16px",
            "peso-cta": "700", "caja-cta": "0.01em", "mayus-cta": "none",
        },
    },
    # Gemelo oscuro del default, para clientes con sitio en fondo oscuro. No
    # copia ninguna landing de la agencia: es neutro a propósito.
    "oscuro": {
        "fuentes": "family=Inter:wght@400;500;600;700;800",
        "vars": {
            "fondo-pagina": "#12141C",
            "ink": "#F2F3F7", "ink-soft": "#9CA0B0",
            "superficie": "rgba(242,243,247,0.04)",
            "superficie-alta": "rgba(242,243,247,0.08)",
            "linea": "rgba(242,243,247,0.16)",
            "linea-fuerte": "rgba(242,243,247,0.32)",
            "acento": "#8446E3", "acento-alto": "#9d69ee", "acento-ink": "#FFFFFF",
            "acento-tenue": "rgba(132,70,227,0.18)",
            "error": "#F0846B",
            "display": '"Inter", system-ui, sans-serif',
            "body": '"Inter", system-ui, sans-serif',
            "radio": "10px", "radio-lg": "14px",
            "peso-cta": "600", "caja-cta": "0.01em", "mayus-cta": "none",
        },
    },
    # Default de cualquier cliente nuevo: fondo claro, morado de Sofía. Es el
    # único tema que se ve bien sin saber nada de la página anfitriona.
    "claro": {
        "fuentes": "family=Inter:wght@400;500;600;700;800",
        "vars": {
            "fondo-pagina": "#F7F6FB",
            "ink": "#1B1B25", "ink-soft": "#6B6B7B",
            "superficie": "#FFFFFF", "superficie-alta": "#F7F6FB",
            "linea": "#E4E4EC", "linea-fuerte": "#c9c9d8",
            "acento": "#8446E3", "acento-alto": "#6B33C4", "acento-ink": "#FFFFFF",
            "acento-tenue": "rgba(132,70,227,0.10)",
            "error": "#C0392B",
            "display": '"Inter", system-ui, sans-serif',
            "body": '"Inter", system-ui, sans-serif',
            "radio": "10px", "radio-lg": "14px",
            "peso-cta": "600", "caja-cta": "0.01em", "mayus-cta": "none",
        },
    },
}
TEMA_DEFAULT = "claro"


def _tema(nombre: str | None, doc) -> dict:
    """Resuelve el tema: `?tema=` manda, luego el default del formulario, luego claro."""
    candidatos = [
        (nombre or "").strip().lower(),
        (doc.get("sofia_tema") or "").strip().lower(),
        TEMA_DEFAULT,
    ]
    for c in candidatos:
        if c in TEMAS:
            return {"nombre": c, **TEMAS[c]}
    return {"nombre": TEMA_DEFAULT, **TEMAS[TEMA_DEFAULT]}


def _bloques(doc) -> list:
    """Agrupa `web_form_fields` en bloques renderizables.

    Frappe entrega los campos en una lista plana donde el Section Break es un
    campo más. Aquí se convierte en la estructura que la plantilla necesita:
    una lista de bloques, cada uno con su título y sus campos.

    El agrupado importa por una razón de diseño, no de comodidad: una sección
    cuyos campos son TODOS `Check` deja de pintarse como una fila de casillas y
    pasa a ser una rejilla de tarjetas seleccionables. Es justo lo que la
    plantilla genérica de Frappe no permite hacer y lo que vuelve legible
    "¿cuál es el principal producto que te interesa?".
    """
    bloques, actual = [], {"titulo": None, "campos": []}

    for f in doc.web_form_fields:
        if f.fieldtype in ("Section Break", "Column Break"):
            if f.fieldtype == "Column Break":
                continue
            if actual["campos"] or actual["titulo"]:
                bloques.append(actual)
            actual = {"titulo": f.label or None, "campos": []}
            continue

        if f.fieldname in CAMPOS_ATRIBUCION or f.fieldname == CAMPO_TRAMPA or f.hidden:
            continue

        actual["campos"].append({
            "fieldname": f.fieldname,
            "fieldtype": f.fieldtype,
            "label": f.label or f.fieldname,
            "reqd": int(f.reqd or 0),
            "options": f.options or "",
            "description": f.description or "",
            "max_length": int(f.max_length or 0) if hasattr(f, "max_length") else 0,
        })

    if actual["campos"] or actual["titulo"]:
        bloques.append(actual)

    for b in bloques:
        campos = b["campos"]
        b["solo_checks"] = bool(campos) and all(c["fieldtype"] == "Check" for c in campos)
        # Nombre y correo caben lado a lado en escritorio; el resto no se parte
        # porque un teléfono a media caja invita a escribirlo incompleto.
        b["pares"] = _emparejar(campos) if not b["solo_checks"] else []
    return bloques


def _emparejar(campos: list) -> list:
    """Reparte los campos en filas de 1 o 2 columnas.

    Solo se emparejan `Data` cortos consecutivos. Un `Small Text` o un campo con
    descripción larga ocupa fila completa: partirlo deja la ayuda colgando en
    una columna de 200 px.
    """
    filas, buffer = [], []

    def corto(c):
        return c["fieldtype"] == "Data" and not c["description"]

    for c in campos:
        if corto(c):
            buffer.append(c)
            if len(buffer) == 2:
                filas.append(buffer)
                buffer = []
        else:
            if buffer:
                filas.append(buffer)
                buffer = []
            filas.append([c])
    if buffer:
        filas.append(buffer)
    return filas


def contexto_formulario(context, ruta: str | None = None):
    """Contexto de `/f/<ruta>`. `ruta` es el `route` del Web Form, no su `name`.

    Se usa el `route` a propósito: es lo que el equipo ya conoce y lo que
    aparece en el embed del sitio. El `name` de un Web Form lo autogenera
    Frappe desde el título y trae acentos (`solicita-una-cotización-ahora`) —
    un id con acentos dentro de una URL es una fuente de bugs gratis.
    """
    context.no_cache = 1
    ruta = (ruta or frappe.form_dict.get("ruta") or "").strip().strip("/")
    context.ruta = ruta

    # Defaults antes de cualquier salida temprana: la plantilla es una sola y
    # su bloque de JS (el que avisa la altura al padre) corre también en el
    # camino de error. Sin esto, un formulario inexistente revienta en 500 en
    # vez de mostrar su aviso — y un 500 dentro de un iframe es una franja en
    # blanco en media landing.
    context.web_form_name = ""
    context.campos_atribucion = []
    context.campo_trampa = CAMPO_TRAMPA
    context.bloques = []
    # `?titulo=0` oculta el encabezado propio del formulario. Las 4 landings de
    # lavendi.mx ya encabezan su sección ("Cuéntanos de tu equipo y te
    # cotizamos"), así que sin esto el visitante lee dos títulos seguidos que
    # dicen lo mismo. En un link directo sí hace falta: es lo único que explica
    # de qué va la página.
    context.mostrar_titulo = frappe.form_dict.get("titulo", "1") != "0"
    context.embebido = frappe.form_dict.get("marco", "1") != "0"

    # `get_csrf_token()` lo GENERA si el visitante aún no tiene uno; leer
    # `session.data.csrf_token` a secas devuelve vacío para un visitante nuevo,
    # y con token vacío Frappe se salta la validación (`auth.py:90`) — o sea,
    # cualquiera podría publicar el formulario por curl. Mismo criterio que
    # `agenda_publica.py`, donde ya costó descubrirlo.
    from frappe.sessions import get_csrf_token

    context.csrf_token = get_csrf_token()

    nombre = frappe.db.get_value("Web Form", {"route": ruta}, "name") if ruta else None
    if not nombre:
        context.error = _("Formulario no encontrado.")
        context.tema = _tema(frappe.form_dict.get("tema"), frappe._dict())
        return context

    doc = frappe.get_doc("Web Form", nombre)

    # El mismo portero que la ruta original: un formulario despublicado no se
    # sirve por una URL alterna. Si no, despublicar dejaría de significar nada.
    if not doc.published:
        context.error = _("Este formulario no está disponible.")
        context.tema = _tema(frappe.form_dict.get("tema"), doc)
        return context

    if doc.login_required and frappe.session.user == "Guest":
        context.error = _("Necesitas iniciar sesión para usar este formulario.")
        context.tema = _tema(frappe.form_dict.get("tema"), doc)
        return context

    context.error = None
    context.web_form_name = nombre
    context.titulo = doc.title
    context.intro = (doc.get("sofia_intro") or doc.introduction_text or "").strip()
    context.boton = doc.button_label or _("Enviar")
    context.exito_titulo = doc.success_title or _("¡Gracias!")
    context.exito_mensaje = doc.success_message or _("Recibimos tu solicitud.")
    context.bloques = _bloques(doc)
    context.campos_atribucion = list(CAMPOS_ATRIBUCION)
    context.campo_trampa = CAMPO_TRAMPA
    context.tema = _tema(frappe.form_dict.get("tema"), doc)
    # `1` por default: el caso de uso es el embed. Con `?marco=0` la página
    # sirve también como link directo (WhatsApp, bio, QR) con su propio fondo.
    context.embebido = frappe.form_dict.get("marco", "1") != "0"
    return context


def csp_embebido(response=None, request=None):
    """Hook `after_request`: CSP `frame-ancestors` para `/f/<ruta>`.

    La ruta original de un Web Form recibe este header del renderer de Frappe
    (`page_renderers/web_form.py`), pero una página `www/` normal no tiene por
    dónde ponerlo — no hay gancho en `get_context` que llegue a la respuesta.

    Sin este header pasan dos cosas malas a la vez: el iframe de lavendi.mx se
    ve en blanco (el default del sitio bloquea el embebido), y en el escenario
    contrario —si el default fuera permisivo— cualquiera podría embeber el
    formulario en su sitio y cosechar leads a nuestro nombre.
    """
    try:
        ruta_http = (getattr(request, "path", "") or "") if request else ""
        if not ruta_http.startswith("/f/"):
            return
        dominios = dominios_permitidos(ruta_http[3:])
        valor = f"frame-ancestors 'self' {dominios}".strip() if dominios else "frame-ancestors 'self'"
        response.headers["Content-Security-Policy"] = valor
    except Exception:
        # Un fallo aquí no puede tumbar la respuesta: el formulario ya se
        # renderizó bien y el visitante está a un clic de convertir.
        frappe.logger().debug("csp_embebido falló", exc_info=True)


def dominios_permitidos(ruta: str) -> str:
    """Los `allowed_embedding_domains` del formulario, para el CSP de `/f/<ruta>`.

    Se reutiliza el campo que Frappe ya usa en la ruta original en vez de
    inventar uno nuevo: así el equipo administra el permiso de embebido en un
    solo lugar y las dos rutas no pueden quedar en desacuerdo.
    """
    nombre = frappe.db.get_value("Web Form", {"route": (ruta or "").strip("/")}, "name")
    if not nombre:
        return ""
    dominios = frappe.db.get_value("Web Form", nombre, "allowed_embedding_domains") or ""
    return dominios.replace("\n", " ").strip()
