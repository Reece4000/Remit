"""Documents that are lines of another, as an item on an order is, and say so.

A screen shows a document's lines inside it, so a line names the field that refers to
the document it belongs to.
"""

from typing import Annotated
from uuid import UUID

import pytest

from remit.kernel import Document, Lifecycle, Module, References, RegistrationError, Registry

kitchen = Module("kitchen")

RecipeId = Annotated[UUID, References("kitchen.recipe")]


@kitchen.document
class Recipe(Document):
    """A dish, and how to make it."""

    lifecycle = Lifecycle(initial="written")

    title: str


@kitchen.document
class Ingredient(Document):
    """What a recipe needs, and how much."""

    lifecycle = Lifecycle(initial="listed")
    part_of = "recipe_id"

    recipe_id: RecipeId
    food: str
    grams: int


def test_the_registry_describes_what_a_line_is_part_of() -> None:
    documents = Registry([kitchen]).describe()["kitchen"]["documents"]

    assert documents["kitchen.ingredient"]["part_of"] == {
        "document": "kitchen.recipe",
        "field": "recipe_id",
    }
    assert documents["kitchen.recipe"]["part_of"] is None


class Garnish(Document):
    """A garnish, said to be part of a recipe through a field it does not have."""

    lifecycle = Lifecycle(initial="listed")
    part_of = "recipe"

    recipe_id: RecipeId


class Substitute(Document):
    """Something to use instead, for a recipe or for none."""

    lifecycle = Lifecycle(initial="listed")
    part_of = "recipe_id"

    recipe_id: Annotated[UUID | None, References("kitchen.recipe")] = None


class Step(Document):
    """A step, said to be part of the step it follows."""

    lifecycle = Lifecycle(initial="written")
    part_of = "after"

    after: Annotated[UUID, References("pantry.step")]


@pytest.mark.parametrize(
    ("line", "said"),
    [
        (Garnish, r"pantry\.garnish declares part_of = 'recipe', but has no such field"),
        (
            Substitute,
            r"pantry\.substitute declares part_of = 'recipe_id', but Substitute\.recipe_id does "
            r"not hold a single document's id",
        ),
        (
            Step,
            r"pantry\.step declares part_of = 'after', which refers to pantry\.step itself; a "
            r"line is part of another kind of document",
        ),
    ],
)
def test_a_line_names_one_document_of_another_kind_as_its_whole(
    line: type[Document], said: str
) -> None:
    pantry = Module("pantry", requires=(kitchen,))
    pantry.document(line)

    with pytest.raises(RegistrationError, match=said):
        Registry([pantry])
