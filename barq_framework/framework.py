"""Framework layer, part 2 (v0.10.0): events, the service container,
service providers, fake data and model factories.

    Event            Event.listen("user.*", fn) / Event.fire("user.created", user) / Event.fake()
    Container        app().bind(), singleton(), instance(), make() — with constructor autowiring
    ServiceProvider  app/Providers/*.bq: register() for every provider, then boot()
    Fake             Fake.name(), Fake.email(), Fake.int(1, 9), Fake.unique().email(), Fake.seed(42)
    Factory          database/factories/UserFactory.bq: User.factory().count(3).create()

Model events and soft deletes live in orm.py; this file adds the static
registration functions (User.creating(fn), User.observe(Observer)).
"""

import os
import random
import re
import string
import time
import uuid as _uuid

from barq.builtins import need
from barq.runtime import BarqClass, BarqFunction, BarqObject, BoundMethod, NativeFunction, PropInfo, to_display, type_name
from barq.stdlib import arg, module
from barq.stdlib2 import native_class, new_native


def is_callable(v):
    return isinstance(v, (BarqFunction, NativeFunction, BoundMethod))


def arity(fn):
    """How many arguments a function takes (-1: any number)."""
    f = fn.func if isinstance(fn, BoundMethod) else fn
    if isinstance(f, BarqFunction):
        if f.params is None or (f.params and f.params[-1].variadic):
            return -1
        return len(f.params)
    if isinstance(f, NativeFunction):
        return f.max_args if f.max_args is not None else -1
    return -1


def class_key(i, key, n, fname):
    """Event and container keys: a string, or a class (its full name)."""
    if isinstance(key, str) and key:
        return key
    if isinstance(key, BarqClass):
        return key.fqn
    i.throw("TypeException", f"{fname}() needs a name (string) or a class, got {type_name(key)}", n)


# ===========================================================================
# Event
# ===========================================================================

class EventState:
    def __init__(self):
        self.listeners = {}     # name -> [[handler, once]]
        self.wildcards = []     # [[pattern, regex, handler, once]]
        self.fake = None        # None: off; True: every event; set: only these names
        self.fired = []         # (name, data) while faked


def events(i):
    st = i.__dict__.get("_events")
    if st is None:
        st = i._events = EventState()
    return st


def check_handler(i, h, n, fname):
    if is_callable(h):
        return h
    if isinstance(h, BarqClass):
        if h.find_method("handle") is None:
            i.throw("TypeException", f"{fname}(): listener class {h.name} needs a handle() method", n)
        return h
    i.throw("TypeException", f"{fname}() needs a function or a listener class, got {type_name(h)}", n)


def ev_listen(once):
    def fn(i, a, n):
        fname = "Event.once" if once else "Event.listen"
        names = a[0] if isinstance(a[0], list) else [a[0]]
        h = check_handler(i, a[1], n, fname)
        st = events(i)
        for name in names:
            key = class_key(i, name, n, fname)
            if "*" in key:
                rx = re.compile("^" + ".*".join(re.escape(p) for p in key.split("*")) + "$")
                st.wildcards.append([key, rx, h, once])
            else:
                st.listeners.setdefault(key, []).append([h, once])
        return None
    return fn


def call_listener(i, h, name, data, n):
    if isinstance(h, BarqClass):
        obj = container_make(i, h, n)
        h = BoundMethod(obj, obj.cls.find_method("handle"))
    if arity(h) >= 2:
        return i.call_function(h, [name, data], n)
    if arity(h) == 0:
        return i.call_function(h, [], n)
    return i.call_function(h, [data], n)


def event_target(i, a, n):
    """fire("name", data) or fire(eventObject): (keys to look up, display name, data)."""
    first = a[0]
    if isinstance(first, BarqObject) and len(a) == 1:
        keys = [first.cls.fqn] + ([first.cls.name] if first.cls.name != first.cls.fqn else [])
        return keys, first.cls.fqn, first
    name = need(i, first, ("string",), "Event.fire", 1, n)
    if not name:
        i.throw("ValueException", "Event.fire(): the event name is empty", n)
    return [name], name, a[1] if len(a) > 1 else None


def is_faked(st, keys):
    if st.fake is None:
        return False
    return st.fake is True or any(k in st.fake for k in keys)


def ev_fire(i, a, n):
    keys, name, data = event_target(i, a, n)
    st = events(i)
    if is_faked(st, keys):
        st.fired.append((name, data))
        return []
    results = []
    todo = []
    for key in keys:
        todo.extend((st.listeners, key, entry) for entry in list(st.listeners.get(key, [])))
    todo.extend((None, None, entry) for entry in list(st.wildcards) if entry[1].match(name))
    for table, key, entry in todo:
        h, once = (entry[0], entry[1]) if table is not None else (entry[2], entry[3])
        if once:
            if table is not None:
                if entry in table.get(key, []):
                    table[key].remove(entry)
            elif entry in st.wildcards:
                st.wildcards.remove(entry)
        result = call_listener(i, h, name, data, n)
        if result is False:
            break           # a listener returning false stops the others
        if result is not None:
            results.append(result)
    return results


def ev_forget(i, a, n):
    key = class_key(i, a[0], n, "Event.forget")
    st = events(i)
    st.listeners.pop(key, None)
    st.wildcards = [w for w in st.wildcards if w[0] != key]
    return None


def ev_has(i, a, n):
    key = class_key(i, a[0], n, "Event.has")
    st = events(i)
    return bool(st.listeners.get(key)) or any(w[1].match(key) for w in st.wildcards)


def ev_fake(i, a, n):
    st = events(i)
    if a:
        names = a[0] if isinstance(a[0], list) else list(a)
        st.fake = {class_key(i, x, n, "Event.fake") for x in names}
    else:
        st.fake = True
    st.fired = []
    return None


def fired_matching(i, key):
    return [d for name, d in events(i).fired if name == key or name.rsplit("\\", 1)[-1] == key]


