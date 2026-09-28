"""Stage 5：LLM Provider 层验收测试。

覆盖：
1. Provider Factory 切换（qwen / ollama）
2. 未知 Provider 明确报错（不静默回退）
3. Provider Interface 合规（子类 + generate）
4. AgentRuntime 与具体 Provider 解耦（通过 MockProvider 验证）
5. Factory 纯职责静态检查（不导入 DB / RAG / HTTP SDK）
6. Runtime 静态检查（不导入具体 Provider）
7. LLM_TEMPERATURE 配置透传
8. 无 API Key 时不伪造真实调用

注意：本脚本不会读取或打印 backend/.env 中的敏感值。
"""

import ast
import asyncio
import inspect
import sys
from pathlib import Path

from app.agents.providers import (
    LLMProvider,
    OllamaProvider,
    QwenProvider,
    SUPPORTED_PROVIDERS,
    get_llm_provider,
)
from app.agents.runtime import AgentRuntime
from app.core.config import settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROVIDERS_DIR = BACKEND_DIR / "app" / "agents" / "providers"

passed: list[str] = []
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        passed.append(name)
        print(f"  [PASS] {name}")
    else:
        failed.append(name)
        print(f"  [FAIL] {name} {detail}")


# ----------------------------------------------------------------------
# 1. Provider Factory 切换
# ----------------------------------------------------------------------
def test_factory_switching() -> None:
    print("\n[1] Provider Factory 切换")
    original = settings.LLM_PROVIDER
    try:
        settings.LLM_PROVIDER = "qwen"
        p = get_llm_provider()
        check("LLM_PROVIDER=qwen → QwenProvider", isinstance(p, QwenProvider),
              f"got {type(p).__name__}")

        settings.LLM_PROVIDER = "ollama"
        p = get_llm_provider()
        check("LLM_PROVIDER=ollama → OllamaProvider", isinstance(p, OllamaProvider),
              f"got {type(p).__name__}")

        # 大小写 / 空白容错
        settings.LLM_PROVIDER = "  QWEN  "
        p = get_llm_provider()
        check("大小写/空白容错 (QWEN)", isinstance(p, QwenProvider),
              f"got {type(p).__name__}")
    finally:
        settings.LLM_PROVIDER = original


# ----------------------------------------------------------------------
# 2. 未知 Provider 明确报错
# ----------------------------------------------------------------------
def test_unknown_provider() -> None:
    print("\n[2] 未知 Provider 报错")
    original = settings.LLM_PROVIDER
    try:
        settings.LLM_PROVIDER = "openai"
        raised = False
        try:
            get_llm_provider()
        except ValueError as exc:
            raised = True
            check("抛出 ValueError", True)
            check("错误信息包含受支持列表",
                  all(name in str(exc) for name in SUPPORTED_PROVIDERS),
                  f"msg={exc}")
        check("未知 Provider 不静默回退", raised)
    finally:
        settings.LLM_PROVIDER = original


# ----------------------------------------------------------------------
# 3. Provider Interface 合规
# ----------------------------------------------------------------------
def test_interface_compliance() -> None:
    print("\n[3] Provider Interface 合规")
    for cls in (QwenProvider, OllamaProvider):
        check(f"{cls.__name__} 继承 LLMProvider",
              issubclass(cls, LLMProvider))
        check(f"{cls.__name__} 实现 generate",
              "generate" in cls.__dict__)


# ----------------------------------------------------------------------
# 4. AgentRuntime 与具体 Provider 解耦（用 Mock）
# ----------------------------------------------------------------------
class MockProvider(LLMProvider):
    """测试用假 Provider：记录收到的 messages。"""

    def __init__(self) -> None:
        self.received: list[dict] = []

    async def generate(self, messages: list[dict]) -> str:
        self.received = messages
        return "MOCK_REPLY"


def test_runtime_decoupling() -> None:
    print("\n[4] AgentRuntime 解耦（MockProvider）")
    mock = MockProvider()
    runtime = AgentRuntime(mock)

    result = asyncio.run(runtime.run([{"role": "user", "content": "你好"}]))
    check("Runtime 通过 LLMProvider.generate 调用", result == "MOCK_REPLY",
          f"got {result}")
    check("Runtime 注入默认 system prompt",
          mock.received[0]["role"] == "system")
    check("Runtime 透传用户消息",
          mock.received[-1] == {"role": "user", "content": "你好"})

    # 自定义 system_prompt（模拟 RAG context）
    mock2 = MockProvider()
    runtime2 = AgentRuntime(mock2)
    asyncio.run(runtime2.run(
        [{"role": "user", "content": "北京"}],
        system_prompt="[Context]\nRAG内容",
    ))
    check("Runtime 接受上游注入的 system_prompt",
          "[Context]" in mock2.received[0]["content"])


