"""知识库管理 API Router（``/knowledge-bases``，Stage 16）。

职责
----
把「知识库管理」包装成 HTTP 接口，全部为**管理员**接口：

    knowledge_bases  ← 平台级资源（无 owner / tenant）
        └── documents（原始正文）
                └── index → 复用现有 RAG 引擎写入该知识库专属的 Chroma collection

文档正文有两个来源（Stage 22 起）：

1. **直接输入**：``POST /knowledge-bases/{id}/documents``（JSON：name + content）；
2. **文件上传**：``POST /knowledge-bases/{id}/documents/upload``（multipart/form-data），
   解析动作全部在 ``KnowledgeBaseService`` + ``app.rag.loader`` 内完成 ——
   Router 只接收文件流并转交 Service，**不解析文件内容、不接触 ChromaDB**。

Stage 23 起，``POST /knowledge-bases`` 自身也接受 ``multipart/form-data``：
同一请求里可选择性地带一个 ``file``（首份文档），等价于「先建库、再上传首份文档」
两步组合 —— 仍然复用上面的上传链路，Router 依旧不解析文件内容。

``POST /knowledge-bases`` 的请求形态（同一个端点 / 同一个响应结构）
------------------------------------------------------------------
- ``application/json``：Stage 16 起的既有形态，行为完全不变；
- ``multipart/form-data``：表单字段 ``name`` / ``description`` + 可选 ``file``；
- ``application/x-www-form-urlencoded``：HTML ``<form>`` 未设 ``enctype`` 且
  没有文件输入时的默认编码，等价于「multipart 但不带文件」，只建库。

薄层原则
--------
Router 只做：解析请求 → 调用 Service → 组装响应。
不触碰数据库 / Repository / Session，不含业务规则；事务由 ``get_db`` 统一负责。

权限
----
全部端点 ``Depends(require_admin)``：未登录 401、普通用户 403。
（前端菜单隐藏只是体验层，不能替代这里的后端校验。）

与 Agent Chat 的关系
--------------------
本模块**不参与**对话检索：Agent 与知识库的绑定属于后续阶段，
``AgentService`` / ``AgentRuntime`` / 默认 collection 均不受影响。
"""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.core.config import settings
from app.core.dependencies import get_knowledge_base_service, require_admin
from app.core.exceptions import AppException
from app.models.user import User
from app.schemas.document import (
    DocumentCreate,
    DocumentDetail,
    DocumentIndexResponse,
    DocumentSummary,
    DocumentUpdate,
)
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
)
from app.services.knowledge_base import KnowledgeBaseService

router = APIRouter(
    prefix="/knowledge-bases",
    tags=["knowledge-bases"],
)

#: ``POST /knowledge-bases`` 除 FastAPI 自动声明的 ``multipart/form-data`` 之外，
#: 仍接受 Stage 16 起的 JSON 体 ``{"name": ..., "description": ...}``，以及
#: 等价于「没有文件的 HTML 表单」的 ``application/x-www-form-urlencoded``。
#: FastAPI 无法在同一个 operation 上同时声明多种 body 形态，因此用
#: ``openapi_extra`` 把这两种补回 OpenAPI
#: （**只影响 /docs 与 /openapi.json 的文档，不影响运行期解析逻辑**）。
_EXTRA_CREATE_BODIES: dict[str, object] = {
    "content": {
        "application/json": {
            "schema": KnowledgeBaseCreate.model_json_schema(),
        },
        # 表单（不带文件）形态：字段全是字符串，因此 schema 与 JSON 形态同构。
        "application/x-www-form-urlencoded": {
            "schema": KnowledgeBaseCreate.model_json_schema(),
        },
    }
}

