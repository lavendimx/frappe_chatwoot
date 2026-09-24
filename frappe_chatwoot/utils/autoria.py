# Añadido por lavendi.mx. Marca de autoría de los mensajes salientes de Chatwoot.
#
# EL PROBLEMA QUE RESUELVE: `sender` de Chatwoot es inservible para saber quién
# escribió. Todo lo que sale por la API viaja con el usuario dueño del token
# (verificado: el 100% de los salientes del sitio dicen "Alejandro Moreno", id=1),
# así que el bot, una persona desde el CRM y un automatismo son indistinguibles
# para la UI. Ese es el mismo agujero que el 2026-09-09 hizo que el agente se
# firmara como Valente delante de un cliente.
#
# LA SOLUCIÓN: cada emisor estampa su propia marca en `content_attributes` al
# crear el mensaje. Las marcas ya existentes (no las inventa este módulo) son:
#
#   {"sofia_bot": True}                 -> el agente (lo pone `agente-ia/server.js`)
#   {"humano": True, ...}               -> una persona desde el CRM
#   {"humano": True, "programado": True} -> "enviar más tarde"
#   {"automatico": "<origen>", ...}      -> un job del sitio
#   {}                                   -> salió por otra vía (celular del equipo,
#                                           o es anterior a este cambio)
#
# Este módulo solo centraliza cómo se construyen, para que ningún call site nuevo
# tenga que acordarse de la forma exacta. La lectura la hace el frontend
# (`useConversationThread.autorDe`) y `agente-ia/server.js` (`esDeHumanoDelEquipo`).
#
# `usuario_nombre` viaja además del correo a propósito: la burbuja muestra
# iniciales y el correo no siempre las da bien (`contacto@lavendi.mx` -> Zaira).
# Resolver el nombre aquí, una vez, evita que el frontend dependa de un store de
# usuarios que en móvil puede no estar cargado.

import frappe

# Etiquetas cortas de los automatismos. La burbuja es angosta: lo que se pinta es
# esto, y el detalle largo va en el tooltip (`detalle`).
ORIGENES = {
    "secuencia": "Secuencia",
    "recordatorio": "Recordatorio",
    "onboarding": "Onboarding",
    "bienvenida": "Bienvenida",
    "followup": "Reactivación",
}


def marca_humana(programado: bool = False, usuario: str = None) -> dict:
    """Marca de un envío hecho por una persona desde la plataforma.

    `usuario` explícito para los mensajes que despacha un cron: en "enviar más
    tarde" el envío lo corre el scheduler (sesión = Administrator) pero la
    autoría es de quien lo programó, que el doctype ya guardó en
    `programado_por`. Sin pasarlo, la burbuja atribuiría el mensaje al sistema."""
    marca = {"humano": True}
    if programado:
        marca["programado"] = True

    usuario = usuario or frappe.session.user
    # Administrator/Guest no son personas del equipo: estampar su correo pintaría
    # unas iniciales sin sentido ("A") en la burbuja. Mejor caer al genérico.
    if usuario and usuario not in ("Administrator", "Guest"):
        marca["usuario"] = usuario
        nombre = frappe.db.get_value("User", usuario, "full_name")
        if nombre:
            marca["usuario_nombre"] = nombre
    return marca


def marca_automatica(origen: str, detalle: str = None) -> dict:
    """Marca de un mensaje que despachó un job, no una persona.

    `origen` debe ser una clave de ORIGENES; si llega otra cosa se conserva tal
    cual (el frontend cae a "Automático") en vez de tirar el envío — un mensaje
    al cliente no se pierde por una etiqueta mal escrita."""
    marca = {"automatico": origen}
    if detalle:
        marca["detalle"] = detalle
    return marca
