"""barq-framework test suite: models, migrations, container, events, factories, scheduler.

Run from this folder (needs a barq checkout next to this one, or BARQ_HOME):

    python3 tests/test_framework.py
"""

import glob
import http.cookiejar
import json
import signal
import socket
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.environ.get("BARQ_HOME") or os.path.join(os.path.dirname(ROOT), "barq")
if not os.path.isdir(os.path.join(CORE, "barq")):
    sys.exit(f"cannot find the barq checkout at {CORE} — clone https://github.com/barq-lang/barq next to "
             "this repo or set BARQ_HOME")
sys.path.insert(0, CORE)

from barq import devtest  # noqa: E402
from barq.devtest import ENGINES, barq_env, fetch, free_port, run_barq, run_both, wait_for_port  # noqa: E402,F401

devtest.PACKAGES = "barq_framework"
devtest.PATHS = [ROOT] + [os.path.join(os.path.dirname(ROOT), d) for d in []]


class GoldenOutput(unittest.TestCase):
    """tests/cases/NAME.bq must print exactly tests/cases/NAME.out, on both engines."""


devtest.add_golden_tests(GoldenOutput, os.path.join(HERE, "cases"))


class ProjectCommands(unittest.TestCase):
    """make:*, migrate, migrate:status/rollback/fresh/reset and db:seed in a fresh project."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="barq-project-")
        with open(os.path.join(self.dir, ".env"), "w") as f:
            f.write("DB_PATH=storage/app.db\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def barq(self, *args, engine="vm"):
        extra = ["--interp"] if engine == "tree" and args[0].startswith(("migrate", "db:")) else []
        if engine == "tree" and args[0].endswith(".bq"):
            args = ("run", "--interp") + args
        proc = subprocess.run([sys.executable, "-m", "barq", *args, *extra], capture_output=True, text=True,
                              env=barq_env(), timeout=60, cwd=self.dir)
        return proc.returncode, proc.stdout, proc.stderr

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def edit(self, rel, old, new):
        path = os.path.join(self.dir, rel)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(old, text)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))

    def test_generators_migrations_and_seeding(self):
        for args in (("make:model", "Post", "-m"), ("make:migration", "add_views_to_posts_table"),
                     ("make:controller", "Api/PostController", "--resource"), ("make:request", "StorePost"),
                     ("make:resource", "PostResource"), ("make:seeder", "DatabaseSeeder"),
                     ("make:middleware", "Auth")):
            code, out, err = self.barq(*args)
            self.assertEqual(code, 0, err)
            self.assertTrue(out.startswith("Created "), out)
        self.assertEqual(self.barq("make:model", "Post")[0], 1)       # never overwrites
        self.assertEqual(self.barq("compile", "app", "database")[0], 0)
        migrations = sorted(os.listdir(os.path.join(self.dir, "database", "migrations")))
        self.assertTrue(migrations[0].endswith("_create_posts_table.bq") and migrations[1].endswith("_add_views_to_posts_table.bq"))
        create, add = (os.path.join("database", "migrations", m) for m in migrations)
        self.edit(create, "table.id()", 'table.id()\n            table.string("title")')
        self.edit(add, '// table.string("new_column").nullable()\n            return', 'table.int("views").default(0)')
        self.edit(add, '// table.dropColumn("new_column")\n            return', 'table.dropColumn("views")')
        self.edit(os.path.join("app", "Models", "Post.bq"), "fillable = #[]", 'fillable = #["title"]')
        self.write(os.path.join("database", "seeders", "DatabaseSeeder.bq"),
                   "namespace Database\\Seeders\n\nuse App\\Models\\Post\n\nclass DatabaseSeeder extends Seeder\n\n"
                   "    #[Public]\n    #[void]\n    karo run()\n        Post.create(#{\"title\": \"Hello\"})\n")
        self.write("check.bq", "use App\\Models\\Post\nprint(Post.count(), Post.first()?.title, Post.first()?.views)\n")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                shutil.rmtree(os.path.join(self.dir, "storage"), ignore_errors=True)
                code, out, err = self.barq("migrate", engine=engine)
                self.assertEqual(code, 0, err)
                self.assertEqual(out.count("Migrated:"), 2)
                self.assertEqual(self.barq("migrate", engine=engine)[1], "Nothing to migrate.\n")
                self.assertEqual(self.barq("migrate:status", engine=engine)[1].count("Ran (batch 1)"), 2)
                code, out, err = self.barq("db:seed", engine=engine)
                self.assertEqual((code, out), (0, "Seeding:   DatabaseSeeder\nDatabase seeded.\n"), err)
                self.assertEqual(self.barq("check.bq", engine=engine)[1], "1 Hello 0\n")
                code, out, _ = self.barq("migrate:rollback", "--step", "1", engine=engine)
                self.assertIn("Rolled back:  " + add[len("database/migrations/"):-3], out)
                self.assertIn("has no column or property 'views'", self.barq("check.bq", engine=engine)[2])
                code, out, err = self.barq("migrate:fresh", "--seed", engine=engine)
                self.assertEqual(code, 0, err)
                self.assertIn("Dropped all tables.", out)
                self.assertEqual(self.barq("check.bq", engine=engine)[1], "1 Hello 0\n")
                code, out, _ = self.barq("migrate:reset", engine=engine)
                self.assertEqual(out.count("Rolled back:"), 2)
                self.assertEqual(self.barq("migrate:status", engine=engine)[1].count("Pending"), 2)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "storage", "app.db")))   # .env was read


class ProjectApp(unittest.TestCase):
    """tests/project: service providers, a factory in the seeder, an observer firing an event,
    container injection in controllers, barq serve (with reload) and barq route:list."""

    def setUp(self):
        self.dir = os.path.join(tempfile.mkdtemp(prefix="barq-app-"), "app")
        shutil.copytree(os.path.join(HERE, "project"), self.dir)

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.dir), ignore_errors=True)

    def barq(self, *args, engine="vm"):
        extra = ["--interp"] if engine == "tree" else []
        if args[0].endswith(".bq"):
            args = ("run",) + tuple(extra) + args
            extra = []
        proc = subprocess.run([sys.executable, "-m", "barq", *args, *extra], capture_output=True, text=True,
                              env=barq_env(), timeout=60, cwd=self.dir)
        return proc.returncode, proc.stdout, proc.stderr

    def fresh_db(self, engine="vm"):
        shutil.rmtree(os.path.join(self.dir, "storage"), ignore_errors=True)
        code, _, err = self.barq("migrate", engine=engine)
        self.assertEqual(code, 0, err)
        code, out, err = self.barq("db:seed", engine=engine)
        self.assertEqual(code, 0, err)
        return out

    def test_commands(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                out = self.fresh_db(engine)
                self.assertEqual(out, 'Seeding:   DatabaseSeeder\nsignups seen by the listener: #{"signup": 3}\n'
                                      "Database seeded.\n")
                code, out, err = self.barq("main.bq", engine=engine)
                self.assertEqual((code, out), (0, "true 2 AppServiceProvider + salaam from Barq Test App\n"), err)
                code, out, err = self.barq("route:list", engine=engine)
                self.assertEqual(code, 0, err)
                self.assertIn("POST    /api/users         users.store  UserController.store", out)
                self.assertTrue(out.endswith("\n6 routes\n"), out)

    def test_serve(self):
        self.fresh_db()
        paths = [("GET", "/", None), ("GET", "/hello.txt", None), ("GET", "/api/users", None),
                 ("POST", "/api/users", "name=Zoya&email=z@x.pk"), ("DELETE", "/api/users/1", None),
                 ("GET", "/api/users", None), ("GET", "/api/users/1", None), ("GET", "/api/stats", None)]
        for engine in ENGINES:
            with self.subTest(engine=engine):
                if engine == "tree":
                    self.fresh_db()
                port = free_port()
                cmd = [sys.executable, "-m", "barq", "serve", "--port", str(port), "--max", str(len(paths) + 1)]
                proc = subprocess.Popen(cmd + (["--interp"] if engine == "tree" else []), cwd=self.dir,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=barq_env())
                try:
                    self.assertTrue(wait_for_port(port), "the server did not start")
                    results = [fetch(port, p, m, b) for m, p, b in paths]
                    out, err = proc.communicate(timeout=20)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                self.assertEqual(proc.returncode, 0, err)
                self.assertEqual(results[0], (200, "<h1>home</h1>"))
                self.assertEqual(results[1], (200, "static file\n"))
                self.assertEqual(json.loads(results[2][1]), ["Ali", "Sara", "Hira"])
                self.assertEqual(results[3][0], 201)
                self.assertEqual(json.loads(results[4][1]), {"deleted": 1, "trashed": True})
                self.assertEqual(json.loads(results[5][1]), ["Sara", "Hira", "Zoya"])    # soft-deleted Ali is gone
                self.assertEqual(results[6][0], 404)
                # one Stats singleton: shared by the controller and the event listener
                self.assertEqual(json.loads(results[7][1]), {"stats": {"index": 2, "signup": 1},
                                                             "booted": "AppServiceProvider + salaam from Barq Test App"})
                self.assertIn("POST /api/users -> 201", err)

    def test_serve_reloads_on_change(self):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-m", "barq", "serve", "--port", str(port)], cwd=self.dir,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=barq_env(),
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        try:
            self.assertTrue(wait_for_port(port), "the server did not start")
            self.assertEqual(fetch(port, "/"), (200, "<h1>home</h1>"))
            with open(os.path.join(self.dir, "routes", "web.bq"), "w") as f:
                f.write('Route.get("/", kr(): string => "<h1>changed</h1>")\n')
            got = None
            for _ in range(100):
                time.sleep(0.1)
                try:
                    got = fetch(port, "/")
                except OSError:
                    continue
                if got[1] == "<h1>changed</h1>":
                    break
            self.assertEqual(got, (200, "<h1>changed</h1>"))
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("Changed: routes/web.bq — restarting.", err)

    def test_serve_needs_routes(self):
        shutil.rmtree(os.path.join(self.dir, "routes"))
        code, _, err = self.barq("serve", "--no-reload")
        self.assertEqual(code, 1)
        self.assertIn("no routes", err)

    def test_generators(self):
        for args in (("make:model", "Tag", "-a"), ("make:factory", "Post"), ("make:provider", "BillingServiceProvider"),
                     ("make:observer", "TagObserver")):
            code, out, err = self.barq(*args)
            self.assertEqual(code, 0, err)
        for rel in ("database/factories/TagFactory.bq", "database/factories/PostFactory.bq",
                    "app/Providers/BillingServiceProvider.bq", "app/Observers/TagObserver.bq"):
            self.assertTrue(os.path.isfile(os.path.join(self.dir, rel)), rel)
        self.assertTrue(any(f.endswith("_create_tags_table.bq") for f in os.listdir(os.path.join(self.dir, "database", "migrations"))))
        self.assertEqual(self.barq("compile", "app", "database")[0], 0)
        self.fresh_db()                         # the new provider boots without breaking anything
        self.assertEqual(self.barq("main.bq")[1], "true 3 AppServiceProvider + salaam from Barq Test App\n")


class BuildProject(unittest.TestCase):
    """barq build bundles barq-framework: migrate and db:seed work from the built folder alone."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="barq-build-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_build(self):
        app = os.path.join(self.dir, "shop")
        self.assertEqual(devtest.barq_cli("new", "shop", cwd=self.dir)[0], 0)
        with open(os.path.join(app, "count.bq"), "w") as f:
            f.write(PRINT_USERS)
        code, out, err = devtest.barq_cli("build", "--with-env", cwd=app)
        self.assertEqual(code, 0, out + err)
        self.assertIn("with barq-framework", out)
        self.assertIn("barq migrate", out)
        dist = os.path.join(self.dir, "deployed")
        os.rename(os.path.join(app, "dist"), dist)
        self.assertTrue(os.path.isfile(os.path.join(dist, "runtime", "barq-framework", "barq_framework", "__init__.py")))
        with open(os.path.join(dist, "build.json")) as f:
            self.assertEqual([p["module"] for p in json.load(f)["packages"]], ["barq_framework"])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.dir, "PYTHON": sys.executable,
               "NO_COLOR": "1"}
        for args, expect in ((["migrate"], "Migrated"), (["db:seed"], "Seed"), (["run", "count.bq"], "3\n")):
            proc = subprocess.run([os.path.join(dist, "barq"), *args], capture_output=True, text=True,
                                  env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(expect, proc.stdout)


class NewProject(unittest.TestCase):
    """barq new with barq-framework: the User model, migration, factory and seeder work as written."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="barq-new-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_new_project(self):
        app = os.path.join(self.dir, "shop")
        code, out, err = devtest.barq_cli("new", "shop", cwd=self.dir)
        self.assertEqual(code, 0, err)
        self.assertIn("barq migrate", out)
        for rel in ("app/Models/User.bq", "app/Providers/AppServiceProvider.bq", "database/factories/UserFactory.bq",
                    "database/seeders/DatabaseSeeder.bq"):
            self.assertTrue(os.path.isfile(os.path.join(app, rel)), rel)
        self.assertEqual(len(glob.glob(os.path.join(app, "database", "migrations", "*_create_users_table.bq"))), 1)
        self.assertEqual(devtest.barq_cli("fmt", "--check", ".", cwd=app)[0], 0)
        with open(os.path.join(app, "count.bq"), "w") as f:
            f.write(PRINT_USERS)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                flag = "--interp" if engine == "tree" else "--vm"
                code, out, err = devtest.barq_cli("migrate:fresh", "--seed", *(["--interp"] if engine == "tree" else []), cwd=app)
                self.assertEqual(code, 0, out + err)
                code, out, err = devtest.barq_cli("run", flag, "count.bq", cwd=app)
                self.assertEqual((code, out), (0, "3\n"), err)
                code, out, err = devtest.barq_cli("test", flag, cwd=app)
                self.assertEqual(code, 0, out + err)
        code, _, err = devtest.barq_cli("new", "tool", "--console", cwd=self.dir)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tool", "database")))


PRINT_USERS = """use App\\Models\\User

print(User.count())
"""


if __name__ == "__main__":
    unittest.main()
