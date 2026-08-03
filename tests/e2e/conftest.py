"""Fixtures for the end-to-end suite.

The suite owns its database. A PostgreSQL cluster is created in a temporary
directory when the session starts, listens on a port nobody else holds, and is
destroyed when the session ends. Nothing connects to a pre-existing server, so
there is no credential to configure, no shared state to collide with, and
nothing left behind.

Run it:

    make test-e2e

or directly:

    pytest -m e2e --ds=config.settings.e2e tests/e2e

The settings module is not optional. config/settings/e2e.py ENFORCES the
Content Security Policy; config/settings/test.py only reports it.
"""

import itertools
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

SUPERUSER = "postgres"
DATABASE_NAME = "brightbean_e2e"
READY_TIMEOUT_S = 60

#: How many times to try starting the cluster on a freshly chosen port.
PORT_ATTEMPTS = 3

#: Installed over CDP before the first byte of the page, so it is not itself
#: subject to the page's Content Security Policy. A recorder injected as an
#: inline <script> would be silenced by exactly the fault it exists to observe.
CSP_VIOLATION_RECORDER = """
window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', function (event) {
    window.__cspViolations.push({
        directive: event.violatedDirective,
        blockedURI: event.blockedURI,
        sample: event.sample,
    });
});
"""


def pytest_collection_modifyitems(items):
    """Mark everything under this directory e2e, so no test file has to remember.

    The path check is load-bearing. A conftest hook is handed EVERY collected
    item, not only the ones beneath it, so without the check a plain `pytest`
    run would mark the entire unit suite e2e and the default `-m "not e2e"`
    would then deselect all of it.
    """
    here = Path(__file__).parent.resolve()
    for item in items:
        path = Path(item.path).resolve()
        if here == path.parent or here in path.parents:
            item.add_marker("e2e")


def _find_postgres_bin_dir() -> Path:
    """Locate initdb/pg_ctl.

    A VERSIONED INSTALL DIRECTORY FIRST, PATH only as a fallback - which is the
    opposite of the obvious order, on purpose.

    On Debian and Ubuntu `/usr/bin/initdb` and `/usr/bin/pg_ctl` are pg_wrapper
    shims that pick a cluster by their own rules rather than being one server's
    binaries. Taking PATH first therefore hands `-D` and `-o "-p PORT ..."` to a
    wrapper on the assumption it behaves like the real thing, and skips the
    version selection below entirely - in exactly the environment where more
    than one major is likely to be installed, which is CI.

    Preferring the versioned directory makes the choice explicit and reachable.
    PATH still covers an install somewhere non-standard.
    """
    if os.name == "nt":
        roots = [Path(r"C:\Program Files\PostgreSQL")]
        pattern = "*/bin/initdb.exe"
    else:
        roots = [Path("/usr/lib/postgresql")]
        pattern = "*/bin/initdb"
    found = [p.parent for root in roots if root.is_dir() for p in root.glob(pattern)]
    if not found:
        on_path = shutil.which("initdb")
        if on_path:
            return Path(on_path).parent
        raise RuntimeError("no PostgreSQL binaries on PATH or in the usual install locations")

    def version(path):
        """Parse the version directory name, so 18 sorts above 9.6.

        These names are versions, not words. Sorted as STRINGS - which is what
        this did - "9.6" beats "18" beats "16" beats "10", so on any host with
        more than one server installed the harness picked the OLDEST. Locally
        there is exactly one install, so the sort is a no-op and the suite
        passing proves nothing about this branch; on GitHub's runner, where the
        image may already carry a major before apt installs another, which one
        gets used would not be decided by anything anybody chose.
        """
        try:
            return tuple(int(part) for part in path.parent.name.split("."))
        except ValueError:
            # Not a version at all - a vendor directory, a symlink farm. Rank it
            # below everything that does parse rather than crashing the session.
            return (-1,)

    return sorted(found, key=version)[-1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_to_completion(cmd, *, sink=None):
    """Run a command to completion, raising on a non-zero exit.

    `sink` names a file to receive the child's output. Pass it for anything
    that leaves a daemon behind: `pg_ctl start` hands its standard handles to
    the postgres it spawns, and the server holds them open for its entire life,
    so a pipe here never reaches EOF and the caller blocks forever. `-l` does
    not help - that redirects the server's log, not the inherited handles.

    Output is decoded with errors="replace" because postgres speaks the
    operating system's language, which is not necessarily UTF-8.
    """
    if sink is not None:
        with open(sink, "ab") as handle:
            completed = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, check=False)
        # ON FAILURE, READ THE SINK BACK.
        #
        # This used to report an empty string, so a failed `pg_ctl start` raised
        # exactly "pg_ctl failed (1):" and nothing after the colon. That is what
        # this harness reported on its first run against Linux - twelve
        # identical errors, none of which said why - while the reason sat in the
        # sink file the whole time.
        #
        # A failure path that discards the diagnosis is worse than no failure
        # path, because it turns a five-minute fix into guesswork about a
        # platform you cannot reproduce locally.
        output = ""
        if completed.returncode != 0:
            output = Path(sink).read_text(encoding="utf-8", errors="replace")
            # pg_ctl writes its own complaint to the sink, but the SERVER's
            # reason for refusing to start goes to the -l logfile beside it.
            server_log = Path(sink).parent / "postgres.log"
            if server_log.is_file():
                output += "\n--- postgres.log ---\n" + server_log.read_text(encoding="utf-8", errors="replace")
    else:
        completed = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({completed.returncode}):\n{output}")
    return output


