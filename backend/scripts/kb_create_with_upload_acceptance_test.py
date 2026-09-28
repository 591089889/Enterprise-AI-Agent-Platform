"""Stage 23：创建知识库时可选上传「首份文档」验收测试。

被测能力
--------
``POST /knowledge-bases`` 在 **同一个端点** 上支持三种请求形态：

1. ``application/json``：``{"name": ..., "description": ...}``（Stage 16 起的既有形态）；
2. ``multipart/form-data``：表单字段 ``name`` / ``description`` + **可选** ``file``；
   带 ``file`` 时等价于「建库 + ``POST /knowledge-bases/{id}/documents/upload``」，
   但两者在**同一个事务**内完成 —— 任一步失败则整体回滚；
3. ``application/x-www-form-urlencoded``：等价于第 2 种但不带文件
   （HTML ``<form>`` 的默认编码），只建库；该形态的请求体已由表单解析消费，
   因此**不能**再抛 ``RuntimeError: Stream consumed``（曾经会 500）。

验证目标（Case 1 - Case 8）
--------------------------
1. **Case 1**：multipart + ``.md`` → 201；知识库出现在列表；该库恰好 1 篇文档，
   数据库 ``content`` 与文件内容逐字一致、``index_status=pending``、
   ``knowledge_base_id`` 正确；**不创建 Chroma collection**（不自动索引）；
2. **Case 2**：**回归** —— JSON 形态（带 / 不带 ``description``）行为不变：
   201、不产生任何文档、响应字段与 Stage 16 一致；
3. **Case 3 / 4**：multipart 但**没有文件**（纯表单字段 / 浏览器未选文件时的空
   ``file`` 部分）→ 201 且不产生文档；
4. **Case 5**：**事务性** —— 不支持扩展名 400 / 空文件 400 / 损坏 ``.docx`` 400 /
   超过大小上限 413，且四种失败**都不留孤儿知识库**；文件名目录穿越被剥离为安全基础名；
5. **Case 6**：校验与权限 —— multipart 缺 ``name`` / 空 ``name`` / 超长 ``name``、
   urlencoded 超长 ``name`` 与非法 JSON 体均 422 ``VALIDATION_ERROR``；
   urlencoded 合法请求 → 201 仅建库（且不产生文档）；JSON 缺 ``name`` 仍 422（回归）；
   未登录 401、普通用户 403，且都不产生知识库；
6. **Case 7**：**端到端** —— multipart 建库（``.txt``）→ 显式索引 → 200 ``indexed``、
   collection 出现且 chunk 数 > 0、chunk 文本来自上传内容 → 删除库 → 文档级联删除、
   collection 真正消失；
7. **Case 8 / 生命周期**：项目内不出现 ``uploads/`` 目录、上传临时目录已清理、
   ``documents`` 表没有 ``file_path`` / ``storage_key`` / ``oss_url`` 等字段、
   OpenAPI 同时声明三种请求形态（既有 JSON 契约未丢失）、Stage 22 的
   ``.../documents/upload`` 路由仍在；
8. **前端接线静态检查**：``api`` / ``stores`` / 视图均已接入 multipart 建库。

约定
----
- 只调用既有 HTTP 接口，**不修改任何架构**（Agent / LLM / Conversation / Alembic 均不涉及）；
- 不新增上传目录、不给 ``documents`` 表加字段、不保存原始文件；
- 测试数据（``s23-*`` 用户 / ``S23知识库-*`` / 残留 collection）全部清理；
- 不读取、不打印任何密钥。
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
CREATE_URL = "/knowledge-bases"

#: DOCX（OOXML）主文档命名空间
DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

TXT_CONTENT = (
    "员工差旅政策（TXT）：\n"
    "1. 出差前需在系统提交申请并获得直属主管审批。\n"
    "2. 高铁二等座与经济舱机票可全额报销。\n"
)
MD_CONTENT = (
    "# 员工手册（MD）\n\n"
    "- 每周一 10:00 召开全员例会；\n"
    "- 年假需提前 3 个工作日在系统提交。\n"
)

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
# 测试用文件构造（不依赖任何额外库）
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


def upload_files(name: str, content: bytes, mime: str = "text/plain"):
    """构造 httpx 的 multipart 文件参数（表单字段名固定为 ``file``）。"""
    return {"file": (name, content, mime)}


def form_field(value: str):
    """构造 multipart 里的**普通表单字段**（无 ``filename``，不是文件部分）。"""
    return (None, value)


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
            "SELECT COUNT(e.id) FROM embeddings e "
            "JOIN segments s ON s.id = e.segment_id "
            "JOIN collections c ON c.id = s.collection "
            "WHERE c.name = ?",
            (collection_name,),
        ).fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def chroma_document_texts(collection_name: str) -> list[str]:
    """读取某个 collection 中的 chunk 原文（用于验证 KB → Chroma 全链路）。"""
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
    """按既有命名约定推导 collection 名（不复用被测函数）。"""
    return f"{settings.VECTOR_COLLECTION_NAME}__kb_{knowledge_base_id}"


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
    """兜底：删除本测试新建、但结束时仍残留的 collection。

    只清理**测试开始前不存在**的 collection，绝不触碰既有向量库。
    """
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    leftovers = [
        collection.name
        for collection in client.list_collections()
        if collection.name not in COLLECTIONS_BEFORE
    ]
    for name in leftovers:
        client.delete_collection(name)
        print(f"[cleanup] 兜底清理残留 collection: {name}")
    del client
    return leftovers


def summary_and_exit() -> None:
    print("\n" + "=" * 76)
    print("Stage 23 建库 + 首份文档（multipart）验收汇总")
    print("-" * 76)
    print(f"PASS = {len(PASS)}   FAIL = {len(FAIL)}")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 76)
    sys.exit(1 if FAIL else 0)


def static_frontend_checks() -> None:
    """前端接线静态检查（与既有验收脚本的做法一致）。"""
    api_source = read_frontend_file("api/knowledgeBase.ts")
    store_source = read_frontend_file("stores/knowledgeBase.ts")
    view_source = read_frontend_file("views/KnowledgeBaseView.vue")

    check(
        "前端: api 新增 createKnowledgeBaseWithUpload（同一端点 + FormData）",
        "export async function createKnowledgeBaseWithUpload" in api_source
        and "formData.append('file', file)" in api_source
        and "'/knowledge-bases'" in api_source,
    )
    check(
        "前端: store 暴露 createKnowledgeBaseWithUpload（含 uploading 状态）",
        "createKnowledgeBaseWithUpload as createKnowledgeBaseWithUploadApi"
        in store_source
        and "async function createKnowledgeBaseWithUpload" in store_source
        and "createKnowledgeBaseWithUpload," in store_source
        and "uploading.value = true" in store_source,
    )
    check(
        "前端: 新建知识库表单含可选「首份文档」（仅 create 模式）",
        "kbFormMode === 'create'" in view_source
        and "file-input--inline" in view_source
        and "onKbFileSelected" in view_source
        and "store.createKnowledgeBaseWithUpload(" in view_source,
    )
    check(
        "前端: Stage 22 详情页上传入口保持不变（未破坏既有能力）",
        'type="file"' in view_source
        and "store.uploadDocument(" in view_source
        and "openFilePicker" in view_source,
    )


def openapi_contract_checks() -> None:
    """OpenAPI 契约检查：一个端点同时声明三种请求形态，且路由未被破坏。"""
    schema = app.openapi()
    paths = schema["paths"]
    post = paths[CREATE_URL]["post"]
    content = post.get("requestBody", {}).get("content", {})
    components = schema.get("components", {}).get("schemas", {})

    def resolve(spec: dict) -> dict:
        """跟随 ``$ref`` 取真实 schema（FastAPI 对表单体固定生成 ``$ref``）。"""
        ref = spec.get("$ref")
        if not ref:
            return spec
        return components.get(ref.rsplit("/", 1)[-1], {})

    def props(content_type: str) -> set[str]:
        spec = resolve(content.get(content_type, {}).get("schema", {}))
        return set(spec.get("properties", {}))

    check(
        "契约: POST /knowledge-bases 同时声明 multipart / urlencoded / JSON 三种形态",
        {
            "multipart/form-data",
            "application/x-www-form-urlencoded",
            "application/json",
        }
        <= set(content),
        f"{sorted(content)}",
    )
    json_schema = resolve(content.get("application/json", {}).get("schema", {}))
    check(
        "契约: JSON 形态仍文档化 name（required）+ description（既有契约未丢失）",
        "name" in json_schema.get("properties", {})
        and json_schema.get("required") == ["name"],
        f"{json_schema.get('required')}",
    )
    multipart_props = props("multipart/form-data")
    check(
        "契约: multipart 形态声明 name / description / file 字段",
        {"name", "description", "file"} <= multipart_props,
        f"{sorted(multipart_props)}",
    )
    urlencoded_props = props("application/x-www-form-urlencoded")
    check(
        "契约: urlencoded（无文件表单）形态声明 name / description 且不含 file",
        {"name", "description"} <= urlencoded_props
        and "file" not in urlencoded_props,
        f"{sorted(urlencoded_props)}",
    )
    check(
        "契约: Stage 22 的 .../documents/upload 路由仍存在（未被改动）",
        "/knowledge-bases/{knowledge_base_id}/documents/upload" in paths,
    )


async def run_all() -> None:  # noqa: C901
    """执行 Case 1 - Case 8（测试数据统一使用 ``s23-`` / ``S23知识库-`` 前缀）。"""
    global COLLECTIONS_BEFORE

    tag = uuid.uuid4().hex[:8]
    pwd = "Passw0rd!23"
    admin_name = f"s23-admin-{tag}"
    admin_email = f"{admin_name}@example.com"
    user_name = f"s23-user-{tag}"
    user_email = f"{user_name}@example.com"
    kb_prefix = f"S23知识库-{tag}"

    temp_root = Path(tempfile.gettempdir())
    temp_dirs_before = {item.name for item in temp_root.glob("kb_upload_*")}
    COLLECTIONS_BEFORE = chroma_collection_names()
    document_columns = await load_document_columns()

    # ==============================================================
    # Case 8（前半）：OpenAPI 契约
    # ==============================================================
    print("\n=== Case 8: OpenAPI 契约（两种请求形态）===")
    openapi_contract_checks()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://s23.test", timeout=120.0
    ) as client:
        # ==============================================================
        # 准备：管理员 + 普通用户
        # ==============================================================
        print("\n=== 准备: 测试用户 ===")
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
        admin_h = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        user_h = {"Authorization": f"Bearer {user_login.json()['access_token']}"}
        check(
            "准备: 管理员 / 普通用户均可登录（测试前置条件成立）",
            admin_login.status_code == 200 and user_login.status_code == 200,
            f"{admin_login.status_code}/{user_login.status_code}",
        )
        documents_before = await db_scalar("SELECT COUNT(*) FROM documents")

        # ==============================================================
        # Case 1：multipart + 文件 → 建库 + 首份文档（一次请求）
        # ==============================================================
        print("\n=== Case 1: multipart 建库 + 首份 .md 文档 → 201 ===")
        kb1_name = f"{kb_prefix}-md"
        md_name = f"员工手册-{tag}.md"
        resp1 = await client.post(
            CREATE_URL,
            data={"name": kb1_name, "description": "Stage23 建库并导入首份文档"},
            files=upload_files(md_name, MD_CONTENT.encode("utf-8"), "text/markdown"),
            headers=admin_h,
        )
        check(
            "Case 1: multipart 建库 + 文件 → 201",
            resp1.status_code == 201,
            f"{resp1.status_code} {resp1.text[:200]}",
        )
        kb1 = resp1.json() if resp1.status_code == 201 else {}
        kb1_id = kb1.get("id")
        check(
            "Case 1: 返回体仍是知识库对象（name / description 正确）",
            kb1.get("name") == kb1_name
            and kb1.get("description") == "Stage23 建库并导入首份文档"
            and isinstance(kb1_id, int),
            f"{kb1}",
        )
        params1 = {"k": kb1_id}
        doc1_count = (
            await db_scalar(
                "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
                params1,
            )
            if isinstance(kb1_id, int)
            else -1
        )
        check(
            "Case 1: 同一个请求内创建了恰好 1 篇文档（不是 0，也不是多篇）",
            doc1_count == 1,
            f"{doc1_count}",
        )
        doc1_id = (
            await db_scalar(
                "SELECT id FROM documents WHERE knowledge_base_id = :k", params1
            )
            if doc1_count == 1
            else None
        )
        if doc1_id is not None:
            doc1 = (
                await db_scalar(
                    "SELECT content FROM documents WHERE id = :i", {"i": doc1_id}
                ),
                await db_scalar(
                    "SELECT name FROM documents WHERE id = :i", {"i": doc1_id}
                ),
                await db_scalar(
                    "SELECT index_status FROM documents WHERE id = :i",
                    {"i": doc1_id},
                ),
            )
        else:
            doc1 = ("", "", "")
        check(
            "Case 1: 文档正文与上传文件内容逐字一致（解析结果落库）",
            doc1[0] == MD_CONTENT,
            f"{str(doc1[0])[:80]}",
        )
        check(
            "Case 1: 文档 name = 上传文件名，index_status = pending（不自动索引）",
            (doc1[1], doc1[2]) == (md_name, "pending"),
            f"{doc1[1]} / {doc1[2]}",
        )
        check(
            "Case 1: 上传不创建 Chroma collection（向量库仍无该 KB）",
            kb_collection_name(kb1_id) not in chroma_collection_names(),
            f"{sorted(chroma_collection_names())}",
        )
        listing1 = await client.get("/knowledge-bases", headers=admin_h)
        check(
            "Case 1: 知识库列表可查到新建的知识库",
            listing1.status_code == 200
            and any(item["id"] == kb1_id for item in listing1.json()),
            f"{listing1.status_code}",
        )
        docs1 = await client.get(
            f"/knowledge-bases/{kb1_id}/documents", headers=admin_h
        )
        check(
            "Case 1: 详情页文档列表可见首份文档（pending）",
            docs1.status_code == 200
            and [item["index_status"] for item in docs1.json()] == ["pending"],
            f"{docs1.status_code} {docs1.text[:160]}",
        )

        # ==============================================================
        # Case 2：JSON 形态回归（Stage 16 行为不变）
        # ==============================================================
        print("\n=== Case 2: JSON 形态回归（不带文件）===")
        json_full = await client.post(
            CREATE_URL,
            json={"name": f"{kb_prefix}-json", "description": "JSON 形态回归"},
            headers=admin_h,
        )
        check(
            "Case 2: JSON 建库（含 description）→ 201（既有行为不变）",
            json_full.status_code == 201,
            f"{json_full.status_code} {json_full.text[:200]}",
        )
        json_body = json_full.json() if json_full.status_code == 201 else {}
        check(
            "Case 2: 响应字段与 Stage 16 一致（5 个字段）",
            set(json_body)
            == {"id", "name", "description", "created_at", "updated_at"},
            f"{sorted(json_body)}",
        )
        json_min = await client.post(
            CREATE_URL,
            json={"name": f"{kb_prefix}-json-min"},
            headers=admin_h,
        )
        check(
            "Case 2: JSON 建库（省略 description）→ 201 且 description 为空串",
            json_min.status_code == 201
            and json_min.json().get("description") == "",
            f"{json_min.status_code} {json_min.text[:160]}",
        )
        json_min_id = (
            json_min.json().get("id") if json_min.status_code == 201 else -1
        )
        check(
            "Case 2: JSON 形态不产生任何文档（不会被文件逻辑牵连）",
            await db_scalar(
                "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
                {"k": json_min_id},
            )
            == 0,
        )

        # ==============================================================
        # Case 3 / 4：multipart 但实际没有文件
        # ==============================================================
        print("\n=== Case 3/4: multipart 但实际没有文件 ===")
        kb3_name = f"{kb_prefix}-form"
        resp3 = await client.post(
            CREATE_URL,
            files={
                "name": form_field(kb3_name),
                "description": form_field("只有表单字段，没有文件"),
            },
            headers=admin_h,
        )
        kb3 = resp3.json() if resp3.status_code == 201 else {}
        check(
            "Case 3: multipart 纯表单字段（无 file 字段）→ 201 仅建库",
            resp3.status_code == 201 and kb3.get("name") == kb3_name,
            f"{resp3.status_code} {resp3.text[:200]}",
        )
        check(
            "Case 3: 无 file 时不创建文档",
            isinstance(kb3.get("id"), int)
            and await db_scalar(
                "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
                {"k": kb3["id"]},
            )
            == 0,
        )
        kb4_name = f"{kb_prefix}-empty-part"
        resp4 = await client.post(
            CREATE_URL,
            data={"name": kb4_name, "description": "空 file 部分"},
            files={"file": ("", b"", "application/octet-stream")},
            headers=admin_h,
        )
        kb4 = resp4.json() if resp4.status_code == 201 else {}
        check(
            "Case 4: 空 file 部分（模拟浏览器未选文件）→ 201 仅建库",
            resp4.status_code == 201 and kb4.get("name") == kb4_name,
            f"{resp4.status_code} {resp4.text[:200]}",
        )
        check(
            "Case 4: 空 file 部分不创建文档（也不会误报「不支持的类型」）",
            isinstance(kb4.get("id"), int)
            and await db_scalar(
                "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
                {"k": kb4["id"]},
            )
            == 0,
        )

        # ==============================================================
        # Case 5：事务性 —— 文件失败绝不留孤儿知识库
        # ==============================================================
        print("\n=== Case 5: 文件失败 → 整体回滚（无孤儿知识库）===")

        async def kb_exists(candidate: str) -> int:
            """按名称统计知识库数量（>0 说明事务没有回滚）。"""
            return await db_scalar(
                "SELECT COUNT(*) FROM knowledge_bases WHERE name = :n",
                {"n": candidate},
            )

        bad_ext_name = f"{kb_prefix}-bad-ext"
        resp5a = await client.post(
            CREATE_URL,
            data={"name": bad_ext_name, "description": "不支持的类型"},
            files=upload_files(
                "payload.exe", b"MZ\x90\x00binary", "application/octet-stream"
            ),
            headers=admin_h,
        )
        check(
            "Case 5a: 不支持的扩展名 → 400",
            resp5a.status_code == 400,
            f"{resp5a.status_code} {resp5a.text[:160]}",
        )
        check(
            "Case 5a: 不产生孤儿知识库（知识库随文档失败一并回滚）",
            await kb_exists(bad_ext_name) == 0,
        )

        empty_file_name = f"{kb_prefix}-empty-file"
        resp5b = await client.post(
            CREATE_URL,
            data={"name": empty_file_name, "description": "空文件"},
            files=upload_files(f"empty-{tag}.txt", b"", "text/plain"),
            headers=admin_h,
        )
        check(
            "Case 5b: 空文件 → 400",
            resp5b.status_code == 400,
            f"{resp5b.status_code} {resp5b.text[:160]}",
        )
        check(
            "Case 5b: 不产生孤儿知识库",
            await kb_exists(empty_file_name) == 0,
        )

        broken_name = f"{kb_prefix}-broken-docx"
        resp5c = await client.post(
            CREATE_URL,
            data={"name": broken_name, "description": "损坏的 docx"},
            files=upload_files(
                f"broken-{tag}.docx",
                b"this is not a zip package",
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            ),
            headers=admin_h,
        )
        check(
            "Case 5c: 损坏的 .docx（解析失败）→ 400",
            resp5c.status_code == 400,
            f"{resp5c.status_code} {resp5c.text[:160]}",
        )
        check(
            "Case 5c: 解析失败不产生孤儿知识库",
            await kb_exists(broken_name) == 0,
        )

        big_name = f"{kb_prefix}-too-big"
        original_limit = settings.MAX_UPLOAD_FILE_SIZE_MB
        settings.MAX_UPLOAD_FILE_SIZE_MB = 2
        try:
            resp5d = await client.post(
                CREATE_URL,
                data={"name": big_name, "description": "超过大小上限"},
                files=upload_files(
                    f"big-{tag}.txt",
                    b"A" * (2 * 1024 * 1024 + 1024),
                    "text/plain",
                ),
                headers=admin_h,
            )
        finally:
            settings.MAX_UPLOAD_FILE_SIZE_MB = original_limit
        check(
            "Case 5d: 超过大小上限 → 413",
            resp5d.status_code == 413,
            f"{resp5d.status_code} {resp5d.text[:160]}",
        )
        check(
            "Case 5d: 413 不产生孤儿知识库",
            await kb_exists(big_name) == 0,
        )
        check(
            "Case 5d: 大小上限配置已恢复（测试未污染运行时配置）",
            settings.MAX_UPLOAD_FILE_SIZE_MB == original_limit,
            f"{settings.MAX_UPLOAD_FILE_SIZE_MB}",
        )

        traversal_name = f"{kb_prefix}-traversal"
        resp5e = await client.post(
            CREATE_URL,
            data={"name": traversal_name, "description": "目录穿越文件名"},
            files=upload_files(
                "../../evil.txt", TXT_CONTENT.encode("utf-8"), "text/plain"
            ),
            headers=admin_h,
        )
        check(
            "Case 5e: 目录穿越文件名仍成功建库 → 201",
            resp5e.status_code == 201,
            f"{resp5e.status_code} {resp5e.text[:160]}",
        )
        traversal_id = (
            resp5e.json().get("id") if resp5e.status_code == 201 else None
        )
        traversal_doc_name = (
            await db_scalar(
                "SELECT name FROM documents WHERE knowledge_base_id = :k",
                {"k": traversal_id},
            )
            if isinstance(traversal_id, int)
            else ""
        )
        check(
            "Case 5e: 文档名收敛为安全基础名 evil.txt（无路径成分）",
            traversal_doc_name == "evil.txt",
            f"{traversal_doc_name}",
        )

        # ==============================================================
        # Case 6：校验与权限
        # ==============================================================
        print("\n=== Case 6: 校验 / 权限 ===")

        def is_validation_error(response: httpx.Response) -> bool:
            """422 + 统一错误码 VALIDATION_ERROR（与 JSON 形态同结构）。"""
            if response.status_code != 422:
                return False
            return response.json().get("error", {}).get("code") == "VALIDATION_ERROR"

        documents_before_case6 = await db_scalar(
            "SELECT COUNT(*) FROM documents"
        )
        resp6a = await client.post(
            CREATE_URL,
            files={"description": form_field("缺少 name")},
            headers=admin_h,
        )
        check(
            "Case 6a: multipart 缺 name → 422 VALIDATION_ERROR（与 JSON 同结构）",
            is_validation_error(resp6a),
            f"{resp6a.status_code} {resp6a.text[:200]}",
        )
        resp6b = await client.post(
            CREATE_URL,
            files={"name": form_field(""), "description": form_field("空名称")},
            headers=admin_h,
        )
        check(
            "Case 6b: multipart name 为空串 → 422（min_length 生效）",
            is_validation_error(resp6b),
            f"{resp6b.status_code} {resp6b.text[:200]}",
        )
        resp6c = await client.post(
            CREATE_URL,
            files={
                "name": form_field("N" * 101),
                "description": form_field("超长名称"),
            },
            headers=admin_h,
        )
        check(
            "Case 6c: multipart name 超长（101）→ 422（max_length 生效）",
            is_validation_error(resp6c),
            f"{resp6c.status_code} {resp6c.text[:200]}",
        )
        # urlencoded（HTML <form> 的默认编码，不带文件）：必须走表单解析分支，
        # 否则 FastAPI 已消费请求体，再读 JSON 会抛 Stream consumed（500）。
        resp6d = await client.post(
            CREATE_URL,
            data={"name": "N" * 101, "description": "urlencoded 超长名称"},
            headers=admin_h,
        )
        check(
            "Case 6d: urlencoded 表单 name 超长（101）→ 422（不是 500 / 连接中断）",
            is_validation_error(resp6d),
            f"{resp6d.status_code} {resp6d.text[:200]}",
        )
        urlencoded_name = f"{kb_prefix}-urlencoded"
        resp6e = await client.post(
            CREATE_URL,
            data={"name": urlencoded_name, "description": "urlencoded 仅建库"},
            headers=admin_h,
        )
        urlencoded_kb = resp6e.json() if resp6e.status_code == 201 else {}
        check(
            "Case 6e: urlencoded 表单（无文件）→ 201 仅建库",
            resp6e.status_code == 201
            and urlencoded_kb.get("name") == urlencoded_name,
            f"{resp6e.status_code} {resp6e.text[:200]}",
        )
        check(
            "Case 6e: urlencoded 形态不创建文档",
            isinstance(urlencoded_kb.get("id"), int)
            and await db_scalar(
                "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
                {"k": urlencoded_kb["id"]},
            )
            == 0,
        )
        resp6f = await client.post(
            CREATE_URL,
            content=b"{not-json",
            headers={**admin_h, "Content-Type": "application/json"},
        )
        check(
            "Case 6f: 非法 JSON 体 → 422 VALIDATION_ERROR（既有行为不变）",
            is_validation_error(resp6f),
            f"{resp6f.status_code} {resp6f.text[:200]}",
        )
        resp6g = await client.post(
            CREATE_URL, json={"description": "缺少 name"}, headers=admin_h
        )
        check(
            "Case 6g: JSON 缺 name → 422（Stage 16 回归）",
            is_validation_error(resp6g),
            f"{resp6g.status_code} {resp6g.text[:200]}",
        )

        anon_name = f"{kb_prefix}-anon"
        resp6h = await client.post(
            CREATE_URL,
            data={"name": anon_name, "description": "未登录"},
            files=upload_files(f"anon-{tag}.txt", TXT_CONTENT.encode("utf-8")),
        )
        check(
            "Case 6h: 未登录 multipart 建库 → 401",
            resp6h.status_code == 401,
            f"{resp6h.status_code} {resp6h.text[:160]}",
        )
        check(
            "Case 6h: 未登录不产生知识库",
            await kb_exists(anon_name) == 0,
        )

        forbidden_name = f"{kb_prefix}-forbidden"
        resp6i = await client.post(
            CREATE_URL,
            data={"name": forbidden_name, "description": "普通用户"},
            files=upload_files(f"user-{tag}.txt", TXT_CONTENT.encode("utf-8")),
            headers=user_h,
        )
        check(
            "Case 6i: 普通用户 multipart 建库 → 403",
            resp6i.status_code == 403,
            f"{resp6i.status_code} {resp6i.text[:160]}",
        )
        check(
            "Case 6i: 403 不产生知识库",
            await kb_exists(forbidden_name) == 0,
        )
        documents_after_case6 = await db_scalar("SELECT COUNT(*) FROM documents")
        check(
            "Case 6: 校验 / 权限失败均未改变文档总量（无副作用）",
            documents_after_case6 == documents_before_case6,
            f"{documents_before_case6} -> {documents_after_case6}",
        )

        # ==============================================================
        # Case 7：端到端 —— 建库（含首份文档）→ 索引 → 删除
        # ==============================================================
        print("\n=== Case 7: 端到端（建库 + 首份文档 → 索引 → 删除）===")
        e2e_name = f"{kb_prefix}-e2e"
        txt_file_name = f"差旅政策-{tag}.txt"
        resp7 = await client.post(
            CREATE_URL,
            data={"name": e2e_name, "description": "Stage23 端到端"},
            files=upload_files(
                txt_file_name, TXT_CONTENT.encode("utf-8"), "text/plain"
            ),
            headers=admin_h,
        )
        kb7 = resp7.json() if resp7.status_code == 201 else {}
        kb7_id = kb7.get("id")
        collection7 = kb_collection_name(kb7_id) if isinstance(kb7_id, int) else ""
        check(
            "Case 7: multipart 建库 → 201",
            resp7.status_code == 201 and isinstance(kb7_id, int),
            f"{resp7.status_code} {resp7.text[:200]}",
        )
        docs7 = await client.get(
            f"/knowledge-bases/{kb7_id}/documents", headers=admin_h
        )
        docs7_body = docs7.json() if docs7.status_code == 200 else []
        check(
            "Case 7: 文档列表含首份文档（名称 / pending）",
            len(docs7_body) == 1
            and docs7_body[0]["name"] == txt_file_name
            and docs7_body[0]["index_status"] == "pending",
            f"{docs7_body}",
        )
        doc7_id = docs7_body[0]["id"] if docs7_body else None
        index7 = await client.post(
            f"/knowledge-bases/{kb7_id}/documents/{doc7_id}/index", headers=admin_h
        )
        check(
            "Case 7: 显式索引 → 200 indexed（建库产物可被正常索引）",
            index7.status_code == 200 and index7.json().get("status") == "indexed",
            f"{index7.status_code} {index7.text[:200]}",
        )
        check(
            "Case 7: collection 已创建且 chunk 数 > 0",
            collection7 in chroma_collection_names()
            and chroma_embedding_count(collection7) > 0,
            f"{sorted(chroma_collection_names())} / "
            f"{chroma_embedding_count(collection7)}",
        )
        chunk_texts = chroma_document_texts(collection7)
        check(
            "Case 7: chunk 文本来自上传文件内容（Document → Chroma 全链路）",
            any(
                ("差旅政策" in chunk) or ("高铁二等座" in chunk)
                for chunk in chunk_texts
            ),
            f"{str(chunk_texts)[:160]}",
        )
        del7 = await client.delete(f"/knowledge-bases/{kb7_id}", headers=admin_h)
        check(
            "Case 7: 删除知识库 → 204",
            del7.status_code == 204,
            f"{del7.status_code} {del7.text[:160]}",
        )
        check(
            "Case 7: 文档级联删除",
            await db_scalar(
                "SELECT COUNT(*) FROM documents WHERE knowledge_base_id = :k",
                {"k": kb7_id},
            )
            == 0,
        )
        check(
            "Case 7: Chroma collection 真正消失（保持 Stage 21 行为）",
            collection7 not in chroma_collection_names(),
            f"{sorted(chroma_collection_names())}",
        )
        check(
            "Case 7: 全流程结束后文档总量 = 测试前 + 2（Case 1 与 Case 5e 各 1 篇）",
            await db_scalar("SELECT COUNT(*) FROM documents")
            == documents_before + 2,
            f"{documents_before} -> "
            f"{await db_scalar('SELECT COUNT(*) FROM documents')}",
        )

    # ==============================================================
    # Case 8（后半）：生命周期约束
    # ==============================================================
    print("\n=== Case 8: 生命周期约束 ===")
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
    info(f"生命周期: documents 表字段 = {sorted(document_columns)}")
    static_frontend_checks()


async def teardown() -> None:
    """清理测试命名空间（``s23-*`` 用户 / ``S23知识库-*`` / 残留 collection）。"""
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM users WHERE username LIKE 's23-%'")
            )
            await conn.execute(
                text("DELETE FROM knowledge_bases WHERE name LIKE 'S23知识库-%'")
            )
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响汇总输出
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
        "SELECT COUNT(*) FROM users WHERE username LIKE 's23-%'"
    )
    leftover_kbs = await db_scalar(
        "SELECT COUNT(*) FROM knowledge_bases WHERE name LIKE 'S23知识库-%'"
    )
    check(
        "收尾: PostgreSQL 测试数据已清理（用户 / 知识库均无残留）",
        leftover_users == 0 and leftover_kbs == 0,
        f"users={leftover_users} kbs={leftover_kbs}",
    )
    check(
        "收尾: .chroma 与测试前快照一致（未留下任何 collection）",
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



