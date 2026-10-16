"""Operational memory: customers, technicians, work orders, audit trail.

SurrealDB adapter (graph):   customer -[requested]-> work_order -[assigned_to]-> technician
Runs embedded (surrealkv file, no account) or against SurrealDB Cloud via SURREAL_URL.
If SurrealDB can't start at all, a plain in-memory store keeps the demo running
and the UI shows the mock badge."""
import datetime as dt
import json
import logging
import threading
import uuid
from pathlib import Path

from . import config

log = logging.getLogger("jarvis.store")
SEED = json.loads((Path(__file__).parent / "data" / "seed.json").read_text())


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _clean(o):
    try:
        from surrealdb import RecordID
    except Exception:  # pragma: no cover
        RecordID = ()
    if RecordID and isinstance(o, RecordID):
        return f"{o.table_name}:{o.id}"
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


def _key(rid: str) -> str:
    return str(rid).split(":", 1)[-1]


class SurrealStore:
    def __init__(self, url: str):
        from surrealdb import Surreal
        self.lock = threading.Lock()
        self.remote = url.startswith(("ws", "http"))
        if url.startswith("surrealkv://"):
            Path(url.split("://", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        self.db = Surreal(url)
        if hasattr(self.db, "connect"):
            self.db.connect()
        if self.remote and config.SURREAL_USER:
            self.db.signin({"username": config.SURREAL_USER, "password": config.SURREAL_PASS})
        self.db.use(config.SURREAL_NS, config.SURREAL_DB)
        self.status = {"name": "SurrealDB", "mode": "live",
                       "detail": "SurrealDB Cloud" if self.remote else ("Embedded SurrealDB (in-memory)" if url.startswith("mem") else "Embedded SurrealDB graph (local file)")}
        self._seed()

    def q(self, sql, vars=None):
        with self.lock:
            return _clean(self.db.query(sql, vars or {}))

    def _rid(self, table, key):
        from surrealdb import RecordID
        return RecordID(table, key)

    def _seed(self):
        n = self.q("SELECT count() AS n FROM customer GROUP ALL;")
        if n and (n[0].get("n") or 0) > 0:
            return
        for t in SEED["technicians"]:
            self.q("UPSERT $id CONTENT $d;", {"id": self._rid("technician", t["id"]), "d": {k: v for k, v in t.items() if k != "id"}})
        for c in SEED["customers"]:
            self.q("UPSERT $id CONTENT $d;", {"id": self._rid("customer", c["id"]), "d": {k: v for k, v in c.items() if k != "id"}})
        for w in SEED["work_orders"]:
            self._insert_wo({"summary": w["summary"], "status": w["status"], "created": w["date"] + "T12:00:00+00:00",
                             "tech_id": w["tech"], "source": "history"}, w["customer_id"], w["tech"])

    def _insert_wo(self, data, cust_key, tech_key=None):
        key = uuid.uuid4().hex[:10]
        wid = self._rid("work_order", key)
        self.q("CREATE $w CONTENT $d; RELATE $c->requested->$w SET at = $at;",
               {"w": wid, "d": data, "c": self._rid("customer", cust_key), "at": data.get("created", now_iso())})
        if tech_key:
            self.q("RELATE $w->assigned_to->$t;", {"w": wid, "t": self._rid("technician", tech_key)})
        return f"work_order:{key}"

    # ---------------------------------------------------------------- API
    def find_customer(self, phone=None, name=None):
        if phone:
            r = self.q("SELECT * FROM customer WHERE phone = $p LIMIT 1;", {"p": phone})
            if r:
                return self._cust(r[0])
        if name:
            r = self.q("SELECT * FROM customer WHERE string::lowercase(name) = $n LIMIT 1;", {"n": name.lower().strip()})
            if r:
                return self._cust(r[0])
        return None

    def _cust(self, row):
        row = dict(row)
        row["id"] = _key(row["id"])
        return row

    def history(self, cust_key):
        # Graph traversal: customer -> requested -> work_order (+ assigned technician name)
        r = self.q("SELECT ->requested->work_order.{summary, status, created, priority, "
                   "tech: ->assigned_to->technician.name} AS wos FROM $c;", {"c": self._rid("customer", cust_key)})
        wos = (r[0].get("wos") if r else None) or []
        out = []
        for w in wos:
            tech = w.get("tech")
            if isinstance(tech, list):
                tech = tech[0] if tech else None
            out.append({"summary": w.get("summary"), "status": w.get("status"), "date": str(w.get("created", ""))[:10],
                        "priority": w.get("priority"), "tech": tech})
        return sorted(out, key=lambda x: x["date"], reverse=True)

    def technicians(self):
        rows = self.q("SELECT * FROM technician ORDER BY id;")
        return [{**r, "id": _key(r["id"])} for r in rows]

    def create_customer(self, name, phone):
        key = "C" + uuid.uuid4().hex[:5].upper()
        d = {"name": name or "Unknown caller", "phone": phone or "", "address": "unknown", "zone": "Edison",
             "equipment": {"type": "unknown"}, "plan": "none", "household_notes": "", "new": True}
        self.q("UPSERT $id CONTENT $d;", {"id": self._rid("customer", key), "d": d})
        return {"id": key, **d}

    def create_work_order(self, data, cust_key, tech_key=None):
        return self._insert_wo(data, cust_key, tech_key)

    def get_work_order(self, wid):
        r = self.q("SELECT * FROM $w;", {"w": self._rid("work_order", _key(wid))})
        return r[0] if r else None

    def update_work_order(self, wid, fields):
        r = self.q("UPDATE $w MERGE $f RETURN AFTER;", {"w": self._rid("work_order", _key(wid)), "f": fields})
        return r[0] if r else None

    def list_work_orders(self, limit=12):
        return self.q("SELECT *, <-requested<-customer.name AS customer FROM work_order WHERE source != 'history' "
                      "ORDER BY created DESC LIMIT $l;", {"l": limit})

    def audit(self, **e):
        try:
            e["at"] = now_iso()
            self.q("CREATE audit CONTENT $d;", {"d": _clean(e)})
        except Exception as ex:
            log.warning("audit failed: %s", ex)

    def list_audit(self, limit=30):
        return self.q("SELECT * FROM audit ORDER BY at DESC LIMIT $l;", {"l": limit})


class DictStore:
    """Fallback if SurrealDB is unavailable. Same interface."""

    def __init__(self, reason=""):
        self.status = {"name": "SurrealDB", "mode": "mock", "detail": f"In-memory fallback store ({reason[:100]})"}
        self.techs = {t["id"]: dict(t) for t in SEED["technicians"]}
        self.custs = {c["id"]: dict(c) for c in SEED["customers"]}
        self.wos, self.audits = {}, []
        for w in SEED["work_orders"]:
            self.create_work_order({"summary": w["summary"], "status": w["status"], "created": w["date"] + "T12:00:00+00:00",
                                    "source": "history"}, w["customer_id"], w["tech"])

    def find_customer(self, phone=None, name=None):
        for c in self.custs.values():
            if (phone and c["phone"] == phone) or (name and c["name"].lower() == name.lower().strip()):
                return dict(c)
        return None

    def history(self, cust_key):
        out = [{"summary": w["summary"], "status": w["status"], "date": w["created"][:10], "priority": w.get("priority"),
                "tech": self.techs.get(w.get("tech_id"), {}).get("name")}
               for w in self.wos.values() if w["customer_id"] == cust_key]
        return sorted(out, key=lambda x: x["date"], reverse=True)

    def technicians(self):
        return [dict(t) for t in self.techs.values()]

    def create_customer(self, name, phone):
        key = "C" + uuid.uuid4().hex[:5].upper()
        self.custs[key] = {"id": key, "name": name or "Unknown caller", "phone": phone or "", "address": "unknown",
                           "zone": "Edison", "equipment": {"type": "unknown"}, "plan": "none", "household_notes": "", "new": True}
        return dict(self.custs[key])

    def create_work_order(self, data, cust_key, tech_key=None):
        wid = "work_order:" + uuid.uuid4().hex[:10]
        self.wos[wid] = {**data, "id": wid, "customer_id": cust_key, "tech_id": tech_key}
        return wid

    def get_work_order(self, wid):
        return self.wos.get(wid if wid.startswith("work_order:") else "work_order:" + wid)

    def update_work_order(self, wid, fields):
        w = self.get_work_order(wid)
        if w:
            w.update(fields)
        return w

    def list_work_orders(self, limit=12):
        rows = [dict(w, customer=[self.custs.get(w["customer_id"], {}).get("name")]) for w in self.wos.values() if w.get("source") != "history"]
        return sorted(rows, key=lambda w: w["created"], reverse=True)[:limit]

    def audit(self, **e):
        e["at"] = now_iso()
        self.audits.append(_clean(e))

    def list_audit(self, limit=30):
        return list(reversed(self.audits))[:limit]


def make_store():
    try:
        return SurrealStore(config.SURREAL_URL)
    except Exception as e:
        log.warning("SurrealDB unavailable, using in-memory fallback: %s", e)
        return DictStore(str(e))
