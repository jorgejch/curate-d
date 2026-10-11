"""The served OpenAPI document is valid OpenAPI, and every operation is typed."""

from collections.abc import Mapping
from typing import cast

from openapi_spec_validator import validate

from tests.unit.api.fakes import client

type Json = Mapping[str, object]


def _schema() -> Json:
    """The schema the app serves."""
    with client() as http:
        response = http.get("/openapi.json")
    assert response.status_code == 200
    return cast(Json, response.json())


def _object(value: object) -> Json:
    """`value` as a JSON object; an empty one if it is anything else."""
    return cast(Json, value) if isinstance(value, Mapping) else {}


def test_the_served_schema_is_valid_openapi() -> None:
    """The document validates against the OpenAPI specification."""
    validate(_schema())


def test_every_successful_response_declares_its_body() -> None:
    """No operation answers 2xx with an untyped body."""
    untyped = [
        f"{method.upper()} {path} {code}"
        for path, operations in _object(_schema()["paths"]).items()
        for method, operation in _object(operations).items()
        for code, response in _object(_object(operation)["responses"]).items()
        if code.startswith("2")
        and code != "204"
        and not _object(_object(_object(response).get("content")).get("application/json")).get(
            "schema"
        )
    ]
    assert untyped == []
