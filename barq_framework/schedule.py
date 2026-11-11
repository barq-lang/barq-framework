"""Framework part 3 (v0.11.0): the task scheduler.

    Schedule.call(fn).dailyAt("02:30")          a function
    Schedule.job(new SendReport()).hourly()     a job (pushed onto the Queue)
    Schedule.command("db:seed").weekly()        a barq command
    Schedule.exec("backup.sh").cron("0 3 * * 1-5")

Tasks are defined in routes/console.bq (or a service provider's boot()).
One cron entry runs everything:  * * * * *  cd /app && barq schedule:run
`barq schedule:work` does the same in the foreground, every minute.

Times are the server's local time unless the task (or config app.timezone)
names another zone: .timezone("utc"), .timezone("Asia/Karachi").
"""

import datetime as dt
import hashlib
import os
import shlex
import subprocess
import sys
import time

from barq.builtins import need
from barq.runtime import BarqClass, BarqFunction, BarqObject, BarqThrow, BoundMethod, NativeFunction, to_display, \
    type_name
from barq.stdlib import arg, module
from barq.stdlib2 import native_class, new_native

FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))
NAMES = {
    3: {m: k + 1 for k, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                         "dec"))},
    4: {d: k for k, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))},
}
DAY_NAMES = ("sundays", "mondays", "tuesdays", "wednesdays", "thursdays", "fridays", "saturdays")


# ===========================================================================
# cron expressions
# ===========================================================================

class CronError(Exception):
    pass


def parse_field(text, k):
    name, lo, hi = FIELDS[k]
    out = set()
    for part in text.split(","):
        step = 1
        stepped = "/" in part
        if stepped:
            part, _, s = part.partition("/")
            if not s.isdigit() or int(s) < 1:
                raise CronError(f'bad step "/{s}" in the {name} field')
            step = int(s)
        if part == "*":
            a, b = lo, hi
        else:
            ends = part.split("-")
            if len(ends) > 2:
                raise CronError(f'bad range "{part}" in the {name} field')
            vals = []
            for e in ends:
                e = e.lower()
                if e.isdigit():
                    vals.append(int(e))
                elif e[:3] in NAMES.get(k, {}):
                    vals.append(NAMES[k][e[:3]])
                else:
                    raise CronError(f'"{e}" is not a valid {name}')
            a, b = vals[0], vals[-1]
            if stepped and len(ends) == 1:
                b = hi              # "5/15": from 5 to the end, every 15
        if a < lo or b > hi or a > b:
            raise CronError(f"{part} is outside the {name} range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    if k == 4 and 7 in out:
        out.discard(7)
        out.add(0)
    return out


def parse_cron(expr):
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(f'a cron expression has 5 fields (minute hour day month weekday), "{expr}" has {len(parts)}')
    return [parse_field(p, k) for k, p in enumerate(parts)]


def day_matches(sets, parts, d):
    dom_ok = d.day in sets[2]
    dow_ok = (d.isoweekday() % 7) in sets[4]
    if parts[2] != "*" and parts[4] != "*":
        return dom_ok or dow_ok          # classic cron: either day field may match
    return dom_ok and dow_ok


def cron_matches(expr, d):
    sets = parse_cron(expr)
    parts = expr.split()
    return d.minute in sets[0] and d.hour in sets[1] and d.month in sets[3] and day_matches(sets, parts, d)


def make_time(tz, y, mo, d, h, mi):
    if tz is None:
        return int(dt.datetime(y, mo, d, h, mi).timestamp())
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp())


def next_after(expr, ts, tz):
    """First matching minute strictly after ts."""
    from barq.stdlib import to_dt
    sets = parse_cron(expr)
    parts = expr.split()
    start = to_dt(ts - ts % 60 + 60, tz)
    day = start.date()
    for _ in range(366 * 8):
        if day.month in sets[3] and day_matches(sets, parts, day):
            first = day == start.date()
            for h in sorted(sets[1]):
                if first and h < start.hour:
                    continue
                for m in sorted(sets[0]):
                    if first and h == start.hour and m < start.minute:
                        continue
                    return make_time(tz, day.year, day.month, day.day, h, m)
        day += dt.timedelta(days=1)
    return None


