"""统一业务异常体系。

本模块定义平台所有业务异常，**不依赖 FastAPI / HTTPException / Request**。
业务层（Service / Runtime / Provider）只负责抛出这些异常，
由 ``app.core.exception_handlers`` 中的全局处理器统一转换为 HTTP 响应。

异常层次结构
------------

  AppException (基类：message + status_code + code)
    ├── UnauthorizedException      (401 / AUTHENTICATION_REQUIRED)
    ├── PermissionDeniedException  (403 / PERMISSION_DENIED)
    ├── ResourceNotFoundException  (404 / RESOURCE_NOT_FOUND)
    ├── BusinessConflictException  (409 / BUSINESS_CONFLICT)
    └── UpstreamServiceException   (502 / UPSTREAM_SERVICE_ERROR)

统一错误响应结构
----------------

所有错误路径（业务异常 / ``HTTPException`` / 参数校验 / 未预期异常）
最终都由 ``exception_handlers.py`` 输出同一结构::

    {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "会话 ID=42 不存在"
        }
    }

- ``code``    ：稳定错误码，前端按它分支，不去解析 message 文案；
- ``message`` ：面向用户的说明，**不含** traceback / SQL / 密钥 / 文件路径。


使用方式
--------

**Service / Runtime 层**（框架无关）::

    from app.core.exceptions import BusinessConflictException

    if existing_user:
        raise BusinessConflictException("用户名已被占用")

**Router 层**（可选：显式映射为 HTTPException，以便附加自定义 header）::

    try:
        result = await service.create_user(data)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

即使 Router 不做映射，``main.py`` 注册的全局处理器也会把 ``AppException``
转换成同样的 JSON 结构（见
``app.core.exception_handlers.register_exception_handlers``）。
"""


class AppException(Exception):
    """应用基础业务异常。

    所有业务异常由此派生，携带 HTTP 状态码与稳定错误码，
    由 ``app.core.exception_handlers`` 统一映射为 HTTP 响应。

    Attributes:
        message: 人类可读的错误信息（面向用户，不含内部细节）。
        status_code: 对应的 HTTP 状态码（如 404、409、403）。
        code: 稳定错误码（类属性），前端按它分支处理。
            子类通过覆盖类属性声明自己的错误码；基类默认 ``APP_ERROR``，
            仅用于「通用业务错误」这一兜底场景。
    """

    #: 稳定错误码（子类覆盖；前端只依赖该值，不解析 message 文案）
    code: str = "APP_ERROR"

    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ResourceNotFoundException(AppException):
    """资源未找到异常 (404)。

    当请求的资源（如用户、文档）在数据库中不存在时抛出。

    示例::

        raise ResourceNotFoundException(f"用户 ID={user_id} 不存在")
    """

    #: 稳定错误码
    code = "RESOURCE_NOT_FOUND"

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)


class BusinessConflictException(AppException):
    """业务冲突异常 (409)。

    当操作与现有数据发生冲突时抛出（如用户名/邮箱重复）。

    示例::

        raise BusinessConflictException(f"用户名 '{username}' 已被占用")
    """

    #: 稳定错误码
    code = "BUSINESS_CONFLICT"

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409)


class PermissionDeniedException(AppException):
    """权限不足异常 (403)。

    当已认证的用户尝试执行无权限的操作时抛出。

    示例::

        raise PermissionDeniedException("只能修改自己的账户")
    """

    #: 稳定错误码
    code = "PERMISSION_DENIED"

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=403)


class UnauthorizedException(AppException):
    """未认证异常 (401)。

    当用户凭证无效或缺失时抛出。

    ``code`` 与认证依赖（``get_current_user``）的 401 保持一致，
    即同一 HTTP 状态码只有一个错误码，前端无需区分来源。

    示例::

        raise UnauthorizedException("Invalid email or password")
    """

    #: 稳定错误码
    code = "AUTHENTICATION_REQUIRED"

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=401)


class UpstreamServiceException(AppException):
    """上游依赖异常 (502)。

    当外部依赖（当前为 LLM Provider 等上游服务）不可用或返回异常时，
    在 API 边界由 Router 抛出。使用 502 而不是 500 的原因：

    - 502 Bad Gateway：请求本身没问题，是**上游**返回错误 / 不可达；
    - 500 Internal Server Error：保留给「本服务自身的代码缺陷」，
      由未预期异常处理器兜底。

    安全约束：``message`` 必须是面向用户的固定文案，
    **不得**透传上游原始错误（可能包含 URL / 响应体 / Key 等内部信息）。

    示例::

        raise UpstreamServiceException("AI 模型服务暂时不可用，请稍后重试")
    """

    #: 稳定错误码
    code = "UPSTREAM_SERVICE_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=502)


#: 统一错误响应中最外层字段名
ERROR_FIELD = "error"

#: 错误对象内部字段名：稳定错误码
ERROR_CODE_FIELD = "code"

#: 错误对象内部字段名：面向用户的错误说明
ERROR_MESSAGE_FIELD = "message"


def build_error_payload(code: str, message: str) -> dict:
    """构造统一错误响应体（纯数据，不依赖任何 HTTP 框架）。

    HTTP 处理器（``app.core.exception_handlers``）与需要返回
    ``JSONResponse`` 的位置统一调用本函数，保证结构永远只有一个来源。

    Args:
        code: 稳定错误码（如 ``RESOURCE_NOT_FOUND``）。
        message: 面向用户的错误说明（不含内部细节）。

    Returns:
        dict: 形如::

            {"error": {"code": "RESOURCE_NOT_FOUND", "message": "..."}}
    """
    return {
        ERROR_FIELD: {
            ERROR_CODE_FIELD: code,
            ERROR_MESSAGE_FIELD: message,
        }
    }


__all__ = [
    "AppException",
    "BusinessConflictException",
    "ERROR_CODE_FIELD",
    "ERROR_FIELD",
    "ERROR_MESSAGE_FIELD",
    "PermissionDeniedException",
    "ResourceNotFoundException",
    "UnauthorizedException",
    "UpstreamServiceException",
    "build_error_payload",
]
