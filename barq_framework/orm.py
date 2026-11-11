"""Framework layer, part 1 (v0.9.0): the ORM and friends.

    Model            base class for database models: `class User extends Model`
    ModelQuery       query builder that returns models (User.where(...).get())
    Relation         HasMany, HasOne, BelongsTo, BelongsToMany
    Resource         API response transformer: `class UserResource extends Resource`
    FormRequest      validated request: `class StoreUser extends FormRequest`
    Migration        base class for files in database/migrations/
    Seeder           base class for files in database/seeders/

Model columns are not declared as properties. They live in the hidden state of
the object (BarqObject.native = ModelState) and are reached through the
`__get` / `__set` hooks, so `user.name` reads a column.

Static functions such as User.find() are stored once on Model and marked
`late_static`: the interpreter hands them the class they were called on.
"""

import json
import os
import re
import time

from barq.builtins import T, need
from barq.runtime import BarqClass, BarqObject, BarqThrow, BoundMethod, NativeFunction, PropInfo, to_display, type_name
from barq.stdlib import arg, to_json_value
from barq.stdlib2 import (QUERY_METHODS, aggregate, add_where, database_connect, db_rows, db_run, db_state, db_value,
                      insert_rows, native_class, new_native, new_query, qb, qb_first, qb_get, qid, select_sql,
                      where_in)


# ===========================================================================
# naming helpers
# ===========================================================================

IRREGULAR = {"person": "people", "child": "children", "man": "men", "woman": "women", "mouse": "mice",
             "goose": "geese", "foot": "feet", "tooth": "teeth", "datum": "data", "medium": "media"}
UNCOUNTABLE = {"news", "equipment", "information", "rice", "money", "series", "species", "sheep", "fish", "data"}


def snake(name):
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    s = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", s)
    return s.lower()


def plural(word):
    head, _, last = word.rpartition("_")
    w = last
    if w in UNCOUNTABLE:
        out = w
    elif w in IRREGULAR:
        out = IRREGULAR[w]
    elif re.search(r"[^aeiou]y$", w):
        out = w[:-1] + "ies"
    elif re.search(r"(s|x|z|ch|sh)$", w):
        out = w + "es"
    else:
        out = w + "s"
    return f"{head}_{out}" if head else out


def table_for(cls_name):
    """User -> users, BlogPost -> blog_posts, Category -> categories."""
    return plural(snake(cls_name))


# ===========================================================================
# state
# ===========================================================================

class ModelState:
    __slots__ = ("attrs", "original", "exists", "relations", "recent", "token")

    def __init__(self):
        self.attrs = {}
        self.original = {}
        self.exists = False
        self.relations = {}
        self.recent = False      # inserted by this program run (for 201 responses)
        self.token = None        # API token row when authenticated by the token guard (auth.py)


class Meta:
    __slots__ = ("cls", "table", "pk", "fillable", "guarded", "hidden", "casts", "timestamps", "connection",
                 "per_page", "soft")


class MQState:
    """Hidden state of a ModelQuery (and every Relation)."""
    __slots__ = ("cls", "q", "eager", "rel", "constrained", "trashed", "scoped")

    def __init__(self, cls, q):
        self.cls = cls
        self.q = q                  # QueryBuilder object
        self.eager = []             # relation names for with()
        self.rel = None             # RelInfo for relations
        self.constrained = False    # parent constraint added?
        self.trashed = "without"    # soft deletes: without / with / only
        self.scoped = False         # soft-delete constraint added?


class RelInfo:
    __slots__ = ("kind", "parent", "related", "fk", "key", "pivot", "fpk", "rpk", "extra")

    def __init__(self, kind, parent, related, fk, key, pivot=None, fpk=None, rpk=None, extra=None):
        self.kind = kind            # HasMany / HasOne / BelongsTo / BelongsToMany
        self.parent = parent        # the model the relation was called on
        self.related = related      # related model class
        self.fk = fk                # HasMany/HasOne: column on related; BelongsTo: column on parent
        self.key = key              # HasMany/HasOne: parent key; BelongsTo: related key; BelongsToMany: related pk
        self.pivot = pivot
        self.fpk = fpk              # pivot column pointing at the parent
        self.rpk = rpk              # pivot column pointing at the related model
        self.extra = extra or {}    # BelongsToMany: more pivot columns every row has ({"model_type": ...});
                                    # a None value means IS NULL


RELATION_KINDS = ("HasMany", "HasOne", "BelongsTo", "BelongsToMany")
MANY = ("HasMany", "BelongsToMany")

# fn(i, model, what, n) with what = "creating" (before the INSERT), "saved" or "deleted"
# (a row removed, not soft-deleted). Used by the permissions package.
WRITE_HOOKS = []


def write_hooks(i, obj, what, n):
    for fn in WRITE_HOOKS:
        fn(i, obj, what, n)


def pivot_extra_where(i, r, q, n, table=None):
    """Add the relation's fixed pivot columns to a query on (or joined with) the pivot table."""
    for col, value in r.extra.items():
        name = qid(i, f"{table}.{col}" if table else col, n)
        if value is None:
            qb(q).wheres.append(("AND", f"{name} IS NULL", []))
        else:
            qb(q).wheres.append(("AND", f"{name} = ?", [db_value(i, value, n, col)]))


def orm(i):
    st = i.__dict__.get("_orm")
    if st is None:
        st = i._orm = {"default": None, "named": {}}
    return st


def mstate(obj):
    st = obj.native
    if st is None:
        st = obj.native = ModelState()
    return st


def mq(this):
    return this.native


def model_class(i):
    return i.classes["Model"]


def is_model(i, v):
    return isinstance(v, BarqObject) and v.cls.is_subclass_of(model_class(i)) and v.cls is not model_class(i)


def need_model_class(i, cls, n, fname):
    if not isinstance(cls, BarqClass) or not cls.is_subclass_of(model_class(i)) or cls is model_class(i):
        what = cls.name if isinstance(cls, BarqClass) else type_name(cls)
        if cls is model_class(i):
            i.throw("TypeException", f"{fname}() must be called on a model class such as User, not on Model itself", n)
        i.throw("TypeException", f"{fname}() needs a model class (a class that extends Model), got {what}", n)
    return cls


def field_value(cls_obj, name):
    """A model setting: instance property default, else a static of that name."""
    if name in cls_obj.fields:
        return True, cls_obj.fields[name]
    p = cls_obj.cls.find_static(name)
    if p is not None and not p.owner.native:
        return True, p.owner.static_values[name]
    return False, None


def meta(i, cls, n=None):
    m = cls.__dict__.get("_orm_meta")
    if m is not None:
        return m
    need_model_class(i, cls, n, "Model")
    bare = i.instantiate_bare(cls)

    def setting(name, kinds, default):
        found, v = field_value(bare, name)
        if not found or v is None:
            return default
        if type_name(v) not in kinds:
            i.throw("TypeException", f"{cls.name}.{name} must be {' or '.join(kinds)}, got {type_name(v)}", n)
        return v

    m = Meta()
    m.cls = cls
    m.table = setting("table", ("string",), None) or table_for(cls.name)
    m.pk = setting("primaryKey", ("string",), "id")
    m.fillable = setting("fillable", ("list",), None)
    m.guarded = setting("guarded", ("list",), None)
    m.hidden = setting("hidden", ("list",), [])
    m.casts = setting("casts", ("dict",), {})
    m.timestamps = setting("timestamps", ("bool",), True)
    m.connection = setting("connection", ("string",), None)
    m.per_page = setting("perPage", ("int",), 15)
    m.soft = setting("softDeletes", ("bool",), False)
    for name, t in m.casts.items():
        if t not in CASTS:
            i.throw("ValueException", f'{cls.name}.casts: unknown cast "{t}" for "{name}" (use {", ".join(sorted(CASTS))})', n)
    cls._orm_meta = m
    return m