# ===========================================================================
# tasks
# ===========================================================================

class TaskState:
    def __init__(self, kind, target, label):
        self.kind = kind            # call / job / command / exec
        self.target = target
        self.label = label
        self.name = None
        self.description = None
        self.cron = ["*", "*", "*", "*", "*"]
        self.tz = UNSET
        self.filters = []           # ("when", fn) / ("skip", fn) / ("between", from, to, inside)
        self.hooks = {"before": [], "after": [], "onSuccess": [], "onFailure": []}
        self.overlap = None         # minutes a lock lasts, when withoutOverlapping()
        self.queue = None


UNSET = object()


def tstate(this):
    return this.native


def expression(st):
    return " ".join(st.cron)


def timezone_of(i, st, n):
    from barq.stdlib import tz_arg
    tz = st.tz
    if tz is UNSET:
        from barq.stdlib2 import config_lookup
        conf = config_lookup(i, "app.timezone")
        tz = conf if isinstance(conf, str) else "local"
    return tz_arg(i, [tz], 0, "Schedule timezone", n)


def chain(fn):
    def run(i, a, n, this):
        fn(i, a, n, tstate(this))
        return this
    return run


def set_cron(i, st, expr, n, fname):
    need(i, expr, ("string",), fname, 1, n)
    try:
        parse_cron(expr)
    except CronError as e:
        i.throw("ScheduleException", f"{fname}(): {e}", n)
    st.cron = expr.split()


def splice(i, st, n, fname, **fields):
    pos = {"minute": 0, "hour": 1, "dom": 2, "month": 3, "dow": 4}
    cron = list(st.cron)
    for k, v in fields.items():
        cron[pos[k]] = str(v)
    set_cron(i, st, " ".join(cron), n, fname)


def hhmm(i, text, n, fname):
    need(i, text, ("string",), fname, 1, n)
    parts = text.split(":")
    if len(parts) not in (1, 2) or not all(p.isdigit() for p in parts):
        i.throw("ScheduleException", f'{fname}(): "{text}" is not a time like "13:00"', n)
    h, m = int(parts[0]), int(parts[1]) if len(parts) == 2 else 0
    if h > 23 or m > 59:
        i.throw("ScheduleException", f'{fname}(): "{text}" is not a time of day', n)
    return h, m


def every(minutes):
    def fn(i, a, n, st):
        splice(i, st, n, "every", minute="*" if minutes == 1 else f"*/{minutes}")
    return fn


def f_cron(i, a, n, st):
    set_cron(i, st, a[0], n, "cron")


def f_hourly_at(i, a, n, st):
    m = need(i, a[0], ("int",), "hourlyAt", 1, n)
    splice(i, st, n, "hourlyAt", minute=m, hour="*")


def f_every_hours(hours):
    def fn(i, a, n, st):
        splice(i, st, n, "every", minute=0, hour="*" if hours == 1 else f"*/{hours}")
    return fn


def f_at(fname, **extra):
    def fn(i, a, n, st):
        h, m = hhmm(i, a[0], n, fname) if a else (0, 0)
        splice(i, st, n, fname, minute=m, hour=h, **extra)
    return fn


def f_twice_daily(i, a, n, st):
    h1 = arg(i, a, 0, ("int",), "twiceDaily", n, 1)
    h2 = arg(i, a, 1, ("int",), "twiceDaily", n, 13)
    splice(i, st, n, "twiceDaily", minute=0, hour=f"{h1},{h2}")


def day_number(i, d, n, fname):
    if isinstance(d, int) and 0 <= d <= 7:
        return d % 7
    if isinstance(d, str) and d[:3].lower() in NAMES[4]:
        return NAMES[4][d[:3].lower()]
    i.throw("ScheduleException", f"{fname}(): {to_display(d, i, True)} is not a day (use 0-6 or a name like \"monday\")", n)