def ev_fired(i, a, n):
    if not a:
        return [name for name, _ in events(i).fired]
    return fired_matching(i, class_key(i, a[0], n, "Event.fired"))


def need_fake(i, n, fname):
    if events(i).fake is None:
        i.throw("TypeException", f"{fname}() needs Event.fake() first", n)


def ev_assert_fired(i, a, n):
    from barq.stdlib3 import fail
    need_fake(i, n, "Event.assertFired")
    key = class_key(i, a[0], n, "Event.assertFired")
    got = fired_matching(i, key)
    if len(a) > 1 and a[1] is not None:
        check = a[1]
        if is_callable(check):
            if not any(i.call_function(check, [d], n) is True for d in got):
                fail(i, f'event "{key}" was fired {len(got)} time(s), but never with matching data', n)
            return None
        times = need(i, check, ("int",), "Event.assertFired", 2, n)
        if len(got) != times:
            fail(i, f'expected event "{key}" to be fired {times} time(s), it was fired {len(got)} time(s)', n)
        return None
    if not got:
        fired = ", ".join(dict.fromkeys(name for name, _ in events(i).fired)) or "none"
        fail(i, f'expected event "{key}" to be fired, it was not (fired: {fired})', n)
    return None


def ev_assert_not_fired(i, a, n):
    from barq.stdlib3 import fail
    need_fake(i, n, "Event.assertNotFired")
    key = class_key(i, a[0], n, "Event.assertNotFired")
    got = fired_matching(i, key)
    if got:
        fail(i, f'expected event "{key}" not to be fired, it was fired {len(got)} time(s)', n)
    return None


def ev_assert_nothing(i, a, n):
    from barq.stdlib3 import fail
    need_fake(i, n, "Event.assertNothingFired")
    if events(i).fired:
        fail(i, "expected no events, got: " + ", ".join(name for name, _ in events(i).fired), n)
    return None


EVENT = {
    "listen": (ev_listen(False), 2, 2),
    "once": (ev_listen(True), 2, 2),
    "fire": (ev_fire, 1, 2),
    "dispatch": (ev_fire, 1, 2),
    "forget": (ev_forget, 1, 1),
    "has": (ev_has, 1, 1),
    "fake": (ev_fake, 0, -1),
    "fired": (ev_fired, 0, 1),
    "assertFired": (ev_assert_fired, 1, 2),
    "assertNotFired": (ev_assert_not_fired, 1, 1),
    "assertNothingFired": (ev_assert_nothing, 0, 0),
}


# ===========================================================================
# Container
# ===========================================================================

class ContainerState:
    def __init__(self):
        self.bindings = {}      # key -> (concrete, shared)
        self.instances = {}     # key -> value
        self.building = []      # keys being built (cycle check)
        self.providers = []     # provider objects
        self.booted = False


def container(i):
    obj = i.__dict__.get("_container")
    if obj is None:
        obj = i._container = new_native(i, "Container", ContainerState())
    return obj


def cstate(i):
    return container(i).native


def c_bind(shared):
    def fn(i, a, n, this):
        fname = "singleton" if shared else "bind"
        key = class_key(i, a[0], n, fname)
        concrete = a[1] if len(a) > 1 else None
        if concrete is None:
            if not isinstance(a[0], BarqClass):
                i.throw("TypeException", f'{fname}("{key}") needs a function or a class as argument 2', n)
            concrete = a[0]
        elif not is_callable(concrete) and not isinstance(concrete, BarqClass):
            i.throw("TypeException", f"{fname}() argument 2 must be a function or a class, got {type_name(concrete)}", n)
        st = this.native
        st.instances.pop(key, None)
        st.bindings[key] = (concrete, shared)
        return None
    return fn


def c_instance(i, a, n, this):
    key = class_key(i, a[0], n, "instance")
    this.native.instances[key] = a[1]
    return a[1]


def c_has(i, a, n, this):
    key = class_key(i, a[0], n, "has")
    st = this.native
    return key in st.instances or key in st.bindings


def c_make(i, a, n, this):
    return container_make(i, a[0], n)


def c_forget(i, a, n, this):
    key = class_key(i, a[0], n, "forget")
    this.native.instances.pop(key, None)
    this.native.bindings.pop(key, None)
    return None


def container_make(i, what, n):
    key = class_key(i, what, n, "make")
    st = cstate(i)
    if key in st.instances:
        return st.instances[key]
    if key in st.building:
        i.throw("ContainerException", "circular dependency: " + " -> ".join(st.building + [key]), n)
    binding = st.bindings.get(key)
    st.building.append(key)
    try:
        if binding is None:
            if not isinstance(what, BarqClass):
                i.throw("ContainerException", f'nothing is bound to "{key}" — bind it in a service provider '
                        f'with app().bind("{key}", fn)', n)
            value = autowire(i, what, n)
        else:
            concrete, shared = binding
            if isinstance(concrete, BarqClass):
                value = autowire(i, concrete, n)
            else:
                value = i.call_function(concrete, [container(i)] if arity(concrete) != 0 else [], n)
            if shared:
                st.instances[key] = value
    finally:
        st.building.pop()
    return value


BUILTIN_TYPES = {"any", "string", "int", "float", "bool", "list", "dict", "set", "null", "void", "function"}


