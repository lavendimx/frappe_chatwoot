# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Selftest del texto del recordatorio por modalidad (2026-10-13). Read-only, no toca la red
ni la DB: solo llama a `utils.recordatorios._mensaje` y afirma sobre la cadena.

    bench --site <site> execute frappe_chatwoot.agenda_modalidad_selftest.ejecutar

Existe porque el banco de regresión de `recordatorios.py` no está cubierto por los bancos de
`agente-ia` (es Python del lado de Frappe): este es el equivalente mínimo que corre en el
contexto real de Frappe sin depender de un runner de tests.
"""

import frappe

from frappe_chatwoot.utils import recordatorios


def ejecutar() -> dict:
    fallos = []

    def chk(nombre, cond, extra=""):
        if cond:
            print(f"  ok  {nombre}")
        else:
            print(f"  FALLA  {nombre}  {extra}")
            fallos.append(nombre)

    # Presencial con dirección: sin videollamada/llamada, con la palabra presencial y la
    # dirección; y NO debe traer la liga.
    pres = frappe._dict(
        nombre_participante="Samantha Campos", modalidad="presencial",
        ubicacion="Av. Reforma 123, CDMX", meet_link="https://meet.google.com/xxx",
    )
    m = recordatorios._mensaje(pres)
    chk("presencial: no dice 'videollamada'", "videollamada" not in m.lower(), m)
    chk("presencial: no dice 'llamada'", "llamada" not in m.lower(), m)
    chk("presencial: dice 'cita presencial'", "cita presencial" in m.lower(), m)
    chk("presencial: incluye la dirección", "Av. Reforma 123" in m, m)
    chk("presencial: no incluye la liga de Meet", "meet.google" not in m, m)

    # Presencial sin dirección: mismo texto, sin renglón de ubicación.
    pres2 = frappe._dict(nombre_participante="Ana", modalidad="presencial", ubicacion="", meet_link="")
    m2 = recordatorios._mensaje(pres2)
    chk("presencial sin dirección: no agrega 'Nos vemos en'", "Nos vemos en" not in m2, m2)

    # Videollamada: literal de siempre, carácter por carácter.
    vid_literal = (
        "Hola Samantha 🙂, confirmando nuestra videollamada / Llamada dentro de 1 hora, "
        "correcto? 🗓️\n\nEl enlace para unirte es el siguiente: \nhttps://meet.google.com/xxx"
    )
    vid = frappe._dict(
        nombre_participante="Samantha Campos", modalidad="videollamada",
        ubicacion="", meet_link="https://meet.google.com/xxx",
    )
    chk("videollamada: literal sin cambios", recordatorios._mensaje(vid) == vid_literal, recordatorios._mensaje(vid))

    # Cita vieja sin modalidad (NULL): trata como videollamada, literal de siempre.
    vieja = frappe._dict(nombre_participante="Samantha Campos", modalidad=None, ubicacion=None, meet_link="https://meet.google.com/xxx")
    chk("sin modalidad (cita vieja): literal de siempre", recordatorios._mensaje(vieja) == vid_literal, recordatorios._mensaje(vieja))

    print(f"\n{len(fallos)} fallos")
    return {"fallos": fallos}