def f_weekly_on(i, a, n, st):
    day = day_number(i, a[0], n, "weeklyOn")
    h, m = hhmm(i, a[1], n, "weeklyOn") if len(a) > 1 else (0, 0)
    splice(i, st, n, "weeklyOn", minute=m, hour=h, dow=day)


def f_monthly_on(i, a, n, st):
    day = arg(i, a, 0, ("int",), "monthlyOn", n, 1)
    h, m = hhmm(i, a[1], n, "monthlyOn") if len(a) > 1 else (0, 0)
    splice(i, st, n, "monthlyOn", minute=m, hour=h, dom=day)


def f_days(i, a, n, st):
    days = a[0] if isinstance(a[0], list) else list(a)
    splice(i, st, n, "days", dow=",".join(str(day_number(i, d, n, "days")) for d in days))


def f_dow(value):
    def fn(i, a, n, st):
        splice(i, st, n, "days", dow=value)
    return fn


def f_between(inside):
    fname = "between" if inside else "unlessBetween"

    def fn(i, a, n, st):
        st.filters.append(("between", hhmm(i, a[0], n, fname), hhmm(i, a[1], n, fname), inside))
    return fn


def f_filter(kind):
    def fn(i, a, n, st):
        if not isinstance(a[0], (bool, BarqFunction, NativeFunction, BoundMethod)):
            i.throw("TypeException", f"{kind}() needs a bool or a function, got {type_name(a[0])}", n)
        st.filters.append((kind, a[0]))
    return fn


def f_hook(kind):
    def fn(i, a, n, st):
        if not isinstance(a[0], (BarqFunction, NativeFunction, BoundMethod)):
            i.throw("TypeException", f"{kind}() needs a function, got {type_name(a[0])}", n)
        st.hooks[kind].append(a[0])
    return fn


def f_name(i, a, n, st):
    st.name = need(i, a[0], ("string",), "name", 1, n)


def f_description(i, a, n, st):
    st.description = need(i, a[0], ("string",), "description", 1, n)


def f_timezone(i, a, n, st):
    from barq.stdlib import tz_arg
    tz = need(i, a[0], ("string",), "timezone", 1, n)
    tz_arg(i, [tz], 0, "timezone", n)
    st.tz = tz


def f_overlap(i, a, n, st):
    st.overlap = arg(i, a, 0, ("int",), "withoutOverlapping", n, 1440)


def f_on_queue(i, a, n, st):
    st.queue = need(i, a[0], ("string",), "onQueue", 1, n)


TASK_CHAIN = {
    "cron": (f_cron, 1, 1),
    "everyMinute": (every(1), 0, 0),
    "everyTwoMinutes": (every(2), 0, 0),
    "everyThreeMinutes": (every(3), 0, 0),
    "everyFourMinutes": (every(4), 0, 0),
    "everyFiveMinutes": (every(5), 0, 0),
    "everyTenMinutes": (every(10), 0, 0),
    "everyFifteenMinutes": (every(15), 0, 0),
    "everyThirtyMinutes": (every(30), 0, 0),
    "hourly": (f_every_hours(1), 0, 0),
    "hourlyAt": (f_hourly_at, 1, 1),
    "everyTwoHours": (f_every_hours(2), 0, 0),
    "everyThreeHours": (f_every_hours(3), 0, 0),
    "everySixHours": (f_every_hours(6), 0, 0),
    "daily": (f_at("daily"), 0, 0),
    "dailyAt": (f_at("dailyAt"), 1, 1),
    "at": (f_at("at"), 1, 1),
    "twiceDaily": (f_twice_daily, 0, 2),
    "weekly": (f_at("weekly", dow=0), 0, 0),
    "weeklyOn": (f_weekly_on, 1, 2),
    "monthly": (f_at("monthly", dom=1), 0, 0),
    "monthlyOn": (f_monthly_on, 0, 2),
    "quarterly": (f_at("quarterly", dom=1, month="1,4,7,10"), 0, 0),
    "yearly": (f_at("yearly", dom=1, month=1), 0, 0),
    "weekdays": (f_dow("1-5"), 0, 0),
    "weekends": (f_dow("0,6"), 0, 0),
    "days": (f_days, 1, -1),
    "between": (f_between(True), 2, 2),
    "unlessBetween": (f_between(False), 2, 2),
    "when": (f_filter("when"), 1, 1),
    "skip": (f_filter("skip"), 1, 1),
    "before": (f_hook("before"), 1, 1),
    "after": (f_hook("after"), 1, 1),
    "onSuccess": (f_hook("onSuccess"), 1, 1),
    "onFailure": (f_hook("onFailure"), 1, 1),
    "name": (f_name, 1, 1),
    "description": (f_description, 1, 1),
    "timezone": (f_timezone, 1, 1),
    "withoutOverlapping": (f_overlap, 0, 1),
    "onQueue": (f_on_queue, 1, 1),
}
for k, d in enumerate(DAY_NAMES):
    TASK_CHAIN[d] = (f_dow(str(k)), 0, 0)


