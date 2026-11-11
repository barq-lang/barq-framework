# barq-framework

The Barq framework: models (ORM) with relations, scopes and soft deletes, migrations and seeders, factories and fake data, form requests and resources, route model binding, the service container and providers, events and observers, and the task scheduler — plus the `migrate`, `db:seed`, `make:*` and `schedule:*` commands.

Part of [Barq (برق)](https://github.com/barq-lang/barq). Needs the [barq](https://github.com/barq-lang/barq) language.

## Install

```bash
git clone https://github.com/barq-lang/barq
git clone https://github.com/barq-lang/barq-framework
barq/bin/barq packages        # lists barq-framework
```

With barq 0.21 or later you can instead run `barq install barq-lang/barq-framework` in a project: it goes into the project's `vendor/` and loads from there.

The `barq` command loads packages cloned next to the `barq` checkout (or listed in `BARQ_PATH`); see [Repositories and packages](https://github.com/barq-lang/barq#repositories-and-packages).

## Tests

```bash
cd barq-framework && python3 tests/test_framework.py    # both engines; finds ../barq (or set BARQ_HOME)
```

## Commands

```bash
barq migrate | migrate:rollback [--step N] | migrate:reset | migrate:fresh [--seed] | migrate:status
barq db:seed [--class Name]
barq make:model Post -a        # model + migration + factory (-m, -f for one)
barq make:migration create_posts_table | controller [--resource] | request | resource | seeder
barq make:middleware | factory | provider | observer | policy [--model Post]
barq schedule:run | schedule:list | schedule:work
```

| File | Job |
|------|-----|
| [barq_framework/orm.py](barq_framework/orm.py) | `Model`, `ModelQuery`, relations, `Resource`, `FormRequest`, `Migration`, `Seeder`, `unique` / `exists` rules |
| [barq_framework/framework.py](barq_framework/framework.py) | `Event`, `Container` / `app()`, `ServiceProvider`, model events and observers, `Fake`, `Factory` |
| [barq_framework/schedule.py](barq_framework/schedule.py) | `Schedule` and its cron parser |
| [barq_framework/commands.py](barq_framework/commands.py) | `migrate*`, `db:seed`, `make:*` |
| [barq_framework/\_\_init\_\_.py](barq_framework/__init__.py) | Registers all of it with the language's hooks |

## Framework: models, requests, resources, migrations (v0.9.0)

The first part of the Step 10 framework. Everything works with the SQLite driver and the router from v0.6.0.

### Models

```barq
// app/Models/User.bq
namespace App\Models

class User extends Model

    protected list<string> fillable = #["name", "email", "age"]
    protected list<string> hidden = #["password"]
    protected dict<string, string> casts = #{"active": "bool", "settings": "json"}

    #[Public]
    #[HasMany]
    karo posts()
        return this.hasMany(Post)

    #[Public]
    #[ModelQuery]
    karo scopeAdults(ModelQuery q)
        return q.where("age", ">=", 18)
```

```barq
use App\Models\User

User alice = User.create(#{"name": "Alice", "email": "a@example.com", "age": 30})
print(alice.id, alice.name)                    // columns are read like properties
alice.name = "Alice B."
alice.save()                                   // only changed columns are written

User.find(1)              User.findOrFail(1)     // findOrFail → 404 inside a route
User.where("age", ">", 18).orderBy("name").get()
User.adults().count()                          // scope
User.with("posts").paginate(15, 2)             // eager loading + pagination
User.firstOrCreate(#{"email": "a@example.com"}, #{"name": "Alice"})
alice.posts().create(#{"title": "Hello"})      // relation as a query: sets user_id
for post in alice.posts                        // relation as a value: loaded once
    print(post.title)
```

| Setting (a property in the model) | Default | Meaning |
|-----------------------------------|---------|---------|
| `table` | plural snake_case of the class: `User` → `users`, `BlogPost` → `blog_posts`, `Category` → `categories` | Table name |
| `primaryKey` | `"id"` | Primary key column |
| `fillable` / `guarded` | neither: every column may be mass-assigned | Columns `create()`, `fill()`, `update()` accept. A column outside the list is an error (`MassAssignmentException`), not silently dropped |
| `hidden` | `#[]` | Left out of `toArray()` and JSON |
| `casts` | `#{}` | `bool`, `int`, `float`, `string`, `json` / `list` / `dict` (stored as JSON text) |
| `timestamps` | `true` | Fill `created_at` / `updated_at` (int Unix time) when the table has those columns |
| `connection` | the default connection | Name of a connection in `config/database.bq` |
| `perPage` | `15` | Default page size for `paginate()` |

| | |
|-|-|
| **Static** (called on the model class) | `query()`, `all()`, `find(id or list)`, `findOrFail(id)`, `first()`, `firstOrFail()`, `where`/`orWhere`/`whereIn`/`whereNotIn`/`whereNull`/`whereNotNull`/`whereBetween`, `orderBy`/`orderByDesc`/`latest`/`oldest`, `limit`/`take`, `select`, `with(names...)`, `count`/`sum`/`avg`/`min`/`max`, `pluck`, `paginate(perPage?, page?)`, `create(dict)`, `make(dict)`, `firstOrNew`/`firstOrCreate(match, extra?)`, `updateOrCreate(match, values)`, `destroy(ids...)`, `tableName()`, `connection()`, `useConnection(db)`, and every scope (`scopeAdults` → `User.adults()`) |
| **ModelQuery** (what those return) | the same builder methods, plus `get()` → `list` of models, `first()`, `find()`, `paginate()`, `update(dict)`, `delete()`, `increment`/`decrement`, `exists()`, `value()`, `toSql()`, `each(fn)`, scopes |
| **Object** | `save()`, `update(dict)`, `fill(dict)`, `delete()`, `refresh()`, `fresh()`, `toArray()` (also `toDict()`), `load(names...)`, `getKey()`, `exists()`, `wasRecentlyCreated()`, `isDirty(col?)`, `getDirty()`, `getOriginal(col?)`, `getAttributes()`, `only(cols...)`, `is(other)` |

- **Columns are not declared.** A model object keeps its row in hidden state; `user.name` reads it through `__get` and `user.name = x` writes it through `__set`. Reading a column the table doesn't have is an error that lists the real columns.
- **`__get` / `__set` are a general language feature now**: any class can define `karo __get(string name)` and `karo __set(string name, any value)`; they run when a property isn't declared.
- **Models print to JSON** (`Json.encode`, `Response.json`, returning one from a route) through `toArray()`, which also includes loaded relations.
- **Default connection**: `Model.useConnection(db)`, or else the first query opens `Database.connect()` (config / `.env`).

### Relations

The return type attribute marks a method as a relation:

| Attribute | Builder | Defaults |
|-----------|---------|----------|
| `#[HasMany]` | `this.hasMany(Post, foreignKey?, localKey?)` | `user_id` on posts → `id` |
| `#[HasOne]` | `this.hasOne(Profile, foreignKey?, localKey?)` | `user_id` on the profile |
| `#[BelongsTo]` | `this.belongsTo(User, foreignKey?, ownerKey?)` | `user_id` on this model → users.`id` |
| `#[BelongsToMany]` | `this.belongsToMany(Tag, pivot?, foreignPivotKey?, relatedPivotKey?)` | pivot `post_tag` (both names, alphabetical), `post_id`, `tag_id` |

- `post.tags()` (with parentheses) is a query: add `where`, `orderBy`… then `get()`, `count()`, `first()`. A relation method may add its own conditions (`return this.hasMany(Comment).orderBy("id", "desc")`).
- `post.tags` (no parentheses) is the result — a list for HasMany/BelongsToMany, a model or `null` for HasOne/BelongsTo — loaded on first use and kept. Templates use the same form: `{post.user.name}`.
- **Eager loading**: `Post.with("user", "tags", "comments.author").get()` runs one query per relation instead of one per row. `model.load("posts")` does the same for an object you already have.
- **Writing**: `create(dict)` and `save(model)` on HasMany/HasOne/BelongsToMany; `associate(model)` / `dissociate()` on BelongsTo; `attach(ids or models, extraPivotColumns?)`, `detach(ids?)`, `sync(ids)` → `#{"attached": ..., "detached": ...}` on BelongsToMany.

### FormRequest

```barq
namespace App\Requests

class StorePostRequest extends FormRequest

    #[Public]
    #[bool]
    karo authorize()
        return this.header("X-Token") != "banned"

    #[Public]
    #[dict<string, string>]
    karo rules()
        return #{"title": "required|min:3|unique:posts", "user_id": "required|int|exists:users,id"}
```

A route handler that takes a `FormRequest` subclass gets it already checked: `authorize()` false → 403; failed rules → 422 JSON with `errors` (for an HTML form with a `Referer`, a redirect back with `errors` and `old` input flashed to the session). Inside the handler, `request.validated()` returns only the validated fields; every `Request` method works too. `rules()`, `messages()` and `authorize()` are optional.

New validation rules for every validator: `unique:table,column?,ignoreId?,idColumn?` and `exists:table,column?` (the column defaults to the field name).

### Route model binding, Resources, status codes

```barq
Route.get("/posts/:post", kr(Post post): PostResource => new PostResource(post))
```

- A handler parameter whose type is a model gets that model, found by the route parameter; a missing one is a 404 (`ModelNotFoundException`, a subclass of `AbortException`).
- `class PostResource extends Resource` with `toArray()` shapes API output; `this.title` reads from the wrapped model. `PostResource.collection(posts)` maps a list (or a `paginate()` result, keeping its page fields).
- A handler may return a model or a Resource: it's sent as JSON, with status **201** when the model was created during the request.

### Migrations, seeders and generators

```bash
barq make:model Post -m                       # app/Models/Post.bq + database/migrations/<time>_create_posts_table.bq
barq make:migration add_views_to_posts_table  # an alter() stub for "posts"
barq make:controller Api/PostController --resource
barq make:request StorePostRequest            # also make:resource, make:seeder, make:middleware
barq migrate                                  # run pending migrations, one batch
barq migrate:status
barq migrate:rollback                         # undo the last batch (--step N: the last N migrations)
barq migrate:fresh --seed                     # drop every table, migrate, run DatabaseSeeder
barq migrate:reset                            # undo everything
barq db:seed                                  # database/seeders/DatabaseSeeder.bq (--class Name)
```

A migration file holds one class that extends `Migration` with `up(Schema schema)` and (optionally) `down(Schema schema)`. Each runs in a transaction; the ones that ran are recorded in a `migrations` table. Seeders extend `Seeder`, put their work in `run()`, and can run others with `this.call(#[UserSeeder, PostSeeder])`. The commands work in the current folder, use the default connection, and read `.env` first — `barq run` and `barq test` now read the project's `.env` at startup too (variables already set win).

## Framework, part 2: events, container, factories, serving (v0.10.0)

In `barq_framework/framework.py`, plus hooks in the ORM and the CLI. [tests/project/](tests/project/) is a small working app that uses all of it.

### Events

```barq
Event.listen("user.registered", kr(User u): void => Mail.to(u.email).send(new WelcomeMail(u)))
Event.listen("user.*", karo(string name, any data): void     // wildcard: gets the event name too
    Log.info("user event: {name}")
)
Event.listen("user.registered", SendWelcomeMail)             // listener class: handle(data), made by the container
Event.once("payment.complete", kr(any p): void => print("first payment"))
list<any> results = Event.fire("user.registered", user)      // also Event.dispatch() and the global event()
Event.fire(new OrderShipped(order))                          // an object: its class name is the event name
```

A listener that returns `false` stops the ones after it. `Event.forget(name)` and `Event.has(name)` do what they say.

In tests, `Event.fake()` (or `Event.fake(#["user.registered"])` for some events) records events instead of running listeners. Then use `Event.assertFired(name)`, `Event.assertFired(name, 2)`, `Event.assertFired(name, kr(any d): bool => ...)`, `Event.assertNotFired(name)`, `Event.assertNothingFired()`, or `Event.fired(name)` to get the payloads.

### Service container

```barq
app().bind("mailer", kr(Container c): Mailer => new SmtpMailer())   // a new one each make()
app().singleton(PaymentGateway)                                     // one shared object
app().singleton(Gateway, StripeGateway)                             // ask for Gateway, get a StripeGateway
app().instance("version", "1.0")
Mailer m = app("mailer")                                            // = app().make("mailer")
app().has("mailer")
```

`make(SomeClass)` works without a binding: the container builds the class and makes each constructor parameter by its type ("autowiring"). Constructor parameters that aren't classes (`int`, `string`, ...) need a binding. Cycles give a `ContainerException`.

The container is also used for:
- controllers (constructor injection)
- class middleware
- listener classes and observers
- route handler parameters whose type is a class and which don't match a route parameter: `karo index(Request r, Stats stats)`

Models still come from route model binding.

### Service providers

```barq
// app/Providers/AppServiceProvider.bq
namespace App\Providers

class AppServiceProvider extends ServiceProvider

    #[Public]
    #[void]
    karo register()
        this.app.singleton(Stats)

    #[Public]
    #[void]
    karo boot()
        User.observe(UserObserver)
        Event.listen("user.created", SendWelcomeMail)
```

When a project has an `app/Providers/` folder, `barq run`, `barq test`, `barq serve`, `migrate` and `db:seed` start the providers before anything else. Every provider's `register()` runs first, then every provider's `boot()`. The providers are every class in `app/Providers/` in file-name order, or the list in `config/app.bq` under `"providers"` if there is one. `app().isBooted()`, `app().providers()` and `app().register(SomeProvider)` are there too.

### Model events and observers

```barq
User.creating(karo(User u): void
    u.name = u.name.trim()
)
User.deleting(kr(User u): bool => u.email != "admin@example.com")   // false cancels the delete
User.observe(UserObserver)          // a class with methods named after events: created(User u), ...
User.withoutEvents(karo(): void
    User.create(#{"name": "quiet"})
)
User.flushEventListeners()
```

The events are:
- saving: `saving` → (`creating` → insert → `created` | `updating` → update → `updated`) → `saved`
- deleting: `deleting` → `deleted`
- soft deletes: `restoring` → `restored`, and `forceDeleting` → `forceDeleted`
- loading: `retrieved`

A listener for a `...ing` event that returns `false` cancels it, and `save()` / `delete()` return `false`. Listeners on `Model` itself run for every model. Bulk query writes (`User.where(...).update(...)`) don't fire events, the same as in Laravel.

### Soft deletes

```barq
class Post extends Model
    protected bool softDeletes = true          // and table.softDeletes() in the migration
```

- `post.delete()` sets `deleted_at` instead of removing the row.
- Every query, relation and route model binding then leaves out deleted rows.
- `Post.withTrashed()` includes them and `Post.onlyTrashed()` returns only them.
- `post.trashed()`, `post.restore()` and `post.forceDelete()` (really delete) work on one model.
- On queries: `Post.onlyTrashed().restore()`, `Post.where(...).delete()` (soft) and `.forceDelete()`.
- An `orWhere` stays grouped: `(a OR b) AND deleted_at IS NULL`.

Barq has no traits yet, so soft deletes is a model property, not a `use SoftDeletes` line.

### Fake data

`Fake.name()`, `firstName()`, `lastName()`, `email()`, `userName()`, `phone()`, `word()`, `words(n)`, `sentence(words?)`, `paragraph(sentences?)`, `text(maxChars?)`, `title()`, `slug(words?)`, `int(max)` / `int(min, max)`, `float(min, max, places?)`, `bool(percentTrue?)`, `digits(n)`, `pick(list)`, `pickMany(list, n)`, `shuffle(list)`, `city()`, `country()`, `address()`, `company()`, `jobTitle()`, `url()`, `domain()`, `uuid()`, `password(len?)`, `color()`, `hexColor()`, `timestamp(min?, max?)`, `date(format?)`.

- `Fake.unique().email()` never repeats a value; `Fake.unique(true)` starts over.
- `Fake.seed(42)` makes the values the same on every run.
- Names mix Pakistani and international names. Emails use `example.com/org/net`.

### Factories

```barq
// database/factories/UserFactory.bq
namespace Database\Factories

class UserFactory extends Factory

    #[Public]
    #[dict<string, any>]
    karo definition()
        return #{"name": Fake.name(), "email": Fake.unique().email(), "active": true}

    #[Public]
    #[UserFactory]
    karo inactive()
        return this.state(#{"active": false})
```

```barq
User u = User.factory().create()                              // saved
list<User> users = User.factory().count(3).make()             // not saved
User.factory().inactive().create(#{"name": "Off"})            // states + overrides
User.factory().count(4).sequence(#{"age": 20}, #{"age": 30}).create()
User.factory().state(kr(dict<string, any> a): dict<string, any> => #{"email": a["name"] + "@x.pk"}).create()
User.factory().has(Post.factory().count(2)).create()          // children through user.posts()
User.factory().afterCreating(kr(User u): void => u.profile().create(#{"bio": "hi"})).create()
dict<string, any> attrs = User.factory().raw()                // just the attributes
```

- `User.factory()` finds `UserFactory`: already loaded, or from `database/factories/UserFactory.bq`.
- A factory knows its model from its name. Set `protected string model = 'App\Models\User'` to choose another (single quotes, so the `\` stays as it is).
- In a definition, a factory as a value (`"user_id": User.factory()`) creates the parent and uses its id. A function as a value is called with the attributes.
- Factories skip `fillable` / `guarded`, but still fire model events.
- A `configure()` method, if the factory has one, runs when `User.factory()` makes the factory.
- Every chain method returns a new factory, so you can keep one and reuse it.

## Task scheduler (v0.11.0)

```barq
// routes/console.bq
Schedule.call(karo(): void
    Storage.deleteDirectory("tmp")
).daily().name("clear tmp")
Schedule.job(new SendReport()).weekdays().dailyAt("08:00")       // pushed onto the Queue
Schedule.command("db:seed --class DemoSeeder").weekly()          // another barq command
Schedule.exec("./backup.sh").cron("0 3 * * 1-5").withoutOverlapping()
```

```bash
* * * * * cd /path/to/app && barq schedule:run    # the one cron line a server needs
barq schedule:list                                # expression, name, next run
barq schedule:work                                # the same, in the foreground (development)
```

**Frequencies:**
- every N minutes: `everyMinute`, `everyTwoMinutes` … `everyThirtyMinutes`
- hours: `hourly`, `hourlyAt(17)`, `everyTwoHours`, `everySixHours`
- days and weeks: `daily`, `dailyAt("13:00")` (or `at()`), `twiceDaily(1, 13)`, `weekly`, `weeklyOn("friday", "17:00")`
- months and years: `monthly`, `monthlyOn(15, "08:00")`, `quarterly`, `yearly`
- which days: `weekdays`, `weekends`, `mondays` … `sundays`, `days(#["tue", 4])`
- any cron expression: `cron("*/20 8-18 * jan-jun 1-5")`. Names and steps work. When both day fields are set, either one may match, like classic cron.

**Conditions:** `between("09:00", "17:00")` (`"22:00"`–`"06:00"` spans midnight), `unlessBetween()`, `when(fn or bool)`, `skip(fn or bool)`.

**Hooks:** `before`, `after`, `onSuccess`, `onFailure(kr(Exception e): void => ...)`. With an `onFailure` hook, a failing task doesn't stop the others.

**Other settings:**
- `name()` / `description()` label a task.
- `withoutOverlapping(minutes?)` uses a lock file in `storage/framework/`.
- `timezone("Asia/Karachi")` sets a task's zone.
- `onQueue("reports")` picks the queue for jobs.

**Times** are the server's local time. Set config `app.timezone` (e.g. `"utc"`) or `.timezone()` per task to change that.

**From Barq code:** `Schedule.tasks()`, `Schedule.due(ts?)`, `Schedule.run(ts?)` (returns the names that ran), `task.isDue(ts?)`, `task.nextRun(ts?)` and `task.expression()`. Tests can pass a fixed time with these.

## License

MIT — see [LICENSE](LICENSE).
