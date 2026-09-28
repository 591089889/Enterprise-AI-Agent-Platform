"""认证基础工具层。

仅负责密码哈希/验证和 JWT 生成，不依赖任何上层模块
（Router、Service、Repository、数据库）。
"""

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# ---------------------------------------------------------------------------
# bcrypt 密码上下文
# ---------------------------------------------------------------------------
# passlib 自动处理 salt 生成、算法选择和哈希格式
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def password_hash(password: str) -> str:
    """将明文密码转换为 bcrypt 哈希值。

    参数
    ----------
    password : str
        用户输入的明文密码。

    返回
    -------
    str
        bcrypt 哈希字符串，可直接存入数据库 ``hashed_password`` 字段。

    注意
    ----
    本函数只负责哈希运算，**不接触数据库**。
    调用方（Service 层）负责将返回值写入 User.hashed_password。
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证明文密码是否与哈希值匹配。

    参数
    ----------
    plain_password : str
        用户登录时输入的明文密码。
    hashed_password : str
        数据库中存储的 bcrypt 哈希值。

    返回
    -------
    bool
        True 表示密码匹配，False 表示不匹配。
    """
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None,
) -> str:
    """生成 JWT 访问令牌（HS256）。

    参数
    ----------
    data : dict
        要编码到 JWT payload 中的自定义数据（例如 ``{"sub": user_id}``）。
    expires_delta : timedelta | None
        令牌过期时长。为 None 时使用配置中的默认值
        ``ACCESS_TOKEN_EXPIRE_MINUTES``。

    返回
    -------
    str
        编码后的 JWT 字符串。
    """
    to_encode = data.copy()

    if expires_delta is not None:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})

    return jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
