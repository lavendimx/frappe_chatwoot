# Copyright (c) 2026, lavendi.mx
# License: MIT
"""Búsqueda "inteligente" compartida por los buscadores del producto.

Qué resuelve (pedido de Alejandro, 2026-09-17): que los buscadores NO exijan la
coincidencia exacta. Reglas:

    - Parcial: "moctez" encuentra "Moctezuma".
    - Sin acentos ni mayúsculas: "jose" encuentra "José". La base es
      `utf8mb4_unicode_ci` (case- y accent-insensitive), así que un `LIKE` ya lo
      cumple; `normalizar()` es para el filtrado en Python/JS.
    - Palabras sueltas y en cualquier orden: "moctezuma jose" encuentra
      "José Moctezuma". Cada palabra debe aparecer en ALGÚN campo (AND entre
      palabras, OR entre campos).
    - Partes de número: se comparan solo los dígitos del teléfono, así que
      "5559 668622", "(55) 5966" o "668622" encuentran "+52 5559 668622".

Se usa desde `utils/secuencias.py` (buscar_deals / listar_inscripciones) y desde
cualquier endpoint que necesite el mismo criterio. El espejo de frontend es
`frontend/src/utils/busqueda.js` (mismo criterio, para los filtros locales).
"""

import re
import unicodedata


def normalizar(texto) -> str:
    """Minúsculas, sin acentos, espacios colapsados."""
    if texto is None:
        return ""
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def solo_digitos(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def tokens(q) -> list:
    return [t for t in normalizar(q).split(" ") if t]


def coincide(campos, q) -> bool:
    """Versión Python (para filtrar listas ya cargadas). `campos` es un iterable
    de valores; basta que cada palabra aparezca en alguno."""
    toks = tokens(q)
    if not toks:
        return True
    textos = [normalizar(c) for c in campos if c]
    if all(any(t in texto for texto in textos) for t in toks):
        return True
    # Teléfono: si la búsqueda trae dígitos, comparar contra los dígitos de los campos.
    d = solo_digitos(q)
    if d and any(d in solo_digitos(c) for c in campos):
        return True
    return False


def condiciones_sql(q, campos, campos_telefono=()):
    """Fragmento WHERE (sin la palabra WHERE) + parámetros para usar en `frappe.db.sql`.

    `campos`: expresiones SQL que devuelven texto (ya calificadas con su alias).
    `campos_telefono`: columnas de teléfono; se comparan por dígitos.

    Devuelve `(sql, params)`; `sql` es None si la búsqueda está vacía.
    """
    toks = tokens(q)
    if not toks:
        return None, {}

    params = {}
    partes = []
    for i, t in enumerate(toks):
        params[f"t{i}"] = f"%{t}%"
        ors = " OR ".join(f"{c} LIKE %(t{i})s" for c in campos)
        partes.append(f"({ors})")
    sql = " AND ".join(partes)

    d = solo_digitos(q)
    if d and campos_telefono:
        tels = []
        for j, c in enumerate(campos_telefono):
            params[f"d{j}"] = f"%{d}%"
            tels.append(f"REGEXP_REPLACE(IFNULL({c}, ''), '[^0-9]', '') LIKE %(d{j})s")
        sql = f"({sql}) OR (" + " OR ".join(tels) + ")"

    return sql, params
