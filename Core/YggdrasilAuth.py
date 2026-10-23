from urllib.parse import urlparse, urlunparse
from hashlib import sha256, md5
from base64 import b64decode
from threading import Lock
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
import httpx
import json


class YggdrasilClient:
    def __init__(self):
        self.client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(15, connect=10),
            follow_redirects=True,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
        )

    @staticmethod
    def _to_https(url: str) -> str:
        """严格实现 Authlib-Injector 标准, 将协议头改为 https"""
        parsed = urlparse(url)

        # 已有协议 → 直接替换为 https 或 无协议，但以 // 开头（如 //example.com）→ 补上 https
        if parsed.scheme or parsed.netloc:
            parsed = parsed._replace(scheme="https")
        else:
            # 完全无协议（如 example.com/path）→ 手动补全 https:// 重新解析
            parsed = urlparse(f"https://{url}")

        # 显式解包成元组，确保类型明确，避免类型检查器误报
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment
        ))

    def follow_ali(self, url: str):
        """
        通过 ALI 找到 Yggdrasil 端点
        :param url: 提供 Yggdrasil 登录的网站 URL
        :return: Yggdrasil 端点
        """
        url = self._to_https(url)
        resp = self.client.head(url)
        resp.raise_for_status()
        ali_url = resp.headers.get("X-Authlib-Injector-API-Location")
        if ali_url:
            return ali_url.strip("/")
        return url.strip("/")

    def auth(self, url: str, username: str, password: str, follow_ali: bool = True, client_token: str | None = None) -> dict:
        """
        登录 Yggdrasil
        :param url: 提供 Yggdrasil 登录的网站 URL
        :param username: 用户名
        :param password: 密码
        :param follow_ali: 是否跟随 ALI
        :param client_token: 自定义客户端令牌
        :return:
        """
        root_url = self.follow_ali(url) if follow_ali else url.strip("/")
        auth_url = f"{root_url}/authserver/authenticate"
        payload = {
            "agent": {
                "name": "Minecraft",
                "version": 1
            },
            "username": username,
            "password": password,
            "clientToken": client_token or uuid4().hex,
            "requestUser": True  # 请求返回用户信息
        }

        resp = self.client.post(auth_url, json=payload)
        resp.raise_for_status()

        return resp.json()

    def validate(self, url: str, access_token: str, client_token: str, follow_ali: bool = True) -> bool:
        """
        检验令牌是否有效
        :param url: 提供 Yggdrasil 登录的网站 URL
        :param access_token: 登录令牌
        :param client_token: 客户端令牌
        :param follow_ali: 是否跟随 ALI
        :return: bool
        """
        root_url = self.follow_ali(url) if follow_ali else url.strip("/")
        validate_url = f"{root_url}/authserver/validate"

        payload = {
            "accessToken": access_token,
            "clientToken": client_token
        }

        resp = self.client.post(validate_url, json=payload)

        if resp.status_code == 204:
            return True
        return False

    def refresh(self, url: str, access_token: str, client_token: str, follow_ali: bool = True) -> dict:
        """
        :param url: 提供 Yggdrasil 登录的网站 URL
        :param access_token: 登录令牌
        :param client_token: 客户端令牌
        :param follow_ali: 是否跟随 ALI
        """
        root_url = self.follow_ali(url) if follow_ali else url.strip("/")
        refresh_url = f"{root_url}/authserver/refresh"

        payload = {
            "accessToken": access_token,
            "clientToken": client_token,
            "requestUser": True
        }

        resp = self.client.post(refresh_url, json=payload)
        resp.raise_for_status()

        return resp.json()

    def invalidate(self, url: str, access_token: str, client_token: str, follow_ali: bool = True) -> bool:
        """
        吊销指定令牌
        :param url: 提供 Yggdrasil 登录的网站 URL
        :param access_token: 登录令牌
        :param client_token: 客户端令牌
        :param follow_ali: 是否跟随 ALI
        :return: bool
        """
        root_url = self.follow_ali(url) if follow_ali else url.strip("/")
        invalidate_url = f"{root_url}/authserver/invalidate"

        payload = {
            "accessToken": access_token,
            "clientToken": client_token
        }

        resp = self.client.post(invalidate_url, json=payload)

        if resp.status_code == 204:
            return True
        return False

    def signout(self, url: str, username: str, password: str, follow_ali: bool = True) -> bool:
        """
        吊销用户的所有令牌
        :param url: 提供 Yggdrasil 登录的网站 URL
        :param username: 用户名
        :param password: 密码
        :param follow_ali: 是否跟随 ALI
        :return: bool
        """
        root_url = self.follow_ali(url) if follow_ali else url.strip("/")
        signout_url = f"{root_url}/authserver/signout"

        payload = {
            "username": username,
            "password": password
        }

        resp = self.client.post(signout_url, json=payload)

        if resp.status_code == 204:
            return True
        return False

    def get_skin(self, url: str, user_uuid: str, follow_ali: bool = True) -> dict:
        root_url = self.follow_ali(url) if follow_ali else url.strip("/")
        skin_url = f"{root_url}/sessionserver/session/minecraft/profile/{user_uuid}"

        resp = self.client.get(skin_url)
        resp.raise_for_status()

        skin_info = resp.json()
        properties = []
        for skin_property in skin_info["properties"]:
            properties.append({
                "name": skin_property["name"],
                "value": json.loads(b64decode(skin_property["value"]))
            })
        skin_info["properties"] = properties

        return skin_info

    def close(self):
        """关闭 HTTP 客户端"""
        if hasattr(self, "client"):
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self):
        self.close()