def _can_connect(port: int) -> bool:
    import psycopg

    try:
        with psycopg.connect(host="127.0.0.1", port=port, user=SUPERUSER, dbname="postgres", connect_timeout=3):
            return True
    except Exception:
        return False


def _wait_until_ready(port: int) -> None:
    """Poll until the cluster accepts a real connection.

    Readiness is proven by connecting rather than by trusting `pg_ctl -w`,
    because what the suite needs to know is whether psycopg can talk to it.
    """
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if _can_connect(port):
            return
        time.sleep(0.3)
    raise RuntimeError(f"cluster did not accept connections within {READY_TIMEOUT_S}s")


@pytest.fixture(scope="package", autouse=True)
def allow_sync_orm_on_the_browser_greenlet():
    """Let synchronous ORM calls run while a Playwright greenlet is on the stack.

    Playwright's synchronous API drives the browser from a greenlet running on
    an asyncio loop, so Django sees "an async context" and refuses every
    synchronous ORM call. That breaks pytest-django's own teardown, which
    flushes the database after a live_server test - the tests pass and the run
    still exits non-zero. The loop belongs to the browser driver, not to the
    application, so a blocking query on it cannot starve anything that matters.

    AUTOUSE AND SCOPED TO THIS DIRECTORY, DEFINED IN THIS CONFTEST, WHICH IS
    THE WHOLE POINT. An earlier version set the variable at module level. pytest imports
    every conftest it walks past during COLLECTION, and mark-based deselection
    happens afterwards - so a plain `pytest` run, which is what `make test` and
    CI both run, imported this file and turned Django's async-safety guard off
    for all 1072 unit tests. Nothing could catch that: the variable can only
    ever REMOVE failures, so every gate stayed green while a real
    SynchronousOnlyOperation in application code would have passed the suite
    and raised only in production.

    PACKAGE scope, not session. An autouse fixture scoped to this directory
    runs only when a test in this directory does - but a SESSION-scoped one is
    finalised at the END OF THE SESSION, not when this directory's tests
    finish. Under `-m ""` or a nightly "run everything" job, that leaves the
    guard disarmed for every unit test collected after the first e2e test - the
    module-level failure mode described above, narrowed but not removed. Package
    scope makes teardown symmetric with setup, which is what was meant.

    The previous value is restored, so nothing leaks back out to a caller that
    had set it deliberately.
    """
    previous = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = previous