# ----------------------------------------------------------------------
# 5. Factory 纯职责静态检查
# ----------------------------------------------------------------------
def test_factory_purity() -> None:
    print("\n[5] Factory 静态检查")
    source = (PROVIDERS_DIR / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    forbidden = ("sqlalchemy", "app.database", "app.rag", "chromadb",
                 "urllib", "httpx", "requests", "openai", "dashscope")
    violations = [
        m for m in imported_modules
        if any(m == f or m.startswith(f + ".") for f in forbidden)
    ]
    check("Factory 不导入 DB / RAG / HTTP SDK", not violations, f"{violations}")


# ----------------------------------------------------------------------
# 6. Runtime 静态检查
# ----------------------------------------------------------------------
def test_runtime_purity() -> None:
    print("\n[6] Runtime 静态检查")
    runtime_path = BACKEND_DIR / "app" / "agents" / "runtime.py"
    source = runtime_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    forbidden = ("sqlalchemy", "app.database", "app.rag", "chromadb",
                 "fastapi", "app.agents.providers.qwen",
                 "app.agents.providers.ollama")
    violations = [
        m for m in imported_modules
        if any(m == f or m.startswith(f + ".") for f in forbidden)
    ]
    check("Runtime 不导入具体 Provider / DB / RAG / FastAPI",
          not violations, f"{violations}")

    check("Runtime 源码不出现 QwenProvider",
          "QwenProvider" not in source)
    check("Runtime 源码不出现 OllamaProvider",
          "OllamaProvider" not in source)


# ----------------------------------------------------------------------
# 7. LLM_TEMPERATURE 配置透传
# ----------------------------------------------------------------------
def test_temperature() -> None:
    print("\n[7] LLM_TEMPERATURE 透传")
    original = settings.LLM_TEMPERATURE
    try:
        settings.LLM_TEMPERATURE = 0.25
        q = QwenProvider()
        o = OllamaProvider()
        check("QwenProvider 读取 LLM_TEMPERATURE", q.temperature == 0.25,
              f"got {q.temperature}")
        check("OllamaProvider 读取 LLM_TEMPERATURE", o.temperature == 0.25,
              f"got {o.temperature}")

        # 显式参数优先
        q2 = QwenProvider(temperature=0.9)
        check("显式 temperature 参数优先", q2.temperature == 0.9,
              f"got {q2.temperature}")
    finally:
        settings.LLM_TEMPERATURE = original


# ----------------------------------------------------------------------
# 8. API Key 配置状态下的 Provider 行为（Stage 18 修正）
# ----------------------------------------------------------------------
def test_no_api_key() -> None:
    """按环境验证「有 Key / 无 Key」两条路径（不读取、不打印任何 Key）。

    修正说明
    --------
    原断言写死「无 API Key → 返回本地模拟回复」，但 ``QwenProvider`` 的
    ``api_key`` 参数为 ``None`` / 空串时会回退到 ``settings.LLM_API_KEY``
    （设计如此），因此在**已配置真实 Key** 的环境中该断言必然失败。

    现在按环境分支（保持原意：不伪造真实调用）：

    - 有 Key → 用 spy 替换 ``_call_qwen``，断言 generate 走**真实调用路径**
      （不发起网络请求、不消耗额度、不泄露 Key）；
    - 无 Key → 断言返回本地模拟回复。
    """
    print("\n[8] API Key 配置状态下的 Provider 行为")
    if settings.LLM_API_KEY:
        calls: list[dict] = []
        original = QwenProvider._call_qwen

        async def spy_call(self, messages):  # noqa: ANN001
            calls.append({"model": self.model, "messages": list(messages)})
            return "SPY-REAL-PATH"

        QwenProvider._call_qwen = spy_call
        try:
            provider = QwenProvider(api_key="configured-key-stub")
            reply = asyncio.run(provider.generate(
                [{"role": "user", "content": "测试"}]
            ))
        finally:
            QwenProvider._call_qwen = original

        check("有 API Key 时走真实调用路径（_call_qwen，而非本地模拟）",
              len(calls) == 1 and reply == "SPY-REAL-PATH",
              f"reply={reply[:80]}")
    else:
        provider = QwenProvider(api_key="")
        reply = asyncio.run(provider.generate(
            [{"role": "user", "content": "测试"}]
        ))
        check("无 API Key 时返回本地模拟回复（非真实调用）",
              "模拟" in reply or "LLM_API_KEY" in reply,
              f"reply={reply[:80]}")


def main() -> None:
    test_factory_switching()
    test_unknown_provider()
    test_interface_compliance()
    test_runtime_decoupling()
    test_factory_purity()
    test_runtime_purity()
    test_temperature()
    test_no_api_key()

    print("\n" + "=" * 50)
    print(f"Provider Factory            : "
          f"{'PASS' if not failed else 'CHECK'}")
    print(f"通过: {len(passed)}  失败: {len(failed)}")
    if failed:
        for f in failed:
            print(f"  - {f}")
        sys.exit(1)
    print("全部通过！")


if __name__ == "__main__":
    main()