def now_arg(i, a, k, n, fname):
    return arg(i, a, k, ("int",), fname, n, None) or int(time.time())


def is_due(i, st, ts, n):
    from barq.stdlib import to_dt
    return cron_matches(expression(st), to_dt(ts, timezone_of(i, st, n)))


def filters_pass(i, st, ts, n):
    from barq.stdlib import to_dt
    for f in st.filters:
        if f[0] == "between":
            d = to_dt(ts, timezone_of(i, st, n))
            cur = (d.hour, d.minute)
            lo, hi, inside = f[1], f[2], f[3]
            within = lo <= cur <= hi if lo <= hi else (cur >= lo or cur <= hi)     # "22:00"-"06:00" spans midnight
            if within != inside:
                return False
            continue
        v = f[1] if isinstance(f[1], bool) else i.call_function(f[1], [], n)
        if not isinstance(v, bool):
            i.throw("TypeException", f"{f[0]}() functions must return bool, got {type_name(v)}", n)
        if v != (f[0] == "when"):
            return False
    return True


def label_of(st):
    return st.name or st.description or st.label


def t_next_run(i, a, n, this):
    st = tstate(this)
    return next_after(expression(st), now_arg(i, a, 0, n, "nextRun"), timezone_of(i, st, n))


TASK_METHODS = {k: (chain(fn), lo, hi) for k, (fn, lo, hi) in TASK_CHAIN.items()}
TASK_METHODS.update({
    "expression": (lambda i, a, n, this: expression(tstate(this)), 0, 0),
    "isDue": (lambda i, a, n, this: is_due(i, tstate(this), now_arg(i, a, 0, n, "isDue"), n), 0, 1),
    "nextRun": (t_next_run, 0, 1),
    "label": (lambda i, a, n, this: label_of(tstate(this)), 0, 0),
    "kind": (lambda i, a, n, this: tstate(this).kind, 0, 0),
    "run": (lambda i, a, n, this: run_task(i, this, int(time.time()), n, None), 0, 0),
})


# ===========================================================================
# running
# ===========================================================================

def lock_path(i, st):
    key = hashlib.sha1((expression(st) + "|" + label_of(st)).encode()).hexdigest()[:16]
    return os.path.join(i.root, "storage", "framework", f"schedule-{key}.lock")


def take_lock(i, st):
    path = lock_path(i, st)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if time.time() - os.path.getmtime(path) > st.overlap * 60:
            os.remove(path)             # a stale lock from a crashed run
    except OSError:
        pass
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return path
    except FileExistsError:
        return None


