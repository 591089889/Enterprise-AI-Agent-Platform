"""用户角色常量。

定义系统中所有有效的角色值，避免字符串散落在 Router / Service 中。

示例::

    from app.core.roles import ADMIN_ROLE, USER_ROLE

    if current_user.role == ADMIN_ROLE:
        ...
"""

# 普通用户角色（注册时的默认角色）
USER_ROLE = "user"

# 管理员角色（拥有更高权限）
ADMIN_ROLE = "admin"
