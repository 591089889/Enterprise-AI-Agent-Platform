"""Stage 22：知识库文件上传与文档导入验收测试。

验证目标
--------
1. **Case 1**：上传 ``.txt`` → 201，Document 创建且 ``content`` 与文件内容一致、
   ``index_status=pending``（上传不会直接写入向量库）；
2. **Case 1b / 1c / 2**：``.md`` / ``.docx``（现场生成 OOXML）/ ``.pdf``（现场生成
   最小合法 PDF）文本均成功提取；
3. **Case 3**：Index → 200 ``indexed``，Chroma collection 出现且 chunk 数 > 0，
   且 chunk 文本确实包含上传内容（Document → Chroma 全链路）；
4. **Case 4**：删除 Knowledge Base → Document 级联删除、collection 真正消失
   （保持 Stage 21 行为）；
5. **Case 5**：权限 —— 普通用户上传到管理员的知识库 → 403；未登录 → 401；
6. **安全 / 边界**：不支持扩展名 400、空文件 400、超大文件 413、二进制 ``.txt``
   400、损坏 ``.docx`` 400、不存在的知识库 404、文件名目录穿越被剥离、
   超长文件名按列宽截断且保留扩展名；
7. **生命周期约束**：项目内不出现 ``uploads/`` 目录、``documents`` 表没有
   ``file_path`` / ``storage_key`` / ``oss_url`` 等字段、上传用的临时目录已删除；
8. **前端接线静态检查**：api / store / view 均已接入上传（含 ``accept`` 限制）。

测试数据全部清理（用户 / 知识库 / 文档 / collection）；不读取、不打印任何密钥。
只调用既有 HTTP 接口，不修改任何架构（Agent / LLM / Conversation / Alembic 均不涉及）。

运维提示（沿用 Stage 21 结论）
------------------------------
ChromaDB 的 HNSW 索引文件在 Windows 上被**执行本测试的进程**内存映射（mmap），
因此 `drop_collection()` 的**物理目录删除会被延迟**（逻辑删除已完成、元数据已消失）。
需要时在**后端服务停止后**执行：

    uv run python scripts/cleanup_orphan_chroma_storage.py --apply --include-orphan-physical

被延迟的目录已无任何元数据引用，不会影响任何存活的 collection。
"""

import asyncio
import io
import os
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import chromadb  # noqa: E402
import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database.session import engine  # noqa: E402
from app.main import app  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
INFO: list[str] = []

#: 测试开始前的 collection 快照（收尾时复核，避免污染既有向量库）
COLLECTIONS_BEFORE: set[str] = set()

CHROMA_DIR = Path(settings.VECTOR_STORE_DIR)
PROJECT_ROOT = Path(BACKEND).parent
FRONTEND_SRC = PROJECT_ROOT / "frontend" / "src"
UPLOAD_DIR_NAME = "uploads"

#: DOCX（OOXML）主文档命名空间
DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

TXT_CONTENT = (
    "员工差旅政策（TXT）：\n"
    "1. 出差前需在系统提交申请并获得直属主管审批。\n"
    "2. 高铁二等座与经济舱机票可全额报销。\n"
)
MD_CONTENT = (
    "# 售后政策（MD）\n\n"
    "- 订单支付后 7 天内可以申请退款；\n"
    "- 超过 7 天需走人工审核流程。\n"
)
DOCX_PARAGRAPHS = [
    "信息安全规范（DOCX）",
    "员工不得将内部资料上传到未授权的第三方平台。",
    "违反规定的账号将被立即停用。",
]
PDF_LINES = [
    "STAGE22 PDF POLICY",
    "Refund within 7 days after payment.",
    "Contact service@example.com for details.",
]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"PASS  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} | {detail}")


def info(message: str) -> None:
    """记录一条环境事实（不参与 PASS/FAIL，但会写进汇总）。"""
    INFO.append(message)
    print(f"[info] {message}")


