"""知识文档加载器。

职责（**只有一件事：文件 → 文本**）
----------------------------------
1. :class:`KnowledgeLoader`：读取本地 ``knowledge/*.txt`` 文本文件目录
   （Stage 4 以来的离线自检用法，自 Stage 20 起不再是运行时数据来源）；
2. :func:`load_file`（Stage 22）：解析**单个上传文件**为纯文本，
   支持 ``.txt`` / ``.md`` / ``.pdf`` / ``.docx``。

不负责数据库、切分、向量化或存储，也不接触 ChromaDB。
"""

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from app.core.config import settings
from app.rag.exceptions import FileLoadException

#: 支持上传解析的文件扩展名（小写、含点）——**全项目唯一来源**
SUPPORTED_UPLOAD_EXTENSIONS: tuple[str, ...] = (".txt", ".md", ".pdf", ".docx")

#: 纯文本扩展名（按文本编码解码，不需要第三方解析库）
_RAW_TEXT_EXTENSIONS: tuple[str, ...] = (".txt", ".md")

#: DOCX（OOXML）主文档命名空间
_DOCX_WORD_NAMESPACE = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
)


@dataclass
class KnowledgeDocument:
    """一份本地知识文档。"""

    #: 文档来源（文件名，如 travel_beijing.txt）
    source: str
    #: 文档全文
    content: str
    #: 附加元数据（如目录名、扩展名等）
    metadata: dict = field(default_factory=dict)


class KnowledgeLoader:
    """从本地目录加载 ``*.txt`` 知识文档。

    Args:
        knowledge_dir: 知识文档目录。相对路径基于当前工作目录（backend/），
            也可以传绝对路径。默认读取 ``settings.KNOWLEDGE_DIR``（"knowledge"）。
    """

    def __init__(self, knowledge_dir: str | None = None) -> None:
        self.knowledge_dir = Path(knowledge_dir or settings.KNOWLEDGE_DIR)

    def load(self) -> list[KnowledgeDocument]:
        """加载目录下所有 ``*.txt`` 文件。

        - 目录不存在时返回空列表（知识库缺失不应导致服务启动失败）。
        - 目录存在但没有 ``.txt`` 文件时返回空列表。
        - 读取文件时按 UTF-8 解码；忽略空文件。

        Returns:
            list[KnowledgeDocument]: 加载到的文档列表（可能为空）。
        """
        if not self.knowledge_dir.exists():
            return []
        if not self.knowledge_dir.is_dir():
            return []

        documents: list[KnowledgeDocument] = []
        for path in sorted(self.knowledge_dir.glob("*.txt")):
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # 单个文件读取失败不中断整个知识库构建
                continue
            # 跳过空文件
            if not content.strip():
                continue
            documents.append(
                KnowledgeDocument(
                    source=path.name,
                    content=content,
                    metadata={
                        "path": str(path),
                        "extension": "txt",
                    },
                )
            )
        return documents


# ----------------------------------------------------------------------
# 单文件加载（Stage 22：知识库文件上传）
# ----------------------------------------------------------------------
def supported_extensions_text() -> str:
    """人类可读的支持格式文案（如 ``.txt / .md / .pdf / .docx``）。"""
    return " / ".join(SUPPORTED_UPLOAD_EXTENSIONS)


