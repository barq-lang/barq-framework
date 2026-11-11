"""barq-framework commands: migrations, seeders and code generators.

    barq migrate                 run pending migrations in database/migrations/
    barq migrate:rollback        undo the last batch (--step N: the last N migrations)
    barq migrate:reset           undo every migration
    barq migrate:fresh           drop every table, then migrate (--seed to seed after)
    barq migrate:status          show which migrations have run
    barq db:seed                 run database/seeders/DatabaseSeeder.bq (--class Name for another)
    barq make:<kind> Name        model, migration, controller, request, resource, seeder, middleware,
                                 factory, provider, observer, policy

They use the default database connection from config/database.bq or .env.
"""

import datetime as dt
import os
import re
import sys
import time

from barq.project import option, run_in_project, write_new


MIGRATIONS = os.path.join("database", "migrations")


SEEDERS = os.path.join("database", "seeders")


FACTORIES = os.path.join("database", "factories")


def connection(i):
    from .orm import default_db
    return default_db(i, None)


def record(i, db, name, batch):
    from barq.stdlib2 import db_run, db_state
    db_run(i, db_state(i, db, None), 'INSERT INTO "migrations" ("migration", "batch") VALUES (?, ?)', [name, batch], None)


def unrecord(i, db, name):
    from barq.stdlib2 import db_run, db_state
    db_run(i, db_state(i, db, None), 'DELETE FROM "migrations" WHERE "migration" = ?', [name], None)


def migrate(i):
    from .orm import load_migration, migration_files, ran_migrations, run_migration
    db = connection(i)
    ran = ran_migrations(i, db)
    folder = os.path.join(i.root, MIGRATIONS)
    pending = [f for f in migration_files(folder) if f[:-3] not in ran]
    if not pending:
        print("Nothing to migrate.")
        return
    batch = max(ran.values(), default=0) + 1
    for f in pending:
        name = f[:-3]
        start = time.perf_counter()
        obj = load_migration(i, os.path.join(folder, f))
        run_migration(i, db, obj, "up")
        record(i, db, name, batch)
        print(f"Migrated:  {name} ({(time.perf_counter() - start) * 1000:.0f} ms)")


def rollback(i, step=None, everything=False):
    from .orm import load_migration, ran_migrations, run_migration
    db = connection(i)
    ran = ran_migrations(i, db)
    if not ran:
        print("Nothing to roll back.")
        return
    names = list(ran)
    if everything:
        chosen = names
    elif step is not None:
        chosen = names[-step:]
    else:
        last = max(ran.values())
        chosen = [n for n in names if ran[n] == last]
    folder = os.path.join(i.root, MIGRATIONS)
    for name in reversed(chosen):
        path = os.path.join(folder, name + ".bq")
        if not os.path.isfile(path):
            i.throw("RuntimeException", f"cannot roll back {name}: {os.path.relpath(path, i.root)} is missing")
        obj = load_migration(i, path)
        if not run_migration(i, db, obj, "down"):
            print(f"Skipped:   {name} (it has no down() method)")
        else:
            print(f"Rolled back:  {name}")
        unrecord(i, db, name)


def drop_all(i):
    from barq.stdlib2 import db_run, db_state, db_tables
    db = connection(i)
    st = db_state(i, db, None)
    db_run(i, st, "PRAGMA foreign_keys = OFF", [], None)
    for t in db_tables(i, st, None):
        db_run(i, st, f'DROP TABLE IF EXISTS "{t}"', [], None)
    db_run(i, st, "PRAGMA foreign_keys = ON", [], None)
    print("Dropped all tables.")


def status(i):
    from .orm import migration_files, ran_migrations
    db = connection(i)
    ran = ran_migrations(i, db)
    files = [f[:-3] for f in migration_files(os.path.join(i.root, MIGRATIONS))]
    names = files + [n for n in ran if n not in files]
    if not names:
        print("No migrations found in " + MIGRATIONS)
        return
    width = max(len(n) for n in names)
    print(f"{'Migration'.ljust(width)}  Status")
    for n in names:
        if n in ran:
            state = f"Ran (batch {ran[n]})" + ("  — file missing" if n not in files else "")
        else:
            state = "Pending"
        print(f"{n.ljust(width)}  {state}")


