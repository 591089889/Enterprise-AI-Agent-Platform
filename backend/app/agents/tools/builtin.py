"""内置 Tool。

当前内置两个「简单、确定性强、无外部依赖」的工具：

- :class:`CalculatorTool`：四则运算（AST 白名单求值，不使用 eval/exec）；
- :class:`DateTimeTool`：返回当前日期 / 时间（仅标准库 datetime）。

安全说明（重要）
----------------
Calculator **不使用** ``eval()`` / ``exec()``。

实现方式：用 ``ast.parse(..., mode="eval")`` 解析表达式，
然后**白名单式**递归求值，只允许：

    数字（int / float）、+、-、*、/、括号、一元正负号

明确拒绝：

    变量名、函数调用、属性访问、下标、import、
    文件/网络/系统操作，以及任何其他 Python 语法节点。

因此 ``__import__("os")``、``open(...)``、``eval(...)``、
``exec(...)`` 等都会被拒绝，不会执行任意 Python 代码。
"""

import ast
import operator
from datetime import datetime, timedelta

from app.agents.tools.base import Tool, ToolExecutionError

#: 表达式长度上限（防止超长表达式带来的性能问题）
MAX_EXPRESSION_LENGTH = 200

#: 允许的二元运算符白名单
_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

#: 允许的一元运算符白名单（仅表示正负号）
_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.AST) -> float:
    """白名单式递归求值 AST 节点。

    Args:
        node: AST 节点。

    Returns:
        float: 节点求值结果。

    Raises:
        ToolExecutionError: 出现白名单之外的语法节点，或除数为 0。
    """
    # 1. 数字字面量
    if isinstance(node, ast.Constant):
        # 注意：bool 是 int 的子类，必须显式拒绝（True/False 不是数字运算）
        if isinstance(node.value, bool) or not isinstance(
            node.value, (int, float)
        ):
            raise ToolExecutionError(
                f"计算器只支持数字，收到: {node.value!r}"
            )
        return float(node.value)

    # 2. 二元运算：+ - * /
    if isinstance(node, ast.BinOp):
        op_func = _BINARY_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ToolExecutionError(
                f"计算器不支持该运算符: {type(node.op).__name__}"
                "（仅支持 + - * /）"
            )
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_func is operator.truediv and right == 0:
            raise ToolExecutionError("除数不能为 0")
        return op_func(left, right)

    # 3. 一元运算：+x / -x
    if isinstance(node, ast.UnaryOp):
        unary_func = _UNARY_OPERATORS.get(type(node.op))
        if unary_func is None:
            raise ToolExecutionError(
                f"计算器不支持该一元运算符: {type(node.op).__name__}"
            )
        return unary_func(_eval_node(node.operand))

    # 4. 其余全部拒绝（变量名 / 函数调用 / 属性 / 下标 / 推导式 ...）
    raise ToolExecutionError(
        f"表达式包含不允许的内容: {type(node).__name__}"
        "（仅支持数字、+ - * / 与括号）"
    )