def run_action(i, st, n):
    if st.kind == "call":
        i.call_function(st.target, [], n)
    elif st.kind == "job":
        from barq.stdlib3 import queue_push
        queue_push(i, st.target, st.queue, 0, n, "Schedule.job")
    else:
        if st.kind == "command":
            cmd = [sys.executable, "-m", "barq"] + shlex.split(st.target)
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.environ.get("PYTHONPATH")])))
            code = subprocess.call(cmd, cwd=i.root, env=env)
        else:
            sys.stdout.flush()
            code = subprocess.call(st.target, shell=True, cwd=i.root)
        if code != 0:
            i.throw("ScheduleException", f"{label_of(st)} exited with code {code}", n)


def run_task(i, task, ts, n, log):
    """-> "ran", "skipped", "overlapping" or "failed" """
    st = tstate(task)
    if not filters_pass(i, st, ts, n):
        return "skipped"
    lock = None
    if st.overlap is not None:
        lock = take_lock(i, st)
        if lock is None:
            return "overlapping"
    started = time.time()
    if log is not None:
        log(st, None)
    try:
        for fn in st.hooks["before"]:
            i.call_function(fn, [], n)
        failed = None
        try:
            run_action(i, st, n)
        except BarqThrow as t:
            if not st.hooks["onFailure"]:
                raise
            failed = t.obj
        for fn in st.hooks["onFailure"] if failed is not None else st.hooks["onSuccess"]:
            i.call_function(fn, [failed] if failed is not None else [], n)
        for fn in st.hooks["after"]:
            i.call_function(fn, [], n)
        if failed is not None:
            return "failed"
    finally:
        if lock is not None:
            try:
                os.remove(lock)
            except OSError:
                pass
        if log is not None:
            log(st, time.time() - started)
    return "ran"


class ScheduleState:
    def __init__(self):
        self.tasks = []


def sched(i):
    st = i.__dict__.get("_schedule")
    if st is None:
        st = i._schedule = ScheduleState()
    return st


def where(fn):
    f = fn.func if isinstance(fn, BoundMethod) else fn
    if isinstance(f, BarqFunction) and f.module is not None:
        path = getattr(f.module, "path", None) or ""
        return f"function at {os.path.basename(path)}:{f.line}" if path else "function"
    return "function"


def add_task(i, kind, target, label):
    task = new_native(i, "ScheduledTask", TaskState(kind, target, label))
    sched(i).tasks.append(task)
    return task


def s_call(i, a, n):
    fn = a[0]
    if not isinstance(fn, (BarqFunction, NativeFunction, BoundMethod)):
        i.throw("TypeException", f"Schedule.call() needs a function, got {type_name(fn)}", n)
    return add_task(i, "call", fn, where(fn))


def s_job(i, a, n):
    job = a[0]
    if isinstance(job, BarqClass):
        from .framework import container_make
        job = container_make(i, job, n)
    if not isinstance(job, BarqObject) or job.cls.find_method("handle") is None:
        i.throw("TypeException", f"Schedule.job() needs a job object with a handle() method, got {type_name(a[0])}", n)
    task = add_task(i, "job", job, job.cls.name)
    if len(a) > 1:
        tstate(task).queue = need(i, a[1], ("string",), "Schedule.job", 2, n)
    return task


def s_command(i, a, n):
    cmd = need(i, a[0], ("string",), "Schedule.command", 1, n).strip()
    if not cmd:
        i.throw("ValueException", 'Schedule.command() needs a barq command, e.g. "db:seed"', n)
    if cmd.startswith("barq "):
        cmd = cmd[5:]
    return add_task(i, "command", cmd, "barq " + cmd)


def s_exec(i, a, n):
    cmd = need(i, a[0], ("string",), "Schedule.exec", 1, n).strip()
    if not cmd:
        i.throw("ValueException", "Schedule.exec() needs a shell command", n)
    return add_task(i, "exec", cmd, cmd)


def s_due(i, a, n):
    ts = now_arg(i, a, 0, n, "Schedule.due")
    return [t for t in sched(i).tasks if is_due(i, tstate(t), ts, n)]