def seed(i, cls_name="DatabaseSeeder"):
    from .orm import run_seeder
    path = os.path.join(i.root, SEEDERS, cls_name + ".bq")
    if not os.path.isfile(path):
        i.throw("NameException", f"seeder not found: {os.path.relpath(path, i.root)}")
    before = set(i.classes)
    i.load_module(path)
    seeder = i.classes["Seeder"]
    found = [c for fqn, c in i.classes.items() if fqn not in before and c.name == cls_name and c.is_subclass_of(seeder)]
    if not found:
        i.throw("NameException", f"{os.path.relpath(path, i.root)} must declare class {cls_name} extends Seeder")
    i._seed_log = lambda name: print(f"Seeding:   {name}")
    run_seeder(i, found[0])
    print("Database seeded.")


def cmd_db(cmd, args, engine=None):
    if cmd == "migrate":
        return run_in_project(migrate, engine)
    if cmd == "migrate:rollback":
        step, _ = option(args, "--step")
        if step is not None and (step is True or not str(step).isdigit() or int(step) < 1):
            sys.stderr.write("barq: --step needs a positive number\n")
            return 2
        return run_in_project(lambda i: rollback(i, int(step) if step else None), engine)
    if cmd == "migrate:reset":
        return run_in_project(lambda i: rollback(i, everything=True), engine)
    if cmd == "migrate:fresh":
        with_seed, _ = option(args, "--seed")

        def fresh(i):
            drop_all(i)
            migrate(i)
            if with_seed:
                seed(i)
        return run_in_project(fresh, engine)
    if cmd == "migrate:status":
        return run_in_project(status, engine)
    if cmd == "db:seed":
        name, _ = option(args, "--class", "DatabaseSeeder")
        if name is True:
            sys.stderr.write("barq: --class needs a seeder name\n")
            return 2
        return run_in_project(lambda i: seed(i, name), engine)
    return None


def studly(name):
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[_\-\s]+", name) if p)


def stub_model(name, ns):
    return f'''namespace {ns}

class {name} extends Model

    // Table "{table_of(name)}" is inferred from the class name; set it with:
    // protected string table = "{table_of(name)}"
    protected list<string> fillable = #[]
    protected list<string> hidden = #[]
'''


def table_of(name):
    from .orm import table_for
    return table_for(name)


def stub_migration(cls, table, create):
    if create:
        up = f'''        schema.create("{table}", karo(Blueprint table): void
            table.id()
            table.timestamps()
        )'''
        down = f'        schema.dropIfExists("{table}")'
    else:
        up = f'''        schema.alter("{table}", karo(Blueprint table): void
            // table.string("new_column").nullable()
            return
        )'''
        down = f'''        schema.alter("{table}", karo(Blueprint table): void
            // table.dropColumn("new_column")
            return
        )'''
    return f'''namespace Database\\Migrations

class {cls} extends Migration

    #[Public]
    #[void]
    karo up(Schema schema)
{up}

    #[Public]
    #[void]
    karo down(Schema schema)
{down}
'''


BASE_CONTROLLER = os.path.join("app", "Controllers", "Controller.bq")


def controller_head(name, ns):
    """`namespace` and `class` lines; the class extends App\\Controllers\\Controller when the
    project has one (barq new writes it), like Laravel's base controller."""
    if name == "Controller" and ns == "App\\Controllers" or not os.path.isfile(BASE_CONTROLLER):
        return f"namespace {ns}\n\nclass {name}\n"
    use = "" if ns == "App\\Controllers" else "use App\\Controllers\\Controller\n\n"
    return f"namespace {ns}\n\n{use}class {name} extends Controller\n"


def stub_controller(name, ns, resource):
    head = controller_head(name, ns)
    if not resource:
        return f'''{head}
    #[Public]
    #[any]
    karo index(Request request)
        return view("welcome")
'''
    return f'''{head}
    #[Public]
    #[any]
    karo index()
        return #[]

    #[Public]
    #[any]
    karo store(Request request)
        return Response.json(request.all(), 201)

    #[Public]
    #[any]
    karo show(int id)
        return #{{"id": id}}

    #[Public]
    #[any]
    karo update(Request request, int id)
        return #{{"id": id}}

    #[Public]
    #[any]
    karo destroy(int id)
        return null
'''


def stub_request(name, ns):
    return f'''namespace {ns}

class {name} extends FormRequest

    #[Public]
    #[bool]
    karo authorize()
        return true

    #[Public]
    #[dict<string, string>]
    karo rules()
        return #{{
            "name": "required|string|max:255"
        }}
'''


def stub_resource(name, ns):
    return f'''namespace {ns}

class {name} extends Resource

    #[Public]
    #[dict<string, any>]
    karo toArray()
        return #{{
            "id": this.id
        }}
'''