def autowire(i, cls, n):
    """new Cls(...) with each constructor parameter made by the container (by its type)."""
    if cls.is_abstract or getattr(cls, "is_interface", False):
        i.throw("ContainerException", f"cannot create {cls.name}: it is abstract — bind a class to it with "
                f"app().bind({cls.name}, SomeClass)", n)
    if cls.is_enum:
        i.throw("ContainerException", f"cannot create enum {cls.name} with the container", n)
    if cls is container_class(i):
        return container(i)
    ctor = cls.find_method("__construct")
    params = getattr(ctor, "params", None) if isinstance(ctor, BarqFunction) else None
    if not params:
        return i.instantiate(cls, [], n)
    args = []
    kwargs = None       # after a parameter left to its default, the rest go by name
    for p in params:
        if p.variadic:
            break
        t = p.type
        dep = None if t is None or t.name in BUILTIN_TYPES else param_class(i, t, ctor)
        if p.default is not None and (dep is None or not resolvable_dep(i, dep)):
            kwargs = kwargs if kwargs is not None else {}
            continue        # `int retries = 3`: keep the default
        if t is None or t.name in BUILTIN_TYPES:
            i.throw("ContainerException", f"cannot create {cls.name}: constructor parameter '{p.name}' "
                    f"({t.name if t else 'untyped'}) is not a class — bind {cls.name} with app().bind({cls.name}, fn)", n)
        if dep is None:
            i.throw("ContainerException", f"cannot create {cls.name}: unknown type {t.name} for parameter '{p.name}'", n)
        if kwargs is None:
            args.append(container_make(i, dep, n))
        else:
            kwargs[p.name] = container_make(i, dep, n)
    return i.instantiate(cls, args, n, None, kwargs or None)


def resolvable_dep(i, cls):
    """Can the container build this class (bound, or a concrete class)?"""
    st = cstate(i)
    return cls.fqn in st.instances or cls.fqn in st.bindings or not (cls.is_abstract or cls.is_enum)


def param_class(i, t, fn):
    from barq.web import param_class as pc
    owner = fn.owner if fn is not None else None
    module = owner.module if owner is not None else getattr(fn, "module", None)
    return pc(i, t, module)


def container_class(i):
    return i.classes["Container"]


def resolvable(i, cls):
    """Can a handler parameter of this class type come from the container?"""
    st = cstate(i)
    if cls.fqn in st.instances or cls.fqn in st.bindings:
        return True
    return not cls.native and not cls.is_enum and not cls.is_abstract and \
        not cls.is_subclass_of(i.classes["Model"])


def c_call(i, a, n, this):
    """app().call(fn): call a function, making each class-typed parameter."""
    fn = a[0]
    if not is_callable(fn):
        i.throw("TypeException", f"call() needs a function, got {type_name(fn)}", n)
    f = fn.func if isinstance(fn, BoundMethod) else fn
    extra = list(a[1:])
    args = []
    kwargs = None
    for p in (f.params or []) if isinstance(f, BarqFunction) else []:
        if p.variadic:
            args.extend(extra)
            extra = []
            break
        cls = param_class(i, p.type, f) if p.type is not None and p.type.name not in BUILTIN_TYPES else None
        if cls is not None:
            value = container_make(i, cls, n)
        elif extra:
            value = extra.pop(0)
        elif p.default is not None:
            kwargs = kwargs if kwargs is not None else {}
            continue
        else:
            i.throw("ContainerException", f"call(): no value for parameter '{p.name}'", n)
        if kwargs is None:
            args.append(value)
        else:
            kwargs[p.name] = value
    return i.call_function(fn, args, n, kwargs or None)


CONTAINER_METHODS = {
    "bind": (c_bind(False), 1, 2),
    "singleton": (c_bind(True), 1, 2),
    "instance": (c_instance, 2, 2),
    "make": (c_make, 1, 1),
    "has": (c_has, 1, 1),
    "bound": (c_has, 1, 1),
    "forget": (c_forget, 1, 1),
    "call": (c_call, 1, -1),
    "boot": (lambda i, a, n, this: boot_providers(i, n), 0, 0),
    "isBooted": (lambda i, a, n, this: this.native.booted, 0, 0),
    "providers": (lambda i, a, n, this: [p.cls for p in this.native.providers], 0, 0),
    "register": (lambda i, a, n, this: register_provider(i, a[0], n), 1, 1),
}


def g_app(i, a, n):
    if a:
        return container_make(i, a[0], n)
    return container(i)


# ===========================================================================
# ServiceProvider
# ===========================================================================

PROVIDERS = os.path.join("app", "Providers")


def sp_construct(i, a, n, this):
    this.fields["app"] = container(i)
    return None


def provider_classes(i, n=None):
    """config app.providers (classes or names), else every provider in app/Providers/."""
    from barq.stdlib2 import config_lookup
    base = i.classes["ServiceProvider"]
    listed = config_lookup(i, "app.providers", n)
    if listed is not None:
        if not isinstance(listed, list):
            i.throw("TypeException", f"config app.providers must be a list, got {type_name(listed)}", n)
        out = []
        for x in listed:
            cls = x if isinstance(x, BarqClass) else i.lookup_fqn(x, n, i.module) if isinstance(x, str) else None
            if not isinstance(cls, BarqClass) or not cls.is_subclass_of(base):
                i.throw("TypeException", f"config app.providers: {to_display(x, i, True)} is not a ServiceProvider class", n)
            out.append(cls)
        return out
    folder = os.path.join(i.root, PROVIDERS)
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".bq"):
            continue
        mod = i.load_module(os.path.join(folder, name))
        found = [c for c in mod.classes.values() if c.is_subclass_of(base) and c is not base and not c.is_abstract] \
            if getattr(mod, "classes", None) else \
            [c for c in i.classes.values() if c.module is mod and c.is_subclass_of(base) and c is not base]
        out.extend(found)
    return out


def has_providers(root):
    from barq.stdlib2 import ConfigState  # noqa: F401
    return os.path.isdir(os.path.join(root, PROVIDERS))


def register_provider(i, cls, n):
    base = i.classes["ServiceProvider"]
    if not isinstance(cls, BarqClass) or not cls.is_subclass_of(base) or cls is base:
        i.throw("TypeException", f"register() needs a ServiceProvider class, got {type_name(cls)}", n)
    st = cstate(i)
    if any(p.cls is cls for p in st.providers):
        return None
    obj = i.instantiate(cls, [], n)
    obj.fields["app"] = container(i)
    st.providers.append(obj)
    run_hook(i, obj, "register", n)
    if st.booted:
        run_hook(i, obj, "boot", n)
    return obj


