"""使用者與認證模型。"""

from pydantic import Field

from app.models.base import MESModel
from app.models.enums import Role


class UserIn(MESModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    full_name: str = ""
    employee_no: str = ""
    department: str = ""
    roles: list[Role] = Field(default_factory=lambda: [Role.VIEWER])
    #: 已受訓合格的站別代碼；站別若要求資格認證則必須包含該站
    certifications: list[str] = Field(default_factory=list)
    active: bool = True


class UserUpdate(MESModel):
    full_name: str | None = None
    employee_no: str | None = None
    department: str | None = None
    roles: list[Role] | None = None
    certifications: list[str] | None = None
    active: bool | None = None
    password: str | None = Field(default=None, min_length=6, max_length=128)


class UserOut(MESModel):
    username: str
    full_name: str = ""
    employee_no: str = ""
    department: str = ""
    roles: list[str] = []
    certifications: list[str] = []
    active: bool = True


class LoginIn(MESModel):
    username: str
    password: str


class Token(MESModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