def stub_seeder(name):
    return f'''namespace Database\\Seeders

class {name} extends Seeder

    #[Public]
    #[void]
    karo run()
        // User.create(#{{"name": "Admin", "email": "admin@example.com"}})
        // this.call(#[OtherSeeder])
        return
'''


def stub_middleware(name, ns):
    return f'''namespace {ns}

class {name}

    #[Public]
    #[any]
    karo handle(Request request, any next)
        return next(request)
'''


def stub_factory(name):
    model = name[:-len("Factory")] if name.endswith("Factory") and name != "Factory" else name
    return f'''namespace Database\\Factories

// Makes App\\Models\\{model}: {model}.factory().count(3).create()
class {name} extends Factory

    #[Public]
    #[dict<string, any>]
    karo definition()
        return #{{
            "name": Fake.name()
        }}
'''


def stub_provider(name, ns):
    return f'''namespace {ns}

class {name} extends ServiceProvider

    // Runs first, for every provider: bind services into the container.
    #[Public]
    #[void]
    karo register()
        // this.app.singleton(PaymentGateway)
        return

    // Runs after every provider is registered: listeners, observers, routes.
    #[Public]
    #[void]
    karo boot()
        // Event.listen("user.registered", SendWelcomeMail)
        return
'''


def stub_observer(name, ns):
    model = name[:-len("Observer")] if name.endswith("Observer") and name != "Observer" else "Model"
    var = model[:1].lower() + model[1:]
    imp = f"use App\\Models\\{model}\n\n" if model != "Model" else ""
    methods = []
    for ev in ("created", "updated", "deleted"):
        methods.append(f'''    #[Public]
    #[void]
    karo {ev}({model} {var})
        return
''')
    return f'''namespace {ns}

{imp}// Register it in a service provider's boot(): {model}.observe({name})
class {name}

''' + "\n".join(methods)


def stub_policy(name, ns, model):
    var = "target" if model == "User" else model[:1].lower() + model[1:]
    owner = f"{var}.id" if model == "User" else f"{var}.user_id"

    def method(ability, with_model):
        params = f"User user, {model} {var}" if with_model else "User user"
        body = f"return user.id == {owner}" if with_model and ability in ("update", "delete") else "return true"
        return f'''    #[Public]
    #[bool]
    karo {ability}({params})
        {body}
'''
    methods = [method("viewAny", False), method("view", True), method("create", False),
               method("update", True), method("delete", True)]
    imports = "use App\\Models\\User\n" + (f"use App\\Models\\{model}\n" if model != "User" else "")
    return f'''namespace {ns}

{imports}
// Found automatically for App\\Models\\{model} (or register it: Gate.policy({model}, {name})).
// Check it with user.can("update", {var}), Gate.authorize("update", {var}) or the "can:update,{var}" middleware.
// A user typed "any" (instead of User) lets guests reach that method as null.
class {name}

''' + "\n".join(methods)


def migration_stamp():
    """YYYY_MM_DD_HHMMSS, always later than every existing migration so files run in creation order."""
    now = dt.datetime.now().replace(microsecond=0)
    if os.path.isdir(MIGRATIONS):
        for f in os.listdir(MIGRATIONS):
            try:
                existing = dt.datetime.strptime(f[:17], "%Y_%m_%d_%H%M%S")
            except ValueError:
                continue
            if existing >= now:
                now = existing + dt.timedelta(seconds=1)
    return now.strftime("%Y_%m_%d_%H%M%S")


def split_name(raw, base_dir, base_ns):
    """Admin/UserController -> (UserController, app/Controllers/Admin, App\\Controllers\\Admin)"""
    parts = [p for p in re.split(r"[/\\]+", raw) if p]
    name = studly(parts[-1])
    sub = [studly(p) for p in parts[:-1]]
    return name, os.path.join(base_dir, *sub), "\\".join([base_ns, *sub])


MAKE_KINDS = ("model", "migration", "controller", "request", "resource", "seeder", "middleware", "factory",
              "provider", "observer", "policy")


