"""Tool Registry（工具注册表）。

职责边界
--------
ToolRegistry 只做四件事：

    1. 注册 Tool                    register()
    2. 按 name 查找 Tool            get()
    3. 列出全部 Tool                list_tools() / get_schemas()
    4. 校验参数 + 执行指定 Tool      execute()

其中参数校验是**统一的**：规则来自 Tool 自身的 ``parameters``
（见 :mod:`app.agents.tools.validation`），
Registry 只负责调度「查找 → 校验 → 执行」流程，
不硬编码任何具体 Tool 的名称或业务参数规则。

它**不**负责：
- 动态插件加载 / 自动发现；
- 远程 Tool Registry；
- 数据库持久化；
- 复杂权限系统。

设计说明
--------
ToolRegistry 是 Agent 与具体 Tool 之间的唯一调度层：
AgentRuntime 只认识 Registry 与 Tool 抽象，不认识任何具体 Tool 实现。
本模块的代码与文档中都不出现任何具体 Tool 名称或参数名——
具体 Tool 由 ``build_default_registry()`` 注入。
"""

from app.agents.tools.base import Tool, UnknownToolError
from app.agents.tools.validation import validate_arguments


class ToolRegistry:
    """内存级工具注册表。

    使用方式（以任意 Tool 为例）::

        registry = ToolRegistry()
        registry.register(some_tool)              # Tool 抽象的子类实例
        registry.get(some_tool.name)              # → Tool
        await registry.execute(
            some_tool.name, {"param": "value"}    # → tool result
        )

    Args:
        tools: 可选的初始工具列表（便于测试与 DI 一次性构建）。
    """

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    # ------------------------------------------------------------------
    # 注册 / 查找
    # ------------------------------------------------------------------
    def register(self, tool: Tool) -> None:
        """注册一个 Tool。

        Args:
            tool: :class:`Tool` 子类实例。

        Raises:
            ValueError: 当 ``tool.name`` 为空，或名称已被占用时抛出。
                明确报错而不是静默覆盖，避免同名工具相互覆盖。
        """
        name = (tool.name or "").strip()
        if not name:
            raise ValueError(
                f"Tool {type(tool).__name__} 缺少 name，无法注册"
            )
        if name in self._tools:
            raise ValueError(
                f"Tool 名称重复: '{name}' 已注册，"
                "请使用不同的 name（不静默覆盖）"
            )
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        """按名称获取 Tool。

        Args:
            name: 工具名称。

        Returns:
            Tool: 对应的工具实例。

        Raises:
            UnknownToolError: 工具不存在时抛出，并在消息中列出可用工具。
        """
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(sorted(self._tools)) or "(空)"
            raise UnknownToolError(
                f"Unknown tool: {name}. 可用工具: {available}"
            )
        return tool

    def list_tools(self) -> list[Tool]:
        """返回全部已注册 Tool（按名称排序，保证顺序稳定）。"""
        return [self._tools[name] for name in sorted(self._tools)]

    def get_schemas(self) -> list[dict]:
        """返回全部 Tool 的 OpenAI function calling Schema。

        该结果会作为 ``tools`` 参数随请求发给 LLM。
        """
        return [tool.get_schema() for tool in self.list_tools()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def execute(self, name: str, arguments: dict) -> str:
        """执行指定 Tool（**执行前统一做参数校验**）。

        调用链::

            execute(name, arguments)
                    ↓
            get(name)                  # 找不到 → UnknownToolError
                    ↓
            validate_arguments(...)    # 按 Tool 自身 Schema 校验
                    ↓                  # 不合法 → ToolParameterError
            tool.execute(arguments)    # 具体工具逻辑
                    ↓                  # 失败 → ToolExecutionError

        参数规则只写在具体 Tool 的 ``parameters`` 里，
        Registry 不写「按工具名 if/elif 分支」这类硬编码逻辑。

        Args:
            name: 工具名称。
            arguments: LLM 给出的参数。

        Returns:
            str: 工具执行结果。

        Raises:
            UnknownToolError: 工具不存在。
            ToolParameterError: 参数不符合 Tool 的 Schema（缺失 / 类型错误 /
                未定义参数 / enum 取值非法）。
            ToolExecutionError: 工具执行失败（由 Tool 自身抛出，原样向上传播，
                不在此处吞掉）。
        """
        tool = self.get(name)
        # 统一参数校验入口：规则来自 Tool 的 parameters Schema，
        # Registry 只负责调度校验流程
        validated = validate_arguments(tool, arguments)
        return await tool.execute(validated)
