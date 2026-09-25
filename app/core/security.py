"""基于请求头的操作人身份与责任范围解析。

接口调用方（网关/前端）通过以下请求头传入已认证身份：
  X-User-Id    操作人账号
  X-User-Name  操作人姓名
  X-User-Role  角色: manager(就业管理处) / operator(学院经办) / reviewer(复核人)
  X-College-Id 学院经办人的责任学院ID（operator 用于数据隔离）
  X-College-Name 责任学院名称
"""

from dataclasses import dataclass
from typing import Optional

from fastapi import Header, HTTPException

from app.models import UserRole


@dataclass
class CurrentUser:
    user_id: str
    user_name: str
    role: UserRole
    college_id: Optional[int] = None
    college_name: Optional[str] = None

    @property
    def is_manager(self) -> bool:
        return self.role == UserRole.MANAGER

    @property
    def is_operator(self) -> bool:
        return self.role == UserRole.OPERATOR

    @property
    def is_reviewer(self) -> bool:
        return self.role == UserRole.REVIEWER


def get_current_user(
    x_user_id: str = Header(..., alias="X-User-Id", description="操作人账号"),
    x_user_name: str = Header("未知", alias="X-User-Name", description="操作人姓名"),
    x_user_role: str = Header(..., alias="X-User-Role", description="manager/operator/reviewer"),
    x_college_id: Optional[str] = Header(None, alias="X-College-Id"),
    x_college_name: Optional[str] = Header(None, alias="X-College-Name"),
) -> CurrentUser:
    try:
        role = UserRole(x_user_role)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-User-Role 只能是 manager/operator/reviewer")

    college_id: Optional[int] = None
    if x_college_id:
        try:
            college_id = int(x_college_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="X-College-Id 必须为整数")

    return CurrentUser(
        user_id=x_user_id,
        user_name=x_user_name,
        role=role,
        college_id=college_id,
        college_name=x_college_name,
    )
