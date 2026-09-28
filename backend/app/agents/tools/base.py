"""Tool 抽象接口。

Tool 的职责只有一个：

    把一个外部能力（计算 / 查询 / 动作）包装成 Agent 可以调用的标准接口。

设计说明
--------
- ``Tool`` 不依赖 FastAPI / Database / LLMProvider / AgentRuntime。
- ``get_schema()`` 描述"这个工具叫什么、做什么、需要什么参数"，
  用于告诉 LLM 可以调用什么工具（OpenAI function calling 格式）。
- ``execute()`` 是工具的唯一执行入口，由 ``ToolRegistry`` 统一调度。
- 参数规则（有哪些参数 / 是什么类型 / 哪些必填）由 ``parameters``
  （JSON Schema）表达，由 ``ToolRegistry`` 在执行前统一校验
  （见 ``tools/validation.py``）：参数规则属于具体 Tool，调度与校验流程属于 Registry。
- 异常统一继承 :class:`ToolError`，由 AgentRuntime 捕获后
  作为 tool_result 明确回传给 LLM（不静默吞掉）。
"""

from abc import ABC, abstractmethod


class ToolError(Exception):
    """Tool 领域异常基类。

    所有 Tool 相关异常由此派生，携带人类可读的 ``message``。
    本异常体系保持最小：一个基类 + 少量具体子类，
    不引入复杂层次，也不依赖 FastAPI / HTTPException。
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class UnknownToolError(ToolError):
    """请求的 Tool 不存在（Registry 无法按 name 找到）。

    示例::

        raise UnknownToolError("Unknown tool: calculator")
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ToolParameterError(ToolError):
    """Tool 参数校验失败（调用契约错误）。

    由 ``ToolRegistry.execute()`` 在执行前按 Tool 自身的 ``parameters``
    Schema 校验时抛出，涵盖：

    - arguments 不是 JSON 对象；
    - 缺少必填参数（``required``）；
    - 参数类型错误（``type``）；
    - 参数取值不在允许范围（``enum``）；
    - 出现 Schema 未定义的参数（``additionalProperties`` 非 ``true``）。

    与 :class:`ToolExecutionError` 的区别：本异常表示「调用方给的参数不合法」，
    工具业务逻辑尚未开始执行；两者都是 :class:`ToolError` 子类，
    因此 AgentRuntime 统一捕获并以 ``Tool error: ...`` 回传给 LLM。

    示例::

        raise ToolParameterError(
            "工具 'calculator' 参数校验失败：缺少必填参数 'expression'"
        )
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ToolExecutionError(ToolError):
    """Tool 执行失败。

    涵盖：

    - 工具内部执行失败（如 calculator 表达式非法 / 除数为 0）；
    - 运行时错误。

    注意：**参数校验失败**（缺失 / 类型错误 / 未定义参数）属于调用契约问题，
    由 :class:`ToolParameterError` 表示，在 ``Tool.execute()`` 之前抛出。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class Tool(ABC):
    """Agent 可调用工具的抽象接口。

    子类只需声明元数据并实现 :meth:`execute`。

    Attributes:
        name: 工具唯一名称（LLM 通过该名称发起调用）。
        description: 工具用途描述（给 LLM 阅读，用于判断何时调用）。
        parameters: 参数 JSON Schema（OpenAI function calling 的
            ``parameters`` 字段）。
    """

    #: 工具唯一名称
    name: str = ""
    #: 工具用途描述（LLM 依据它决定是否调用）
    description: str = ""
    #: 参数 JSON Schema
    parameters: dict = {}

    def get_schema(self) -> dict:
        """返回 OpenAI function calling 格式的工具描述。

        该 Schema 会随请求一起发给 LLM，用于说明：

        - 可以调用什么工具（``name``）；
        - 这个工具做什么（``description``）；
        - 调用需要哪些参数（``parameters``）。

        Returns:
            dict: 形如::

                {
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "description": "...",
                        "parameters": {...},
                    },
                }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    async def execute(self, arguments: dict) -> str:
        """执行工具并返回字符串结果（tool_result）。

        Args:
            arguments: LLM 给出的参数（已从 JSON 解析为 dict）。

        Returns:
            str: 执行结果文本，将被加入 messages 回传给 LLM。

        Raises:
            ToolParameterError: 由 ``ToolRegistry`` 在执行前按 Schema 校验抛出
                （Tool 被直接调用时也可按其自身校验逻辑抛出）。
            ToolExecutionError: 工具内部执行失败（如表达式非法、除数为 0）。
        """