@pytest.fixture(scope="session", autouse=True)
def allow_sync_orm_while_the_test_database_is_dropped(django_db_setup):
    """Keep the guard disarmed for pytest-django's OWN database teardown.

    The fixture above is package-scoped for a good reason, and that reason
    leaves a hole. Dropping the test database happens at SESSION scope, after
    this package's fixtures have been finalised and the guard put back - so
    whenever a browser test is the last thing in the session, the drop runs
    with Playwright's event loop still on the thread and Django refuses it.

    What that looked like: a passing run carrying

        PytestWarning: Error when trying to teardown test databases:
        SynchronousOnlyOperation

    which is an ERROR reported as a warning. It is invisible in a full-suite
    run, where the last test is not a browser test, and appears the moment
    somebody runs the browser file on its own - so it is order-dependent,
    which makes it worse than a consistent failure rather than better.

    THE ORDERING IS THE MECHANISM. This fixture depends on django_db_setup, so
    it is created after it and therefore finalised BEFORE it. Setting the
    variable on the way out means it is in force for exactly one thing: the
    drop that follows. Nothing runs after a session finaliser, so unlike a
    session-scoped autouse fixture this cannot leave the guard off under any
    other test.
    """
    yield
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"


def _stop_cluster_immediately(binaries: Path, datadir: Path) -> None:
    subprocess.run(
        [str(binaries / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "-w", "stop"],
        capture_output=True,
        check=False,
    )


@pytest.fixture(scope="session")
def ephemeral_postgres():
    """Create a PostgreSQL cluster for this session; destroy it afterwards."""
    binaries = _find_postgres_bin_dir()
    workdir = Path(tempfile.mkdtemp(prefix="brightbean-e2e-pg-"))
    datadir = workdir / "data"
    started = False
    port = 0
    try:
        _run_to_completion(
            [
                str(binaries / "initdb"),
                "-D",
                str(datadir),
                "-U",
                SUPERUSER,
                "--auth-local=trust",
                "--auth-host=trust",
                "--encoding=UTF8",
                "--no-sync",
            ]
        )
        # A FRESH PORT PER ATTEMPT, because _free_port cannot reserve one.
        # It binds a socket, reads the number the kernel chose and closes it
        # again - so between that and pg_ctl binding, anything else on the host
        # may take it. On a developer machine that is rare enough never to have
        # been seen; on a shared CI runner it is a classic flake, and the e2e
        # job now blocks every merge, so one flake is indistinguishable from a
        # real environmental failure. Retrying is cheaper than diagnosing that.
        for attempt in range(PORT_ATTEMPTS):
            port = _free_port()
            # THE SOCKET DIRECTORY IS NOT OPTIONAL ON DEBIAN AND UBUNTU.
            #
            # Their postgres is built with a compiled-in unix_socket_directories
            # of /var/run/postgresql, which exists and is owned by the `postgres`
            # user. A cluster started by anyone else - the `runner` user on a
            # GitHub runner - cannot create its socket there, and the server
            # exits before it ever listens. initdb succeeds, because initdb
            # never touches that path, so the failure lands on pg_ctl and looks
            # like a start-up problem rather than a permissions one.
            #
            # Point it at the directory this fixture already owns and destroys.
            # Windows has no unix sockets and rejects the setting, so it is
            # POSIX-only. Still no quoted values in -o: a quoted one is passed
            # through verbatim and the server rejects its own configuration.
            server_options = f"-p {port} -F -c listen_addresses=127.0.0.1"
            if os.name != "nt":
                server_options += f" -c unix_socket_directories={workdir}"
            try:
                # No quoted values in -o. A quoted one is passed through
                # verbatim and the server rejects its own configuration; that
                # is the bug which makes pytest-postgresql unusable on Windows.
                _run_to_completion(
                    [
                        str(binaries / "pg_ctl"),
                        "-D",
                        str(datadir),
                        "-l",
                        str(workdir / "postgres.log"),
                        "-o",
                        server_options,
                        "-w",
                        "start",
                    ],
                    sink=workdir / "pg_ctl.log",
                )
                started = True
                _wait_until_ready(port)
                break
            except RuntimeError:
                if started:
                    _stop_cluster_immediately(binaries, datadir)
                    started = False
                if attempt == PORT_ATTEMPTS - 1:
                    raise
        yield {"host": "127.0.0.1", "port": port, "user": SUPERUSER}
    finally:
        if started:
            _stop_cluster_immediately(binaries, datadir)
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture(scope="session")
def django_db_modify_db_settings(ephemeral_postgres):
    """Point Django at the ephemeral cluster before the test database is built.

    pytest-django's own `django_db_setup` depends on this fixture by name, so
    overriding it here is all that is needed to get the ordering right. The
    settings are mutated in place, which is what pytest-django's own xdist
    variant does, and happens before any connection is opened.
    """
    from django.conf import settings

    settings.DATABASES["default"].update(
        {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": DATABASE_NAME,
            "USER": ephemeral_postgres["user"],
            "PASSWORD": "",
            "HOST": ephemeral_postgres["host"],
            "PORT": ephemeral_postgres["port"],
        }
    )


@pytest.fixture
def browser_context_args(browser_context_args):
    """Give the browser a window tall enough to photograph a whole screen.

    THE SCREENSHOTS ARE THE REVIEW CHANNEL, and at the default 1280x720 they
    were lying by omission. `full_page=True` extends a screenshot to the height
    of the DOCUMENT, but this application scrolls an INNER container - the
    document itself is exactly one viewport tall - so a full-page shot of the
    connect screen showed nine platform cards and cut the remaining four off
    mid-row. Anything below the fold was invisible to whoever read the run
    back, including me.

    A taller window is not a lie about the product: it is a bigger monitor.
    Nothing is faked and no scrolling is skipped - Playwright already scrolls a
    control into view before clicking it, so this changes what the picture
    CONTAINS, not what the test does.
    """
    return {**browser_context_args, "viewport": {"width": 1280, "height": 2000}}


#: Where a run leaves the pictures of itself. Inside the repository, because
#: they have to be readable by whoever is reading the run; ignored by git,
#: because they are regenerated every time and asserted against never.
SCREENS = Path(__file__).resolve().parents[2] / ".e2e-screens"


@pytest.fixture(scope="session", autouse=True)
def only_this_run_leaves_pictures():
    """Empty the screenshot directory once, before anything photographs itself.

    A run is ASSESSED from these pictures, so anything left over from an
    earlier run is not merely clutter - it is evidence for something that did
    not happen. A suite that was interrupted, or a provider that has since
    been renamed, leaves a directory that looks exactly like a result.

    Each journey clears its own directory too, but only its own; that cannot
    remove what nothing in this run is going to write.
    """
    shutil.rmtree(SCREENS, ignore_errors=True)
    SCREENS.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def journey(request):
    """Leave the test's path as numbered, full-page screenshots.

    EVERY browser test should call this at every step it takes. A run that
    ends red tells you which assertion failed; the trail tells you what the
    person driving the browser was actually looking at when it did - which is
    the difference between a diagnosis and a guess about a page you cannot
    reproduce.

    Each test gets its own directory, emptied at the start of the test, so
    what is on disk afterwards is that test's most recent run and nothing
    else - no leftovers from an earlier attempt to mistake for this one.
    """
    directory = SCREENS / re.sub(r"[^\w.-]+", "-", request.node.name)
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    step = itertools.count(1)

    def look(page, name):
        path = directory / f"{next(step):02d}-{name}.png"
        page.screenshot(path=str(path), full_page=True)
        return path

    return look


@pytest.fixture(scope="session", autouse=True)
def the_database_is_open_for_the_whole_run(django_db_setup, django_db_blocker):
    """Open the database once, for the entire session, and never reset it.

    The serial suites here are continuous sessions: a person signs up, connects
    a channel and then writes several posts, and each step relies on what the
    last one did. Nothing may truncate between them.

    Each suite keeps out of the others' way by using a different address, and
    the database itself is created and dropped per run, so nothing survives.
    """
    with django_db_blocker.unblock():
        yield


@pytest.fixture(autouse=True)
def _live_server_helper():
    """Override pytest-django's fixture of this name, WHICH FLUSHES THE DATABASE.

    THIS IS WHAT KEPT SIGNING THE PERSON OUT, and it took far too long to find
    because it was never written down here. pytest-django ships a
    function-scoped autouse fixture called `_live_server_helper` whose own
    docstring says it "will dynamically request the transactional_db fixture
    for a test which uses the live_server fixture" - and `transactional_db`
    re-initialises the database for every test.

    So merely MENTIONING live_server anywhere in a test's fixture closure
    silently turns on a truncation after that test. The serial suite signed up
    in its first step, had every table emptied underneath it, and met
    /accounts/login/ in its second - reported as a missing "Connect" button,
    which is nothing like the truth.

    Replacing it with a no-op takes the database back into this conftest's
    hands, where `the_database_is_open_for_the_whole_run` above keeps it open
    and untouched. The only other thing the original did was enable
    live_server's modified settings, which exist to admit the server's host -
    and config/settings/e2e.py already sets ALLOWED_HOSTS = ["*"].
    """


@pytest.fixture(scope="class")
def a_page(browser, spec):
    """ONE browser session for a whole serial suite, not one per test.

    IT DEPENDS ON `spec` AND MUST. `spec` is a parametrized class-scoped
    fixture, and pytest reorders tests to keep each parameter's tests
    together. A step that did NOT reach `spec` fell outside that grouping and
    was run after the others - which tore this fixture down and built it
    again, handing that step a FRESH CONTEXT with no session cookie. The
    person was silently signed out, and the screenshot showed the application
    with a "Welcome back" login form boosted in underneath it.

    Depending on `spec` here puts every test that touches the browser into the
    same group, so the steps run in the order they are written.

    The suite in test_compose_and_publish.py is a single continuous session
    per provider: a person signs up, connects a channel and then writes
    several posts, and each step leaves the next one somewhere to start. A
    fresh page per test would throw that away and force every step to redo the
    preamble - at which point each step is mostly testing the preamble.
    """
    context = browser.new_context(viewport={"width": 1280, "height": 2000})
    page = context.new_page()

    # THE POLICY IS ENFORCED HERE, so the page can be refused things without
    # any HTTP response to show for it - a blocked image simply never asks.
    # This recorder is installed before the first byte, exactly as it is for
    # the CSP tests, so any refusal is readable from window.__cspViolations
    # instead of being invisible.
    page.add_init_script(CSP_VIOLATION_RECORDER)

    yield page
    context.close()


@pytest.fixture(scope="class")
def a_journey(spec):
    """One numbered filmstrip per provider, in the order the steps happened.

    `journey` above gives each TEST its own directory, which is right for
    tests that stand alone. A serial suite is one story, so it gets one
    directory and the numbering runs across the whole of it - which is also
    what makes it readable afterwards.

    `spec` is supplied by the suite being run and names the provider.
    """
    directory = SCREENS / spec["platform"]
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    step = itertools.count(1)

    def look(page, name):
        path = directory / f"{next(step):02d}-{name}.png"
        page.screenshot(path=str(path), full_page=True)
        return path

    return look


@pytest.fixture
def csp_page(page):
    """A Playwright page that records CSP violations from before the first byte."""
    page.add_init_script(CSP_VIOLATION_RECORDER)
    return page
