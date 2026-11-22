"""References: fields holding another document's id, described and kept sound."""

from typing import Annotated
from uuid import UUID, uuid7

import pytest
from conftest import ROOT, every, kernel_on
from pydantic import BaseModel

from remit import ledger, parties
from remit.kernel import (
    Actor,
    ActorKind,
    Command,
    Context,
    Document,
    Kernel,
    Lifecycle,
    Module,
    Problem,
    References,
    RegistrationError,
    Registry,
    Rejected,
    Store,
)

library = Module("library")

ShelfId = Annotated[UUID, References("library.shelf")]


@library.document
class Shelf(Document):
    """Where books are kept."""

    lifecycle = Lifecycle(initial="in_use")

    label: str


class Move(BaseModel):
    shelf_id: ShelfId


@library.document
class Book(Document):
    """A book, where it is, and where it has been."""

    lifecycle = Lifecycle(initial="held")

    title: str
    shelf_id: ShelfId
    """Where the book is now."""
    returned_to: Annotated[UUID | None, References("library.shelf")] = None
    history: tuple[Move, ...] = ()
    see_also: Annotated[tuple[UUID, ...], References("library.book")] = ()


class ShelveBook(Command[Book]):
    title: str
    shelf_id: ShelfId
    returned_to: UUID | None = None
    history: tuple[Move, ...] = ()
    see_also: tuple[UUID, ...] = ()


class OpenShelfWithBook(Command[Book]):
    label: str
    title: str


@library.command
def add_shelf(command: OpenShelfWithBook, context: Context) -> Book:
    """Put up a shelf and put a book on it."""
    shelf = context.create(Shelf(label=command.label))
    return context.create(Book(title=command.title, shelf_id=shelf.id))


@library.command
def shelve_book(command: ShelveBook, context: Context) -> Book:
    """Put a book on a shelf."""
    return context.create(Book(**command.model_dump()))


LIBRARIAN = Actor(id="librarian", kind=ActorKind.HUMAN, permissions=frozenset({"library.*"}))


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return kernel_on(store, [library])


@pytest.fixture
def shelf(kernel: Kernel) -> UUID:
    book = kernel.execute(OpenShelfWithBook(label="A", title="First"), actor=LIBRARIAN).result
    return book.shelf_id


def test_a_reference_may_lead_to_a_document_written_in_the_same_unit(kernel: Kernel) -> None:
    book = kernel.execute(OpenShelfWithBook(label="B", title="Second"), actor=LIBRARIAN).result
    assert kernel.get(Shelf, book.shelf_id, actor=ROOT).label == "B"


def test_a_document_whose_reference_leads_nowhere_is_refused(kernel: Kernel) -> None:
    nowhere = uuid7()
    with pytest.raises(
        Rejected, match=r"library\.book shelf_id refers to nothing: there is no library\.shelf"
    ) as refused:
        kernel.execute(ShelveBook(title="Lost", shelf_id=nowhere), actor=LIBRARIAN)

    message = f"there is no library.shelf with id {nowhere}"
    assert refused.value.problems == (Problem(("shelf_id",), message, "library.book"),)
    assert every(kernel, Book, title="Lost") == []


@pytest.mark.parametrize(
    ("shape", "path"),
    [
        ("optional", ("returned_to",)),
        ("nested", ("history", 1, "shelf_id")),
        ("tuple", ("see_also", 0)),
    ],
)
def test_references_are_followed_into_optional_fields_tuples_and_nested_models(
    kernel: Kernel, shelf: UUID, shape: str, path: tuple[str | int, ...]
) -> None:
    nowhere = uuid7()
    changes: dict[str, dict[str, object]] = {
        "optional": {"returned_to": nowhere},
        "nested": {"history": (Move(shelf_id=shelf), Move(shelf_id=nowhere))},
        "tuple": {"see_also": (nowhere,)},
    }
    command = ShelveBook.model_validate({"title": "T", "shelf_id": shelf, **changes[shape]})

    with pytest.raises(Rejected) as refused:
        kernel.execute(command, actor=LIBRARIAN)
    [problem] = refused.value.problems
    assert problem.path == path


def test_sound_references_of_every_shape_are_written(kernel: Kernel, shelf: UUID) -> None:
    [first] = every(kernel, Book, title="First")
    command = ShelveBook(
        title="Second",
        shelf_id=shelf,
        returned_to=shelf,
        history=(Move(shelf_id=shelf),),
        see_also=(first.id,),
    )
    assert kernel.execute(command, actor=LIBRARIAN).result.see_also == (first.id,)


def test_the_registry_describes_what_a_reference_leads_to() -> None:
    described = Registry([library]).describe()["library"]
    book = described["documents"]["library.book"]["schema"]
    assert book["properties"]["shelf_id"]["x-references"] == "library.shelf"
    assert book["properties"]["shelf_id"]["description"] == "Where the book is now."
    assert book["properties"]["returned_to"]["x-references"] == "library.shelf"
    assert book["properties"]["see_also"]["x-references"] == "library.book"
    assert book["$defs"]["Move"]["properties"]["shelf_id"]["x-references"] == "library.shelf"
    shelving = described["commands"]["library.shelve_book"]["input"]["properties"]
    assert shelving["shelf_id"]["x-references"] == "library.shelf"


