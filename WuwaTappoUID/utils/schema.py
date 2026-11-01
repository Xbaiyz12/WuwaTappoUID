"""游戏接口的数据结构；缺失必要字段属于接口错误。"""

from typing import Literal

from pydantic import BaseModel, Field, JsonValue

BASE = "https://api-mc-wuwatappo.kurogames-global.com"
ORIGIN = "http://tauri.localhost"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 WuwaTappo/1.0"
CODE_OK = (0, 200)
CODE_TOKEN_INVALID = (10002, 10003)
TASK_SECONDS = 24 * 3600
RENEW_WINDOW = 60 * 60
AUTO_CLICKS = 10_000
# 请求发出这么久后该侧冷却仍未推进，就判定那次开箱没有生效（HTTP 超时为 25 秒）。
SETTLE_SECONDS = 120
ASSET_MANIFEST_PATH = "/app/asset-json/appearance_asset.json"
SIDE_NAMES = {"left": "左", "right": "右"}
Side = Literal["left", "right"]


class WuwaError(Exception):
    """可向用户展示的中文错误，不携带 token、URL 查询串或原始响应。"""


class ApiError(WuwaError):
    def __init__(self, code: int):
        self.code = code
        messages = {
            10002: "登录态失效（10002）",
            10003: "登录态失效（10003）",
            10014: "服务端正在处理另一项操作（10014）",
            70004: "当前没有可用宝箱（70004）",
            70005: "点击不足（70005）",
            70006: "宝箱仍在冷却（70006）",
            50002: "合成失败（50002）",
            50006: "未拥有该物品（50006）",
        }
        super().__init__(messages.get(code, f"游戏接口返回错误（{code}）"))


class NetworkError(WuwaError):
    """网络失败或服务端暂不可用；修改请求可能已经到达服务端。"""


class DataError(WuwaError):
    """响应数据不符合当前接口结构。"""


class AuthenticationError(WuwaError):
    """登录态失效且无法重新登录。"""


class Expired(Exception):
    """本轮已到期，不再提交操作。"""


class Envelope(BaseModel):
    code: int
    data: JsonValue = None


class LoginData(BaseModel):
    token: str


class DesktopCharacter(BaseModel):
    """user/me 当前桌宠外观；字段与官方客户端 appearanceAssetId/Name 一致。"""

    id: int | str
    name: str = ""


class UserInfo(BaseModel):
    id: int
    cuid: str | int
    nickname: str | None = None
    cname: str | None = None
    player_id: str | int
    server_id: str | int
    is_domestic: Literal[0, 1] | None = None
    character: DesktopCharacter | None = None
    total_click_count: int
    total_click_consume: int
    left_treasure_chest: int
    right_treasure_chest: int

    @property
    def remaining(self) -> int:
        return self.total_click_count - self.total_click_consume

    def cooldown(self, side: Side) -> float:
        ms = self.left_treasure_chest if side == "left" else self.right_treasure_chest
        return ms / 1000


class Item(BaseModel):
    asset_id: int | str
    asset_name: str | None = None
    asset_type: str = "其他"
    quantity: int = 1
    rarity: str | int | None = None


class OpenResult(BaseModel):
    asset_id: int | str
    asset_name: str | None = None
    rarity_type: str | int
    click_consume: int


class SynthesisResult(BaseModel):
    asset_id: int | str
    asset_name: str | None = None
    asset_type: str
    rarity_type: str | int


class Dictionary(BaseModel):
    character: dict[str, str]
    series: dict[str, str] = {}
    accessory: dict[str, str] = {}
    emote: dict[str, str] = {}


class RoleInfo(BaseModel):
    """角色查询接口的一项；只取展示需要的字段。"""

    playerId: int | str
    playerName: str = ""
    level: int | None = None
    serverId: str = ""
    headPhoto: str = ""


class RoleQuery(BaseModel):
    items: list[RoleInfo] = []


# 鸣潮服务器编号，取自 XutheringWavesUID 的 SERVER_ID / NET_SERVER_ID_MAP。
CN_SERVER_ID = "76402e5b20be2c39f095a152090afddc"
NET_SERVER_IDS = {
    "591d6af3a3090d8ea00d8f86cf6d7501",
    "6eb2a235b30d05efd77bedb5cf60999e",
    "86d52186155b148b5c138ceb41be9650",
    "919752ae5ea09c1ced910dd668a63ffb",
}


class AssetLayer(BaseModel):
    url: str


class AssetEntry(BaseModel):
    """外观清单中的主图、物品映射和桌宠 head1 头像层。"""

    url: str
    height_light_id: int | None = None
    head1: AssetLayer | None = None


class AssetManifest(BaseModel):
    character_skin_atlas: dict[str, AssetEntry] = {}
    accessory_atlas: dict[str, AssetEntry] = {}
    emote_atlas: dict[str, AssetEntry] = {}
    character_skin: dict[str, AssetEntry] = {}
    accessory: dict[str, AssetEntry] = {}
    emote: dict[str, AssetEntry] = {}

    def images(self) -> dict[str, str]:
        return {
            asset_id: entry.url
            for group in (self.character_skin, self.accessory, self.emote)
            for asset_id, entry in group.items()
        }

    def lookup(self, asset_id: str) -> str:
        for group in (self.character_skin, self.accessory, self.emote):
            entry = group.get(asset_id)
            if entry is not None:
                return entry.url
        return ""


class SessionImport(BaseModel):
    platform: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    cuid: str = Field(pattern=r"^[0-9]+$")
    domestic: Literal[0, 1] = 1
    token: str = Field(min_length=1, repr=False)
