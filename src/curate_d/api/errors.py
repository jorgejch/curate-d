"""Errors the control plane's backends raise, and the responses they become.

A backend says what went wrong in domain terms; the mapping to a status code lives here
once, so every endpoint answers an unknown identifier or a refused change the same way.
"""

from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class Problem(BaseModel):
    """Why a request was not carried out."""

    detail: str


class NotFoundError(LookupError):
    """The resource a request names does not exist."""

    def __init__(self, what: str, identifier: object) -> None:
        """Name the kind of resource and the identifier that matched nothing."""
        super().__init__(f"no {what} {identifier}")


class RejectedError(ValueError):
    """The request was understood and refused; nothing changed."""


class ConflictError(RuntimeError):
    """The request conflicts with the current state of what it names; nothing changed."""


class UnavailableError(RuntimeError):
    """What the request needs is not configured or reachable in this deployment."""


NOT_FOUND: Final = status.HTTP_404_NOT_FOUND
REJECTED: Final = status.HTTP_422_UNPROCESSABLE_CONTENT
CONFLICT: Final = status.HTTP_409_CONFLICT
UNAVAILABLE: Final = status.HTTP_503_SERVICE_UNAVAILABLE

type Responses = dict[int | str, dict[str, object]]


def problems(*codes: int) -> Responses:
    """Document `codes` as answered with a `Problem`."""
    return {code: {"model": Problem} for code in codes}


type Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def _answer(code: int) -> Handler:
    """A handler answering `code` with the error's message."""

    async def handle(_request: Request, error: Exception) -> JSONResponse:
        """Answer with the error's message."""
        return JSONResponse(status_code=code, content=Problem(detail=str(error)).model_dump())

    return handle


def install(app: FastAPI) -> None:
    """Answer each backend error with its status code and a `Problem`."""
    app.add_exception_handler(NotFoundError, _answer(NOT_FOUND))
    app.add_exception_handler(RejectedError, _answer(REJECTED))
    app.add_exception_handler(ConflictError, _answer(CONFLICT))
    app.add_exception_handler(UnavailableError, _answer(UNAVAILABLE))
