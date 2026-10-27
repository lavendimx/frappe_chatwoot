# Añadido por lavendi.mx -- panel de solo lectura de la base de conocimiento del agente IA.
#
# La KB real (chunks + embeddings) vive fuera de Frappe, en el Postgres pgvector
# "sofia_kb" que llena agente-ia/lib/ingest.js. KB Source en Frappe es el metadato
# (titulo, tipo, estado, conteo) -- leerlo no requiere tocar la vector DB. Ver el
# contenido indexado si: es la unica forma de ver exactamente que texto quedo
# embebido (puede diferir del archivo original tras la extraccion/chunking).

import frappe

KB_SOURCE_FIELDS = [
    "name",
    "title",
    "source_type",
    "status",
    "chunk_count",
    "last_synced_at",
    "error_message",
    "siempre_incluir",
]


@frappe.whitelist()
def listar_fuentes(inbox_id):
    """Fuentes de KB del inbox dado. frappe.get_list (no get_all) para que el
    aislamiento multi-cliente (kb_isolation.py / User Permission "KB Inbox")
    siga aplicando tal cual a quien no sea System Manager."""
    return frappe.get_list(
        "KB Source",
        filters={"inbox_id": inbox_id},
        fields=KB_SOURCE_FIELDS,
        order_by="title asc",
    )


@frappe.whitelist()
def ver_chunks(source_id):
    """Contenido real embebido de una fuente -- tal como lo ve el retrieval, no
    el archivo original. Permiso explicito: la vector DB no tiene el permission
    layer de Frappe, asi que se valida aqui antes de tocar Postgres."""
    if not frappe.has_permission("KB Source", ptype="read", doc=source_id):
        frappe.throw("Sin permiso para ver esta fuente", frappe.PermissionError)

    fuente = frappe.get_doc("KB Source", source_id)
    chunks = _consultar_chunks(source_id)
    return {
        "fuente": {
            "name": fuente.name,
            "title": fuente.title,
            "source_type": fuente.source_type,
            "status": fuente.status,
            "chunk_count": fuente.chunk_count,
            "last_synced_at": fuente.last_synced_at,
        },
        "chunks": chunks,
    }


@frappe.whitelist()
def crear_fuente(inbox_id, title, source_type, content=None, url=None, attachment=None):
    """Alta de una fuente nueva desde el panel (System Manager). El Web Form
    publico de clientes ya hace esto mismo vía kb_isolation.py -- antes de esto
    la unica via de alta era ese Web Form o Frappe Desk."""
    if not frappe.has_permission("KB Source", ptype="create"):
        frappe.throw("Sin permiso para crear fuentes de conocimiento", frappe.PermissionError)

    doc = frappe.get_doc(
        {
            "doctype": "KB Source",
            "inbox_id": inbox_id,
            "title": title,
            "source_type": source_type,
            "content": content,
            "url": url,
            "attachment": attachment,
        }
    )
    doc.insert()
    return doc.name


@frappe.whitelist()
def detectar_conflictos(source_id, umbral=0.15, limite=30):
    """Fragmentos de OTRAS fuentes del mismo inbox muy parecidos (coseno) a los
    de esta fuente -- candidatos a duplicado, dato desactualizado o informacion
    contradictoria. Solo mide cercania semantica, no hay juicio de IA sobre si
    de verdad contradice: el humano decide viendo ambos fragmentos."""
    if not frappe.has_permission("KB Source", ptype="read", doc=source_id):
        frappe.throw("Sin permiso para ver esta fuente", frappe.PermissionError)

    pares = _consultar_candidatos_conflicto(source_id, float(umbral), int(limite))
    if not pares:
        return []

    otros_ids = sorted({p["fuente_id"] for p in pares})
    titulos = {
        d.name: d.title
        for d in frappe.get_all("KB Source", filters={"name": ["in", otros_ids]}, fields=["name", "title"])
    }
    for p in pares:
        p["fuente_title"] = titulos.get(p["fuente_id"], p["fuente_id"])
    return pares


def _consultar_candidatos_conflicto(source_id, umbral, limite):
    import psycopg2

    conf = frappe.conf.get("kb_pg") or {}
    if not conf.get("host"):
        frappe.throw("Conexion a la base de conocimiento (kb_pg) no configurada en este sitio")

    conn = psycopg2.connect(
        host=conf["host"],
        port=conf.get("port", 5432),
        dbname=conf["database"],
        user=conf["user"],
        password=conf["password"],
        connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.chunk_index, a.chunk_text, b.source_id, b.chunk_index, b.chunk_text,
                       (a.embedding <=> b.embedding) AS distancia
                FROM kb_chunks a
                JOIN kb_chunks b ON b.inbox_id = a.inbox_id AND b.source_id != a.source_id
                WHERE a.source_id = %s AND (a.embedding <=> b.embedding) < %s
                ORDER BY distancia ASC
                LIMIT %s
                """,
                (source_id, umbral, limite),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "chunk_index": r[0],
            "chunk_text": r[1],
            "fuente_id": r[2],
            "fuente_chunk_index": r[3],
            "fuente_chunk_text": r[4],
            "similitud": round(1 - r[5], 4),
        }
        for r in rows
    ]


def _consultar_chunks(source_id):
    import psycopg2

    conf = frappe.conf.get("kb_pg") or {}
    if not conf.get("host"):
        frappe.throw("Conexion a la base de conocimiento (kb_pg) no configurada en este sitio")

    conn = psycopg2.connect(
        host=conf["host"],
        port=conf.get("port", 5432),
        dbname=conf["database"],
        user=conf["user"],
        password=conf["password"],
        connect_timeout=5,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_index, chunk_text FROM kb_chunks WHERE source_id = %s ORDER BY chunk_index",
                (source_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [{"chunk_index": r[0], "chunk_text": r[1]} for r in rows]