def model_db(i, m, n):
    st = orm(i)
    if m.connection:
        db = st["named"].get(m.connection)
        if db is None or db.native.conn is None:
            db = st["named"][m.connection] = database_connect(i, [m.connection], n)
        return db
    db = st["default"]
    if db is None or db.native.conn is None:
        db = st["default"] = database_connect(i, [], n)
    return db


def default_db(i, n):
    db = orm(i)["default"]
    if db is None or db.native.conn is None:
        db = orm(i)["default"] = database_connect(i, [], n)
    return db


def table_columns(i, db, table, n):
    cur = db_run(i, db_state(i, db, n), f"PRAGMA table_info({qid(i, table, n, 'table')})", [], n)
    return [r[1] for r in cur.fetchall()]


# ===========================================================================
# casts
# ===========================================================================

CASTS = {"bool", "int", "float", "string", "json", "list", "dict"}


def cast_in(i, m, attrs):
    """Database value -> Barq value."""
    for name, t in m.casts.items():
        v = attrs.get(name)
        if v is None or name not in attrs:
            continue
        if t == "bool":
            attrs[name] = v in (1, "1", True, "true")
        elif t == "int":
            try:
                attrs[name] = int(v)
            except (TypeError, ValueError):
                pass
        elif t == "float":
            try:
                attrs[name] = float(v)
            except (TypeError, ValueError):
                pass
        elif t == "string":
            attrs[name] = to_display(v)
        elif isinstance(v, str):
            try:
                attrs[name] = json.loads(v)
            except ValueError:
                pass
    return attrs


def cast_out(i, m, name, v, n):
    """Barq value -> database value."""
    if v is None:
        return None
    t = m.casts.get(name)
    if t in ("json", "list", "dict") and not isinstance(v, str):
        return json.dumps(to_json_value(i, v, n), ensure_ascii=False)
    return db_value(i, v, n, f"{m.cls.name}.{name}")


# ===========================================================================
# hydrating models
# ===========================================================================

def hydrate(i, cls, row):
    m = meta(i, cls)
    obj = i.instantiate_bare(cls)
    st = obj.native = ModelState()
    st.attrs = cast_in(i, m, dict(row))
    st.original = dict(st.attrs)
    st.exists = True
    if i.__dict__.get("_model_events"):
        from .framework import fire_model_event
        fire_model_event(i, obj, "retrieved", None)
    return obj


def hydrate_all(i, cls, rows):
    return [hydrate(i, cls, r) for r in rows]


# ===========================================================================
# ModelQuery
# ===========================================================================

def new_mq(i, cls, n, kind="ModelQuery"):
    m = meta(i, cls, n)
    db = model_db(i, m, n)
    q = new_query(i, db, qid(i, m.table, n, "table"))
    return new_native(i, kind, MQState(cls, q))


def group_wheres(q):
    """Before adding a constraint, wrap `a OR b` in parentheses so it stays `(a OR b) AND c`."""
    from barq.stdlib2 import where_sql
    if any(op == "OR" for op, _, _ in q.wheres[1:]):
        sql, params = where_sql(q)
        q.wheres = [("AND", f"({sql[7:]})", params)]


def soft_scope(i, st, n):
    """Soft-deleting models: leave out rows with deleted_at set (unless withTrashed/onlyTrashed)."""
    if st.scoped:
        return
    st.scoped = True
    m = meta(i, st.cls, n)
    if not m.soft or st.trashed == "with":
        return
    q = qb(st.q)
    group_wheres(q)
    col = qid(i, f"{m.table}.deleted_at", n)
    q.wheres.append(("AND", f"{col} IS {'NOT ' if st.trashed == 'only' else ''}NULL", []))


def prepare(i, this, n):
    """Relations add `where parent_key = ...` only when the query runs, so
    eager loading can reuse the same relation with whereIn instead."""
    st = mq(this)
    soft_scope(i, st, n)
    r = st.rel
    if r is None or st.constrained:
        return st
    st.constrained = True
    group_wheres(qb(st.q))
    pst = mstate(r.parent)
    rm = meta(i, r.related, n)
    if r.kind in ("HasMany", "HasOne"):
        column, value = f"{rm.table}.{r.fk}", pst.attrs.get(r.key)
    elif r.kind == "BelongsTo":
        column, value = f"{rm.table}.{r.key}", pst.attrs.get(r.fk)
    else:
        column, value = f"{r.pivot}.{r.fpk}", pst.attrs.get(meta(i, r.parent.cls, n).pk)
    if value is None:
        qb(st.q).wheres.append(("AND", "0 = 1", []))    # unsaved parent: nothing matches
    else:
        add_where(i, [column, value], n, st.q, "AND", "where")
        if r.extra:
            pivot_extra_where(i, r, st.q, n, r.pivot)
    return st


def chain(name):
    fn, lo, hi = QUERY_METHODS[name]

    def f(i, a, n, this):
        fn(i, a, n, mq(this).q)
        return this
    return f, lo, hi


def passthru(name):
    fn, lo, hi = QUERY_METHODS[name]

    def f(i, a, n, this):
        st = prepare(i, this, n)
        return fn(i, a, n, st.q)
    return f, lo, hi


def mq_get(i, a, n, this):
    st = prepare(i, this, n)
    models = hydrate_all(i, st.cls, qb_get(i, [], n, st.q))
    if st.eager and models:
        load_relations(i, models, st.eager, n)
    return models


def mq_first(i, a, n, this):
    st = prepare(i, this, n)
    row = qb_first(i, [], n, st.q)
    if row is None:
        return None
    model = hydrate(i, st.cls, row)
    if st.eager:
        load_relations(i, [model], st.eager, n)
    return model


def not_found(i, cls, n, key=None):
    what = f"{cls.name} {to_display(key, i, True)}" if key is not None else cls.name
    obj = i.make_exception("ModelNotFoundException", f"{what} not found", n)
    obj.fields["status"] = 404
    obj.fields["model"] = cls.name
    raise BarqThrow(obj)


def mq_first_or_fail(i, a, n, this):
    model = mq_first(i, a, n, this)
    if model is None:
        not_found(i, mq(this).cls, n)
    return model


def mq_find(i, a, n, this):
    st = mq(this)
    m = meta(i, st.cls, n)
    key = a[0]
    if isinstance(key, list):
        where_in(False)(i, [f"{m.table}.{m.pk}", key], n, st.q)
        return mq_get(i, [], n, this)
    add_where(i, [f"{m.table}.{m.pk}", key], n, st.q, "AND", f"{st.cls.name}.find")
    return mq_first(i, [], n, this)


def mq_find_or_fail(i, a, n, this):
    found = mq_find(i, a, n, this)
    if found is None or (isinstance(a[0], list) and len(found) != len(set(map(repr, a[0])))):
        not_found(i, mq(this).cls, n, a[0])
    return found


