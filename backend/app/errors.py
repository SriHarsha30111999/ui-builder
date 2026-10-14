import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError

logger = logging.getLogger("app")


class AppError(Exception):
    """Expected, user-facing error. Rendered as {"error": {"code", "message", "fields"}}."""

    def __init__(self, status_code: int, code: str, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields or {}


def error_body(code: str, message: str, fields: dict[str, str] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "fields": fields or {}}}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


# MySQL error numbers (first element of the driver exception's args).
DEADLOCK, LOCK_WAIT_TIMEOUT = 1213, 1205


def mysql_error_code(exc: Exception) -> int | None:
    args = getattr(getattr(exc, "orig", None), "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def is_busy(exc: Exception) -> bool:
    return mysql_error_code(exc) in (DEADLOCK, LOCK_WAIT_TIMEOUT)


def _is_connection_problem(exc: Exception) -> bool:
    return isinstance(exc, OperationalError) or (isinstance(exc, DBAPIError) and exc.connection_invalidated)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(error_body(exc.code, exc.message, exc.fields), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        fields: dict[str, str] = {}
        for err in exc.errors():
            loc = [str(p) for p in err["loc"] if p not in ("body", "query", "path")]
            key = _camel(loc[-1]) if loc else "request"
            fields.setdefault(key, str(err["msg"]))
        return JSONResponse(error_body("invalid_request", "The request is not valid.", fields), status_code=422)

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: DBAPIError):
        if is_busy(exc):  # deadlock / lock timeout: transient contention, not an outage
            logger.warning("Database busy (MySQL error %s)", mysql_error_code(exc))
            return JSONResponse(
                error_body("database_busy", "The database is busy. Please try again."), status_code=503
            )
        if _is_connection_problem(exc):
            logger.error("Database unavailable: %s", type(exc.orig).__name__)  # no connection details in logs
            return JSONResponse(
                error_body("database_unavailable", "The database is currently unavailable. Try again shortly."),
                status_code=503,
            )
        logger.exception("Unexpected database error")
        return JSONResponse(error_body("internal_error", "An unexpected error occurred."), status_code=500)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("Unhandled error")
        return JSONResponse(error_body("internal_error", "An unexpected error occurred."), status_code=500)
