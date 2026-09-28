"""Agent Tool 包。

包含 Tool 抽象、Tool Registry 与内置 Tool。

调用链::

    AgentRuntime
        ↓
    ToolRegistry      # 注册 / 查找 / 调度
        ↓
    Tool.execute()    # 具体能力（如 calculator）

职责边界
--------
- ``Tool``         ：把外部能力包装成标准接口（name / description / schema / execute）。
- ``ToolRegistry`` ：管理工具集合，按名称查找、**统一校验参数**并调度执行。
- ``validation``   ：Schema 驱动的参数校验（Registry 的统一校验入口）。
- ``builtin``      ：内置工具实现（当前为 Calculator、DateTime）。

本包不依赖 FastAPI / Database / Repository；
具体 Tool 通过依赖注入交给 AgentRuntime，Runtime 只认识 ``Tool`` 抽象。
"""

from app.agents.tools.base import (
    Tool,
    ToolError,
    ToolExecutionError,
    ToolParameterError,
    UnknownToolError,
)
from app.agents.tools.builtin import CalculatorTool, DateTimeTool
from app.agents.tools.registry import ToolRegistry
from app.agents.tools.validation import validate_arguments


def build_default_registry() -> ToolRegistry:
    """构建包含全部内置 Tool 的注册表。

    新增一个内置 Tool 只需要在本函数追加一行实例化：
    上层（``get_tool_registry`` / ``AgentRuntime`` / Provider）无需任何改动。

    Returns:
        ToolRegistry: 已注册 Calculator、DateTime 等内置工具的注册表。
    """
    return ToolRegistry(tools=[CalculatorTool(), DateTimeTool()])


__all__ = [
    "CalculatorTool",
    "DateTimeTool",
    "Tool",
    "ToolError",
    "ToolExecutionError",
    "ToolParameterError",
    "ToolRegistry",
    "UnknownToolError",
    "build_default_registry",
    "validate_arguments",
]