# ----------------------------------------------------------------------
# 测试用文件构造（不依赖任何额外库；PDF / DOCX 现场生成）
# ----------------------------------------------------------------------
def build_docx_bytes(paragraphs: list[str]) -> bytes:
    """生成一个最小但结构合法的 ``.docx``（ZIP + OOXML）。"""
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{DOCX_NS}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"/>',
        )
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def build_pdf_bytes(lines: list[str]) -> bytes:
    """生成一个最小但**结构合法**（含正确 xref 偏移）的 PDF 1.4。"""
    content_parts = ["BT /F1 14 Tf 72 720 Td 18 TL"]
    for index, line in enumerate(lines):
        escaped = (
            line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        )
        if index:
            content_parts.append("T*")
        content_parts.append(f"({escaped}) Tj")
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


# ----------------------------------------------------------------------
# 独立复核工具（刻意不复用被测实现，避免「自己验证自己」）
# ----------------------------------------------------------------------
async def db_scalar(query: str, params: dict | None = None):
    """执行一条只读 SQL 并返回标量（用于与数据库交叉校验）。"""
    async with engine.connect() as conn:
        return (await conn.execute(text(query), params or {})).scalar_one()


def _chroma_connection() -> sqlite3.Connection | None:
    """只读打开 ChromaDB 元数据库（不启动 Chroma 客户端）。"""
    db = CHROMA_DIR / "chroma.sqlite3"
    if not db.exists():
        return None
    try:
        return sqlite3.connect(
            f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5.0
        )
    except sqlite3.Error:
        return None


def chroma_collection_names() -> set[str]:
    conn = _chroma_connection()
    if conn is None:
        return set()
    try:
        return {row[0] for row in conn.execute("SELECT name FROM collections")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def chroma_embedding_count(collection_name: str) -> int:
    conn = _chroma_connection()
    if conn is None:
        return 0
    try:
        return conn.execute(
            "SELECT COUNT(e.id) FROM collections c "
            "LEFT JOIN segments s ON s.collection = c.id "
            "LEFT JOIN embeddings e ON e.segment_id = s.id "
            "WHERE c.name = ?",
            (collection_name,),
        ).fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def chroma_chunk_texts(collection_name: str) -> list[str]:
    """只读读取该 collection 的全部 chunk 文本（key = ``chroma:document``）。"""
    conn = _chroma_connection()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT em.string_value FROM embedding_metadata em "
            "JOIN embeddings e ON e.id = em.id "
            "JOIN segments s ON s.id = e.segment_id "
            "JOIN collections c ON c.id = s.collection "
            "WHERE c.name = ? AND em.key = 'chroma:document'",
            (collection_name,),
        ).fetchall()
        return [row[0] for row in rows if row[0]]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def kb_collection_name(knowledge_base_id: int) -> str:
    """按 Stage 16 的命名约定推导 collection 名（不复用被测函数）。"""
    return f"{settings.VECTOR_COLLECTION_NAME}__kb_{knowledge_base_id}"


def upload_files(name: str, content: bytes, mime: str = "text/plain"):
    """构造 httpx 的 multipart 文件参数（表单字段名固定为 ``file``）。"""
    return {"file": (name, content, mime)}


async def load_document_columns() -> list[str]:
    """读取 ``documents`` 表列名（用于确认没有新增文件相关字段）。"""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'documents'"
                )
            )
        ).fetchall()
    return [row[0] for row in rows]


def read_frontend_file(relative_path: str) -> str:
    """读取前端源码文件（不存在时返回空串，由断言判失败）。"""
    path = FRONTEND_SRC / relative_path
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def cleanup_chroma_leftovers() -> list[str]:
    """兜底：删除测试结束时仍残留的 collection（正常情况下应已不存在）。"""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    leftover = [collection.name for collection in client.list_collections()]
    for name in leftover:
        client.delete_collection(name)
        print(f"[cleanup] 兜底清理残留 collection: {name}")
    del client
    return leftover