def safe_eval(expression: str) -> float:
    """安全求值一个算术表达式。

    Args:
        expression: 算术表达式，如 ``"2 + 3 * 4"``。

    Returns:
        float: 求值结果。

    Raises:
        ToolExecutionError: 表达式为空 / 过长 / 语法错误 / 包含非法内容。
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ToolExecutionError("参数 'expression' 必须是非空字符串")

    expression = expression.strip()
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ToolExecutionError(
            f"表达式过长（最多 {MAX_EXPRESSION_LENGTH} 个字符）"
        )

    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ToolExecutionError(
            f"表达式语法错误: {expression}"
        ) from exc

    return _eval_node(parsed.body)


def _format_number(value: float) -> str:
    """把数值格式化为简洁字符串（整数不带 .0）。"""
    if value == int(value):
        return str(int(value))
    return str(value)


class CalculatorTool(Tool):
    """计算器工具：执行基础四则运算。

    示例::

        输入: {"expression": "2 + 3 * 4"}
        输出: "14"

    该工具：
    - 不需要 API Key；
    - 不依赖网络；
    - 结果确定，易于测试与演示 Tool Calling。
    """

    name = "calculator"
    description = (
        "计算数学表达式并返回结果。"
        "仅支持数字、四则运算 (+ - * /) 与括号，例如 '2 + 3 * 4'、'(10 - 2) / 4'。"
        "当用户需要精确的算术计算时调用本工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 '2 + 3 * 4'",
            }
        },
        "required": ["expression"],
    }

    async def execute(self, arguments: dict) -> str:
        """执行计算。

        Args:
            arguments: 必须包含字符串字段 ``expression``。

        Returns:
            str: 计算结果文本（如 ``"14"``）。

        Raises:
            ToolExecutionError: 参数缺失 / 类型错误 / 表达式非法。
        """
        if not isinstance(arguments, dict):
            raise ToolExecutionError("calculator 参数必须是 JSON 对象")

        if "expression" not in arguments:
            raise ToolExecutionError(
                "calculator 缺少必填参数 'expression'"
            )

        expression = arguments["expression"]
        if not isinstance(expression, str):
            raise ToolExecutionError(
                "calculator 参数 'expression' 必须是字符串，"
                f"收到: {type(expression).__name__}"
            )

        return _format_number(safe_eval(expression))


# ----------------------------------------------------------------------
# DateTimeTool
# ----------------------------------------------------------------------
#: 支持的输出格式 → strftime 模板
_DATETIME_FORMATS: dict[str, str] = {
    "date": "%Y-%m-%d",
    "time": "%H:%M:%S",
    "datetime": "%Y-%m-%d %H:%M:%S",
}

#: 默认输出格式
DEFAULT_DATETIME_FORMAT = "datetime"


class DateTimeTool(Tool):
    """日期时间工具：返回（可按天数偏移的）当前日期 / 时间。

    示例::

        输入: {}                                    → "2026-09-16 15:04:05"
        输入: {"format": "date"}                    → "2026-09-16"
        输入: {"format": "date", "offset_days": 1}  → 明天的日期

    该工具：
    - 不需要 API Key；
    - 不依赖网络 / 数据库 / 第三方库（仅标准库 ``datetime``）；
    - 无需外部状态，便于与 Calculator 一起演示「多 Tool」场景。

    说明：使用服务器本地时间（naive datetime），不引入时区处理，
    避免为演示额外引入 tzdata 等依赖。
    """

    name = "datetime"
    description = (
        "获取当前日期或时间，支持按天数偏移（如昨天 / 明天）。"
        "当用户询问今天几号、现在几点、明天 / 昨天是哪天时调用本工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "enum": list(_DATETIME_FORMATS),
                "description": (
                    "返回格式：date='2026-09-16'，time='15:04:05'，"
                    "datetime='2026-09-16 15:04:05'（默认 datetime）"
                ),
            },
            "offset_days": {
                "type": "integer",
                "description": (
                    "相对今天的日期偏移天数：-1 表示昨天，1 表示明天（默认 0）"
                ),
            },
        },
        "required": [],
    }

    async def execute(self, arguments: dict) -> str:
        """返回格式化后的日期 / 时间字符串。

        Args:
            arguments: 可选参数 ``format`` / ``offset_days``。

        Returns:
            str: 格式化后的日期时间文本。

        Raises:
            ToolExecutionError: 参数类型错误 / ``format`` 取值非法 /
                ``format='time'`` 时使用了 ``offset_days``。
        """
        if not isinstance(arguments, dict):
            raise ToolExecutionError("datetime 参数必须是 JSON 对象")

        # format（可选，默认 datetime）
        date_format = arguments.get("format", DEFAULT_DATETIME_FORMAT)
        if not isinstance(date_format, str):
            raise ToolExecutionError(
                "datetime 参数 'format' 必须是字符串，"
                f"收到: {type(date_format).__name__}"
            )
        date_format = date_format.strip().lower() or DEFAULT_DATETIME_FORMAT
        pattern = _DATETIME_FORMATS.get(date_format)
        if pattern is None:
            allowed = ", ".join(_DATETIME_FORMATS)
            raise ToolExecutionError(
                f"datetime 参数 'format' 仅支持: {allowed}，收到: {date_format!r}"
            )

        # offset_days（可选，默认 0）
        offset_days = arguments.get("offset_days", 0)
        if isinstance(offset_days, bool) or not isinstance(offset_days, int):
            raise ToolExecutionError(
                "datetime 参数 'offset_days' 必须是整数，"
                f"收到: {type(offset_days).__name__}"
            )
        if offset_days and date_format == "time":
            raise ToolExecutionError(
                "datetime 参数 'offset_days' 仅在 format 为 "
                "'date' / 'datetime' 时可用"
            )

        moment = datetime.now() + timedelta(days=offset_days)
        return moment.strftime(pattern)