#: 视为「表单」解析的 ``Content-Type`` 前缀（字段由 FastAPI 的 ``Form`` 注入）。
#:
#: ``application/x-www-form-urlencoded`` 必须和 ``multipart/form-data`` 一样在
#: 读取请求体之前被识别出来：这类请求的 body 已由 FastAPI 的表单解析消费，
#: 再调用 ``request.json()`` 会抛 ``RuntimeError: Stream consumed``（500）。
_FORM_CONTENT_TYPE_PREFIXES = (
    "multipart/form-data",
    "application/x-www-form-urlencoded",
)


def _is_absent_upload(upload: UploadFile | None) -> bool:
    """判断 multipart 表单里的 ``file`` 字段是否「实际没有选择文件」。

    浏览器提交 ``<input type="file">`` 且用户未选择文件时，仍会发送一个
    ``filename=""`` 的空部分；此时应视为「只创建知识库」，
    而不是返回「不支持的文件类型」。
    """
    return upload is None or not (upload.filename or "").strip()


async def _resolve_create_payload(
    request: Request,
    name: str | None,
    description: str | None,
) -> KnowledgeBaseCreate:
    """把 ``POST /knowledge-bases`` 的三种请求形态统一解析为 ``KnowledgeBaseCreate``。

    - ``multipart/form-data`` / ``application/x-www-form-urlencoded``：
      取表单字段 ``name`` / ``description``（后者不可能带 ``file``）；
    - 其它（``application/json`` 等）：按 JSON 体解析，
      **保持 Stage 16 起的既有行为**（含非法 JSON → 422）。

    校验统一交给 :class:`KnowledgeBaseCreate`（规则与「Pydantic 直接解析请求体」
    完全相同）；失败转成 ``RequestValidationError``，由全局异常处理器返回
    422 ``VALIDATION_ERROR``，``loc`` 统一补 ``body`` 前缀以对齐 FastAPI
    直接解析请求体时的报错位置。
    """
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith(_FORM_CONTENT_TYPE_PREFIXES):
        raw: object = {"name": name, "description": description or ""}
    else:
        try:
            raw = await request.json()
        except ValueError as exc:
            raise RequestValidationError(
                [
                    {
                        "type": "json_invalid",
                        "loc": ("body",),
                        "msg": "JSON decode error",
                    }
                ]
            ) from exc

    try:
        return KnowledgeBaseCreate.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [
                {**error, "loc": ("body", *tuple(error.get("loc", ())))}
                for error in exc.errors()
            ]
        ) from exc


