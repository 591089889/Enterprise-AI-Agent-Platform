"""FastAPI 全局异常处理器（平台唯一的 HTTP 错误边界）。

职责
----

把平台内部**各种来源**的异常，统一转换成同一种 JSON 错误响应::

    业务异常 / HTTPException / 参数校验失败 / 未预期异常
                          ↓
                  exception_handlers
                          ↓
        {"error": {"code": "...", "message": "..."}}

处理器注册在 ``main.py`` 的 FastAPI app 上（见
:func:`register_exception_handlers`），业务代码（Service / Runtime /
Provider / Repository）不需要也不应该知道 HTTP 的存在。

处理器清单
----------

============================  ==========  ==============================
异常                          状态码      错误码
============================  ==========  ==============================
``AppException`` 及其子类      异常自带    ``exc.code``
``HTTPException``（Starlette）  异常自带    按状态码映射（见下表）
``RequestValidationError``     422         ``VALIDATION_ERROR``
``Exception``（未预期）         500         ``INTERNAL_ERROR``
============================  ==========  ==============================

设计说明
--------

- **不改变异常语义**：HTTP 状态码（401 / 403 / 404 / 409 / 422 / 500）
  全部保持原有语义，本模块只统一「响应体结构」与「错误码」。
- **保留 headers**：``HTTPException`` 自带的响应头（如 401 的
  ``WWW-Authenticate: Bearer``）原样透传。
- **不泄露内部信息**：响应体只包含固定文案与安全 message；
  traceback / SQL / 密钥 / 文件路径 / 上游原始错误只写入服务端日志。
- **必须同时注册 ``HTTPException`` 与 ``StarletteHTTPException``**：
  FastAPI 与 Starlette 各自预注册了 HTTPException 处理器
  （异常查找按 ``__mro__`` 命中子类），只注册基类会导致
  FastAPI 抛出的 ``HTTPException`` 仍走默认 ``{"detail": ...}`` 格式。
"""

import logging

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException, build_error_payload

logger = logging.getLogger(__name__)

#: 参数校验失败（HTTP 422）错误码
VALIDATION_ERROR_CODE = "VALIDATION_ERROR"

#: 未预期异常（HTTP 500）错误码
INTERNAL_ERROR_CODE = "INTERNAL_ERROR"

#: 未预期异常对外的固定文案（不暴露 Python 异常信息）
INTERNAL_ERROR_MESSAGE = "服务器内部错误，请稍后重试"

#: 参数校验失败时的兜底文案
VALIDATION_ERROR_MESSAGE = "请求参数校验失败"

#: HTTP 状态码 → 稳定错误码（仅覆盖当前平台实际会出现的状态码）
STATUS_CODE_MAP: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "PERMISSION_DENIED",
    404: "RESOURCE_NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "BUSINESS_CONFLICT",
    422: VALIDATION_ERROR_CODE,
    429: "RATE_LIMITED",
    502: "UPSTREAM_SERVICE_ERROR",
    503: "SERVICE_UNAVAILABLE",
}

#: 状态码未在 :data:`STATUS_CODE_MAP` 中时的兜底错误码
DEFAULT_HTTP_ERROR_CODE = "HTTP_ERROR"

#: 最多展示几条参数校验错误（避免响应体过长 / 泄露过多字段结构）
MAX_VALIDATION_DETAILS = 1


def code_for_status(status_code: int) -> str:
    """把 HTTP 状态码映射为稳定错误码。

    Args:
        status_code: HTTP 状态码（如 401 / 403 / 404）。

    Returns:
        str: 与 :data:`STATUS_CODE_MAP` 对应的错误码；
        未收录的状态码返回 :data:`DEFAULT_HTTP_ERROR_CODE`（不编造语义）。
    """
    return STATUS_CODE_MAP.get(status_code, DEFAULT_HTTP_ERROR_CODE)


def _request_label(request: object) -> str:
    """返回用于服务端日志的请求简述（``METHOD path``）。

    仅用于日志，不进入响应体；``request`` 缺失时返回 ``"-"``，
    保证处理器本身不会因为取不到请求信息而抛异常。
    """
    method = getattr(request, "method", None)
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    if not method or not path:
        return "-"
    return f"{method} {path}"


