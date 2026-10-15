# Python Style Guide

> **Note**: This style guide is for esoteric, project-specific conventions that are **not** automatically enforced by tools like [Ruff](https://docs.astral.sh/ruff/) or [Pyright](https://github.com/microsoft/pyright).
> It should be kept **minimal and focused**, and not attempt to duplicate or override existing linting/type-checking policies.

<!-- mdformat-toc start --slug=github --maxlevel=6 --minlevel=2 -->

- [1. Prefer `@dataclass` for Class definitions](#1-prefer-dataclass-for-class-definitions)
- [2. Almost never use dict.get](#2-almost-never-use-dictget)
- [3. On tests, prefer passing fixture name instead of file bytes](#3-on-tests-prefer-passing-fixture-name-instead-of-file-bytes)
- [4. Use simple test values, not pseudo-realistic ones](#4-use-simple-test-values-not-pseudo-realistic-ones)
- [5. Place private methods/functions before the methods/functions that use them](#5-place-private-methodsfunctions-before-the-methodsfunctions-that-use-them)
- [6. Test only the public interface](#6-test-only-the-public-interface)
- [7. Use `@dataclass(slots=True)` for internal DTOs](#7-use-dataclassslotstrue-for-internal-dtos)
- [8. Use `create_autospec` for mocking in tests](#8-use-create_autospec-for-mocking-in-tests)
- [9. Almost never use globals](#9-almost-never-use-globals)
- [10. Public methods should never call other public methods](#10-public-methods-should-never-call-other-public-methods)
- [11. Keep `try`/`except` blocks as small as possible](#11-keep-tryexcept-blocks-as-small-as-possible)
- [12. Write abbreviations, acronyms and initialisms in uppercase](#12-write-abbreviations-acronyms-and-initialisms-in-uppercase)
- [13. Don't nest functions](#13-dont-nest-functions)
- [14. Don't silence the type checker](#14-dont-silence-the-type-checker)

<!-- mdformat-toc end -->

______________________________________________________________________

## 1. Prefer `@dataclass` for Class definitions<a name="1-prefer-dataclass-for-class-definitions"></a>

We acknowledge that this is a **slight abuse** of what `dataclass` was originally intended for (pure data containers), but in practice, the benefits — reduced boilerplate, clear structure, and ease of use — outweigh the downsides.

```Python
from dataclasses import dataclass


@dataclass
class Controller:
    _service1: Service1
    _service2: Service2


# Use positional args when using this pattern to avoid doing _thing=thing
controller = Controller(service1, service2)
```

If an attribute needs to be set after instantiation, use `field(init=False)` and use the `__post_init__` method to set it.

## 2. Almost never use dict.get<a name="2-almost-never-use-dictget"></a>

Dictionary `.get` method should only ever be used when a field in a dictionary is optional and you want to provide a default value.
For example, if some API returns a dictionary with an optional field then `dict.get` can be used to elegantly handle the case where the field is not present.
A slightly contrived example would be, maybe an API returns a person object, if they don't have a middle name the api doesn't return this field,
then we can use `dict.get` to provide a default value of None:

```Python
middle_name = person.get("middleName")
```

But if we expect every person to have a first name, then we **must** use.

```Python
first_name = person["firstName"]
```

If the field is required, and the code block can't continue without it, then simply allow a `KeyError` to be raised.
This makes it clear that the code expects the key to be present, and if it is not, it is a bug that should be fixed.
And, in this case, allowing a default and then raising an error does not provide us with any valable additional information about the error and
actually may hinder us by swallowing the stack trace and making it harder to debug.

For example

**Good**

```Python
value = my_dict["key"]
```

**Bad**

```Python
value = my_dict.get("key")
if value is None:
    raise KeyError("Key 'key' is required in dict")
```

## 3. On tests, prefer passing fixture name instead of file bytes<a name="3-on-tests-prefer-passing-fixture-name-instead-of-file-bytes"></a>

When a test fails with a bytes parameter, using actual file bytes, the resulting pytest logs become hard to read.
Instead, pass the name of the fixture itself then load the fixture value inside the test function.

For example

**Good**

```Python
def test_example(fixture_name: str, request: pytest.FixtureRequest):
    # Instead of using file_bytes directly, use the fixture name
    file_bytes = request.getfixturevalue(fixture_name)
    # ...
    assert file_bytes == b"expected bytes"
```

**Bad**

```Python
def test_example(file_bytes: bytes):
    # Using file_bytes directly
    assert file_bytes == b"expected bytes"
```

## 4. Use simple test values, not pseudo-realistic ones<a name="4-use-simple-test-values-not-pseudo-realistic-ones"></a>

In tests, use simple, obvious values instead of pseudo-realistic ones. This makes tests more readable and maintainable, while pseudo-realistic values add zero benefit.

**Good**

```Python
create_user("first-name", "last-name")
create_user.assert_called_with("first-name", "last-name")
```

**Bad**

```Python
first_name = "John"
last_name = "Doe"
create_user(first_name, last_name)
create_user.assert_called_with(first_name, last_name)
```

The simple approach is clearer and eliminates unnecessary variables that don't contribute to the test's purpose.

Also, only provide minimal values that are required for the test to pass or for the interface of the function/method.

Given the following function:

```Python
def create_token(token_id: str): ...
```

**Good**

```Python
def test_create_token():
    create_token("token-id")
```

**Bad**

```Python
def test_create_token():
    create_token("00000000-0000-0000-0000-000000000000")
```

There is no added value in using a pseudo-realistic value like a UUID when a simple string suffices, even if in normal operation this value would be a UUID (unless that function does some validation that forces it to be a UUID, but in this case, the type hint should then be a UUID and the test should then also pass a UUID).

As a general convention, use the `kebab-case` version of the variable name as the test value. For example, `first_name` becomes `"first-name"`, `last_name` becomes `"last-name"`, `token_id` becomes `"token-id"`, and so on. This keeps test values predictable and trivially derivable from the variable they represent.

Also, prefer inlining these simple test values directly at the call site rather than extracting them into variables or fixtures. A literal like `"user-id"` is more readable inline than a `user_id` fixture or constant — there is no shared construction cost or duplication being eliminated, just unnecessary indirection. Reserve fixtures for values that are non-trivial to construct or genuinely benefit from being shared (see [Section 9](#9-almost-never-use-globals)).

## 5. Place private methods/functions before the methods/functions that use them<a name="5-place-private-methodsfunctions-before-the-methodsfunctions-that-use-them"></a>

Private methods and functions (those prefixed with `_`) should be defined before the public methods that call them. This improves code readability by following a logical flow where dependencies are defined before their usage.

**All of a class's private methods go in one block, before any public method** — not merely somewhere above the caller. When adding a private to an existing class, put it with the other privates rather than next to the public method that uses it. Interleaving the two (public, private, public) is what this rule exists to prevent, and satisfying "before its caller" locally is not enough.

Note: This differs from languages like Java where private methods are typically placed after public methods. PEP 8 doesn't specify ordering for private vs public methods, so this is a project-specific convention.

**Good**

```Python
class DocumentProcessor:
    def _validate_document(self, doc: bytes) -> bool:
        # Private validation logic
        return True

    def _extract_metadata(self, doc: bytes) -> dict:
        # Private extraction logic
        return {}

    def process_document(self, doc: bytes) -> dict:
        if not self._validate_document(doc):
            raise ValueError("Invalid document")
        return self._extract_metadata(doc)
```

**Bad**

```Python
class DocumentProcessor:
    def process_document(self, doc: bytes) -> dict:
        if not self._validate_document(doc):
            raise ValueError("Invalid document")
        return self._extract_metadata(doc)

    def _validate_document(self, doc: bytes) -> bool:
        # Private validation logic
        return True

    def _extract_metadata(self, doc: bytes) -> dict:
        # Private extraction logic
        return {}
```

**Also bad** — the private is above its caller, but sits between two public methods:

```Python
class DocumentProcessor:
    def process_document(self, doc: bytes) -> dict:
        return {}

    def _extract_metadata(self, doc: bytes) -> dict:
        # Private extraction logic
        return {}

    def summarise_document(self, doc: bytes) -> str:
        return str(self._extract_metadata(doc))
```

## 6. Test only the public interface<a name="6-test-only-the-public-interface"></a>

Private names (prefixed with `_`) are implementation details. Tests that import them, call them or read private attributes couple the tests to the internals: every refactor breaks them, and they can pass while the public behaviour is wrong.

Test through the public interface only — what a user of the code can reach. If private logic is complex enough to want its own tests, that is a signal it should be extracted into its own public function or class.

- **No private imports** in tests: `from package._module import ...` and `package._thing` are off limits.
- **No reaching into private attributes** (`obj._state`) to set up or assert. Drive the behaviour through the public API, or inject a dependency and assert on its interactions.
- **For a library, the tests are the typed contract.** They use the public API exactly as a user would, and every type checker the library supports must pass on them, so typing regressions show up as test failures.

**Good** — the dependency is injected, and the test asserts on its interactions:

```Python
@pytest.fixture(name="handler_mock")
def _handler_mock() -> MagicMock:
    return create_autospec(Handler, spec_set=True, instance=True)


@pytest.fixture(name="service")
def _service(handler_mock: MagicMock) -> Service:
    return Service(handler_mock)


def test_service_calls_handler_with_correct_args(service: Service, handler_mock: MagicMock):
    service.process("input")

    handler_mock.handle.assert_called_once_with("input")
```

**Bad** — reaching into the private attribute instead of using the injected mock:

```Python
def test_service_calls_handler_with_correct_args(service: Service):
    service.process("input")

    service._handler.handle.assert_called_once_with("input")  # Accessing internals
```

## 7. Use `@dataclass(slots=True)` for internal DTOs<a name="7-use-dataclassslotstrue-for-internal-dtos"></a>

When defining internal data transfer objects (DTOs) — structs that carry data between layers within the application — use `@dataclass(slots=True)` rather than a plain `@dataclass`, `NamedTuple`, or Pydantic `BaseModel`.

Slots eliminate the per-instance `__dict__`, reducing memory usage and improving attribute access speed. Based on benchmarks over 10 million iterations:

| Operation             | `dataclass(slots=True)` | `namedtuple`         | `dataclass`          | `pydantic`           |
| --------------------- | ----------------------- | -------------------- | -------------------- | -------------------- |
| Create                | **1316ms** ✓            | 1771ms (1.3x slower) | 1460ms (1.1x slower) | 7781ms (5.9x slower) |
| Attribute access      | **716ms** ✓             | 1080ms (1.5x slower) | 723ms (1.0x slower)  | 1862ms (2.6x slower) |
| Memory (per instance) | **274 B** ✓             | 290 B (1.1x larger)  | 631 B (2.3x larger)  | 1551 B (5.7x larger) |

`@dataclass(slots=True)` is the fastest across all benchmarks. The memory saving is the most significant benefit — slotted dataclasses use roughly the same memory as a `namedtuple` and 2.3x less than a plain `@dataclass`.

**Pydantic should still be used at I/O boundaries** (router request/response models, external API payloads, config deserialization) where validation, serialization, and schema generation are needed. For everything in between — data passed between components and internal helpers — prefer `@dataclass(slots=True)`.

**Exception: import time.** In a library, modules loaded by a plain `import package` should keep their own imports cheap, because every user pays for them on startup. If `dataclasses` isn't otherwise needed there, a `NamedTuple` or a plain class is fine — measure with `python -X importtime`. The same goes for Section 1.

```Python
from dataclasses import dataclass


@dataclass(slots=True)
class ParsedDocument:
    document_id: str
    page_count: int
    text: str
```

## 8. Use `create_autospec` for mocking in tests<a name="8-use-create_autospec-for-mocking-in-tests"></a>

Always use `create_autospec(Thing, spec_set=True, instance=True)` when creating mocks, rather than `MagicMock()` or `mocker.MagicMock()`.

- **`spec_set=True`**: Raises `AttributeError` if you access or set an attribute that doesn't exist on the real class — catches typos in attribute/method names at test time rather than silently passing.
- **`instance=True`**: Creates a mock that behaves like an _instance_ of the class, not the class itself (correct `isinstance` checks, correct method signatures).
- **Auto-specced methods**: All method mocks automatically enforce the real method's signature, so calls with wrong arguments fail immediately.

```Python
@pytest.fixture(name="handler_mock")
def _handler_mock() -> MagicMock:
    return create_autospec(Handler, spec_set=True, instance=True)
```

**Bad** — `MagicMock()` silently accepts any attribute or call signature:

```Python
@pytest.fixture(name="handler_mock")
def _handler_mock() -> MagicMock:
    return MagicMock()  # typos in method names go undetected
```

## 9. Almost never use globals<a name="9-almost-never-use-globals"></a>

Module-level globals (constants, configuration values, or shared state defined outside of a class) are mostly a design smell. They make code harder to test, harder to reason about, and harder to override in different contexts. Prefer encapsulating these values as class attributes (or dependency-injected settings), so that they live alongside the code that uses them and can be substituted in tests or different runtime contexts.

**Good** — values are encapsulated as class attributes:

```Python
class Client:
    _http_client: httpx.Client
    _base_url: str = "http://url/api"
```

**Bad** — values leak into module scope as globals:

```Python
_BASE_URL: str = "http://url/api"


class Client:
    _http_client: httpx.Client
```

Class-based settings (or similar dependency-injected configuration) are preferred over module-level globals. This keeps related state colocated with the class that owns it, makes the dependency surface explicit, and avoids hidden coupling between modules.

In tests, the same principle applies: prefer pytest fixtures over module-level globals for shared test setup that is non-trivial to construct. Fixtures make dependencies explicit at the test signature level, support scoping (function/module/session), and can be overridden or parametrized — all of which globals cannot. (For trivial values like a single string, inline them at the call site instead — see [Section 4](#4-use-simple-test-values-not-pseudo-realistic-ones).)

**Good** — shared test setup is exposed via a fixture:

```Python
@pytest.fixture(name="parsed_document")
def _parsed_document() -> ParsedDocument:
    return ParsedDocument(document_id="document-id", page_count=1, text="text")


def test_something(parsed_document: ParsedDocument): ...
```

**Bad** — shared test setup defined as a module-level global:

```Python
_PARSED_DOCUMENT = ParsedDocument(document_id="document-id", page_count=1, text="text")


def test_something():
    # implicitly depends on _PARSED_DOCUMENT
    ...
```

## 10. Public methods should never call other public methods<a name="10-public-methods-should-never-call-other-public-methods"></a>

A public method should never call another public method on the same class. Route shared behavior through a private method instead, and have every public entry point that needs it call the private method directly. If the same functionality also needs to be exposed as its own public method, give that public method a body that is nothing but a call to the private one.

This keeps a class's public methods independent of each other: overriding, subclassing, or mocking one public method can never silently change the behavior of another, and each public method can be understood (and tested) without tracing through a chain of sibling public calls.

**Good** — both public methods call the shared private method directly:

```Python
class Example:
    def _thing(self) -> int: ...

    def public1(self) -> None:
        thing = self._thing()
        ...

    def thing(self) -> int:
        return self._thing()
```

**Bad** — `public1` reaches another public method instead of the shared private one:

```Python
class Example:
    def thing(self) -> int: ...

    def public1(self) -> None:
        thing = self.thing()  # Should call a private method instead
        ...
```

## 11. Keep `try`/`except` blocks as small as possible<a name="11-keep-tryexcept-blocks-as-small-as-possible"></a>

Only wrap the line(s) that can actually raise the exception being handled — not surrounding code that can't. A wider `try` block risks catching an unrelated error and mishandling it as if it were the expected one, and makes it unclear which line is actually expected to fail.

**Good**

```Python
try:
    value = lookup["key"]
except KeyError:
    ...
```

**Bad** — `logger.info` is inside the `try`, so a bug in it would be caught as if it were the `KeyError`:

```Python
try:
    value = lookup["key"]
    logger.info(f"Got {value}")
except KeyError:
    ...
```

If you genuinely need to catch a broad exception (e.g. `except Exception`) around code that can't be reduced to a line or two, extract that code into a helper function and wrap the call to it, rather than wrapping the code inline. This keeps the `try` block itself minimal while still covering everything the operation can raise.

**Good**

```Python
try:
    helper_func()
except Exception:
    ...
```

**Bad**

```Python
try:
    lots()
    of()
    code()
except Exception:
    ...
```

## 12. Write abbreviations, acronyms and initialisms in uppercase<a name="12-write-abbreviations-acronyms-and-initialisms-in-uppercase"></a>

In CapWords names — classes, exceptions, enums, type aliases, test classes — write every abbreviation, acronym or initialism in full uppercase, as [PEP 8](https://peps.python.org/pep-0008/#descriptive-naming-styles) recommends. It keeps the abbreviation recognisable, and matches the standard library (`HTTPServer`, `JSONDecodeError`, `URLError`).

**Good**

```Python
class HTTPClientFactory: ...


class PDFTextBoxParams: ...


class JSONBody: ...
```

**Bad**

```Python
class HttpClientFactory: ...


class PdfTextBoxParams: ...


class JsonBody: ...
```

In snake_case names — functions, methods, variables, attributes, modules — the abbreviation stays lowercase, like the rest of the name: `http_client_factory`, `pdf_bytes`, `json_body`.

## 13. Don't nest functions<a name="13-dont-nest-functions"></a>

Don't define a function, class or lambda inside another function — in library code, tests or scripts. Nested definitions hide state in closures, can't be tested or reused on their own, and make the outer function harder to read.

Use a module-level function (passing what it needs as arguments), a method on the class that owns the state, a small class with `__call__`, or `functools.partial`.

**Good**

```Python
def _decode(token: str, *, client: Client, issuer: str) -> Claims:
    return client.decode(token, issuer=issuer)


def run(client: Client, issuer: str) -> None:
    decode = partial(_decode, client=client, issuer=issuer)
    ...
```

**Bad**

```Python
def run(client: Client, issuer: str) -> None:
    def decode(token: str) -> Claims:
        return client.decode(token, issuer=issuer)

    ...
```

## 14. Don't silence the type checker<a name="14-dont-silence-the-type-checker"></a>

Don't add `# type: ignore`, `# pyright: ignore[...]` or other checker-specific suppressions, and don't use `typing.cast` — it silences the checker just the same. When a checker complains, fix the types: narrow an annotation, use a precise alias or `TypedDict`, or write a `TypeIs`/`TypeGuard` helper.

If a suppression is genuinely unavoidable, it must come with a comment saying why, and be flagged in review.

One pattern is fine: a test that deliberately passes ill-typed input, to check what an untyped caller gets, routes it through an `Any`-typed local:

```Python
def test_rejects_a_str_cooldown() -> None:
    untyped_caller: Any = "30"  # what an untyped caller could pass

    with pytest.raises(TypeError):
        Client("https://issuer/jwks", cooldown=untyped_caller)
```