def run_hook(i, obj, name, n):
    m = obj.cls.find_method(name)
    if m is not None and not isinstance(m, NativeFunction):
        i.call_function(BoundMethod(obj, m), [], n)


def boot_providers(i, n=None):
    """register() on every provider, then boot() on every provider. Runs once."""
    st = cstate(i)
    if st.booted:
        return None
    for cls in provider_classes(i, n):
        if any(p.cls is cls for p in st.providers):
            continue
        obj = i.instantiate(cls, [], n)
        obj.fields["app"] = container(i)
        st.providers.append(obj)
        run_hook(i, obj, "register", n)
    st.booted = True
    for obj in list(st.providers):
        run_hook(i, obj, "boot", n)
    return None


# ===========================================================================
# Fake
# ===========================================================================

FIRST_NAMES = [
    "Ali", "Ayesha", "Bilal", "Fatima", "Hamza", "Hira", "Imran", "Zainab", "Usman", "Sana", "Omar", "Mariam",
    "Ahmed", "Noor", "Hassan", "Amna", "Saad", "Iqra", "Daniyal", "Mehwish", "Emma", "Liam", "Olivia", "Noah",
    "Sofia", "Lucas", "Mia", "Leo", "Chloe", "Ethan", "Yuki", "Kenji", "Mei", "Wei", "Priya", "Arjun", "Ananya",
    "Rohan", "Amara", "Kwame", "Zara", "Ivan", "Elena", "Mateo", "Lucia", "Nadia", "Tariq", "Layla", "Yusuf", "Sara",
]
LAST_NAMES = [
    "Khan", "Ahmed", "Malik", "Hussain", "Qureshi", "Siddiqui", "Sheikh", "Chaudhry", "Raza", "Butt", "Iqbal",
    "Mirza", "Abbasi", "Farooq", "Smith", "Johnson", "Garcia", "Martin", "Rossi", "Müller", "Tanaka", "Chen",
    "Patel", "Sharma", "Okafor", "Mensah", "Novak", "Silva", "Kowalski", "Haddad", "Nguyen", "Kim", "Lopez", "Brown",
]
WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore "
    "magna aliqua enim ad minim veniam quis nostrud exercitation ullamco laboris nisi aliquip ex ea commodo "
    "consequat duis aute irure in reprehenderit voluptate velit esse cillum fugiat nulla pariatur excepteur sint "
    "occaecat cupidatat non proident sunt culpa qui officia deserunt mollit anim id est laborum"
).split()
CITIES = ["Lahore", "Karachi", "Islamabad", "Peshawar", "Multan", "Quetta", "London", "Paris", "Berlin", "Madrid",
          "Rome", "Tokyo", "Seoul", "Dubai", "Istanbul", "Cairo", "Nairobi", "Toronto", "New York", "Sydney",
          "Mumbai", "Jakarta", "Lisbon", "Oslo"]
COUNTRIES = ["Pakistan", "United Kingdom", "France", "Germany", "Spain", "Italy", "Japan", "South Korea",
             "United Arab Emirates", "Turkey", "Egypt", "Kenya", "Canada", "United States", "Australia", "India",
             "Indonesia", "Portugal", "Norway", "Brazil"]
STREETS = ["Main", "Mall", "Canal", "Park", "Garden", "Station", "Church", "Mill", "Lake", "Hill", "Market", "Bridge"]
STREET_KINDS = ["Road", "Street", "Avenue", "Lane", "Boulevard", "Way"]
COMPANY_WORDS = ["Barq", "Nova", "Apex", "Indus", "Zenith", "Falcon", "Cedar", "Orbit", "Summit", "Pixel", "Atlas",
                 "Crescent", "Lumen", "Vertex", "Harbor", "Saffron"]
COMPANY_KINDS = ["Labs", "Systems", "Solutions", "Technologies", "Studio", "Works", "Group", "Partners", "Digital"]
DOMAINS = ["example.com", "example.org", "example.net"]
TLDS = ["com", "org", "net", "io", "dev", "pk"]
COLORS = ["red", "green", "blue", "yellow", "purple", "orange", "pink", "teal", "black", "white", "gray", "indigo"]
JOBS = ["Engineer", "Designer", "Teacher", "Doctor", "Accountant", "Writer", "Chef", "Pilot", "Nurse", "Architect",
        "Lawyer", "Photographer", "Farmer", "Pharmacist", "Translator"]


class FakeState:
    def __init__(self):
        self.rng = random.Random()
        self.seen = {}          # generator name -> set of values, for Fake.unique()


def fstate(i):
    st = i.__dict__.get("_fake")
    if st is None:
        st = i._fake = FakeState()
    return st


def rng(i):
    return fstate(i).rng


def pick(i, items):
    return items[rng(i).randrange(len(items))]


def ascii_slug(s):
    s = s.replace("ü", "u").replace("ö", "o").replace("ä", "a")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def f_int(i, a, n):
    """int() any; int(max) 0..max; int(min, max)."""
    first = arg(i, a, 0, ("int",), "Fake.int", n, 2147483647)
    lo, hi = (0, first) if len(a) < 2 else (first, arg(i, a, 1, ("int",), "Fake.int", n))
    if hi < lo:
        i.throw("ValueException", f"Fake.int({lo}, {hi}): max is smaller than min", n)
    return rng(i).randint(lo, hi)


def f_float(i, a, n):
    lo = arg(i, a, 0, ("int", "float"), "Fake.float", n, 0)
    hi = arg(i, a, 1, ("int", "float"), "Fake.float", n, 1)
    places = arg(i, a, 2, ("int",), "Fake.float", n, 2)
    if hi < lo:
        i.throw("ValueException", f"Fake.float({lo}, {hi}): max is smaller than min", n)
    return round(rng(i).uniform(lo, hi), places)