def cmd_make(kind, args):
    if kind not in MAKE_KINDS:
        sys.stderr.write(f"barq: unknown generator 'make:{kind}' (use {', '.join('make:' + k for k in MAKE_KINDS)})\n")
        return 2
    policy_model, args = option(args, "--model")
    flags = [a for a in args if a.startswith("-")]
    names = [a for a in args if not a.startswith("-")]
    if len(names) != 1:
        sys.stderr.write(f"usage: barq make:{kind} Name\n")
        return 2
    raw = names[0]
    if kind == "migration":
        snake_name = re.sub(r"[^a-z0-9_]", "_", raw.lower())
        stamp = migration_stamp()
        m = re.match(r"^create_(\w+?)_table$", snake_name)
        table = m.group(1) if m else re.sub(r"^(add|remove|change)_\w+_(to|from|in)_(\w+?)(_table)?$", r"\3", snake_name)
        return write_new(os.path.join(MIGRATIONS, f"{stamp}_{snake_name}.bq"),
                         stub_migration(studly(snake_name), table, bool(m)))
    if kind == "seeder":
        return write_new(os.path.join(SEEDERS, studly(raw) + ".bq"), stub_seeder(studly(raw)))
    if kind == "factory":
        name = studly(raw)
        name = name if name.endswith("Factory") else name + "Factory"
        return write_new(os.path.join(FACTORIES, name + ".bq"), stub_factory(name))
    base = {"model": ("Models", "App\\Models"), "controller": ("Controllers", "App\\Controllers"),
            "request": ("Requests", "App\\Requests"), "resource": ("Resources", "App\\Resources"),
            "middleware": ("Middleware", "App\\Middleware"), "provider": ("Providers", "App\\Providers"),
            "observer": ("Observers", "App\\Observers"), "policy": ("Policies", "App\\Policies")}[kind]
    name, folder, ns = split_name(raw, os.path.join("app", base[0]), base[1])
    path = os.path.join(folder, name + ".bq")
    if kind == "model":
        code = write_new(path, stub_model(name, ns))
        if code == 0 and ("-m" in flags or "--migration" in flags or "-a" in flags or "--all" in flags):
            code = cmd_make("migration", [f"create_{table_of(name)}_table"])
        if code == 0 and ("-f" in flags or "--factory" in flags or "-a" in flags or "--all" in flags):
            code = cmd_make("factory", [name + "Factory"])
        return code
    if kind == "provider":
        return write_new(path, stub_provider(name, ns))
    if kind == "observer":
        return write_new(path, stub_observer(name, ns))
    if kind == "policy":
        model = policy_model
        if model is None or model is True:
            model = name[:-6] if name.endswith("Policy") and len(name) > 6 else "Model"
        return write_new(path, stub_policy(name, ns, studly(model)))
    if kind == "controller":
        return write_new(path, stub_controller(name, ns, "--resource" in flags or "-r" in flags))
    if kind == "request":
        return write_new(path, stub_request(name, ns))
    if kind == "resource":
        return write_new(path, stub_resource(name, ns))
    return write_new(path, stub_middleware(name, ns))


NEW_USER_MODEL = '''namespace App\\Models

class User extends Model

    protected list<string> fillable = #["name", "email"]
    protected list<string> hidden = #["password"]
'''

NEW_USERS_MIGRATION = '''namespace Database\\Migrations

class CreateUsersTable extends Migration

    #[Public]
    #[void]
    karo up(Schema schema)
        schema.create("users", karo(Blueprint table): void
            table.id()
            table.string("name")
            table.string("email").unique()
            table.string("password")
            table.timestamps()
        )

    #[Public]
    #[void]
    karo down(Schema schema)
        schema.dropIfExists("users")
'''

NEW_USER_FACTORY = '''namespace Database\\Factories

// Makes App\\Models\\User: User.factory().count(3).create()
class UserFactory extends Factory

    #[Public]
    #[dict<string, any>]
    karo definition()
        return #{
            "name": Fake.name(),
            "email": Fake.unique().email(),
            "password": Hash.password("password")
        }
'''

NEW_SEEDER = '''namespace Database\\Seeders

use App\\Models\\User

class DatabaseSeeder extends Seeder

    // barq db:seed (or barq migrate:fresh --seed)
    #[Public]
    #[void]
    karo run()
        User.factory().count(3).create()
'''


def new_project(files, opts):
    """barq new: a service provider, plus the User model, its migration, factory and seeder.
    With --auth, barq-auth's auth:install writes the User model and users table instead."""
    if opts["kind"] == "console":
        return
    files["app/Providers/AppServiceProvider.bq"] = stub_provider("AppServiceProvider", "App\\Providers")
    files["database/factories/UserFactory.bq"] = NEW_USER_FACTORY
    files["database/seeders/DatabaseSeeder.bq"] = NEW_SEEDER
    if not opts.get("auth"):
        files["app/Models/User.bq"] = NEW_USER_MODEL
        stamp = dt.datetime.now().strftime("%Y_%m_%d_000000")
        files[f"database/migrations/{stamp}_create_users_table.bq"] = NEW_USERS_MIGRATION