# ------------------------------------------------------------------
# Knowledge Base CRUD
# ------------------------------------------------------------------
@router.get(
    "",
    response_model=list[KnowledgeBaseResponse],
    summary="获取知识库列表",
    description="返回全部知识库。需要管理员权限。",
)
async def list_knowledge_bases(
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> list[KnowledgeBaseResponse]:
    """获取知识库列表（按创建时间正序）。"""
    knowledge_bases = await service.list_knowledge_bases()
    return [
        KnowledgeBaseResponse.model_validate(item) for item in knowledge_bases
    ]


@router.post(
    "",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库（可同时导入首份文档）",
    description=(
        "创建知识库（只写元数据，不涉及向量库）。需要管理员权限。\n\n"
        "同一个端点支持三种请求形态，响应结构完全一致：\n\n"
        "1. ``application/json``：``{\"name\": ..., \"description\": ...}``"
        "（Stage 16 起的既有形态，行为不变）；\n"
        "2. ``multipart/form-data``：表单字段 ``name`` / ``description``"
        " + **可选**的 ``file``；带 ``file`` 时在同一个请求内导入首份文档"
        "（等价于「建库 + 上传首份文档」两步），文档索引状态为 ``pending``，"
        "仍需显式调用索引接口，且**不保存原始文件**；\n"
        "3. ``application/x-www-form-urlencoded``：等价于第 2 种但不带文件"
        "（HTML ``<form>`` 的默认编码），只创建知识库。\n\n"
        "任一步骤失败时整个请求回滚：绝不会留下「建了库但文档没导入」的半成品。\n\n"
        "可选文件支持 ``.txt`` / ``.md`` / ``.pdf`` / ``.docx``；"
        f"单个文件上限 {settings.MAX_UPLOAD_FILE_SIZE_MB}MB。"
    ),
    openapi_extra={"requestBody": _EXTRA_CREATE_BODIES},
)
async def create_knowledge_base(
    request: Request,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> KnowledgeBaseResponse:
    """创建知识库；表单形态（multipart / urlencoded）可同时导入首份文档。

    JSON 请求时 ``Form`` / ``File`` 参数取到的都是「空表单」（全为 None），
    因此仍走手动解析分支 —— 行为与 Stage 16 完全一致（含 422 校验响应）。
    """
    data = await _resolve_create_payload(request, name, description)
    upload = None if _is_absent_upload(file) else file

    try:
        if upload is None:
            knowledge_base = await service.create_knowledge_base(data)
        else:
            knowledge_base, _document = (
                await service.create_knowledge_base_with_upload(
                    data,
                    filename=upload.filename or "",
                    stream=upload.file,
                )
            )
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    return KnowledgeBaseResponse.model_validate(knowledge_base)


@router.get(
    "/{knowledge_base_id}",
    response_model=KnowledgeBaseResponse,
    summary="获取知识库详情",
    description="按 ID 获取知识库。需要管理员权限。",
)
async def get_knowledge_base(
    knowledge_base_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> KnowledgeBaseResponse:
    """获取指定知识库。"""
    try:
        knowledge_base = await service.get_knowledge_base(knowledge_base_id)
        return KnowledgeBaseResponse.model_validate(knowledge_base)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.patch(
    "/{knowledge_base_id}",
    response_model=KnowledgeBaseResponse,
    summary="更新知识库",
    description="部分更新知识库（只更新传入的字段）。需要管理员权限。",
)
async def update_knowledge_base(
    knowledge_base_id: int,
    data: KnowledgeBaseUpdate,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> KnowledgeBaseResponse:
    """更新知识库。"""
    try:
        knowledge_base = await service.update_knowledge_base(
            knowledge_base_id, data
        )
        return KnowledgeBaseResponse.model_validate(knowledge_base)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.delete(
    "/{knowledge_base_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除知识库",
    description=(
        "删除知识库及其全部文档（数据库级联）。需要管理员权限。\n\n"
        "该知识库专属的向量 collection 会被尽力清理（失败不影响删除结果）。"
    ),
)
async def delete_knowledge_base(
    knowledge_base_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> None:
    """删除知识库（204，无响应体）。"""
    try:
        await service.delete_knowledge_base(knowledge_base_id)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ------------------------------------------------------------------
# Document CRUD（始终挂在某个知识库下）
# ------------------------------------------------------------------
@router.get(
    "/{knowledge_base_id}/documents",
    response_model=list[DocumentSummary],
    summary="获取文档列表",
    description=(
        "返回该知识库下的文档列表（**不含正文**，正文通过文档详情接口获取）。"
        "需要管理员权限。"
    ),
)
async def list_documents(
    knowledge_base_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> list[DocumentSummary]:
    """获取知识库下的文档列表（不含 content）。"""
    try:
        documents = await service.list_documents(knowledge_base_id)
        return [DocumentSummary.model_validate(item) for item in documents]
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post(
    "/{knowledge_base_id}/documents",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="创建文档",
    description=(
        "在知识库下创建文档（只保存原始正文，索引状态为 pending）。"
        "需要管理员权限。"
    ),
)
async def create_document(
    knowledge_base_id: int,
    data: DocumentCreate,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> DocumentDetail:
    """创建文档。"""
    try:
        document = await service.create_document(knowledge_base_id, data)
        return DocumentDetail.model_validate(document)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post(
    "/{knowledge_base_id}/documents/upload",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="上传文件创建文档",
    description=(
        "以 ``multipart/form-data`` 上传单个文件（表单字段 ``file``），"
        "后端解析为纯文本后保存为文档正文（索引状态 pending）。需要管理员权限。\n\n"
        "支持格式：``.txt`` / ``.md`` / ``.pdf`` / ``.docx``；"
        f"单个文件上限 {settings.MAX_UPLOAD_FILE_SIZE_MB}MB。\n\n"
        "**不保存原始文件**：文件在临时目录中解析后立即删除，数据库只保留提取出的"
        "正文；上传成功后需显式调用索引接口（``.../index``）才会写入向量库。"
    ),
)
async def upload_document(
    knowledge_base_id: int,
    file: Annotated[UploadFile, File(description="待上传的文件")],
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> DocumentDetail:
    """上传文件并创建文档（Router 只转交文件流，不解析内容）。"""
    try:
        document = await service.create_document_from_upload(
            knowledge_base_id,
            filename=file.filename or "",
            stream=file.file,
        )
        return DocumentDetail.model_validate(document)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get(
    "/{knowledge_base_id}/documents/{document_id}",
    response_model=DocumentDetail,
    summary="获取文档详情",
    description=(
        "返回文档完整内容。需要管理员权限。\n\n"
        "**资源关系校验**：文档不属于该知识库时返回 404（不泄露其它知识库的数据）。"
    ),
)
async def get_document(
    knowledge_base_id: int,
    document_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> DocumentDetail:
    """获取文档详情（含正文）。"""
    try:
        document = await service.get_document(knowledge_base_id, document_id)
        return DocumentDetail.model_validate(document)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.patch(
    "/{knowledge_base_id}/documents/{document_id}",
    response_model=DocumentDetail,
    summary="更新文档",
    description=(
        "部分更新文档。修改正文会把索引状态重置为 pending（需重新索引）。"
        "需要管理员权限。"
    ),
)
async def update_document(
    knowledge_base_id: int,
    document_id: int,
    data: DocumentUpdate,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> DocumentDetail:
    """更新文档。"""
    try:
        document = await service.update_document(
            knowledge_base_id, document_id, data
        )
        return DocumentDetail.model_validate(document)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.delete(
    "/{knowledge_base_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除文档",
    description=(
        "删除文档并同步重建该知识库的向量索引（避免已删除内容仍可被检索）。"
        "需要管理员权限。"
    ),
)
async def delete_document(
    knowledge_base_id: int,
    document_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> None:
    """删除文档（204，无响应体）。"""
    try:
        await service.delete_document(knowledge_base_id, document_id)
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ------------------------------------------------------------------
# 索引
# ------------------------------------------------------------------
@router.post(
    "/{knowledge_base_id}/documents/{document_id}/index",
    response_model=DocumentIndexResponse,
    summary="建立 / 重新建立文档索引",
    description=(
        "把该知识库的文档正文交给现有 RAG 引擎（切分 → 向量化 → 向量库）。需要管理员权限。\n\n"
        "**状态语义**：返回 ``status=indexed`` 表示确实写入了向量库；\n"
        "``status=failed`` 表示索引失败（``message`` 说明原因），**绝不谎报成功**。\n\n"
        "实现说明：重建粒度为「知识库」（清空该知识库的向量 collection 后按数据库内容全量重建），"
        "因此调用成功后该知识库下所有文档都处于最新索引状态。"
    ),
)
async def index_document(
    knowledge_base_id: int,
    document_id: int,
    admin: Annotated[User, Depends(require_admin)],
    service: Annotated[KnowledgeBaseService, Depends(get_knowledge_base_service)],
) -> DocumentIndexResponse:
    """为文档建立索引（返回真实状态，不伪造成功）。"""
    try:
        document, index_status, message = await service.index_document(
            knowledge_base_id, document_id
        )
        return DocumentIndexResponse(
            document_id=document.id,
            status=index_status,
            message=message,
        )
    except AppException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