def load_file(path: str | Path) -> KnowledgeDocument:
    """把**单个文件**解析为纯文本文档。

    文件生命周期由调用方负责（上传接口使用 ``tempfile`` 临时目录，
    解析完成后立即删除）；本函数**只读**该路径，不复制、不移动、不删除。

    Args:
        path: 待解析文件路径，扩展名决定解析器：

            - ``.txt`` / ``.md``：按文本编码解码（UTF-8 优先，GB18030 兜底）；
            - ``.pdf``：``pypdf`` 提取文本层（纯 Python，无外部服务）；
            - ``.docx``：标准库读取 OOXML（docx 本质是 ZIP + XML），零额外依赖。

    Returns:
        KnowledgeDocument: ``source`` 为文件名，``content`` 为提取出的纯文本，
            ``metadata`` 只记录扩展名与解析器名（辅助信息，不入库、不含路径）。

    Raises:
        FileLoadException: 扩展名不支持、文件无法读取 / 损坏 / 加密，
            或未能提取到任何文本（客户端错误，由 Service 映射为 HTTP 400）。
    """
    file_path = Path(path)
    extension = file_path.suffix.lower()

    if extension not in SUPPORTED_UPLOAD_EXTENSIONS:
        raise FileLoadException(
            f"不支持的文件类型（{file_path.suffix or '无扩展名'}），"
            f"仅支持 {supported_extensions_text()}"
        )
    if not file_path.is_file():
        raise FileLoadException("文件不存在或不是普通文件")

    if extension in _RAW_TEXT_EXTENSIONS:
        content, loader_name = _read_text_file(file_path), "text"
    elif extension == ".pdf":
        content, loader_name = _read_pdf_file(file_path), "pypdf"
    else:
        content, loader_name = _read_docx_file(file_path), "ooxml"

    if not content.strip():
        raise FileLoadException("未能从文件中提取到文本内容")

    return KnowledgeDocument(
        source=file_path.name,
        content=content,
        metadata={"extension": extension.lstrip("."), "loader": loader_name},
    )


def _read_text_file(path: Path) -> str:
    """读取纯文本文件（UTF-8 / UTF-8-BOM 优先，GB18030 兜底）。

    含 ``NUL`` 字节的内容判定为二进制（例如把其它格式改名成 ``.txt``），
    明确报错而不是把乱码写进数据库。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FileLoadException("文件读取失败") from exc

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            raise FileLoadException("文件内容不是文本（可能被改名为 .txt）")
        return text
    raise FileLoadException("文本文件编码无法识别，请使用 UTF-8 编码")


def _read_pdf_file(path: Path) -> str:
    """用 ``pypdf`` 提取 PDF 文本层。

    - 不依赖任何外部服务 / 二进制扩展；
    - 加密 PDF 先尝试空密码（很多文档只是「限制编辑」），失败则明确报错；
    - 底层异常一律转换为固定文案，不透传可能含路径的内部信息。
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 依赖缺失时给出可读错误，而不是 500
        raise FileLoadException(
            "服务端缺少 PDF 解析依赖（pypdf），请联系管理员"
        ) from exc

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise FileLoadException("PDF 已加密，无法提取文本")
        pages = [page.extract_text() or "" for page in reader.pages]
    except FileLoadException:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf 异常类型众多，统一收敛
        raise FileLoadException("PDF 解析失败，请确认文件未损坏") from exc
    return "\n".join(pages)


def _read_docx_file(path: Path) -> str:
    """提取 ``.docx`` 文本（标准库：ZIP + OOXML，**不引入 python-docx**）。

    读取 ``word/document.xml`` 中每个段落（``w:p``）下的文本节点（``w:t``），
    段落之间换行；表格单元格内的文本同样是 ``w:p``，因此一并被提取。
    旧版二进制 ``.doc`` 不是 OOXML，会在此明确报错。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                xml_bytes = archive.read("word/document.xml")
            except KeyError as exc:
                raise FileLoadException(
                    "文件不是有效的 .docx（缺少 word/document.xml）"
                ) from exc
    except FileLoadException:
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        raise FileLoadException(
            "docx 解析失败，请确认文件未损坏（旧版 .doc 请另存为 .docx）"
        ) from exc

    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise FileLoadException("docx 解析失败，请确认文件未损坏") from exc

    paragraphs: list[str] = []
    for paragraph in root.iter(f"{{{_DOCX_WORD_NAMESPACE}}}p"):
        text = "".join(
            node.text or ""
            for node in paragraph.iter(f"{{{_DOCX_WORD_NAMESPACE}}}t")
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)