def summary_and_exit() -> None:
    print("\n" + "=" * 76)
    print("Stage 22 知识库文件上传验收汇总")
    print("-" * 76)
    print(f"PASS = {len(PASS)}   FAIL = {len(FAIL)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 76)
    sys.exit(1 if FAIL else 0)


def static_frontend_checks() -> None:
    """前端接线静态检查（与 Stage 19 脚本的做法一致）。"""
    api_source = read_frontend_file("api/knowledgeBase.ts")
    store_source = read_frontend_file("stores/knowledgeBase.ts")
    view_source = read_frontend_file("views/KnowledgeBaseView.vue")

    check(
        "前端: api/knowledgeBase.ts 新增 uploadDocument（POST .../documents/upload）",
        "export async function uploadDocument" in api_source
        and "/documents/upload" in api_source
        and "FormData" in api_source,
    )
    check(
        "前端: store 暴露 uploadDocument + uploading（上传后刷新文档列表）",
        "uploadDocument as uploadDocumentApi" in store_source
        and "async function uploadDocument" in store_source
        and "uploading = ref(false)" in store_source
        and "loadDocuments(knowledgeBaseId)" in store_source,
    )
    check(
        "前端: 视图含文件上传入口（type=file + accept 四种格式 + store.uploadDocument）",
        'type="file"' in view_source
        and ".txt" in view_source
        and ".docx" in view_source
        and "store.uploadDocument(" in view_source,
    )


async def run_all() -> None:  # noqa: C901
    global COLLECTIONS_BEFORE

    tag = uuid.uuid4().hex[:8]
    pwd = "Passw0rd!23"
    admin_name = f"s22-admin-{tag}"
    admin_email = f"{admin_name}@example.com"
    user_name = f"s22-user-{tag}"
    user_email = f"{user_name}@example.com"
    kb_name = f"S22知识库-{tag}"

    temp_root = Path(tempfile.gettempdir())
    temp_dirs_before = {item.name for item in temp_root.glob("kb_upload_*")}
    COLLECTIONS_BEFORE = chroma_collection_names()
    document_columns = await load_document_columns()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://s22.test", timeout=120.0
    ) as client:
        # ==============================================================
        # 准备：管理员 + 普通用户 + 知识库
        # ==============================================================
        print("\n=== 准备: 测试用户与知识库 ===")
        await client.post(
            "/users/",
            json={"username": admin_name, "email": admin_email, "password": pwd},
        )
        await client.post(
            "/users/",
            json={"username": user_name, "email": user_email, "password": pwd},
        )
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET role='admin' WHERE username=:u"),
                {"u": admin_name},
            )
        admin_login = await client.post(
            "/auth/login", json={"email": admin_email, "password": pwd}
        )
        user_login = await client.post(
            "/auth/login", json={"email": user_email, "password": pwd}
        )
        admin_h = {
            "Authorization": f"Bearer {admin_login.json()['access_token']}"
        }
        user_h = {"Authorization": f"Bearer {user_login.json()['access_token']}"}

        kb_resp = await client.post(
            "/knowledge-bases",
            json={"name": kb_name, "description": "stage22 文件上传验收"},
            headers=admin_h,
        )
        kb = kb_resp.json()
        kb_id = kb["id"]
        collection = kb_collection_name(kb_id)
        upload_url = f"/knowledge-bases/{kb_id}/documents/upload"
        check(
            "准备: 创建测试知识库 → 201",
            kb_resp.status_code == 201,
            f"{kb_resp.status_code} {kb_resp.text[:200]}",
        )
        check(
            "准备: 初始不存在该知识库的 collection",
            collection not in chroma_collection_names(),
            f"{sorted(chroma_collection_names())}",
        )

        # ==============================================================
        # Case 1：上传 TXT
        # ==============================================================
        print("\n=== Case 1: 上传 .txt → Document 创建 + content 正确 ===")
        txt_name = f"员工差旅政策-{tag}.txt"
        txt_resp = await client.post(
            upload_url,
            files=upload_files(
                txt_name, TXT_CONTENT.encode("utf-8"), "text/plain"
            ),
            headers=admin_h,
        )
        txt_doc = txt_resp.json()
        check(
            "Case 1: 上传 TXT → 201",
            txt_resp.status_code == 201,
            f"{txt_resp.status_code} {txt_resp.text[:200]}",
        )
        check(
            "Case 1: 返回的文档名 = 上传文件名（含扩展名）",
            txt_doc.get("name") == txt_name,
            f"{txt_doc.get('name')}",
        )
        check(
            "Case 1: 索引状态为 pending（上传不直接写入向量库）",
            txt_doc.get("index_status") == "pending",
            f"{txt_doc.get('index_status')}",
        )
        dtxt_params = {"i": txt_doc.get("id")}
        db_txt = (
            await db_scalar(
                "SELECT content FROM documents WHERE id = :i", dtxt_params
            ),
            await db_scalar(
                "SELECT name FROM documents WHERE id = :i", dtxt_params
            ),
            await db_scalar(
                "SELECT index_status FROM documents WHERE id = :i", dtxt_params
            ),
            await db_scalar(
                "SELECT knowledge_base_id FROM documents WHERE id = :i",
                dtxt_params,
            ),
        )
        check(
            "Case 1: 数据库正文与文件内容完全一致",
            db_txt[0] == TXT_CONTENT,
            f"{str(db_txt[0])[:80]}",
        )
        check(
            "Case 1: 数据库 name / index_status / knowledge_base_id 正确",
            (db_txt[1], db_txt[2], db_txt[3]) == (txt_name, "pending", kb_id),
            f"{db_txt[1:]}",
        )
        check(
            "Case 1: 上传不创建 collection（向量库仍无该 KB）",
            collection not in chroma_collection_names(),
            f"{sorted(chroma_collection_names())}",
        )
        listing = await client.get(
            f"/knowledge-bases/{kb_id}/documents", headers=admin_h
        )
        check(
            "Case 1: 文档列表可查到该文档",
            any(item["id"] == txt_doc.get("id") for item in listing.json()),
            f"{listing.status_code}",
        )

        # ==============================================================
        # Case 1b / 1c：上传 Markdown / DOCX
        # ==============================================================
        print("\n=== Case 1b/1c: 上传 .md / .docx（现场生成 OOXML） ===")
        md_name = f"售后政策-{tag}.md"
        md_resp = await client.post(
            upload_url,
            files=upload_files(
                md_name, MD_CONTENT.encode("utf-8"), "text/markdown"
            ),
            headers=admin_h,
        )
        md_content = await db_scalar(
            "SELECT content FROM documents WHERE name = :n", {"n": md_name}
        )
        check(
            "Case 1b: 上传 MD → 201 且正文正确",
            md_resp.status_code == 201 and md_content == MD_CONTENT,
            f"{md_resp.status_code} {str(md_content)[:60]}",
        )

        docx_name = f"信息安全规范-{tag}.docx"
        docx_resp = await client.post(
            upload_url,
            files=upload_files(
                docx_name,
                build_docx_bytes(DOCX_PARAGRAPHS),
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            ),
            headers=admin_h,
        )
        docx_content = await db_scalar(
            "SELECT content FROM documents WHERE name = :n", {"n": docx_name}
        )
        check(
            "Case 1c: 上传 DOCX → 201 且段落文本被完整提取（零额外依赖）",
            docx_resp.status_code == 201
            and docx_content == "\n".join(DOCX_PARAGRAPHS),
            f"{docx_resp.status_code} {str(docx_content)[:80]}",
        )

        # ==============================================================
        # Case 2：上传 PDF
        # ==============================================================
        print("\n=== Case 2: 上传 .pdf → 文本成功提取 ===")
        pdf_name = f"退款政策-{tag}.pdf"
        pdf_resp = await client.post(
            upload_url,
            files=upload_files(
                pdf_name, build_pdf_bytes(PDF_LINES), "application/pdf"
            ),
            headers=admin_h,
        )
        pdf_doc = pdf_resp.json()
        pdf_content = await db_scalar(
            "SELECT content FROM documents WHERE name = :n", {"n": pdf_name}
        )
        check(
            "Case 2: 上传 PDF → 201",
            pdf_resp.status_code == 201,
            f"{pdf_resp.status_code} {pdf_resp.text[:200]}",
        )
        check(
            "Case 2: PDF 文本层被正确提取（含全部行）",
            str(pdf_content).strip() != ""
            and all(line in str(pdf_content) for line in PDF_LINES),
            f"{str(pdf_content)[:120]}",
        )

        # ==============================================================
        # Case 3：Index → Document → Chroma
        # ==============================================================
        print("\n=== Case 3: Index → Document → Chroma ===")
        index_resp = await client.post(
            f"/knowledge-bases/{kb_id}/documents/{pdf_doc['id']}/index",
            headers=admin_h,
        )
        index_body = index_resp.json()
        check(
            "Case 3: Index → 200 且 status=indexed",
            index_resp.status_code == 200
            and index_body.get("status") == "indexed",
            f"{index_resp.status_code} {index_body}",
        )
        check(
            "Case 3: 该 KB 的 collection 已创建（Chroma 元数据口径）",
            collection in chroma_collection_names(),
            f"{sorted(chroma_collection_names())}",
        )
        check(
            "Case 3: collection 内向量行数 > 0（独立 sqlite 口径）",
            chroma_embedding_count(collection) > 0,
            f"{chroma_embedding_count(collection)}",
        )
        chunk_texts = chroma_chunk_texts(collection)
        check(
            "Case 3: chunk 文本确实来自上传的文件（四种格式全部进入向量库）",
            all(
                any(marker in text for text in chunk_texts)
                for marker in (
                    "差旅政策",
                    "售后政策",
                    "信息安全规范",
                    "STAGE22 PDF POLICY",
                )
            ),
            f"chunks={len(chunk_texts)}",
        )
        statuses = (
            await db_scalar(
                "SELECT COUNT(*) FROM documents "
                "WHERE knowledge_base_id = :k AND index_status = 'indexed'",
                {"k": kb_id},
            ),
        )
        check(
            "Case 3: 该 KB 下全部文档索引状态为 indexed",
            statuses[0] == 4,
            f"{statuses[0]}",
        )

        # ==============================================================
        # Case 5：权限（普通用户 / 未登录）
        # ==============================================================
        print("\n=== Case 5: 权限（403 / 401） ===")
        docs_before_forbidden = await db_scalar(
            "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
            {"k": kb_id},
        )
        forbidden_resp = await client.post(
            upload_url,
            files=upload_files(
                "越权上传.txt", "普通用户不应能上传。".encode("utf-8")
            ),
            headers=user_h,
        )
        check(
            "Case 5: 普通用户上传到管理员的知识库 → 403",
            forbidden_resp.status_code == 403,
            f"{forbidden_resp.status_code} {forbidden_resp.text[:160]}",
        )
        anonymous_resp = await client.post(
            upload_url,
            files=upload_files("匿名.txt", b"anonymous"),
        )
        check(
            "Case 5: 未登录上传 → 401",
            anonymous_resp.status_code == 401,
            f"{anonymous_resp.status_code} {anonymous_resp.text[:160]}",
        )
        docs_after_forbidden = await db_scalar(
            "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
            {"k": kb_id},
        )
        check(
            "Case 5: 越权 / 未登录请求没有产生任何文档（无副作用）",
            docs_after_forbidden == docs_before_forbidden,
            f"{docs_before_forbidden} -> {docs_after_forbidden}",
        )

        # ==============================================================
        # 边界与安全：类型 / 大小 / 文件名
        # ==============================================================
        print("\n=== 边界与安全: 类型 / 大小 / 文件名 ===")
        bad_type = await client.post(
            upload_url,
            files=upload_files("malware.exe", b"MZ\x90\x00"),
            headers=admin_h,
        )
        check(
            "边界: 不支持的扩展名 .exe → 400 且提示支持的格式",
            bad_type.status_code == 400
            and ".txt" in bad_type.text
            and ".docx" in bad_type.text,
            f"{bad_type.status_code} {bad_type.text[:160]}",
        )
        no_ext = await client.post(
            upload_url,
            files=upload_files("policy", b"plain text without extension"),
            headers=admin_h,
        )
        check(
            "边界: 无扩展名 → 400（不猜测格式）",
            no_ext.status_code == 400,
            f"{no_ext.status_code} {no_ext.text[:160]}",
        )
        empty_file = await client.post(
            upload_url,
            files=upload_files("empty.txt", b""),
            headers=admin_h,
        )
        check(
            "边界: 空文件 → 400",
            empty_file.status_code == 400,
            f"{empty_file.status_code} {empty_file.text[:160]}",
        )
        binary_txt = await client.post(
            upload_url,
            files=upload_files("fake.txt", b"\x00\x01\x02\xff binary"),
            headers=admin_h,
        )
        check(
            "边界: 二进制内容改名为 .txt → 400",
            binary_txt.status_code == 400,
            f"{binary_txt.status_code} {binary_txt.text[:160]}",
        )
        broken_docx = await client.post(
            upload_url,
            files=upload_files("broken.docx", b"not a zip at all"),
            headers=admin_h,
        )
        check(
            "边界: 损坏的 .docx → 400（不产生 500）",
            broken_docx.status_code == 400,
            f"{broken_docx.status_code} {broken_docx.text[:160]}",
        )
        missing_kb = await client.post(
            "/knowledge-bases/999999999/documents/upload",
            files=upload_files("policy.txt", b"content"),
            headers=admin_h,
        )
        check(
            "边界: 上传到不存在的知识库 → 404（不跨库写入）",
            missing_kb.status_code == 404,
            f"{missing_kb.status_code} {missing_kb.text[:160]}",
        )
        traversal = await client.post(
            upload_url,
            files=upload_files(
                "../../../../evil-escape.txt", b"path traversal check"
            ),
            headers=admin_h,
        )
        check(
            "安全: 文件名中的目录穿越被剥离（只保留基础名）",
            traversal.status_code == 201
            and traversal.json().get("name") == "evil-escape.txt",
            f"{traversal.status_code} {traversal.json().get('name')}",
        )
        long_name = f"{'超长文件名' * 60}.txt"
        long_resp = await client.post(
            upload_url,
            files=upload_files(long_name, b"long name check"),
            headers=admin_h,
        )
        long_saved = long_resp.json().get("name") or ""
        check(
            "安全: 超长文件名按列宽截断且保留扩展名（<= 200 字符）",
            long_resp.status_code == 201
            and len(long_saved) == 200
            and long_saved.endswith(".txt"),
            f"{long_resp.status_code} len={len(long_saved)}",
        )

        # 大小上限：临时把上限降到 2MB 验证 413（随后立即恢复，只发 2MB+1KB）
        original_limit = settings.MAX_UPLOAD_FILE_SIZE_MB
        settings.MAX_UPLOAD_FILE_SIZE_MB = 2
        try:
            big_resp = await client.post(
                upload_url,
                files=upload_files(
                    "oversize.txt", b"A" * (2 * 1024 * 1024 + 1024)
                ),
                headers=admin_h,
            )
        finally:
            settings.MAX_UPLOAD_FILE_SIZE_MB = original_limit
        check(
            "边界: 超过大小上限的文件 → 413（拒绝无限上传）",
            big_resp.status_code == 413,
            f"{big_resp.status_code} {big_resp.text[:160]}",
        )
        check(
            "边界: 大小上限配置已恢复（测试未污染运行时配置）",
            settings.MAX_UPLOAD_FILE_SIZE_MB == original_limit,
            f"{settings.MAX_UPLOAD_FILE_SIZE_MB}",
        )

        # ==============================================================
        # Case 4：删除 Knowledge Base（保持 Stage 21 行为）
        # ==============================================================
        print("\n=== Case 4: 删除 Knowledge Base → Document + Chroma ===")
        documents_in_kb = await db_scalar(
            "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
            {"k": kb_id},
        )
        chunk_count_before_delete = chroma_embedding_count(collection)
        del_resp = await client.delete(
            f"/knowledge-bases/{kb_id}", headers=admin_h
        )
        check(
            "Case 4: 删除 KB → 204",
            del_resp.status_code == 204,
            f"{del_resp.status_code} {del_resp.text[:160]}",
        )
        deleted_docs = await db_scalar(
            "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
            {"k": kb_id},
        )
        check(
            "Case 4: 该 KB 的文档已级联删除",
            deleted_docs == 0 and documents_in_kb > 0,
            f"{documents_in_kb} -> {deleted_docs}",
        )
        check(
            "Case 4: Chroma collection 真正消失（不是 count=0 空集合）",
            collection not in chroma_collection_names(),
            f"{sorted(chroma_collection_names())}",
        )
        check(
            "Case 4: 删除前确实存在向量（本次校验有意义）",
            chunk_count_before_delete > 0,
            f"{chunk_count_before_delete}",
        )

    # ==============================================================
    # 生命周期约束 + 前端接线静态检查
    # ==============================================================
    print("\n=== 生命周期约束 / 前端接线 ===")
    upload_dir = Path(BACKEND) / UPLOAD_DIR_NAME
    check(
        "生命周期: 项目内不存在 backend/uploads 目录（无永久文件存储层）",
        not upload_dir.exists(),
        str(upload_dir),
    )
    temp_dirs_after = {item.name for item in temp_root.glob("kb_upload_*")}
    check(
        "生命周期: 上传使用的临时目录已全部删除（tempfile 自动清理）",
        temp_dirs_after == temp_dirs_before,
        f"新增残留={sorted(temp_dirs_after - temp_dirs_before)}",
    )
    file_columns = sorted(
        column
        for column in document_columns
        if any(
            token in column.lower()
            for token in ("file", "path", "storage", "oss", "url")
        )
    )
    check(
        "生命周期: documents 表未新增文件相关字段（file_path / storage_key / oss_url 等）",
        file_columns == [],
        f"{file_columns}",
    )
    static_frontend_checks()