def run_due(i, ts, n, log=None, on_skip=None):
    ran = []
    for t in list(sched(i).tasks):
        st = tstate(t)
        if not is_due(i, st, ts, n):
            continue
        result = run_task(i, t, ts, n, log)
        if result == "ran":
            ran.append(label_of(st))
        elif on_skip is not None:
            on_skip(st, result)
    return ran


def s_run(i, a, n):
    return run_due(i, now_arg(i, a, 0, n, "Schedule.run"), n)


def s_clear(i, a, n):
    sched(i).tasks = []
    return None


SCHEDULE = {
    "call": (s_call, 1, 1),
    "job": (s_job, 1, 2),
    "command": (s_command, 1, 1),
    "exec": (s_exec, 1, 1),
    "tasks": (lambda i, a, n: list(sched(i).tasks), 0, 0),
    "due": (s_due, 0, 1),
    "run": (s_run, 0, 1),
    "clear": (s_clear, 0, 0),
}


# ===========================================================================
# CLI: schedule:run / schedule:list / schedule:work
# ===========================================================================

CONSOLE_FILE = os.path.join("routes", "console.bq")


def load_schedule(i):
    path = os.path.join(i.root, CONSOLE_FILE)
    if os.path.isfile(path):
        i.load_module(path)


def run_logger(stamp):
    def log(st, seconds):
        if seconds is None:
            print(f"  {stamp}  Running [{label_of(st)}]")
        else:
            print(f"  {stamp}  Done [{label_of(st)}] in {seconds * 1000:.0f}ms")
        sys.stdout.flush()
    return log


def cli_run(i, ts=None):
    load_schedule(i)
    ts = ts or int(time.time())
    stamp = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    log = run_logger(stamp)

    def skipped(st, why):
        print(f"  {stamp}  Skipping [{label_of(st)}]" + (" — still running" if why == "overlapping" else ""))

    due = [t for t in sched(i).tasks if is_due(i, tstate(t), ts, None)]
    if not due:
        print("  No scheduled tasks are ready to run.")
        return
    run_due(i, ts, None, log, skipped)


def cli_list(i):
    from barq.stdlib import to_dt
    load_schedule(i)
    tasks = sched(i).tasks
    if not tasks:
        print("No scheduled tasks — add them to routes/console.bq, e.g. Schedule.call(fn).daily()")
        return
    now = int(time.time())
    rows = []
    for t in tasks:
        st = tstate(t)
        tz = timezone_of(i, st, None)
        nxt = next_after(expression(st), now, tz)
        when = to_dt(nxt, tz).strftime("%Y-%m-%d %H:%M") if nxt else "never"
        rows.append([expression(st), label_of(st), "next: " + when])
    widths = [max(len(r[k]) for r in rows) for k in range(3)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def cli_work(i):
    load_schedule(i)
    print("Running the scheduler every minute. Press Ctrl+C to stop.")
    sys.stdout.flush()
    last = None
    while True:
        now = int(time.time())
        minute = now - now % 60
        if minute != last:
            last = minute
            due = [t for t in sched(i).tasks if is_due(i, tstate(t), minute, None)]
            if due:
                stamp = dt.datetime.fromtimestamp(minute).strftime("%Y-%m-%d %H:%M")

                try:
                    run_due(i, minute, None, run_logger(stamp))
                except BarqThrow as t:
                    from barq.cli import report_uncaught
                    report_uncaught(t)
        time.sleep(max(0.2, 60 - time.time() % 60))


def install(i):
    g = i.builtins
    cls = BarqClass("ScheduleException", "ScheduleException", None, parent=i.classes["Exception"], native=True)
    i.classes["ScheduleException"] = cls
    g.declare("ScheduleException", cls, None, const=True)
    native_class(i, "ScheduledTask", None, TASK_METHODS)
    g.declare("Schedule", module("Schedule", SCHEDULE), None, const=True)
    from barq.stdlib2 import STDLIB_ALIASES
    STDLIB_ALIASES.update({"Console\\Schedule": "Schedule", "Console\\ScheduledTask": "ScheduledTask"})