def test_standard_modules_describe_their_references() -> None:
    described = Registry([ledger.module, parties.module]).describe()
    contact = described["parties"]["documents"]["parties.contact"]["schema"]["properties"]
    assert contact["party_id"]["x-references"] == "parties.party"
    entry = described["ledger"]["documents"]["ledger.journal_entry"]["schema"]
    assert entry["$defs"]["Posting"]["properties"]["account"]["x-references"] == "ledger.account"
    assert entry["properties"]["reverses"]["x-references"] == "ledger.journal_entry"


def cataloguing(*, requires: tuple[Module, ...]) -> Module:
    """A module whose cards refer to library books."""
    catalogue = Module("catalogue", requires=requires)

    @catalogue.document
    class Card(Document):
        """An index card for a book."""

        lifecycle = Lifecycle(initial="filed")

        book_id: Annotated[UUID, References("library.book")]

    return catalogue


def test_a_reference_must_lead_to_a_document_of_the_module_or_one_it_requires() -> None:
    with pytest.raises(RegistrationError, match=r"Card\.book_id refers to library\.book, which"):
        Registry([library, cataloguing(requires=())])
    assert "catalogue" in Registry([cataloguing(requires=(library,))]).modules


def test_a_reference_to_a_document_nobody_declares_is_refused() -> None:
    catalogue = Module("catalogue")

    class Find(Command[None]):
        book_id: Annotated[UUID, References("library.boook")]

    @catalogue.command
    def find(command: Find, context: Context) -> None:
        """Look a book up."""

    with pytest.raises(RegistrationError, match=r"Find\.book_id refers to library\.boook"):
        Registry([catalogue])


def test_a_mark_on_part_of_a_type_is_refused() -> None:
    catalogue = Module("catalogue", requires=(library,))

    @catalogue.document
    class Card(Document):
        """An index card for a book."""

        lifecycle = Lifecycle(initial="filed")

        book_id: Annotated[UUID, References("library.book")] | None = None

    with pytest.raises(RegistrationError, match=r"Card\.book_id marks part of its type"):
        Registry([catalogue])


@pytest.mark.parametrize("annotation", [str, tuple[str, ...], UUID | str, list[UUID | int]])
def test_a_mark_on_a_field_that_holds_no_ids_is_refused(annotation: object) -> None:
    catalogue = Module("catalogue", requires=(library,))

    class Find(Command[None]):
        book: Annotated[annotation, References("library.book")]  # pyright: ignore[reportInvalidTypeForm]

    @catalogue.command
    def find(command: Find, context: Context) -> None:
        """Look a book up."""

    with pytest.raises(RegistrationError, match=r"Find\.book is marked as a reference but"):
        Registry([catalogue])


class LendBook(Command[Book]):
    shelf_id: ShelfId
    book_id: Annotated[UUID, References("library.book", where={"shelf_id": "shelf_id"})]


class SeeAlso(Command[Book]):
    book_id: Annotated[UUID, References("library.book")]
    other_id: Annotated[
        UUID | None, References("library.book", where={"shelf_id": "book_id.shelf_id"})
    ] = None


def test_the_registry_describes_how_a_reference_is_narrowed() -> None:
    lending = Module("lending", requires=(library,))

    @lending.command
    def lend_book(command: LendBook, context: Context) -> Book:
        """Lend a book from a shelf."""
        raise NotImplementedError

    @lending.command
    def see_also(command: SeeAlso, context: Context) -> Book:
        """Suggest a book shelved beside another."""
        raise NotImplementedError

    commands = Registry([library, lending]).describe()["lending"]["commands"]
    lent = commands["lending.lend_book"]["input"]["properties"]
    assert lent["book_id"]["x-where"] == {"shelf_id": "shelf_id"}
    seen = commands["lending.see_also"]["input"]["properties"]
    assert seen["other_id"]["x-where"] == {"shelf_id": "book_id.shelf_id"}
    assert "x-where" not in seen["book_id"]


@pytest.mark.parametrize(
    ("where", "refused"),
    [
        ({"label": "shelf_id"}, r"label, which library\.book does not hold"),
        ({"shelf_id": "shelf"}, r"shelf, but Narrowed has no field shelf"),
        ({"shelf_id": "note.shelf_id"}, r"note\.shelf_id, but note does not hold the id of"),
        ({"shelf_id": "shelf_id.label.x"}, r"shelf_id\.label\.x, but a path goes into one"),
        ({"shelf_id": "shelf_id.title"}, r"shelf_id\.title, but library\.shelf has no field"),
    ],
)
def test_a_narrowing_that_cannot_be_followed_is_refused(
    where: dict[str, str], refused: str
) -> None:
    lending = Module("lending", requires=(library,))

    class Narrowed(Command[Book]):
        shelf_id: ShelfId
        note: str
        book_id: Annotated[UUID, References("library.book", where=where)]

    @lending.command
    def lend_book(command: Narrowed, context: Context) -> Book:
        """Lend a book from a shelf."""
        raise NotImplementedError

    with pytest.raises(RegistrationError, match=rf"Narrowed\.book_id is narrowed by {refused}"):
        Registry([library, lending])