async def teardown() -> None:
    """清理测试命名空间（s22-* 用户 / S22知识库* / 残留 collection）。"""
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE username LIKE 's22-%'")
            )
            await conn.execute(
                text(
                    "DELETE FROM knowledge_bases WHERE name LIKE 'S22知识库%'"
                )
            )
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响汇总
        print(f"[cleanup] PostgreSQL 清理失败: {exc}")
    leftovers = cleanup_chroma_leftovers()
    if leftovers:
        print(f"[cleanup] 兜底删除残留 collection: {leftovers}")


async def main() -> None:
    try:
        await run_all()
    except Exception as exc:  # noqa: BLE001 - 中断也要清理并输出汇总
        FAIL.append(f"测试执行中断: {type(exc).__name__}")
        print(f"\n[error] 测试执行中断: {type(exc).__name__}: {exc}")

    await teardown()

    leftover_users = await db_scalar(
        "SELECT COUNT(*) FROM users WHERE username LIKE 's22-%'"
    )
    leftover_kbs = await db_scalar(
        "SELECT COUNT(*) FROM knowledge_bases WHERE name LIKE 'S22知识库%'"
    )
    check(
        "收尾: PostgreSQL 测试数据已清理（用户 / 知识库无残留）",
        leftover_users == 0 and leftover_kbs == 0,
        f"users={leftover_users} kbs={leftover_kbs}",
    )
    check(
        "收尾: .chroma 未留下任何 collection（与测试前快照一致）",
        chroma_collection_names() == COLLECTIONS_BEFORE,
        f"{sorted(chroma_collection_names())}",
    )
    info(
        "收尾: .chroma collections="
        f"{sorted(chroma_collection_names()) or '（空）'}"
    )

    await engine.dispose()
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