def f_bool(i, a, n):
    chance = arg(i, a, 0, ("int",), "Fake.bool", n, 50)
    return rng(i).randrange(100) < chance


def f_pick(i, a, n):
    items = a[0] if len(a) == 1 and isinstance(a[0], list) else list(a)
    if isinstance(items, dict):
        items = list(items.values())
    if not items:
        i.throw("ValueException", "Fake.pick() needs at least one value", n)
    return pick(i, items)


def f_pick_many(i, a, n):
    items = need(i, a[0], ("list",), "Fake.pickMany", 1, n)
    count = arg(i, a, 1, ("int",), "Fake.pickMany", n, 2)
    if count > len(items):
        i.throw("ValueException", f"Fake.pickMany(): asked for {count} of {len(items)} values", n)
    return rng(i).sample(list(items), count)


def f_shuffle(i, a, n):
    items = list(need(i, a[0], ("list",), "Fake.shuffle", 1, n))
    rng(i).shuffle(items)
    return items


def f_words(i, a, n):
    count = arg(i, a, 0, ("int",), "Fake.words", n, 3)
    return [pick(i, WORDS) for _ in range(count)]


def sentence(i, words):
    s = " ".join(pick(i, WORDS) for _ in range(words))
    return s[:1].upper() + s[1:] + "."


def f_sentence(i, a, n):
    words = arg(i, a, 0, ("int",), "Fake.sentence", n, None)
    return sentence(i, words if words is not None else rng(i).randint(4, 10))


def f_paragraph(i, a, n):
    count = arg(i, a, 0, ("int",), "Fake.paragraph", n, 3)
    return " ".join(sentence(i, rng(i).randint(4, 10)) for _ in range(count))


def f_text(i, a, n):
    limit = arg(i, a, 0, ("int",), "Fake.text", n, 200)
    if limit < 5:
        i.throw("ValueException", "Fake.text() needs a limit of at least 5 characters", n)
    out = ""
    while True:
        s = sentence(i, rng(i).randint(4, 10))
        if len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}" if out else s
    return out or sentence(i, 1)[:limit]


def f_email(i, a, n):
    first, last = pick(i, FIRST_NAMES), pick(i, LAST_NAMES)
    sep = pick(i, [".", "_", ""])
    return f"{ascii_slug(first)}{sep}{ascii_slug(last)}{rng(i).randint(1, 99)}@{pick(i, DOMAINS)}"


def f_username(i, a, n):
    return f"{ascii_slug(pick(i, FIRST_NAMES))}{pick(i, ['', '_', '.'])}{rng(i).randint(1, 999)}"


def f_phone(i, a, n):
    r = rng(i)
    return f"+{r.randint(1, 99)} {r.randint(300, 399)} {r.randint(1000000, 9999999)}"


def f_address(i, a, n):
    return f"{rng(i).randint(1, 250)} {pick(i, STREETS)} {pick(i, STREET_KINDS)}, {pick(i, CITIES)}"


def f_url(i, a, n):
    return f"https://{pick(i, WORDS)}{pick(i, WORDS)}.{pick(i, TLDS)}/{pick(i, WORDS)}"


def f_slug(i, a, n):
    words = arg(i, a, 0, ("int",), "Fake.slug", n, 3)
    return "-".join(pick(i, WORDS) for _ in range(words))


def f_uuid(i, a, n):
    return str(_uuid.UUID(int=rng(i).getrandbits(128), version=4))


def f_password(i, a, n):
    length = arg(i, a, 0, ("int",), "Fake.password", n, 12)
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(pick(i, chars) for _ in range(length))


def f_hex_color(i, a, n):
    return "#{:06x}".format(rng(i).getrandbits(24))


def f_timestamp(i, a, n):
    """A Unix time in the past year, or between two given times."""
    now = int(time.time())
    lo = arg(i, a, 0, ("int",), "Fake.timestamp", n, now - 365 * 86400)
    hi = arg(i, a, 1, ("int",), "Fake.timestamp", n, now)
    if hi < lo:
        i.throw("ValueException", "Fake.timestamp(): max is smaller than min", n)
    return rng(i).randint(lo, hi)


def f_date(i, a, n):
    import datetime as dt
    from barq.stdlib import php_format
    fmt = arg(i, a, 0, ("string",), "Fake.date", n, "Y-m-d")
    return php_format(dt.datetime.fromtimestamp(f_timestamp(i, list(a[1:]), n)), fmt)


def f_digits(i, a, n):
    count = arg(i, a, 0, ("int",), "Fake.digits", n, 6)
    return "".join(str(rng(i).randrange(10)) for _ in range(count))


def f_seed(i, a, n):
    v = arg(i, a, 0, ("int",), "Fake.seed", n, None)
    st = fstate(i)
    st.rng = random.Random(v)
    st.seen = {}
    return None


def simple(fn):
    return (lambda i, a, n: fn(i)), 0, 0


