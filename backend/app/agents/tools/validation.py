"""Tool 参数校验（Schema 驱动，最小实现）。

为什么需要
----------
LLM 返回的 ``tool_calls.arguments`` 是**模型生成的 JSON**，可能与 Tool 声明的
参数契约不一致（缺参数 / 类型错误 / 多传参数）。若直接把原始 arguments 交给
``Tool.execute()``，轻则抛出让调用方难以理解的 Python 异常，
重则出现静默的不可预期行为。

因此 ``ToolRegistry.execute()`` 在执行前统一调用本模块的
:func:`validate_arguments`，把「不合法的调用」转换成清晰的
:class:`ToolParameterError`（由 AgentRuntime 作为 tool_result 回传给 LLM）。

设计说明
--------
- **Schema 驱动**：规则全部来自 Tool 自身的 ``parameters``
  （OpenAI function calling 的 JSON Schema），Registry 中不写
  ``if tool_name == "calculator"`` 这类硬编码分支。
- **不加依赖**：只实现 function calling 常用子集
  （``type`` / ``properties`` / ``required`` / ``enum`` / ``additionalProperties``），
  不引入 jsonschema 等第三方库。
- **不做递归**：嵌套 object / array 的内部元素不递归校验
  （当前内置 Tool 用不到），需要时可在本模块内扩展。
"""

from collections.abc import Callable

from app.agents.tools.base import Tool, ToolParameterError


def _is_boolean(value: object) -> bool:
    """是否为 bool（JSON boolean）。"""
    return isinstance(value, bool)


def _is_integer(value: object) -> bool:
    """是否为整数（显式排除 bool：Python 中 bool 是 int 的子类）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    """是否为数字（int / float，排除 bool）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: JSON Schema ``type`` → 校验函数（仅覆盖 function calling 常用类型）
_TYPE_CHECKS: dict[str, Callable[[object], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": _is_integer,
    "number": _is_number,
    "boolean": _is_boolean,
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
    "null": lambda value: value is None,
}


def _actual_type(value: object) -> str:
    """返回实际值的类型名（用于错误消息，区分 boolean / integer / number）。"""
    if value is None:
        return "null"
    if _is_boolean(value):
        return "boolean"
    if _is_integer(value):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _quote_names(names: list[str]) -> str:
    """把参数名列表格式化为 ``'a', 'b'`` 形式（用于错误消息）。"""
    return ", ".join(f"'{name}'" for name in names)


def _matches_enum(value: object, allowed: list) -> bool:
    """判断取值是否命中 ``enum``（严格类型比较，避免 ``True == 1`` 之类误判）。"""
    return any(type(value) is type(item) and value == item for item in allowed)


def validate_arguments(tool: Tool, arguments: dict | None) -> dict:
    """按 Tool 自身的 ``parameters`` Schema 校验参数。

    Args:
        tool: 待调用的 Tool 实例（参数规则来自它的 ``parameters``）。
        arguments: LLM 给出的参数（可能为 ``None`` 或非 dict）。

    Returns:
        dict: 校验通过的参数（浅拷贝，可直接传给 ``Tool.execute()``）。

    Raises:
        ToolParameterError: 参数不是 JSON 对象 / 缺少必填参数 /
            参数类型错误 / ``enum`` 取值非法 / 含 Schema 未定义的参数。
    """
    # 0. 参数必须是 JSON 对象
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolParameterError(
            f"工具 '{tool.name}' 参数校验失败：参数必须是 JSON 对象，"
            f"收到 {_actual_type(arguments)}"
        )

    schema = tool.parameters if isinstance(tool.parameters, dict) else {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    required = [
        name
        for name in (schema.get("required") or [])
        if isinstance(name, str)
    ]
    # JSON Schema 规范默认允许额外字段；本项目默认**显式拒绝**，
    # 避免 LLM 多传的参数静默影响工具行为。
    # Tool 若确实需要开放参数，可在自身 Schema 中声明 "additionalProperties": true。
    # 若 Tool 完全没有声明 ``properties``（parameters 为空），
    # 则视为「该 Tool 不接受任何参数」，传入任何参数都会被明确拒绝。
    allow_additional = schema.get("additionalProperties") is True

    # 1. 必填参数
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolParameterError(
            f"工具 '{tool.name}' 参数校验失败：缺少必填参数 "
            f"{_quote_names(missing)}"
        )

    # 2. 未定义参数（显式拒绝，不静默忽略）
    if not allow_additional:
        unknown = [name for name in arguments if name not in properties]
        if unknown:
            available = ", ".join(sorted(properties)) or "无"
            raise ToolParameterError(
                f"工具 '{tool.name}' 参数校验失败：不支持未定义参数 "
                f"{_quote_names(unknown)}（可用参数: {available}）"
            )

    # 3. 逐个参数校验：类型 + enum 取值
    for name, value in arguments.items():
        prop = properties.get(name)
        if not isinstance(prop, dict):
            continue

        expected_type = prop.get("type")
        if isinstance(expected_type, str):
            checker = _TYPE_CHECKS.get(expected_type)
            if checker is not None and not checker(value):
                raise ToolParameterError(
                    f"工具 '{tool.name}' 参数校验失败：参数 '{name}' 类型错误"
                    f"（期望 {expected_type}，收到 {_actual_type(value)}）"
                )

        allowed_values = prop.get("enum")
        if isinstance(allowed_values, list) and allowed_values:
            if not _matches_enum(value, allowed_values):
                allowed_text = " / ".join(
                    str(item) for item in allowed_values
                )
                raise ToolParameterError(
                    f"工具 '{tool.name}' 参数校验失败：参数 '{name}' 取值非法"
                    f"（允许: {allowed_text}，收到 {value!r}）"
                )

    return dict(arguments)


__all__ = ["validate_arguments"]