class YggdrasilAuthManager:
    def __init__(self, cache_path: Path | str | None = None):
        self.cache_path = Path(cache_path) if cache_path else Path.home() / ".ECL"
        self.cache_path = self.cache_path / "accounts"
        self.cache_path.mkdir(parents=True, exist_ok=True)

        self.account_list_file = self.cache_path / "yggdrasil_accounts_list.json"
        self.account_cache_path = self.cache_path / "yggdrasil_accounts"
        self.account_cache_path.mkdir(parents=True, exist_ok=True)

        self.yggdrasil_accounts: dict[str, dict] = {}   # account_id -> 账户信息
        self.yggdrasil_tokens: dict[str, dict[str, str]] = {}  # token_id -> (access_token, client_token)
        self.yggdrasil_client = YggdrasilClient()

        self._lock = Lock()
        self._load_accounts()

    def _load_accounts(self) -> None:
        """从文件加载账户列表"""
        if not self.account_list_file.is_file():
            return
        data = json.loads(self.account_list_file.read_text(encoding="utf-8"))
        for account_id, info in data.items():
            try:
                token_id = self._get_token_id(info["UserName"], info["YggdrasilAPI"])
                token_info_path = self.account_cache_path / f"{token_id}.json"
                if not token_info_path.is_file():
                    continue
                token_info = json.loads(
                    token_info_path.read_text(encoding="utf-8")
                )

                if not self.yggdrasil_client.validate(
                    url=info["YggdrasilAPI"],
                    access_token=token_info["AccessToken"],
                    client_token=token_info["ClientToken"],
                    follow_ali=False
                ):
                    refresh_info = self.yggdrasil_client.refresh(
                        url=info["YggdrasilAPI"],
                        access_token=token_info["AccessToken"],
                        client_token=token_info["ClientToken"],
                        follow_ali=False
                    )
                    token_info = {
                        "AccessToken": refresh_info["accessToken"],
                        "ClientToken": refresh_info["clientToken"]
                    }
                    self._save_account_cache(token_id, token_info)

                self.yggdrasil_tokens[account_id] = token_info
                self.yggdrasil_accounts[token_id] = info
            except Exception:
                # 单个账户的令牌刷新或缓存写入失败不影响其余账户；
                # 该账户保留原有状态，下次使用时重新尝试。
                pass
        self._save_account_list()

    def _save_account_list(self) -> None:
        self.account_list_file.write_text(
            json.dumps(self.yggdrasil_accounts, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def _save_account_cache(self, save_name: str, data) -> None:
        (self.account_cache_path / f"{save_name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    @staticmethod
    def _get_token_id(username: str, api_url: str) -> str:
        return md5(f"[{username}]@{api_url}".encode("utf-8")).hexdigest()

    def get_yggdrasil_accounts(self) -> dict:
        """
        返回当前所有账户信息的深拷贝
        :return: Microsoft Accounts
        """
        with self._lock:
            return deepcopy(self.yggdrasil_accounts)

    def auth_yggdrasil_account(self, url: str, username: str, password: str) -> dict:
        """
        登录 Yggdrasil (不会添加)
        :param url: 提供 Yggdrasil 登录的网站 URL
        :param username: 用户名
        :param password: 密码
        :return: 返回账户信息(注意, 包含 Token, 传给前端的内容应该去除 Token)
        """
        root_url = self.yggdrasil_client.follow_ali(url)

        auth_info = self.yggdrasil_client.auth(
            url=root_url,
            username=username,
            password=password,
            follow_ali=False,
            client_token=md5(username.encode("utf-8")).hexdigest()
        )

        auth_info["YggdrasilAPI"] = root_url
        auth_info["UserName"] = username

        return auth_info

    def add_yggdrasil_account(self, auth_info: dict, auth_uuid: str) -> str:
        """
        添加 Yggdrasil 账户
        :param auth_info: 账户信息(需要包含 Token 等信息)
        :param auth_uuid: 选择登录角色的 UUID
        :return: 账户 ID
        """
        with self._lock:
            if auth_uuid not in auth_info["availableProfiles"]:
                raise KeyError(f"账户中不存在角色 '{auth_uuid}'")

            token_info = {
                "AccessToken": auth_info["accessToken"],
                "ClientToken": auth_info["clientToken"]
            }

            token_id = self._get_token_id(auth_info["UserName"], auth_info["YggdrasilAPI"])
            self._save_account_cache(token_id, token_info)
            self.yggdrasil_tokens[token_id] = token_info
            auth_info.pop("accessToken", None)
            auth_info.pop("clientToken", None)

            for profile in auth_info["availableProfiles"]:
                if profile["id"] != auth_uuid:
                    continue
                auth_info["selectedProfile"] = profile
            if "selectedProfile" not in auth_info:
                auth_info["selectedProfile"] = auth_info["availableProfiles"][0]

            auth_info.pop("availableProfiles", None)
            account_id = uuid4().hex
            root_url = auth_info["YggdrasilAPI"]
            username = auth_info["UserName"]
            auth_info.pop("YggdrasilAPI", None)
            auth_info.pop("UserName", None)

            account_info = {
                "AccountId": account_id,
                "YggdrasilAPI": root_url,
                "UserName": username,
                "Profiles": auth_info
            }
            self.yggdrasil_accounts[account_id] = account_info
            self._save_account_list()

        return account_id

    def del_yggdrasil_account(self, account_id: str) -> bool:
        """
        删除一个账户
        :param account_id: 账户 ID
        :return: bool
        """
        with self._lock:
            if account_id not in self.yggdrasil_accounts:
                raise KeyError(f"账户 '{account_id}' 不存在")

            account_info = self.yggdrasil_accounts[account_id]
            token_id = self._get_token_id(account_info["UserName"], account_info["YggdrasilAPI"])
            token_info = self.yggdrasil_tokens[token_id]

            try:
                self.yggdrasil_client.invalidate(
                    url=account_info["YggdrasilAPI"],
                    access_token=token_info["AccessToken"],
                    client_token=token_info["ClientToken"],
                    follow_ali=False
                )
            except Exception:
                # 注销远端令牌失败不影响本地清理，网络不可用时也应允许删除账户。
                pass

            self.yggdrasil_accounts.pop(account_id, None)
            self.yggdrasil_tokens.pop(account_id, None)
            # 删除缓存文件（如果没有账户使用）
            used_token = 0
            for profile in self.yggdrasil_accounts.values():
                if self._get_token_id(profile["UserName"], profile["YggdrasilAPI"]) == token_id:
                    used_token += 1
            if used_token <= 0:
                (self.account_cache_path / f"{token_id}.json").unlink(missing_ok=True)
                self._save_account_list()

        return True

    def refresh_token(self, account_id: str) -> bool:
        """
        刷新账户所使用的令牌(多个账户可能使用同一个令牌)
        :param account_id: 账户 ID
        :return: bool
        """
        with self._lock:
            if account_id not in self.yggdrasil_accounts:
                raise KeyError(f"账户 '{account_id}' 不存在")

            account_info = self.yggdrasil_accounts[account_id]
            token_id = self._get_token_id(account_info["UserName"], account_info["YggdrasilAPI"])
            token_info = self.yggdrasil_tokens[token_id]

            refresh_info = self.yggdrasil_client.refresh(
                url=account_info["YggdrasilAPI"],
                access_token=token_info["AccessToken"],
                client_token=token_info["ClientToken"],
                follow_ali=False
            )

            token_info = {
                "AccessToken": refresh_info["accessToken"],
                "ClientToken": refresh_info["clientToken"]
            }
            self._save_account_cache(token_id, token_info)
            self.yggdrasil_tokens[token_id] = token_info

        return True

    def get_yggdrasil_token(self, account_id: str) -> dict:
        """
        获取账户登录令牌
        :param account_id: 账户 ID
        :return: 登录需要的信息
        """
        with self._lock:
            if account_id not in self.yggdrasil_accounts:
                raise KeyError(f"账户 '{account_id}' 不存在")

            account_info = self.yggdrasil_accounts[account_id]
            token_id = self._get_token_id(account_info["UserName"], account_info["YggdrasilAPI"])
            token_info = self.yggdrasil_tokens[token_id]

            if self.yggdrasil_client.validate(
                url=account_info["YggdrasilAPI"],
                access_token=token_info["AccessToken"],
                client_token=token_info["ClientToken"],
                follow_ali=False
            ):
                token_info["YggdrasilAPI"] = account_info["YggdrasilAPI"]
                return token_info

            self.refresh_token(account_id)
            token_info = self.yggdrasil_tokens[account_id]

            token_info["YggdrasilAPI"] = account_info["YggdrasilAPI"]

        return token_info

    def get_yggdrasil_skin(self, account_id: str) -> dict:
        """
        获取账户选择角色皮肤(添加账户时已修改为指定登录账户)
        :param account_id: 账户 ID
        :return: Profile
        """
        with self._lock:
            account_info = self.yggdrasil_accounts[account_id]

        return self.yggdrasil_client.get_skin(
            url=account_info["YggdrasilAPI"],
            user_uuid=account_info["selectedProfile"]["id"],
            follow_ali=False
        )

    def close(self) -> None:
        """释放内部 HTTP 客户端资源"""
        if hasattr(self, "yggdrasil_client") and self.yggdrasil_client:
            self.yggdrasil_client.close()
            self.yggdrasil_client = None

    def __enter__(self):
        return self

    def __exit__(self):
        self.close()


def check_download_authlib(save_path: Path | str, download_source: str = "Official") -> bool:
    """
    检查下载 Authlib-Injector
    :param save_path: 文件存储路径
    :param download_source: 下载源, 可选 "bmclapi", 填其他任何值都是官方
    :return: 检查为最新或下载完成返回 True, 出现问题会抛异常
    """
    save_path = Path(save_path)

    base_url = "https://authlib-injector.yushi.moe"
    if download_source.lower() == "bmclapi":
        base_url = "https://bmclapi2.bangbang93.com/mirrors/authlib-injector"

    resp = httpx.get(f"{base_url}/artifact/latest.json")
    resp.raise_for_status()
    info = resp.json()

    if save_path.is_file() and sha256(save_path.read_bytes()).hexdigest() == info["checksums"]["sha256"]:
        return True

    resp = httpx.get(info["download_url"])
    resp.raise_for_status()

    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(resp.content)

    return True