GENERATORS = {
    "name": simple(lambda i: f"{pick(i, FIRST_NAMES)} {pick(i, LAST_NAMES)}"),
    "firstName": simple(lambda i: pick(i, FIRST_NAMES)),
    "lastName": simple(lambda i: pick(i, LAST_NAMES)),
    "email": (f_email, 0, 0),
    "userName": (f_username, 0, 0),
    "phone": (f_phone, 0, 0),
    "word": simple(lambda i: pick(i, WORDS)),
    "words": (f_words, 0, 1),
    "sentence": (f_sentence, 0, 1),
    "paragraph": (f_paragraph, 0, 1),
    "text": (f_text, 0, 1),
    "slug": (f_slug, 0, 1),
    "title": simple(lambda i: " ".join(w.capitalize() for w in (pick(i, WORDS) for _ in range(rng(i).randint(2, 5))))),
    "int": (f_int, 0, 2),
    "float": (f_float, 0, 3),
    "bool": (f_bool, 0, 1),
    "digits": (f_digits, 0, 1),
    "pick": (f_pick, 1, -1),
    "pickMany": (f_pick_many, 1, 2),
    "shuffle": (f_shuffle, 1, 1),
    "city": simple(lambda i: pick(i, CITIES)),
    "country": simple(lambda i: pick(i, COUNTRIES)),
    "address": (f_address, 0, 0),
    "company": simple(lambda i: f"{pick(i, COMPANY_WORDS)} {pick(i, COMPANY_KINDS)}"),
    "jobTitle": simple(lambda i: pick(i, JOBS)),
    "url": (f_url, 0, 0),
    "domain": simple(lambda i: f"{pick(i, WORDS)}.{pick(i, TLDS)}"),
    "uuid": (f_uuid, 0, 0),
    "password": (f_password, 0, 1),
    "color": simple(lambda i: pick(i, COLORS)),
    "hexColor": (f_hex_color, 0, 0),
    "timestamp": (f_timestamp, 0, 2),
    "date": (f_date, 0, 3),
}


def unique_method(name, fn):
    def run(i, a, n, this):
        seen = fstate(i).seen.setdefault(name, set())
        for _ in range(1000):
            v = fn(i, a, n)
            key = repr(v)
            if key not in seen:
                seen.add(key)
                return v
        i.throw("ValueException", f"Fake.unique().{name}(): no new value after 1000 tries "
                "(call Fake.unique(true) to start over)", n)
    return run


def f_unique(i, a, n):
    reset = arg(i, a, 0, ("bool",), "Fake.unique", n, False)
    if reset:
        fstate(i).seen = {}
    return new_native(i, "FakeUnique")


FAKE = dict(GENERATORS)
FAKE.update({"seed": (f_seed, 0, 1), "unique": (f_unique, 0, 1)})


# ===========================================================================
# Factory
# ===========================================================================

class FactoryState:
    def __init__(self):
        self.model = None           # model class
        self.count = None
        self.states = []            # ("dict", d) | ("fn", f) | ("seq", [dicts])
        self.after_making = []
        self.after_creating = []
        self.children = []          # (factory object, relation name or None)

    def copy(self):
        c = FactoryState()
        c.model, c.count = self.model, self.count
        c.states = list(self.states)
        c.after_making = list(self.after_making)
        c.after_creating = list(self.after_creating)
        c.children = list(self.children)
        return c


def factory_class(i):
    return i.classes["Factory"]


def fac_state(this):
    st = this.native
    if st is None:
        st = this.native = FactoryState()
    return st


def fac_copy(i, this):
    obj = BarqObject(this.cls)
    obj.fields.update(this.fields)
    obj.native = fac_state(this).copy()
    return obj


def fac_construct(i, a, n, this):
    fac_state(this)
    return None


def factory_model(i, this, n):
    """The model a factory makes: set by User.factory(), its `model` property, or its name (UserFactory)."""
    st = fac_state(this)
    if st.model is not None:
        return st.model
    from .orm import model_class
    value = this.fields.get("model")
    cls = None
    if isinstance(value, BarqClass):
        cls = value
    elif isinstance(value, str) and value:
        cls = i.classes.get(value.lstrip("\\"))
        if cls is None:
            cls = i.lookup_fqn(value.lstrip("\\"), n, this.cls.module)
    else:
        name = this.cls.name[:-len("Factory")] if this.cls.name.endswith("Factory") else ""
        if name:
            cls = i.classes.get("App\\Models\\" + name) or next(
                (c for c in i.classes.values() if c.name == name and c.is_subclass_of(model_class(i))), None)
            if cls is None:
                cls = i.lookup_fqn("App\\Models\\" + name, n, this.cls.module, required=False)
        if cls is None:
            i.throw("NameException", f"{this.cls.name}: cannot tell which model it makes — name it after the model "
                    "(UserFactory makes User) or set protected string model = 'App\\Models\\User'", n)
    if not isinstance(cls, BarqClass) or not cls.is_subclass_of(model_class(i)):
        i.throw("TypeException", f"{this.cls.name}.model must be a model class, got {type_name(cls)}", n)
    st.model = cls
    return cls


def fac_chain(fn):
    def run(i, a, n, this):
        obj = fac_copy(i, this)
        fn(i, a, n, obj, fac_state(obj))
        return obj
    return run


def set_count(i, a, n, obj, st):
    c = need(i, a[0], ("int",), "Factory.count", 1, n)
    if c < 0:
        i.throw("ValueException", "Factory.count() needs 0 or more", n)
    st.count = c


def add_state(i, a, n, obj, st):
    v = a[0]
    if isinstance(v, dict):
        st.states.append(("dict", v))
    elif is_callable(v):
        st.states.append(("fn", v))
    else:
        i.throw("TypeException", f"Factory.state() needs a dict or a function, got {type_name(v)}", n)


def add_sequence(i, a, n, obj, st):
    items = a[0] if len(a) == 1 and isinstance(a[0], list) else list(a)
    if not items:
        i.throw("ValueException", "Factory.sequence() needs at least one value", n)
    for x in items:
        if not isinstance(x, dict) and not is_callable(x):
            i.throw("TypeException", f"Factory.sequence() takes dicts (or functions), got {type_name(x)}", n)
    st.states.append(("seq", items))


def add_callback(attr):
    def run(i, a, n, obj, st):
        if not is_callable(a[0]):
            i.throw("TypeException", f"{attr}() needs a function, got {type_name(a[0])}", n)
        getattr(st, "after_making" if attr == "afterMaking" else "after_creating").append(a[0])
    return run