def mq_paginate(i, a, n, this):
    st = prepare(i, this, n)
    m = meta(i, st.cls, n)
    per = arg(i, a, 0, ("int",), f"{st.cls.name}.paginate", n, m.per_page)
    page = arg(i, a, 1, ("int",), f"{st.cls.name}.paginate", n, 1)
    if page < 1 or per < 1:
        i.throw("ValueException", "paginate() needs perPage >= 1 and page >= 1", n)
    total = aggregate("COUNT")(i, [], n, st.q)
    q = qb(st.q)
    q.limit, q.offset = per, (page - 1) * per
    models = hydrate_all(i, st.cls, qb_get(i, [], n, st.q))
    if st.eager and models:
        load_relations(i, models, st.eager, n)
    last = max(1, -(-total // per))
    return {"data": models, "total": total, "page": page, "perPage": per, "lastPage": last,
            "hasMore": page < last}


def relation_names(i, a, n, fname):
    names = []
    for v in a:
        for x in (v if isinstance(v, list) else [v]):
            if not isinstance(x, str) or not x:
                i.throw("TypeException", f"{fname}() takes relation names as strings, got {type_name(x)}", n)
            names.append(x)
    return names


def mq_with(i, a, n, this):
    mq(this).eager.extend(relation_names(i, a, n, "with"))
    return this


def mq_update(i, a, n, this):
    st = prepare(i, this, n)
    values = dict(need(i, a[0], ("dict",), f"{st.cls.name} query update", 1, n))
    m = meta(i, st.cls, n)
    if m.timestamps and "updated_at" not in values and \
            "updated_at" in table_columns(i, qb(st.q).db, m.table, n):
        values["updated_at"] = int(time.time())
    values = {k: cast_out(i, m, k, v, n) for k, v in values.items()}
    return QUERY_METHODS["update"][0](i, [values], n, st.q)


def need_soft(i, cls, n, fname):
    if not meta(i, cls, n).soft:
        i.throw("TypeException", f"{fname}(): {cls.name} does not use soft deletes "
                f"(add 'protected bool softDeletes = true' to {cls.name} and table.softDeletes() to its migration)", n)


def mq_trashed(mode):
    def f(i, a, n, this):
        st = mq(this)
        if mode == "only":
            need_soft(i, st.cls, n, "onlyTrashed")
        if st.scoped:
            i.throw("TypeException", "call withTrashed()/onlyTrashed() before the query runs", n)
        st.trashed = mode
        return this
    return f


def mq_delete(i, a, n, this):
    """Soft-deleting models get deleted_at set instead of losing the rows."""
    st = mq(this)
    if meta(i, st.cls, n).soft:
        return mq_update(i, [{"deleted_at": int(time.time())}], n, this)
    st = prepare(i, this, n)
    return QUERY_METHODS["delete"][0](i, [], n, st.q)


def mq_force_delete(i, a, n, this):
    st = prepare(i, this, n)
    return QUERY_METHODS["delete"][0](i, [], n, st.q)


def mq_restore(i, a, n, this):
    st = mq(this)
    need_soft(i, st.cls, n, "restore")
    if st.trashed == "without" and not st.scoped:
        st.trashed = "with"
    return mq_update(i, [{"deleted_at": None}], n, this)


def mq_each(i, a, n, this):
    from barq.builtins import call
    for model in mq_get(i, [], n, this):
        call(i, a[0], [model], n, "each")
    return None


def mq_scope_get(i, a, n, this):
    """q.active() runs the model's scopeActive(q) method."""
    name = need(i, a[0], ("string",), "ModelQuery", 1, n)
    st = mq(this)
    m = find_scope(st.cls, name)
    if m is None:
        i.throw("NameException", f"{st.cls.name} query has no method '{name}' "
                f"(define a scope with 'karo scope{name[:1].upper()}{name[1:]}(ModelQuery q)' in {st.cls.name})", n)
    return scope_caller(i, st.cls, m, lambda: this, name)


def find_scope(cls, name):
    if not name:
        return None
    return cls.find_method("scope" + name[0].upper() + name[1:])


def scope_caller(i, cls, m, query_fn, name):
    def run(i2, a, n2):
        q = query_fn()
        result = i.call_function(BoundMethod(i.instantiate_bare(cls), m), [q, *a], n2)
        return result if isinstance(result, BarqObject) and result.cls.is_subclass_of(i.classes["ModelQuery"]) else q
    return NativeFunction(f"{cls.name}.{name}", run, 0, -1)


MQ_METHODS = {name: chain(name) for name in (
    "where", "orWhere", "whereIn", "whereNotIn", "orWhereIn", "whereNull", "whereNotNull", "orWhereNull",
    "whereBetween", "join", "leftJoin", "orderBy", "orderByDesc", "latest", "oldest", "groupBy", "limit", "take",
    "offset", "skip", "select", "distinct")}
MQ_METHODS.update({name: passthru(name) for name in (
    "count", "sum", "avg", "min", "max", "exists", "pluck", "value", "toSql", "bindings", "increment", "decrement")})
MQ_METHODS.update({
    "get": (mq_get, 0, 0),
    "first": (mq_first, 0, 0),
    "firstOrFail": (mq_first_or_fail, 0, 0),
    "find": (mq_find, 1, 1),
    "findOrFail": (mq_find_or_fail, 1, 1),
    "paginate": (mq_paginate, 0, 2),
    "with": (mq_with, 1, -1),
    "update": (mq_update, 1, 1),
    "each": (mq_each, 1, 1),
    "delete": (mq_delete, 0, 0),
    "forceDelete": (mq_force_delete, 0, 0),
    "restore": (mq_restore, 0, 0),
    "withTrashed": (mq_trashed("with"), 0, 0),
    "onlyTrashed": (mq_trashed("only"), 0, 0),
    "__get": (mq_scope_get, 1, 1),
})


# ===========================================================================
# Relations
# ===========================================================================

def relation_of(i, this, kind, a, n):
    """this.hasMany(Post, foreignKey?, localKey?) and friends."""
    fname = kind[0].lower() + kind[1:]
    related = need_model_class(i, a[0], n, fname)
    pm = meta(i, this.cls, n)
    rm = meta(i, related, n)
    for k, v in enumerate(a[1:], start=2):
        if v is not None and not isinstance(v, str):
            i.throw("TypeException", f"{fname}() argument {k} must be a column name (string), got {type_name(v)}", n)
    extra = list(a[1:]) + [None] * 4
    if kind in ("HasMany", "HasOne"):
        info = RelInfo(kind, this, related, extra[0] or snake(this.cls.name) + "_id", extra[1] or pm.pk)
    elif kind == "BelongsTo":
        info = RelInfo(kind, this, related, extra[0] or snake(related.name) + "_id", extra[1] or rm.pk)
    else:
        pivot = extra[0] or "_".join(sorted([snake(this.cls.name), snake(related.name)]))
        info = RelInfo(kind, this, related, None, rm.pk, pivot,
                       extra[1] or snake(this.cls.name) + "_id", extra[2] or snake(related.name) + "_id")
    obj = new_mq(i, related, n, kind)
    st = mq(obj)
    st.rel = info
    if kind == "BelongsToMany":
        q = qb(st.q)
        q.columns = [f'{qid(i, rm.table, n, "table")}.*']
        q.joins.append(f" INNER JOIN {qid(i, info.pivot, n, 'table')} ON "
                       f"{qid(i, info.pivot + '.' + info.rpk, n)} = {qid(i, rm.table + '.' + rm.pk, n)}")
    return obj


def rel_info(i, this, n):
    return mq(this).rel


def rel_create(i, a, n, this):
    r = rel_info(i, this, n)
    data = dict(need(i, a[0], ("dict",), f"{r.kind}.create", 1, n))
    if r.kind in ("HasMany", "HasOne"):
        model = new_model(i, r.related, data, n)
        mstate(model).attrs[r.fk] = parent_key(i, r, n)
        save(i, model, n)
        forget_relation(r)
        return model
    if r.kind == "BelongsToMany":
        model = new_model(i, r.related, data, n)
        save(i, model, n)
        pivot_attach(i, r, [mstate(model).attrs.get(r.key)], a[1] if len(a) > 1 else {}, n)
        return model
    i.throw("TypeException", "create() is not available on a BelongsTo relation — create the parent, then associate() it", n)


def parent_key(i, r, n):
    value = mstate(r.parent).attrs.get(r.key)
    if value is None:
        i.throw("DbException", f"save the {r.parent.cls.name} first: it has no {r.key} yet", n)
    return value


def forget_relation(r):
    """A write through a relation clears the parent's loaded copies."""
    mstate(r.parent).relations.clear()


def rel_save(i, a, n, this):
    r = rel_info(i, this, n)
    model = a[0]
    if not is_model(i, model) or not model.cls.is_subclass_of(r.related):
        i.throw("TypeException", f"{r.kind}.save() needs a {r.related.name}, got {type_name(model)}", n)
    if r.kind in ("HasMany", "HasOne"):
        mstate(model).attrs[r.fk] = parent_key(i, r, n)
        save(i, model, n)
    elif r.kind == "BelongsToMany":
        save(i, model, n)
        pivot_attach(i, r, [mstate(model).attrs.get(r.key)], {}, n)
    else:
        i.throw("TypeException", "use associate() on a BelongsTo relation", n)
    forget_relation(r)
    return model


def rel_associate(i, a, n, this):
    r = rel_info(i, this, n)
    if r.kind != "BelongsTo":
        i.throw("TypeException", f"associate() is only for BelongsTo relations, this is {r.kind}", n)
    model = a[0]
    if model is None:
        mstate(r.parent).attrs[r.fk] = None
    else:
        if not is_model(i, model) or not model.cls.is_subclass_of(r.related):
            i.throw("TypeException", f"associate() needs a {r.related.name}, got {type_name(model)}", n)
        mstate(r.parent).attrs[r.fk] = mstate(model).attrs.get(r.key)
    forget_relation(r)
    return r.parent


def key_list(i, r, v, n, fname):
    items = v if isinstance(v, list) else [v]
    out = []
    for x in items:
        if is_model(i, x):
            x = mstate(x).attrs.get(r.key)
        if x is None or isinstance(x, bool) or not isinstance(x, (int, str)):
            i.throw("TypeException", f"{fname}() takes ids or {r.related.name} objects, got {type_name(x)}", n)
        out.append(x)
    return out


def pivot_db(i, r, n):
    return model_db(i, meta(i, r.related, n), n)


def pivot_query(i, r, n):
    return new_query(i, pivot_db(i, r, n), qid(i, r.pivot, n, "table"))


def pivot_attach(i, r, ids, extra, n):
    if not isinstance(extra, dict):
        i.throw("TypeException", f"attach() extra pivot columns must be a dict, got {type_name(extra)}", n)
    owner = mstate(r.parent).attrs.get(meta(i, r.parent.cls, n).pk)
    if owner is None:
        i.throw("DbException", f"save the {r.parent.cls.name} first: it has no id yet", n)
    existing = set(pivot_ids(i, r, n))
    rows = [{r.fpk: owner, r.rpk: x, **r.extra, **extra} for x in ids if x not in existing]
    if rows:
        insert_rows(i, pivot_query(i, r, n), rows, n, "attach")
    forget_relation(r)
    return len(rows)


def pivot_ids(i, r, n):
    owner = mstate(r.parent).attrs.get(meta(i, r.parent.cls, n).pk)
    q = pivot_query(i, r, n)
    add_where(i, [r.fpk, owner], n, q, "AND", "pivot")
    pivot_extra_where(i, r, q, n)
    return QUERY_METHODS["pluck"][0](i, [r.rpk], n, q)


def need_many(i, this, fname, n):
    r = rel_info(i, this, n)
    if r.kind != "BelongsToMany":
        i.throw("TypeException", f"{fname}() is only for BelongsToMany relations, this is {r.kind}", n)
    return r


def rel_attach(i, a, n, this):
    r = need_many(i, this, "attach", n)
    return pivot_attach(i, r, key_list(i, r, a[0], n, "attach"), a[1] if len(a) > 1 else {}, n)


def rel_detach(i, a, n, this):
    r = need_many(i, this, "detach", n)
    owner = mstate(r.parent).attrs.get(meta(i, r.parent.cls, n).pk)
    q = pivot_query(i, r, n)
    add_where(i, [r.fpk, owner], n, q, "AND", "detach")
    pivot_extra_where(i, r, q, n)
    if a and a[0] is not None:
        where_in(False)(i, [r.rpk, key_list(i, r, a[0], n, "detach")], n, q)
    forget_relation(r)
    return QUERY_METHODS["delete"][0](i, [], n, q)


def rel_sync(i, a, n, this):
    r = need_many(i, this, "sync", n)
    wanted = key_list(i, r, need(i, a[0], ("list",), "sync", 1, n), n, "sync")
    current = pivot_ids(i, r, n)
    detach = [x for x in current if x not in wanted]
    attach = [x for x in dict.fromkeys(wanted) if x not in current]
    if detach:
        rel_detach(i, [detach], n, this)
    if attach:
        pivot_attach(i, r, attach, {}, n)
    forget_relation(r)
    return {"attached": attach, "detached": detach}


RELATION_METHODS = {
    "create": (rel_create, 1, 2),
    "save": (rel_save, 1, 1),
    "associate": (rel_associate, 1, 1),
    "dissociate": (lambda i, a, n, this: rel_associate(i, [None], n, this), 0, 0),
    "attach": (rel_attach, 1, 2),
    "detach": (rel_detach, 0, 1),
    "sync": (rel_sync, 1, 1),
}


def is_relation_method(i, cls, m):
    """A method whose return type is HasMany/HasOne/BelongsTo/BelongsToMany."""
    ret = getattr(m, "ret", None)
    return ret is not None and ret.name in RELATION_KINDS and not m.params and not m.is_static \
        and cls.is_subclass_of(model_class(i))


def relation_value(i, obj, m, n):
    """user.posts (no parentheses): the related models, loaded once."""
    st = mstate(obj)
    if m.name in st.relations:
        return st.relations[m.name]
    rel = i.call_function(BoundMethod(obj, m), [], n)
    many = mq(rel).rel.kind in MANY
    value = mq_get(i, [], n, rel) if many else mq_first(i, [], n, rel)
    st.relations[m.name] = value
    return value


def load_relations(i, models, names, n):
    """Eager loading for with()/load(): one query per relation, not one per model."""
    groups = {}
    for name in names:
        head, _, rest = name.partition(".")
        groups.setdefault(head, [])
        if rest:
            groups[head].append(rest)
    for name, nested in groups.items():
        sample = models[0]
        m = sample.cls.find_method(name)
        if m is None or not is_relation_method(i, sample.cls, m):
            i.throw("NameException", f"{sample.cls.name} has no relation '{name}' "
                    f"(a method with #[HasMany], #[HasOne], #[BelongsTo] or #[BelongsToMany] and no parameters)", n)
        rel = i.call_function(BoundMethod(sample, m), [], n)
        st = mq(rel)
        r = st.rel
        st.constrained = True
        soft_scope(i, st, n)
        rm = meta(i, r.related, n)
        if r.kind in ("HasMany", "HasOne"):
            local, remote = r.key, r.fk
            column = f"{rm.table}.{r.fk}"
        elif r.kind == "BelongsTo":
            local, remote = r.fk, r.key
            column = f"{rm.table}.{r.key}"
        else:
            local, remote = meta(i, sample.cls, n).pk, "__pivot_key"
            column = f"{r.pivot}.{r.fpk}"
            qb(st.q).columns.append(f'{qid(i, column, n)} AS "__pivot_key"')
        keys = list(dict.fromkeys(k for k in (mstate(p).attrs.get(local) for p in models) if k is not None))
        related = []
        if keys:
            where_in(False)(i, [column, keys], n, st.q)
            if r.kind == "BelongsToMany" and r.extra:
                pivot_extra_where(i, r, st.q, n, r.pivot)
            related = hydrate_all(i, r.related, qb_get(i, [], n, st.q))
        by_key = {}
        for obj in related:
            attrs = mstate(obj).attrs
            k = attrs.pop(remote) if remote == "__pivot_key" else attrs.get(remote)
            if remote == "__pivot_key":
                mstate(obj).original.pop("__pivot_key", None)
            by_key.setdefault(k, []).append(obj)
        for p in models:
            found = by_key.get(mstate(p).attrs.get(local), [])
            mstate(p).relations[name] = list(found) if r.kind in MANY else (found[0] if found else None)
        if nested and related:
            load_relations(i, related, nested, n)


# ===========================================================================
# Model — instance side
# ===========================================================================

def fill_check(i, m, key, n):
    if not isinstance(key, str):
        i.throw("TypeException", f"{m.cls.name} attribute names must be strings, got {type_name(key)}", n)
    if m.fillable is not None and key not in m.fillable:
        i.throw("MassAssignmentException", f"'{key}' is not fillable on {m.cls.name} — add it to {m.cls.name}.fillable", n)
    if m.guarded is not None and (key in m.guarded or "*" in m.guarded):
        i.throw("MassAssignmentException", f"'{key}' is guarded on {m.cls.name} — remove it from {m.cls.name}.guarded", n)


def fill(i, obj, data, n):
    m = meta(i, obj.cls, n)
    st = mstate(obj)
    for k, v in data.items():
        fill_check(i, m, k, n)
        st.attrs[k] = v
    return obj


def new_model(i, cls, data, n):
    obj = i.instantiate_bare(cls)
    mstate(obj)
    return fill(i, obj, data, n)


def dirty(st):
    from barq.runtime import values_equal
    return {k: v for k, v in st.attrs.items() if k not in st.original or not values_equal(v, st.original[k])}


def event(i, obj, name, n):
    """Fire a model event; False when a listener cancelled it."""
    if not i.__dict__.get("_model_events"):
        return True
    from .framework import fire_model_event
    return fire_model_event(i, obj, name, n)


def save(i, obj, n):
    """saving -> (creating -> INSERT -> created | updating -> UPDATE -> updated) -> saved.
    A listener of a ...ing event that returns false cancels the save: save() returns false."""
    m = meta(i, obj.cls, n)
    db = model_db(i, m, n)
    st = mstate(obj)
    q = new_query(i, db, qid(i, m.table, n, "table"))
    now = int(time.time())
    if not event(i, obj, "saving", n):
        return False
    if st.exists:
        if dirty(st) and not event(i, obj, "updating", n):
            return False
        changes = dirty(st)
        if not changes:
            event(i, obj, "saved", n)
            return True
        if m.timestamps and "updated_at" not in changes and "updated_at" in table_columns(i, db, m.table, n):
            st.attrs["updated_at"] = changes["updated_at"] = now
        key = st.original.get(m.pk, st.attrs.get(m.pk))
        add_where(i, [m.pk, key], n, q, "AND", "save")
        QUERY_METHODS["update"][0](i, [{k: cast_out(i, m, k, v, n) for k, v in changes.items()}], n, q)
        st.original = dict(st.attrs)
        event(i, obj, "updated", n)
    else:
        if not event(i, obj, "creating", n):
            return False
        if WRITE_HOOKS:
            write_hooks(i, obj, "creating", n)
        if m.timestamps:
            cols = table_columns(i, db, m.table, n)
            for c in ("created_at", "updated_at"):
                if c in cols and st.attrs.get(c) is None:
                    st.attrs[c] = now
        if not st.attrs:
            db_run(i, db_state(i, db, n), f"INSERT INTO {qb(q).table} DEFAULT VALUES", [], n)
        else:
            insert_rows(i, q, [{k: cast_out(i, m, k, v, n) for k, v in st.attrs.items()}], n, f"{obj.cls.name}.save")
        if st.attrs.get(m.pk) is None:
            st.attrs[m.pk] = db_state(i, db, n).last_id
        st.exists = True
        st.recent = True
        st.original = dict(st.attrs)
        event(i, obj, "created", n)
    st.original = dict(st.attrs)
    event(i, obj, "saved", n)
    if WRITE_HOOKS:
        write_hooks(i, obj, "saved", n)
    return True


def m_construct(i, a, n, this):
    mstate(this)
    if a:
        fill(i, this, need(i, a[0], ("dict",), this.cls.name, 1, n), n)
    return None


def m_get(i, a, n, this):
    name = a[0]
    st = mstate(this)
    if name in st.attrs:
        return st.attrs[name]
    if name in st.relations:
        return st.relations[name]
    m = meta(i, this.cls, n)
    cols = table_columns(i, model_db(i, m, n), m.table, n)
    if name in cols:
        return None
    if not cols:
        i.throw("DbException", f'{this.cls.name} uses table "{m.table}", which does not exist — create it with a migration '
                f"or set 'protected string table = \"...\"' in {this.cls.name}", n)
    i.throw("NameException", f"{this.cls.name} has no column or property '{name}' (columns: {', '.join(cols)})", n)


def m_set(i, a, n, this):
    mstate(this).attrs[a[0]] = a[1]
    return None


def m_save(i, a, n, this):
    return save(i, this, n)


def m_update(i, a, n, this):
    fill(i, this, need(i, a[0], ("dict",), f"{this.cls.name}.update", 1, n), n)
    return save(i, this, n)


def row_query(i, this, n, fname):
    m = meta(i, this.cls, n)
    st = mstate(this)
    q = new_query(i, model_db(i, m, n), qid(i, m.table, n, "table"))
    add_where(i, [m.pk, st.original.get(m.pk, st.attrs.get(m.pk))], n, q, "AND", fname)
    return q


def soft_write(i, this, value, n):
    """Set deleted_at (and updated_at) on this one row, without the save events."""
    m = meta(i, this.cls, n)
    st = mstate(this)
    values = {"deleted_at": value}
    if m.timestamps and "updated_at" in table_columns(i, model_db(i, m, n), m.table, n):
        values["updated_at"] = int(time.time())
    QUERY_METHODS["update"][0](i, [values], n, row_query(i, this, n, "delete"))
    st.attrs.update(values)
    st.original.update(values)


def m_delete(i, a, n, this, force=False):
    """deleting -> DELETE (or set deleted_at) -> deleted. Returns false when nothing was deleted."""
    st = mstate(this)
    if not st.exists:
        return False
    m = meta(i, this.cls, n)
    soft = m.soft and not force
    if force and m.soft and not event(i, this, "forceDeleting", n):
        return False
    if not event(i, this, "deleting", n):
        return False
    if soft:
        soft_write(i, this, int(time.time()), n)
    else:
        QUERY_METHODS["delete"][0](i, [], n, row_query(i, this, n, "delete"))
        st.exists = False
        if WRITE_HOOKS:
            write_hooks(i, this, "deleted", n)
    event(i, this, "deleted", n)
    if force and m.soft:
        event(i, this, "forceDeleted", n)
    return True


def m_force_delete(i, a, n, this):
    return m_delete(i, a, n, this, force=True)


def m_restore(i, a, n, this):
    need_soft(i, this.cls, n, "restore")
    st = mstate(this)
    if not st.exists or st.attrs.get("deleted_at") is None:
        return False
    if not event(i, this, "restoring", n):
        return False
    soft_write(i, this, None, n)
    event(i, this, "restored", n)
    return True


def m_trashed(i, a, n, this):
    return meta(i, this.cls, n).soft and mstate(this).attrs.get("deleted_at") is not None


def m_refresh(i, a, n, this):
    st = mstate(this)
    m = meta(i, this.cls, n)
    if not st.exists:
        i.throw("DbException", f"cannot refresh a {this.cls.name} that has not been saved", n)
    q = new_query(i, model_db(i, m, n), qid(i, m.table, n, "table"))
    add_where(i, [m.pk, st.attrs.get(m.pk)], n, q, "AND", "refresh")
    row = qb_first(i, [], n, q)
    if row is None:
        not_found(i, this.cls, n, st.attrs.get(m.pk))
    st.attrs = cast_in(i, m, dict(row))
    st.original = dict(st.attrs)
    st.relations = {}
    return this


def m_fresh(i, a, n, this):
    st = mstate(this)
    m = meta(i, this.cls, n)
    if not st.exists:
        return None
    q = new_mq(i, this.cls, n)
    mq(q).trashed = "with"
    return mq_find(i, [st.attrs.get(m.pk)], n, q)


def to_plain(i, v, n):
    """Models inside a value -> their toArray() dicts."""
    if isinstance(v, BarqObject) and v.cls.find_method("toArray") is not None and not v.cls.is_enum:
        return i.call_function(BoundMethod(v, v.cls.find_method("toArray")), [], n)
    if isinstance(v, list):
        return [to_plain(i, x, n) for x in v]
    return v


def m_to_array(i, a, n, this):
    m = meta(i, this.cls, n)
    st = mstate(this)
    out = {k: v for k, v in st.attrs.items() if k not in m.hidden}
    for name, value in st.relations.items():
        if name not in m.hidden:
            out[name] = to_plain(i, value, n)
    return out


def m_to_dict(i, a, n, this):
    return to_plain(i, this, n)


def m_load(i, a, n, this):
    load_relations(i, [this], relation_names(i, a, n, "load"), n)
    return this


def m_is_dirty(i, a, n, this):
    d = dirty(mstate(this))
    if a:
        return need(i, a[0], ("string",), "isDirty", 1, n) in d
    return bool(d)


def m_original(i, a, n, this):
    st = mstate(this)
    if a:
        return st.original.get(need(i, a[0], ("string",), "getOriginal", 1, n))
    return dict(st.original)


def m_only(i, a, n, this):
    keys = a[0] if len(a) == 1 and isinstance(a[0], list) else a
    attrs = mstate(this).attrs
    return {k: attrs.get(k) for k in keys}


def m_is(i, a, n, this):
    other = a[0]
    if not is_model(i, other) or other.cls is not this.cls:
        return False
    m = meta(i, this.cls, n)
    k = mstate(this).attrs.get(m.pk)
    return k is not None and k == mstate(other).attrs.get(m.pk)


def relation_builder(kind, lo, hi):
    return (lambda i, a, n, this: relation_of(i, this, kind, a, n)), lo, hi


MODEL_METHODS = {
    "__get": (m_get, 1, 1),
    "__set": (m_set, 2, 2),
    "getAttribute": (lambda i, a, n, this: m_get(i, [need(i, a[0], ("string",), f"{this.cls.name}.getAttribute", 1, n)],
                                                 n, this), 1, 1),
    "setAttribute": (lambda i, a, n, this: m_set(i, [need(i, a[0], ("string",), f"{this.cls.name}.setAttribute", 1, n),
                                                     a[1]], n, this), 2, 2),
    "save": (m_save, 0, 0),
    "update": (m_update, 1, 1),
    "fill": (lambda i, a, n, this: fill(i, this, need(i, a[0], ("dict",), "fill", 1, n), n), 1, 1),
    "delete": (m_delete, 0, 0),
    "forceDelete": (m_force_delete, 0, 0),
    "restore": (m_restore, 0, 0),
    "trashed": (m_trashed, 0, 0),
    "refresh": (m_refresh, 0, 0),
    "fresh": (m_fresh, 0, 0),
    "toArray": (m_to_array, 0, 0),
    "toDict": (m_to_dict, 0, 0),
    "load": (m_load, 1, -1),
    "getKey": (lambda i, a, n, this: mstate(this).attrs.get(meta(i, this.cls, n).pk), 0, 0),
    "exists": (lambda i, a, n, this: mstate(this).exists, 0, 0),
    "wasRecentlyCreated": (lambda i, a, n, this: mstate(this).recent, 0, 0),
    "isDirty": (m_is_dirty, 0, 1),
    "getDirty": (lambda i, a, n, this: dirty(mstate(this)), 0, 0),
    "getOriginal": (m_original, 0, 1),
    "getAttributes": (lambda i, a, n, this: dict(mstate(this).attrs), 0, 0),
    "only": (m_only, 1, -1),
    "is": (m_is, 1, 1),
    "hasMany": relation_builder("HasMany", 1, 3),
    "hasOne": relation_builder("HasOne", 1, 3),
    "belongsTo": relation_builder("BelongsTo", 1, 3),
    "belongsToMany": relation_builder("BelongsToMany", 1, 4),
}


# ===========================================================================
# Model — static side (late static: `this` is the class it was called on)
# ===========================================================================

def query_static(name):
    fn, lo, hi = MQ_METHODS[name]

    def f(i, a, n, cls):
        return fn(i, a, n, new_mq(i, need_model_class(i, cls, n, name), n))
    return f, lo, hi


def s_query(i, a, n, cls):
    return new_mq(i, need_model_class(i, cls, n, "query"), n)


def s_all(i, a, n, cls):
    return mq_get(i, [], n, s_query(i, a, n, cls))


def s_create(i, a, n, cls):
    need_model_class(i, cls, n, "create")
    model = new_model(i, cls, need(i, a[0], ("dict",), f"{cls.name}.create", 1, n), n)
    save(i, model, n)
    return model


def match_query(i, cls, match, n, fname):
    q = new_mq(i, need_model_class(i, cls, n, fname), n)
    need(i, match, ("dict",), f"{cls.name}.{fname}", 1, n)
    for k, v in match.items():
        add_where(i, [k, v], n, mq(q).q, "AND", fname)
    return q


def s_first_or_new(i, a, n, cls):
    found = mq_first(i, [], n, match_query(i, cls, a[0], n, "firstOrNew"))
    if found is not None:
        return found
    extra = arg(i, a, 1, ("dict",), f"{cls.name}.firstOrNew", n, {})
    return new_model(i, cls, {**a[0], **extra}, n)


def s_first_or_create(i, a, n, cls):
    model = s_first_or_new(i, a, n, cls)
    if not mstate(model).exists:
        save(i, model, n)
    return model


def s_update_or_create(i, a, n, cls):
    values = need(i, a[1], ("dict",), f"{cls.name}.updateOrCreate", 2, n)
    model = mq_first(i, [], n, match_query(i, cls, a[0], n, "updateOrCreate"))
    if model is None:
        model = new_model(i, cls, {**a[0], **values}, n)
    else:
        fill(i, model, values, n)
    save(i, model, n)
    return model


def s_destroy(i, a, n, cls):
    m = meta(i, need_model_class(i, cls, n, "destroy"), n)
    ids = a[0] if len(a) == 1 and isinstance(a[0], list) else a
    count = 0
    for model in mq_find(i, [list(ids)], n, new_mq(i, cls, n)):
        if m_delete(i, [], n, model):
            count += 1
    return count


def s_use_connection(i, a, n, cls):
    db = a[0]
    if not isinstance(db, BarqObject) or db.cls.name != "Database":
        i.throw("TypeException", f"Model.useConnection() needs a Database, got {type_name(db)}", n)
    if cls is model_class(i):
        orm(i)["default"] = db
    else:
        name = meta(i, cls, n).connection
        if name:
            orm(i)["named"][name] = db
        else:
            orm(i)["default"] = db
    return None


def s_connection(i, a, n, cls):
    if cls is model_class(i):
        return default_db(i, n)
    return model_db(i, meta(i, cls, n), n)


MODEL_STATICS = {name: query_static(name) for name in (
    "where", "orWhere", "whereIn", "whereNotIn", "whereNull", "whereNotNull", "whereBetween", "orderBy",
    "orderByDesc", "latest", "oldest", "limit", "take", "select", "with", "first", "firstOrFail", "find",
    "findOrFail", "count", "sum", "avg", "min", "max", "pluck", "paginate", "withTrashed", "onlyTrashed")}
MODEL_STATICS.update({
    "query": (s_query, 0, 0),
    "all": (s_all, 0, 0),
    "create": (s_create, 1, 1),
    "make": (lambda i, a, n, cls: new_model(i, need_model_class(i, cls, n, "make"),
                                           need(i, a[0] if a else {}, ("dict",), "make", 1, n), n), 0, 1),
    "firstOrNew": (s_first_or_new, 1, 2),
    "firstOrCreate": (s_first_or_create, 1, 2),
    "updateOrCreate": (s_update_or_create, 2, 2),
    "destroy": (s_destroy, 1, -1),
    "tableName": (lambda i, a, n, cls: meta(i, need_model_class(i, cls, n, "tableName"), n).table, 0, 0),
    "useConnection": (s_use_connection, 1, 1),
    "connection": (s_connection, 0, 0),
})


def model_static_fallback(i, cls, name, n):
    """User.active() for a scope method scopeActive(ModelQuery q)."""
    if cls is model_class(i):
        return None
    m = find_scope(cls, name)
    if m is None:
        return None
    return scope_caller(i, cls, m, lambda: new_mq(i, cls, n), name)


# ===========================================================================
# Resource
# ===========================================================================

def res_construct(i, a, n, this):
    this.native = a[0]
    return None


def res_get(i, a, n, this):
    target = this.native
    name = a[0]
    if isinstance(target, dict):
        if name in target:
            return target[name]
        i.throw("NameException", f'{this.cls.name}: the resource has no key "{name}"', n)
    if target is None:
        i.throw("NullException", f"{this.cls.name} wraps null — cannot read '{name}'", n)
    from barq.vm import CtxEnv
    return i.get_attr(target, name, n, CtxEnv(None, None))


def res_to_array(i, a, n, this):
    target = this.native
    return to_plain(i, target, n)


def res_collection(i, a, n, cls):
    items = a[0]
    if isinstance(items, dict) and isinstance(items.get("data"), list):
        out = dict(items)
        out["data"] = res_collection(i, [items["data"]], n, cls)
        return out
    need(i, items, ("list",), f"{cls.name}.collection", 1, n)
    return [to_plain(i, i.instantiate(cls, [x], n), n) for x in items]


RESOURCE_METHODS = {
    "__get": (res_get, 1, 1),
    "toArray": (res_to_array, 0, 0),
    "resource": (lambda i, a, n, this: this.native, 0, 0),
}


def response_status(i, obj):
    """201 for a model created during this request (or a Resource wrapping one)."""
    target = obj.native if isinstance(obj, BarqObject) and obj.cls.is_subclass_of(i.classes["Resource"]) else obj
    if is_model(i, target) and mstate(target).recent:
        return 201
    return 200


# ===========================================================================
# FormRequest
# ===========================================================================

def call_method(i, obj, name, default, n):
    m = obj.cls.find_method(name)
    if m is None or isinstance(m, NativeFunction) and m.owner is i.classes["FormRequest"]:
        return default
    return i.call_function(BoundMethod(obj, m), [], n)


def make_form_request(i, cls, req, n):
    """Build the FormRequest subclass for a handler, authorize it, validate it."""
    from barq.stdlib2 import req_all, run_validation
    from barq.web import HttpResponseSignal, error_response, start_session, wants_json, s_flash
    from barq.stdlib2 import header_lookup, resp_redirect
    obj = i.instantiate_bare(cls)
    obj.fields.update(req.fields)
    obj.native = req.native
    allowed = call_method(i, obj, "authorize", True, n)
    if allowed is not True and allowed is not False:
        i.throw("TypeException", f"{cls.name}.authorize() must return bool, got {type_name(allowed)}", n)
    if not allowed:
        raise HttpResponseSignal(error_response(i, req, 403, "This action is unauthorized.", n))
    rules = call_method(i, obj, "rules", {}, n)
    messages = call_method(i, obj, "messages", {}, n)
    if not isinstance(rules, dict) or not isinstance(messages, dict):
        i.throw("TypeException", f"{cls.name}.rules() and messages() must return dicts", n)
    data = req_all(i, [], n, obj)
    errors, validated = run_validation(i, data, rules, messages, n)
    if errors:
        from barq.web import validation_failed
        validation_failed(i, req, data, errors, n)
    obj.fields["__validated"] = validated
    return obj


def fr_validated(i, a, n, this):
    data = this.fields.get("__validated", {})
    if a:
        return data.get(need(i, a[0], ("string",), "validated", 1, n), a[1] if len(a) > 1 else None)
    return dict(data)


FORM_REQUEST_METHODS = {
    "rules": (lambda i, a, n, this: {}, 0, 0),
    "messages": (lambda i, a, n, this: {}, 0, 0),
    "authorize": (lambda i, a, n, this: True, 0, 0),
    "validated": (fr_validated, 0, 2),
    "safe": (fr_validated, 0, 2),
}


# ===========================================================================
# validation rules that need the database: unique, exists
# ===========================================================================

def db_rule(i, rule, args, value, field, n):
    """unique:table,column?,ignoreValue?,ignoreColumn?   exists:table,column?"""
    db = default_db(i, n)
    table = args[0]
    column = args[1] if len(args) > 1 and args[1] else field
    q = new_query(i, db, qid(i, table, n, "table"))
    add_where(i, [column, value], n, q, "AND", rule)
    if rule == "unique" and len(args) > 2 and args[2] not in ("", "null", "NULL"):
        ignore_col = args[3] if len(args) > 3 and args[3] else "id"
        ignore = int(args[2]) if re.match(r"^-?\d+$", args[2]) else args[2]
        add_where(i, [ignore_col, "!=", ignore], n, q, "AND", rule)
    found = QUERY_METHODS["exists"][0](i, [], n, q)
    return not found if rule == "unique" else found


# ===========================================================================
# Migrations and seeders (used by `barq migrate`, `barq db:seed`)
# ===========================================================================

def seeder_call(i, a, n, this):
    targets = a[0] if isinstance(a[0], list) else [a[0]]
    for cls in targets:
        if not isinstance(cls, BarqClass) or not cls.is_subclass_of(i.classes["Seeder"]):
            i.throw("TypeException", f"Seeder.call() needs Seeder classes, got {type_name(cls)}", n)
        run_seeder(i, cls, n)
    return None


def run_seeder(i, cls, n=None):
    obj = i.instantiate(cls, [], n)
    m = cls.find_method("run")
    if m is None or isinstance(m, NativeFunction):
        i.throw("TypeException", f"seeder {cls.name} needs a run() method", n)
    out = getattr(i, "_seed_log", None)
    if out is not None:
        out(cls.name)
    i.call_function(BoundMethod(obj, m), [], n)


def ensure_migrations_table(i, db, n=None):
    st = db_state(i, db, n)
    db_run(i, st, 'CREATE TABLE IF NOT EXISTS "migrations" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, '
                  '"migration" TEXT NOT NULL UNIQUE, "batch" INTEGER NOT NULL)', [], n)


def ran_migrations(i, db, n=None):
    ensure_migrations_table(i, db, n)
    rows = db_rows(db_run(i, db_state(i, db, n), 'SELECT "migration", "batch" FROM "migrations" ORDER BY "id"', [], n))
    return {r["migration"]: r["batch"] for r in rows}


def migration_files(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.endswith(".bq"))


def load_migration(i, path, n=None):
    """Load a migration file and return an instance of its Migration class."""
    from barq.runtime import Module  # noqa: F401  (Module objects come from load_module)
    before = set(i.classes)
    i.load_module(path)
    mig = i.classes["Migration"]
    found = [c for name, c in i.classes.items() if name not in before and c.is_subclass_of(mig) and c is not mig]
    if not found:
        mod = i.modules.get(os.path.abspath(path))
        found = [c for c in (mod.classes.values() if mod else []) if c.is_subclass_of(mig)]
    if len(found) != 1:
        i.throw("TypeException", f"{os.path.basename(path)} must declare exactly one class that extends Migration "
                f"(found {len(found)})", n)
    return i.instantiate(found[0], [], n)


def run_migration(i, db, obj, direction, n=None):
    m = obj.cls.find_method(direction)
    if m is None or isinstance(m, NativeFunction):
        if direction == "down":
            return False
        i.throw("TypeException", f"migration {obj.cls.name} needs an up(Schema schema) method", n)
    schema = new_native(i, "Schema", db)
    from barq.stdlib2 import tx_begin, tx_end
    st = db_state(i, db, n)
    tx_begin(i, st, n)
    try:
        i.call_function(BoundMethod(obj, m), [schema], n)
    except BaseException:
        if st.conn is not None and st.depth > 0:
            tx_end(i, st, n, False)
        raise
    tx_end(i, st, n, True)
    return True


def bind_model(i, cls, raw, n):
    """Route model binding: a handler parameter `User user` gets User {user}, or a 404."""
    if raw is None:
        return None
    key = int(raw) if re.match(r"^-?\d+$", raw) else raw
    found = mq_find(i, [key], n, new_mq(i, cls, n))
    if found is None:
        not_found(i, cls, n, key)
    return found


# ===========================================================================
# hooks used by the interpreter (get_attr / to_response / json)
# ===========================================================================

def property_access(i, obj, m, n):
    """obj.name found a method: relation methods read as their loaded value."""
    if is_relation_method(i, obj.cls, m):
        return True, relation_value(i, obj, m, n)
    return False, None


def dump_rows(i, obj):
    st = obj.native
    if isinstance(st, ModelState):
        return list(st.attrs.items()) + list(st.relations.items())
    return None


# ===========================================================================
# install
# ===========================================================================

def install(i):
    from barq.stdlib2 import STDLIB_ALIASES
    exc = i.classes["Exception"]
    for name, parent in (("ModelNotFoundException", "AbortException"), ("MassAssignmentException", "Exception")):
        cls = BarqClass(name, name, None, parent=i.classes[parent], native=True)
        if name == "ModelNotFoundException":
            cls.props["model"] = PropInfo("model", T("string"), "public", None, cls)
        i.classes[name] = cls
        i.builtins.declare(name, cls, None, const=True)
    assert exc

    model = native_class(i, "Model", None, MODEL_METHODS, construct=m_construct)
    model.is_abstract = True
    for name, (fn, lo, hi) in MODEL_STATICS.items():
        f = NativeFunction(f"Model.{name}", fn, lo, hi)
        f.late_static = True
        model.statics[name] = PropInfo(name, None, "public", None, model, is_static=True, const=True)
        model.static_values[name] = f
    model.static_fallback = model_static_fallback

    mq_cls = native_class(i, "ModelQuery", None, MQ_METHODS)
    rel = native_class(i, "Relation", None, RELATION_METHODS)
    rel.parent = mq_cls
    for kind in RELATION_KINDS:
        native_class(i, kind).parent = rel

    resource = native_class(i, "Resource", None, RESOURCE_METHODS, construct=res_construct)
    f = NativeFunction("Resource.collection", res_collection, 1, 1)
    f.late_static = True
    resource.statics["collection"] = PropInfo("collection", None, "public", None, resource, is_static=True, const=True)
    resource.static_values["collection"] = f

    fr = native_class(i, "FormRequest", None, FORM_REQUEST_METHODS)
    fr.parent = i.classes["Request"]

    native_class(i, "Migration", None, None, construct=lambda i2, a, n, this: None)
    native_class(i, "Seeder", None, {"call": (seeder_call, 1, 1)}, construct=lambda i2, a, n, this: None)

    STDLIB_ALIASES.update({
        "Db\\Model": "Model", "Db\\ModelQuery": "ModelQuery", "Db\\Relation": "Relation",
        "Db\\HasMany": "HasMany", "Db\\HasOne": "HasOne", "Db\\BelongsTo": "BelongsTo",
        "Db\\BelongsToMany": "BelongsToMany", "Db\\Migration": "Migration", "Db\\Seeder": "Seeder",
        "Http\\FormRequest": "FormRequest", "Http\\Resource": "Resource",
    })
