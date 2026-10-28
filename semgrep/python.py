# Test cases for python.yml: `semgrep --test semgrep/` checks each annotation.
from fastapi import APIRouter, Query

router = APIRouter()
_SORT = {"name": "name", "newest": "created_at DESC"}


@router.get("/items")
def list_items(store_id: int = Query(...), sort: str = "name", q: str = ""):
    with db() as cur:
        # ok: request-value-in-sql-string
        cur.execute("SELECT * FROM items WHERE store_id = %s", (store_id,))
        # ok: request-value-in-sql-string
        cur.execute(f"SELECT * FROM items ORDER BY {_SORT.get(sort, 'name')}")
        # ruleid: request-value-in-sql-string
        cur.execute(f"SELECT * FROM items WHERE name LIKE '%{q}%'")
        # ruleid: request-value-in-sql-string
        cur.execute("SELECT * FROM items ORDER BY " + sort)
        where = f"WHERE store_id = {store_id}"
        # ruleid: request-value-in-sql-string
        cur.execute(f"SELECT * FROM items {where}")


@router.post("/items/status")
async def set_status(body: StatusBody):
    ph = ",".join(["%s"] * len(body.ids))
    with db() as cur:
        # ok: request-value-in-sql-string
        cur.execute(f"UPDATE items SET status = %s WHERE id IN ({ph})", (body.status, *body.ids))
        # ruleid: request-value-in-sql-string
        cur.executemany(f"UPDATE items SET {body.column} = %s WHERE id = %s", body.rows)


def not_a_route(table: str):
    with db() as cur:
        # ok: request-value-in-sql-string
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")


@router.get("/items/sorted")
def sorted_items(sort: str, order: dict):
    with db() as cur:
        # ok: request-value-in-sql-string
        cur.execute(f"SELECT * FROM items ORDER BY {_SORT[sort]}")
        # ruleid: request-value-in-sql-string
        cur.execute(f"SELECT * FROM items ORDER BY {order[sort]}")