def add_child(i, a, n, obj, st):
    child = a[0]
    if not isinstance(child, BarqObject) or not child.cls.is_subclass_of(factory_class(i)):
        i.throw("TypeException", f"Factory.has() needs another factory (such as Post.factory().count(3)), got {type_name(child)}", n)
    rel = arg(i, a, 1, ("string",), "Factory.has", n, None)
    st.children.append((child, rel))


def definition(i, this, n):
    m = this.cls.find_method("definition")
    if m is None or isinstance(m, NativeFunction):
        return {}
    d = i.call_function(BoundMethod(this, m), [], n)
    if not isinstance(d, dict):
        i.throw("TypeException", f"{this.cls.name}.definition() must return a dict, got {type_name(d)}", n)
    return dict(d)


def attributes(i, this, k, overrides, n, expand=True):
    """definition() + states (+ the k-th sequence item) + overrides, with lazy values resolved."""
    st = fac_state(this)
    attrs = definition(i, this, n)
    for kind, v in st.states:
        if kind == "seq":
            v = v[k % len(v)]
            kind = "dict" if isinstance(v, dict) else "fn"
        if kind == "fn":
            v = i.call_function(v, [dict(attrs)] if arity(v) != 0 else [], n)
            if not isinstance(v, dict):
                i.throw("TypeException", f"a factory state function must return a dict, got {type_name(v)}", n)
        attrs.update(v)
    attrs.update(overrides)
    if expand:
        for key, v in list(attrs.items()):
            if isinstance(v, BarqObject) and v.cls.is_subclass_of(factory_class(i)):
                parent = build(i, v, {}, n, True, single=True)
                from .orm import mstate, meta
                attrs[key] = mstate(parent).attrs.get(meta(i, parent.cls, n).pk)
            elif is_callable(v):
                attrs[key] = i.call_function(v, [attrs] if arity(v) != 0 else [], n)
    return attrs


def make_one(i, this, k, overrides, n):
    from .orm import mstate
    cls = factory_model(i, this, n)
    obj = i.instantiate_bare(cls)
    mstate(obj).attrs = attributes(i, this, k, overrides, n)     # factories skip fillable/guarded
    for cb in fac_state(this).after_making:
        i.call_function(cb, [obj], n)
    return obj


def create_children(i, parent, children, n):
    from .orm import is_relation_method, mq, meta, mstate, plural, snake, pivot_attach
    for child, rel_name in children:
        child_model = factory_model(i, child, n)
        name = rel_name or plural(snake(child_model.name))
        name = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name) if rel_name is None else name
        m = parent.cls.find_method(name)
        if m is not None and is_relation_method(i, parent.cls, m):
            r = mq(i.call_function(BoundMethod(parent, m), [], n)).rel
            if r.kind in ("HasMany", "HasOne"):
                key = mstate(parent).attrs.get(r.key)
                build(i, child, {r.fk: key}, n, True)
            elif r.kind == "BelongsToMany":
                made = build(i, child, {}, n, True)
                made = made if isinstance(made, list) else [made]
                pivot_attach(i, r, [mstate(x).attrs.get(r.key) for x in made], {}, n)
            else:
                i.throw("TypeException", f"Factory.has(): {parent.cls.name}.{name}() is a BelongsTo relation — "
                        "create the parent with its own factory instead", n)
        elif rel_name is not None:
            i.throw("NameException", f"Factory.has(): {parent.cls.name} has no relation '{rel_name}'", n)
        else:
            fk = snake(parent.cls.name) + "_id"
            build(i, child, {fk: mstate(parent).attrs.get(meta(i, parent.cls, n).pk)}, n, True)
        mstate(parent).relations.pop(name, None)


def build(i, this, overrides, n, persist, single=False):
    from .orm import save
    st = fac_state(this)
    count = 1 if single or st.count is None else st.count
    out = []
    for k in range(count):
        obj = make_one(i, this, k, overrides, n)
        if persist:
            save(i, obj, n)
            if st.children:
                create_children(i, obj, st.children, n)
            for cb in st.after_creating:
                i.call_function(cb, [obj], n)
        out.append(obj)
    if single or st.count is None:
        return out[0]
    return out


def fac_make(persist):
    def run(i, a, n, this):
        overrides = arg(i, a, 0, ("dict",), "Factory." + ("create" if persist else "make"), n, {})
        return build(i, this, overrides, n, persist)
    return run


def fac_raw(i, a, n, this):
    overrides = arg(i, a, 0, ("dict",), "Factory.raw", n, {})
    st = fac_state(this)
    if st.count is None:
        return attributes(i, this, 0, overrides, n)
    return [attributes(i, this, k, overrides, n) for k in range(st.count)]


def fac_create_many(i, a, n, this):
    obj = fac_copy(i, this)
    fac_state(obj).count = need(i, a[0], ("int",), "Factory.createMany", 1, n)
    return build(i, obj, {}, n, True)


FACTORY_METHODS = {
    "definition": (lambda i, a, n, this: {}, 0, 0),
    "count": (fac_chain(set_count), 1, 1),
    "times": (fac_chain(set_count), 1, 1),
    "state": (fac_chain(add_state), 1, 1),
    "sequence": (fac_chain(add_sequence), 1, -1),
    "afterMaking": (fac_chain(add_callback("afterMaking")), 1, 1),
    "afterCreating": (fac_chain(add_callback("afterCreating")), 1, 1),
    "has": (fac_chain(add_child), 1, 2),
    "make": (fac_make(False), 0, 1),
    "create": (fac_make(True), 0, 1),
    "raw": (fac_raw, 0, 1),
    "createMany": (fac_create_many, 1, 1),
    "modelClass": (lambda i, a, n, this: factory_model(i, this, n), 0, 0),
}


FACTORIES = os.path.join("database", "factories")


