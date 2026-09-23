# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Normalización canónica de teléfonos mexicanos.

POR QUÉ EXISTE (2026-09-23)
    El mismo número entra por tres caminos y se guarda distinto según quién lo
    capturó: WhatsApp lo entrega como JID (`5214422196109`), la migración de GHL
    como `+524422196109` (sin el "1" móvil) y un formulario web como
    `442 219 6109`. El resultado ya se vio con Anabel Osuna: dos `Contact` para
    la misma persona y dos conversaciones abiertas, porque el teléfono guardado
    no coincidía.

    La identidad de contacto en todo el CRM es **los últimos 10 dígitos**
    (`api/panel.py`, `utils/chatwoot_contactos.py`, `agente-ia`). Estos helpers
    centralizan ese criterio para no volver a escribir la misma expresión
    regular en cada módulo que crea o busca contactos.

    Convención de formas:
      · `corto()`             -> últimos 10 dígitos (la llave de identidad).
      · `e164_mx()`           -> `+52` + 10 (E.164 sin el "1" móvil).
      · `canonico_whatsapp()` -> `521` + 10 (la forma con la que WhatsApp México
                                 registra el número; es el valor que evita el
                                 duplicado +52 vs +521).

    No adivinar el "1" en `e164_mx()` es deliberado: las líneas mexicanas no
    todas lo llevan, y un número inventado abre una conversación con un
    destinatario inexistente (bug del 2026-09-10). Cuando el "1" importa, el
    llamador que ya lo conoce (p. ej. el JID de Evolution) lo conserva y usa
    `canonico_whatsapp()`.
"""

import re

_RE_NO_DIGITOS = re.compile(r"\D")


def digitos(telefono) -> str:
    """Solo los dígitos del número (quita espacios, `+`, guiones, paréntesis)."""
    return _RE_NO_DIGITOS.sub("", str(telefono or ""))


def corto(telefono) -> str | None:
    """Últimos 10 dígitos, o `None` si el número no llega a 10.

    Un número de menos de 10 dígitos no es identificable de forma segura: es un
    teléfono mal capturado, no un contacto.
    """
    d = digitos(telefono)
    return d[-10:] if len(d) >= 10 else None


def e164_mx(telefono) -> str | None:
    """Forma E.164 mexicana sin el "1" móvil: `+52` + 10 dígitos."""
    c = corto(telefono)
    return "+52" + c if c else None


def canonico_whatsapp(telefono) -> str | None:
    """Forma canónica de WhatsApp México: `521` + 10 dígitos (con el "1").

    Es la que usa el canal para reconocer el número; guardarla evita el
    duplicado que dejó a Anabel Osuna con dos conversaciones.
    """
    c = corto(telefono)
    return "521" + c if c else None