def _format_validation_message(exc: RequestValidationError) -> str:
    """把 Pydantic 校验错误压缩为一条安全、可读的 message。

    只取前 :data:`MAX_VALIDATION_DETAILS` 条错误的 ``loc`` 与 ``msg``：

    - ``loc`` 只包含字段名（如 ``body.email``），不是用户输入值；
    - **不包含** ``input`` / ``ctx``，避免把请求体原样回显
      （例如用户误传的密码、Token）。

    Args:
        exc: FastAPI 抛出的 ``RequestValidationError``。

    Returns:
        str: 形如 ``请求参数校验失败：body.email: Field required`` 的文案。
    """
    errors = exc.errors()
    if not errors:
        return VALIDATION_ERROR_MESSAGE

    parts: list[str] = []
    for error in errors[:MAX_VALIDATION_DETAILS]:
        location = ".".join(
            str(item) for item in error.get("loc", ()) if item is not None
        )
        message = str(error.get("msg", "")).strip()
        if location and message:
            parts.append(f"{location}: {message}")
        elif location:
            parts.append(location)
        elif message:
            parts.append(message)

    if not parts:
        return VALIDATION_ERROR_MESSAGE

    text = f"{VALIDATION_ERROR_MESSAGE}：{'；'.join(parts)}"
    remaining = len(errors) - MAX_VALIDATION_DETAILS
    if remaining > 0:
        text += f"（另有 {remaining} 项未显示）"
    return text


async def app_exception_handler(
    request: object, exc: AppException
) -> JSONResponse:
    """``AppException`` 及其子类的全局处理器。

    覆盖「Router 未显式 try/except 映射」的业务异常路径
    （如依赖注入中抛出的 ``PermissionDeniedException``）。

    - status_code 直接取 ``exc.status_code``（401 / 403 / 404 / 409 / 502）；
    - code        取 ``exc.code``（类属性，稳定不变）；
    - 5xx（如 502 上游异常）额外写服务端日志便于排查，
      日志内容不会进入响应体。
    """
    if exc.status_code >= 500:
        logger.error(
            "business exception (status=%s code=%s) on %s: %s",
            exc.status_code,
            exc.code,
            _request_label(request),
            exc,
        )

    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_payload(exc.code, exc.message),
    )


async def http_exception_handler(
    request: object, exc: StarletteHTTPException
) -> JSONResponse:
    """``HTTPException`` 的全局处理器。

    覆盖两类来源：

    1. Router 层显式抛出（如 ``detail=f"用户 ID={user_id} 不存在"``）；
    2. 框架层自动抛出（如未匹配路由的 404、方法不允许的 405）。

    状态码与响应头保持原样（401 的 ``WWW-Authenticate: Bearer`` 不丢失），
    只把 ``{"detail": ...}`` 统一为 ``{"error": {...}}``。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_payload(
            code_for_status(exc.status_code),
            str(exc.detail),
        ),
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: object, exc: RequestValidationError
) -> JSONResponse:
    """请求参数校验处理器（保持 HTTP 422 语义）。

    只替换响应体结构，不做任何「把 422 改成 400」之类的语义改写。
    """
    return JSONResponse(
        status_code=422,
        content=build_error_payload(
            VALIDATION_ERROR_CODE,
            _format_validation_message(exc),
        ),
    )


async def unhandled_exception_handler(
    request: object, exc: Exception
) -> JSONResponse:
    """未预期异常的兜底处理器（HTTP 500）。

    - 对外只返回固定文案，绝不返回 traceback / 异常字符串；
    - 对内用 ``logger.exception`` 记录完整堆栈（服务端可见）。

    注意：Starlette 的 ``ServerErrorMiddleware`` 在返回该响应后仍会向上
    抛出原异常（便于服务器记录日志 / 测试客户端感知），因此这不是
    「吞掉异常」，而是「不让异常细节泄露给客户端」。
    """
    logger.exception(
        "unhandled exception on %s: %s: %s",
        _request_label(request),
        type(exc).__name__,
        exc,
    )
    return JSONResponse(
        status_code=500,
        content=build_error_payload(
            INTERNAL_ERROR_CODE,
            INTERNAL_ERROR_MESSAGE,
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """把全部全局异常处理器注册到 FastAPI app（在 ``main.py`` 调用一次）。

    集中注册，保证「平台只有一处 HTTP 错误边界」。

    关于同时注册 ``HTTPException`` 与 ``StarletteHTTPException``：
    两者是不同的类（后者是前者的父类），Starlette 按 ``__mro__`` 查找
    处理器，只注册父类时子类自带的默认处理器会先命中，因此两个都要覆盖。

    Args:
        app: FastAPI 应用实例。
    """
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(
        RequestValidationError, validation_exception_handler
    )
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "DEFAULT_HTTP_ERROR_CODE",
    "INTERNAL_ERROR_CODE",
    "INTERNAL_ERROR_MESSAGE",
    "MAX_VALIDATION_DETAILS",
    "STATUS_CODE_MAP",
    "VALIDATION_ERROR_CODE",
    "VALIDATION_ERROR_MESSAGE",
    "app_exception_handler",
    "code_for_status",
    "http_exception_handler",
    "register_exception_handlers",
    "unhandled_exception_handler",
    "validation_exception_handler",
]
