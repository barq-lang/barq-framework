"""barq-framework — models (ORM), migrations, the service container, events,
factories, fake data and the task scheduler for Barq (برق).

Loaded automatically by the `barq` command when this checkout sits next to the
barq checkout, or is listed in BARQ_PATH (see barq/plugins.py).
"""

NAME = "barq-framework"
__version__ = "0.21.0-alpha"
REQUIRES = []

USAGE = """
Framework commands (barq-framework):
  barq migrate                   Run pending migrations in database/migrations/
  barq migrate:rollback          Undo the last batch (--step N: the last N migrations)
  barq migrate:reset             Undo every migration
  barq migrate:fresh             Drop every table and migrate again (--seed to seed too)
  barq migrate:status            Show which migrations have run
  barq db:seed                   Run database/seeders/DatabaseSeeder.bq (--class Name)
  barq make:<kind> Name          Create a model (-m migration, -f factory, -a both), migration,
                                 controller (--resource), request, resource, seeder, middleware,
                                 factory, provider, observer or policy (--model Post)
  barq schedule:run              Run the tasks in routes/console.bq that are due now
                                 (call it every minute from cron)
  barq schedule:list             Show every scheduled task and when it runs next
  barq schedule:work             Run the scheduler in the foreground, every minute
"""


def engine_of(args):
    return "tree" if "--interp" in args else None


def cmd_db(cmd, args):
    from .commands import cmd_db as run
    code = run(cmd, [a for a in args if a not in ("--interp", "--vm")], engine_of(args))
    return 2 if code is None else code


def cmd_make(cmd, args):
    from .commands import cmd_make as run
    return run(cmd[5:], args)


def cmd_schedule(cmd, args):
    from barq.project import run_in_project
    from . import schedule
    fn = {"schedule:run": schedule.cli_run, "schedule:list": schedule.cli_list,
          "schedule:work": schedule.cli_work}.get(cmd)
    if fn is None:
        return None
    return run_in_project(fn, engine_of(args))


def new_project(files, opts):
    from .commands import new_project as run
    run(files, opts)


def boot(interp):
    """Start the app's service providers (app/Providers/, or config app.providers)."""
    from .framework import boot_providers, has_providers
    if has_providers(interp.root):
        boot_providers(interp)


def form_request(i, cls, req, n):
    base = i.classes.get("FormRequest")
    if base is not None and cls is not base and cls.is_subclass_of(base):
        from .orm import make_form_request
        return True, make_form_request(i, cls, req, n)
    return None


def model_binding(i, cls, raw, n):
    if cls.is_subclass_of(i.classes["Model"]):
        from .orm import bind_model
        return True, bind_model(i, cls, raw, n)
    return None


def named_db(i, name, n):
    from barq.stdlib2 import database_connect
    from .orm import orm
    named = orm(i)["named"]
    db = named.get(name)
    if db is None or db.native.conn is None:
        db = named[name] = database_connect(i, [name], n)
    return db


def register(hooks):
    from . import framework, orm, schedule
    hooks.INSTALLERS.extend([orm.install, framework.install, schedule.install])
    hooks.BOOT.append(boot)
    hooks.RELATION_TYPES.update({"HasMany", "HasOne", "BelongsTo", "BelongsToMany"})
    hooks.property_access = orm.property_access
    hooks.response_status = orm.response_status
    hooks.VALIDATION_RULES["unique"] = orm.db_rule
    hooks.VALIDATION_RULES["exists"] = orm.db_rule
    hooks.PARAM_EARLY.append(form_request)
    hooks.PARAM_BINDERS.append(model_binding)
    hooks.resolvable = framework.resolvable
    hooks.container_make = framework.container_make
    hooks.default_db = orm.default_db
    hooks.named_db = named_db
    for name in ("migrate", "migrate:rollback", "migrate:reset", "migrate:fresh", "migrate:status", "db:seed"):
        hooks.add_command(name, cmd_db)
    for name in ("schedule:run", "schedule:list", "schedule:work"):
        hooks.add_command(name, cmd_schedule)
    hooks.add_prefix_command("make:", cmd_make)
    hooks.NEW_PROJECT.append(new_project)
    hooks.USAGE.append(USAGE)
