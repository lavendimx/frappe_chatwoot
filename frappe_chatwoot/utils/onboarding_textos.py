"""Textos de bienvenida por WhatsApp — copia literal de los snippets de GHL.

Leídos el 2026-09-10 de la subcuenta (`snippets/{locationId}`), no reescritos:
el objetivo del corte es que el cliente no note el cambio de plataforma. Los
asteriscos son la negrita de WhatsApp, no markdown.

Duplicados a propósito del `mensajes.py` del servicio del host: son procesos
distintos, en máquinas distintas (contenedor y host), y compartir el archivo
exigiría montar un volumen solo para esto. Si se editan, editar ambos — están
enlazados por este comentario.
"""

DOC_PVP = "https://docs.google.com/document/d/12HfJMfD91CxWpK9IiV37EAgbnTdplLelvS2UMS7nDm8/edit?usp=sharing"
DOC_SGPT = "https://docs.google.com/document/d/1sxpMh_hgKGmXfy9tiki8mYdX9icsgOB5dLd9hlP6FR8/edit?usp=sharing"
DOC_PWP = "https://docs.google.com/document/d/1hMbKbaRiMKw3uKP9i0QaICdW0XyDSGeOBAYDeJrMBvY/edit?usp=sharing"

PRODUCTOS = {
    "PVP": (
        "*¡Te damos la bienvenida a lavendi.mx!*\n\n"
        "En este documento tienes toda la *información y pasos a seguir* "
        "para iniciar con tu proyecto:\n\n"
        f"{DOC_PVP}\n\n"
        "Déjanos saber cualquier duda o comentario y, nuevamente ¡Bienvenid@!\n\n"
        "El equipo de lavendi.mx,"
    ),
    "SGPT": (
        "*¡Te damos la bienvenida a lavendi.mx!*\n\n"
        "En este documento tienes toda la *información y pasos a seguir* "
        "para iniciar con tu proyecto:\n\n"
        f"{DOC_SGPT}\n\n"
        "Déjanos saber cualquier duda o comentario y, nuevamente ¡Bienvenid@!\n\n"
        "El equipo de lavendi.mx,"
    ),
    "PWP": (
        "*¡Te damos la bienvenida a lavendi.mx!*\n\n"
        "En este documento tienes toda la *información y pasos a seguir* "
        f"para iniciar con tu proyecto web: {DOC_PWP}\n\n"
        "Déjanos saber cualquier duda o comentario y, nuevamente ¡Bienvenid@!\n\n"
        "El equipo de lavendi.mx,"
    ),
}

# Segundo mensaje, común a los tres. En el workflow el paso se llama
# "Onboarding 2"; el snippet real se llama "Pago recibido (SPEI)".
SEGUNDO = (
    "Muchas gracias por confiar en nosotros para llevar a cabo tu proyecto. 🙂\n\n"
    "En las próximas 24 horas hábiles🕚 nos pondremos en contacto contigo para "
    "indicarte los siguientes pasos a seguir.✨\n\n"
    "¿Hay algo más en lo que pueda apoyarte por ahora? "
)