def find_factory(i, cls, n):
    """User -> the class UserFactory (already loaded, or from database/factories/UserFactory.bq)."""
    base = factory_class(i)
    want = cls.name + "Factory"

    def search():
        found = [c for c in i.classes.values() if c.name == want and c.is_subclass_of(base) and c is not base]
        return found[0] if found else None
    hit = search()
    if hit is None:
        path = os.path.join(i.root, FACTORIES, want + ".bq")
        if os.path.isfile(path):
            i.load_module(path)
            hit = search()
            if hit is None:
                i.throw("NameException", f"{os.path.relpath(path, i.root)} must declare class {want} extends Factory", n)
    if hit is None:
        i.throw("NameException", f"{cls.name}.factory(): no factory found — create {FACTORIES}/{want}.bq "
                f"(barq make:factory {want})", n)
    return hit


def s_factory(i, a, n, cls):
    from .orm import need_model_class
    need_model_class(i, cls, n, "factory")
    fcls = find_factory(i, cls, n)
    obj = i.instantiate(fcls, [], n)
    st = fac_state(obj)
    st.model = cls
    cfg = fcls.find_method("configure")
    if cfg is not None and not isinstance(cfg, NativeFunction):
        configured = i.call_function(BoundMethod(obj, cfg), [], n)
        if isinstance(configured, BarqObject) and configured.cls is obj.cls:
            obj = configured
    if a and a[0] is not None:
        st = fac_state(obj)
        st.count = need(i, a[0], ("int",), f"{cls.name}.factory", 1, n)
    return obj


# ===========================================================================
# Model events (the static side; firing is in orm.py)
# ===========================================================================

MODEL_EVENTS = ("retrieved", "creating", "created", "updating", "updated", "saving", "saved",
                "deleting", "deleted", "restoring", "restored", "forceDeleting", "forceDeleted")


def model_listeners(i):
    st = i.__dict__.get("_model_events")
    if st is None:
        st = i._model_events = {}
    return st


def s_on_event(name):
    def run(i, a, n, cls):
        fn = a[0]
        if not is_callable(fn):
            i.throw("TypeException", f"{cls.name}.{name}() needs a function, got {type_name(fn)}", n)
        model_listeners(i).setdefault(id(cls), {}).setdefault(name, []).append(fn)
        return None
    return run


def s_observe(i, a, n, cls):
    targets = a[0] if isinstance(a[0], list) else [a[0]]
    for obs in targets:
        if isinstance(obs, BarqClass):
            obs = container_make(i, obs, n)
        if not isinstance(obs, BarqObject):
            i.throw("TypeException", f"{cls.name}.observe() needs an observer class or object, got {type_name(obs)}", n)
        found = False
        for name in MODEL_EVENTS:
            m = obs.cls.find_method(name)
            if m is not None:
                model_listeners(i).setdefault(id(cls), {}).setdefault(name, []).append(BoundMethod(obs, m))
                found = True
        if not found:
            i.throw("TypeException", f"observer {obs.cls.name} has none of the event methods "
                    f"({', '.join(MODEL_EVENTS[:7])}, ...)", n)
    return None


def s_flush_events(i, a, n, cls):
    st = model_listeners(i)
    if cls is i.classes["Model"]:
        st.clear()
    else:
        st.pop(id(cls), None)
    return None


def fire_model_event(i, obj, name, n):
    """Run the listeners for obj's class and its parent classes; False if one returned false."""
    table = i.__dict__.get("_model_events")
    if not table:
        return True
    cls = obj.cls
    while cls is not None:
        for fn in list(table.get(id(cls), {}).get(name, ())):
            if i.call_function(fn, [obj] if arity(fn) != 0 else [], n) is False:
                return False
        cls = cls.parent
    return True


def without_events(i, a, n, cls):
    """User.withoutEvents(fn): run fn with model events switched off."""
    saved = i.__dict__.get("_model_events")
    i._model_events = {}
    try:
        return i.call_function(a[0], [], n)
    finally:
        i._model_events = saved


# ===========================================================================
# install
# ===========================================================================

def install(i):
    from barq.runtime import BarqClass as BC
    g = i.builtins
    for name, parent in (("ContainerException", "Exception"),):
        cls = BC(name, name, None, parent=i.classes[parent], native=True)
        i.classes[name] = cls
        g.declare(name, cls, None, const=True)

    g.declare("Event", module("Event", EVENT), None, const=True)
    g.declare("event", NativeFunction("event", ev_fire, 1, 2), None, const=True)

    native_class(i, "Container", None, CONTAINER_METHODS)
    g.declare("app", NativeFunction("app", g_app, 0, 1), None, const=True)
    sp = native_class(i, "ServiceProvider", {"app": "Container"},
                      {"register": (lambda i2, a, n, this: None, 0, 0), "boot": (lambda i2, a, n, this: None, 0, 0)},
                      construct=sp_construct)
    sp.is_abstract = True

    g.declare("Fake", module("Fake", FAKE), None, const=True)
    native_class(i, "FakeUnique", None, {k: (unique_method(k, fn), lo, hi) for k, (fn, lo, hi) in GENERATORS.items()})

    fac = native_class(i, "Factory", None, FACTORY_METHODS, construct=fac_construct)
    fac.is_abstract = True

    model = i.classes["Model"]
    statics = {"factory": (s_factory, 0, 1), "observe": (s_observe, 1, 1), "flushEventListeners": (s_flush_events, 0, 0),
               "withoutEvents": (without_events, 1, 1)}
    statics.update({name: (s_on_event(name), 1, 1) for name in MODEL_EVENTS})
    for name, (fn, lo, hi) in statics.items():
        f = NativeFunction(f"Model.{name}", fn, lo, hi)
        f.late_static = True
        model.statics[name] = PropInfo(name, None, "public", None, model, is_static=True, const=True)
        model.static_values[name] = f

    from barq.stdlib2 import STDLIB_ALIASES
    STDLIB_ALIASES.update({"Events\\Event": "Event", "Container": "Container", "Support\\ServiceProvider": "ServiceProvider",
                           "Db\\Factory": "Factory", "Testing\\Fake": "Fake"})
