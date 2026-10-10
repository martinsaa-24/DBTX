# Contribution Guidelines
Code contributions should align with the following guidelines:
*   Test Driven Development

## Contribution Flow
Follow a red -> green TDD strategy.

### Patching Code:
`red tests` -> patch -> `green tests` -> refactor

`Red test`: Tests that assert the desired behavior (net new, or a modification of
existing behavior) are written first, and must fail. They expect a desired state that
the current code does not provide.

`Green test`: After the patch, the `red tests` must pass.

`Refactor`: Clean up only while every test stays green. Prefer simple, explicit code;
factor out repetition only where copies could drift apart and cause bugs.

### The cycle, one functional group at a time
A **functional group** is a set of functions or a module that together provide one
behavior (for example "config file selection" or "manifest sync").

1.  **Define the behavior.** Write down what the group must do, and what it must *not*
    do, before writing any code.
2.  **Write the red tests.** Craft deterministic inputs for the passing and the failing
    cases, and turn each into a test. Run them: they must fail.
3.  **Patch.** Write the simplest code that makes the tests pass. Change the
    implementation, not the tests, until they pass.
4.  **Refactor**, as above.

### Red for the right reason
*   A test that fails with an `ImportError`, a typo or a missing fixture proves nothing
    about behavior. First add the function or module with its signature and a body that
    does nothing or raises `NotImplementedError`. The red run must then fail on
    **assertions**.
*   Read the failure messages, and confirm they describe the missing behavior.
*   **Record the evidence.** In the phase state file (for example
    `docs/agentic/phase0_state.md`), note the red result (number of failing tests and
    why) and then the green result, for each functional group.

## Test Driven Development
The database layer (engines, transactions, concurrent writers) will need its own test
fixtures; they are designed when that layer is built.

Before making any changes, think about how the intended functionality or feature will be
proven to work. We should prove the positive and the negative outcomes.

For example, if a `config` file is critical to functionality, we should test that 1) it
is present, and 2) it follows the correct spec.

Accordingly, we would create unit tests that cover the following:
1. Checking for the file
    1.  Pass when the config file is present
    2.  Fail when it is not present
2. File contents
    1. Pass when the file contents are formatted correctly
    2. Fail when they are not formatted correctly

### Test fixtures: state comes from the test, not from extra parameters
When behavior depends on state (environment variables, the current directory, files on
disk, the clock, a rollout switch), a **test fixture** builds that state. Production
functions do **not** grow extra parameters only so tests can inject values. Such
parameters confuse callers, who cannot tell which ones matter in real use.

| State | Test fixture |
|---|---|
| Environment variables | clears and sets them with `monkeypatch.setenv` / `delenv` |
| Current directory | `monkeypatch.chdir` into a directory the test built |
| Files and directory layout | a builder that creates the layout (for example a platform root with project folders) inside `tmp_path` |
| Config file contents | a checked-in sample file under `tests/fixtures/` |
| Rollout switches and module constants | `monkeypatch.setattr` on the constant |
| Time and generated ids (UUIDs) | the module reads them through one internal function (for example `clock.now()`); the fixture replaces that function |

Shared fixtures live in the nearest `conftest.py`, so every test file builds state the
same way.

### Independent tests
*   **No test depends on another.** Each test builds the state it needs from fixtures,
    and nothing it does is visible to any other test. A test passes alone, in any order
    and in any combination.
*   **Sample and template files are tested directly.** A parser is tested against
    checked-in sample files (`tests/fixtures/config/*.conf`), one file per case, valid
    and invalid. Templates shipped to users (for example `examples/dbtx.conf.example`)
    are tested too, so the documented example is proven to be valid.
*   **Behavior that depends on a directory structure** is tested against a clean
    structure built for that one test inside `tmp_path`. For example: startup must fail
    when there is no platform config file. pytest deletes these directories
    (`tmp_path_retention_policy = "failed"` keeps them only for failed tests, for
    debugging).

### Deterministic tests
A test must give the same result on every machine and every run. Isolate everything
that varies, using the test fixtures above:
*   **Environment:** an autouse fixture clears the environment variables the code reads
    (for example every `DBTX_*`, `DBT_PROJECT_DIR`, `DBT_PROFILES_DIR`), so the
    developer's shell cannot change the outcome.
*   **Filesystem:** use `tmp_path`, never real project or home directories.
*   **Time and identifiers:** fixed by a fixture that replaces the module's clock or id
    source, never by extra function parameters.
*   **Ordering:** never depend on dict, set or directory-listing order; sort before
    comparing.
*   **Network and services:** no network calls. A test that needs a service starts a
    local, throwaway one from a dev dependency (Postgres: `pixeltable-pgserver`, once
    per test session), or uses one passed explicitly on the command line
    (`pytest --postgres-uri ...`). It never depends on a server that happens to be
    running on the developer's machine.

### Negative tests also prove that gates hold
Negative tests cover more than bad input. They also prove that something which must
not happen, does not happen. For example:
*   standalone mode never creates a database engine
*   a disabled backend never imports its driver
*   a failed parse never marks existing nodes as removed

These "must not happen" tests protect against the regressions that are hardest to
notice.

### Bugs and exploration
*   **Bug fixes start with a test that reproduces the bug** (red), then the fix (green).
*   **Exploration is not exempt.** Probing a library's internals (for example how dbt
    resolves a flag) is a throwaway spike, not production code. Once the behavior is
    understood, the real change follows the cycle above.
