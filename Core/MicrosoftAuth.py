from typing import Callable
from threading import Lock
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
import base64
import httpx
import json
import time


# ---------- 异常层次 ----------
class BException(Exception):
    """基础异常（未直接使用）"""
    pass

class AuthException(BException):
    """认证相关异常的基类"""
    pass

class MicrosoftAuthError(AuthException):
    """Microsoft OAuth 认证失败"""
    pass

class XboxAuthError(AuthException):
    """Xbox Live 令牌获取失败"""
    pass

class XSTSAuthError(AuthException):
    """XSTS 令牌获取失败"""
    pass

class MinecraftAuthError(AuthException):
    """Minecraft 令牌或档案操作失败"""
    pass

class NetException(BException):
    """网络请求异常的基类"""
    pass

class GetSkinError(NetException):
    """获取皮肤失败"""
    pass

class UpdateSkinError(NetException):
    """更新皮肤失败"""
    pass

class SetNameError(NetException):
    pass


# ---------- 微软认证（纯 OAuth，无 msal） ----------
class MicrosoftAuth:
    """
    负责通过设备码流程进行 Microsoft 账户认证
    提供用于 Xbox Live 的访问令牌 (作用域: 'XboxLive.signin')
    """
    DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
    TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

    def __init__(
        self,
        client_id: str,
        cache_file: str | Path | None = None,
        on_device_code: Callable[[dict[str, str]], None] | None = None,
        verify: bool = True,
        client: httpx.Client | None = None,      # 新增：可共享的客户端
    ):
        """
        :param client_id: Azure AD 应用程序（公共客户端）的客户端 ID
        :param cache_file: 存储令牌缓存的路径 (JSON 文件)。若为 None，则仅在内存中缓存
        :param on_device_code: 接收设备流信息字典的回调函数（包含 'user_code', 'verification_uri' 等）
        :param verify: 是否校验 Microsoft 登录服务器的 SSL 证书
        :param client: 可选的共享 httpx.Client 实例，若不提供则内部自行创建
        """
        self.client_id = client_id
        self.scope = ["XboxLive.signin"]
        self.cache_file = Path(cache_file) if cache_file else None
        self.verify = verify
        self._device_code_callback = on_device_code or (
            lambda flow: print(f"Link: {flow['verification_uri']}, Code: {flow['user_code']}")
        )

        # 客户端管理
        self._external_client = client
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                timeout=httpx.Timeout(15, connect=10),
                verify=verify,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self._owns_client = True

        # 缓存结构:
        # {
        #   "access_token": str,
        #   "refresh_token": str,
        #   "expires_at": float,      # 时间戳
        #   "id_token": str,
        #   "id_token_claims": dict   # 解析后的 claims
        # }
        self._cache = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """从文件加载缓存"""
        if self.cache_file and self.cache_file.exists():
            try:
                with self.cache_file.open("r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def _save_cache(self) -> None:
        """持久化缓存到文件"""
        if self.cache_file and self._cache:
            try:
                with self.cache_file.open("w", encoding="utf-8") as f:
                    json.dump(self._cache, f, indent=2)
            except OSError:
                pass

    @staticmethod
    def _parse_id_token(id_token: str) -> dict:
        """解码 JWT 的 payload 部分（不验证签名）"""
        try:
            # JWT 格式: header.payload.signature
            payload = id_token.split(".")[1]
            # 补齐 base64 填充
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload)
            return json.loads(decoded)
        except Exception:
            return {}

    def _refresh_token(self) -> tuple[str, str] | None:
        """
        使用 refresh_token 获取新令牌，成功返回 (access_token, email)，失败返回 None
        """
        refresh_token = self._cache.get("refresh_token")
        if not refresh_token:
            return None

        data = {
            "client_id": self.client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(self.scope),
        }
        try:
            resp = self._client.post(self.TOKEN_URL, data=data)
            resp.raise_for_status()
            token_data = resp.json()
            if "access_token" in token_data:
                self._update_cache(token_data)
                self._save_cache()
                claims = self._cache.get("id_token_claims", {})
                email = claims.get("preferred_username") or claims.get("email") or ""
                return token_data["access_token"], email
        except Exception:
            # 刷新失败，清空缓存（防止反复尝试）
            self._cache.clear()
            self._save_cache()
        return None

    def _update_cache(self, token_data: dict) -> None:
        """根据 token 端点返回的数据更新缓存"""
        self._cache["access_token"] = token_data["access_token"]
        if "refresh_token" in token_data:
            self._cache["refresh_token"] = token_data["refresh_token"]
        expires_in = token_data.get("expires_in", 86400)
        self._cache["expires_at"] = time.time() + expires_in - 60  # 提前 60 秒视为过期
        if "id_token" in token_data:
            self._cache["id_token"] = token_data["id_token"]
            self._cache["id_token_claims"] = self._parse_id_token(token_data["id_token"])

    def _device_flow(self) -> tuple[str, str]:
        """
        执行设备码流程，返回 (access_token, email)
        若失败则抛出 MicrosoftAuthError
        """
        # 1. 请求 device_code
        data = {
            "client_id": self.client_id,
            "scope": " ".join(self.scope),
        }
        try:
            resp = self._client.post(self.DEVICE_CODE_URL, data=data)
            resp.raise_for_status()
            flow = resp.json()
        except Exception as e:
            raise MicrosoftAuthError(f"获取 device_code 失败: {e}") from e

        if "user_code" not in flow:
            raise MicrosoftAuthError(f"设备码流程初始化失败: {flow}")

        self._device_code_callback(flow)

        # 2. 轮询 token 端点
        device_code = flow["device_code"]
        interval = flow.get("interval", 5)
        expires_in = flow.get("expires_in", 1800)
        start_time = time.time()

        while time.time() - start_time < expires_in:
            time.sleep(interval)
            data = {
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
            try:
                resp = self._client.post(self.TOKEN_URL, data=data)
                if resp.status_code == 400:
                    error = resp.json().get("error")
                    if error == "authorization_pending":
                        continue  # 用户尚未完成授权，继续等待
                    elif error == "slow_down":
                        interval += 2
                        continue
                    elif error == "expired_token":
                        raise MicrosoftAuthError("设备码已过期，请重新尝试")
                    else:
                        # 其他错误（如 access_denied, bad_verification_code 等）
                        raise MicrosoftAuthError(f"授权失败: {error}")
                resp.raise_for_status()
                token_data = resp.json()
                if "access_token" in token_data:
                    self._update_cache(token_data)
                    self._save_cache()
                    claims = self._cache.get("id_token_claims", {})
                    email = claims.get("preferred_username") or claims.get("email") or ""
                    return token_data["access_token"], email
                else:
                    raise MicrosoftAuthError("令牌响应缺少 access_token")
            except httpx.HTTPStatusError as e:
                # 非 400 的其他 HTTP 错误
                raise MicrosoftAuthError(f"轮询令牌失败: {e}") from e
            except Exception as e:
                raise MicrosoftAuthError(f"轮询令牌异常: {e}") from e

        raise MicrosoftAuthError("设备码授权超时")

    def get_token(self) -> tuple[str, str]:
        """
        如果认证失败则抛出 MicrosoftAuthError
        :return: (access_token, email)
        """
        # 1. 检查缓存中的 access_token 是否有效
        access_token = self._cache.get("access_token")
        expires_at = self._cache.get("expires_at", 0)
        if access_token and time.time() < expires_at:
            claims = self._cache.get("id_token_claims", {})
            email = claims.get("preferred_username") or claims.get("email") or ""
            return str(access_token), email

        # 2. 尝试使用 refresh_token 刷新
        refreshed = self._refresh_token()
        if refreshed:
            return refreshed

        # 3. 执行设备码流程
        return self._device_flow()

    def close(self) -> None:
        """如果拥有自己的客户端则关闭；否则不做任何操作"""
        if self._owns_client and hasattr(self, "_client"):
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# ---------- Minecraft API 客户端 ----------
class MinecraftClient:
    def __init__(self, client: httpx.Client | None = None):
        """
        :param client: 可选的共享 httpx.Client 实例，若不提供则内部自行创建
        """
        self._external_client = client
        if client is not None:
            self.client = client
            self._owns_client = False
        else:
            self.client = httpx.Client(
                http2=True,
                timeout=httpx.Timeout(15, connect=10),
                follow_redirects=True,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            self._owns_client = True

    def _get_xbox_tokens(self, ms_token: str) -> tuple[str, str]:
        """
        交换 Microsoft 令牌获取 Xbox Live 令牌和用户哈希
        :param ms_token: Microsoft Token
        :return: (xbox_live_token, user_hash)
        """
        live_url = "https://user.auth.xboxlive.com/user/authenticate"
        live_payload = {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={ms_token}"
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT"
        }
        try:
            resp = self.client.post(live_url, json=live_payload)
            resp.raise_for_status()
            data = resp.json()
            return data["Token"], data["DisplayClaims"]["xui"][0]["uhs"]
        except Exception as e:
            raise XboxAuthError(e) from e

    def _get_xsts_token(self, xbox_token: str) -> str:
        """
        交换 Xbox Live 令牌获取 XSTS 令牌
        :param xbox_token: Xbox Live Token
        :return: XSTS Token
        """
        xsts_url = "https://xsts.auth.xboxlive.com/xsts/authorize"
        xsts_payload = {
            "Properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [xbox_token]
            },
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT"
        }
        try:
            resp = self.client.post(xsts_url, json=xsts_payload)
            resp.raise_for_status()
            return resp.json()["Token"]
        except Exception as e:
            raise XSTSAuthError(e) from e

    def get_minecraft_token(self, microsoft_token: str) -> tuple[str, float, int]:
        """
        完整认证链: Microsoft -> Xbox -> XSTS -> Minecraft
        :return: (access_token, 获取时间戳, 有效期秒数)
        """
        xbox_token, user_hash = self._get_xbox_tokens(microsoft_token)
        xsts_token = self._get_xsts_token(xbox_token)

        url = "https://api.minecraftservices.com/authentication/login_with_xbox"
        payload = {"identityToken": f"XBL3.0 x={user_hash};{xsts_token}"}

        try:
            resp = self.client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["access_token"], time.time(), data.get("expires_in", 86400)
        except Exception as e:
            raise MinecraftAuthError(e) from e

    def get_profile(self, minecraft_token: str) -> dict | None:
        """
        获取 Minecraft 档案，若未购买 Java 版则返回 None
        :param minecraft_token: Minecraft Token
        :return: Minecraft Profile or None
        """
        url = "https://api.minecraftservices.com/minecraft/profile"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {minecraft_token}"
        }
        try:
            resp = self.client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                return None
            resp.raise_for_status()
        except Exception as e:
            raise MinecraftAuthError(e) from e

    def upload_skin(self, minecraft_token: str, variant: str, png_image: bytes) -> dict:
        """
        上传皮肤
        :param minecraft_token: Minecraft Token
        :param variant: "classic"（经典）或 "slim"（滑头）
        :param png_image: PNG Image
        :return: Profile
        """
        url = "https://api.minecraftservices.com/minecraft/profile/skins"

        boundary = f"*****{int(time.time() * 1000)}*****"
        request_parts = [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="variant"\r\n',
            b"\r\n",
            f"{variant}\r\n".encode(),
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="skin.png"\r\n',
            b"Content-Type: image/png\r\n",
            b"\r\n",
            png_image,
            b"\r\n",
            f"--{boundary}--\r\n".encode()
        ]
        request_body = b"".join(request_parts)

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "Content-Length": str(len(request_body)),
            "Authorization": f"Bearer {minecraft_token}"
        }

        try:
            resp = self.client.post(url, content=request_body, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise UpdateSkinError(e) from e

    def reset_skin(self, minecraft_token: str) -> dict:
        """
        重置为默认皮肤
        :param minecraft_token: Minecraft Token
        :return: Profile
        """
        url = "https://api.minecraftservices.com/minecraft/profile/skins/active"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {minecraft_token}"
        }
        try:
            resp = self.client.delete(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise UpdateSkinError(e) from e

    def set_cape(self, minecraft_token: str, cape_id: str) -> dict:
        """
        设置披风
        :param minecraft_token: Minecraft Token
        :param cape_id: 披风 ID
        :return: Profile
        """
        url = "https://api.minecraftservices.com/minecraft/profile/capes/active"
        payload = {"capeId": cape_id}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {minecraft_token}"
        }
        try:
            resp = self.client.put(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise UpdateSkinError(e) from e

    def reset_cape(self, minecraft_token: str) -> dict:
        """
        重置披风(或者说选择无披风)
        :param minecraft_token: Minecraft Token
        :return: Profile
        """
        url = "https://api.minecraftservices.com/minecraft/profile/capes/active"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {minecraft_token}"
        }
        try:
            resp = self.client.delete(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise UpdateSkinError(e) from e

    def set_profile_name(self, minecraft_token: str, new_name: str) -> dict:
        """
        [!未测试, 是否能使用以及返回内容未知!]
        设置 Minecraft Java profile 名称
        :param minecraft_token: Minecraft Token
        :param new_name: 新名称
        :return: Profile?
        """
        url = f"https://api.minecraftservices.com/minecraft/profile/name/{new_name}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {minecraft_token}"
        }
        try:
            resp = self.client.put(url, headers=headers)
            if resp.status_code == 400:
                print(resp.json())
                raise SetNameError("用户名无效")
            elif resp.status_code == 403:
                print(resp.json())
                raise SetNameError("距离上次修改不足30天或处在冷却期或该用户名已被占用")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise SetNameError(e) from e

    def close(self) -> None:
        """如果拥有自己的客户端则关闭，否则不执行任何操作"""
        if self._owns_client and hasattr(self, "client"):
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# ---------- 多账户管理器（线程安全） ----------
class MicrosoftAuthManager:
    def __init__(
        self,
        client_id: str,
        cache_path: Path | str | None = None,
        on_device_code: Callable[[dict[str, str]], None] | None = None,
        verify: bool = True,
    ):
        """
        :param client_id: Azure AD 应用程序客户端 ID
        :param cache_path: 存储数据的根目录
        :param on_device_code: 设备码回调函数
        :param verify: 是否校验 Microsoft 登录服务器的 SSL 证书
        """
        self.client_id = client_id
        self.verify = verify
        self.cache_path = Path(cache_path) if cache_path else Path.home() / ".ECL"
        self.cache_path = self.cache_path / "accounts"
        self.cache_path.mkdir(parents=True, exist_ok=True)
        self.on_device_code = on_device_code

        self.account_list_file = self.cache_path / "ms_accounts_list.json"
        self.account_cache_path = self.cache_path / "ms_accounts"
        self.account_cache_path.mkdir(parents=True, exist_ok=True)

        # 共享 HTTP 客户端
        self._shared_client = httpx.Client(
            timeout=httpx.Timeout(15, connect=10),
            verify=verify,
            http2=True,
            follow_redirects=True,
        )

        # 共享数据结构
        self.microsoft_accounts: dict[str, dict] = {}   # account_id -> 账户信息
        self.microsoft_clients: dict[str, MicrosoftAuth] = {}  # account_id -> MicrosoftAuth 实例
        self.minecraft_tokens: dict[str, tuple[str, float, int]] = {}  # account_id -> (token, time, expires_in)

        # MinecraftClient 也使用共享客户端
        self.minecraft_client = MinecraftClient(client=self._shared_client)

        self._lock = Lock()
        self._load_accounts()

    # ---------- 内部辅助 ----------
    def _load_accounts(self) -> None:
        """从文件加载账户列表，重建 MicrosoftAuth 客户端"""
        if not self.account_list_file.is_file():
            return
        data = json.loads(self.account_list_file.read_text(encoding="utf-8"))
        for account_id, info in data.items():
            try:
                ms_client = MicrosoftAuth(
                    client_id=self.client_id,
                    cache_file=self.account_cache_path / f"{account_id}.json",
                    on_device_code=self.on_device_code,
                    verify=self.verify,
                    client=self._shared_client,           # 共享客户端
                )
                self.microsoft_accounts[account_id] = info
                self.microsoft_clients[account_id] = ms_client
            except Exception:
                pass

    def _save_account_list(self) -> None:
        """保存账户列表到文件"""
        self.account_list_file.write_text(
            json.dumps(self.microsoft_accounts, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def _get_microsoft_token(self, account_id: str) -> str:
        """获取 Microsoft 访问令牌（仅令牌字符串）"""
        return self.microsoft_clients[account_id].get_token()[0]

    # ---------- 公开接口 ----------
    def get_microsoft_accounts(self) -> dict:
        """
        返回当前所有账户信息的深拷贝
        :return: Microsoft Accounts
        """
        with self._lock:
            return deepcopy(self.microsoft_accounts)

    def add_microsoft_account(self) -> str:
        """
        添加一个新 Microsoft 账户
        :return: account_id
        """
        with self._lock:
            account_id = uuid4().hex
            ms_client = MicrosoftAuth(
                client_id=self.client_id,
                cache_file=self.account_cache_path / f"{account_id}.json",
                on_device_code=self.on_device_code,
                verify=self.verify,
                client=self._shared_client,              # 共享客户端
            )
            token, email = ms_client.get_token()

            mc_token_tuple = self.minecraft_client.get_minecraft_token(token)
            mc_profile = self.minecraft_client.get_profile(mc_token_tuple[0])
            if not mc_profile:
                raise MinecraftAuthError("未购买 Minecraft Java 版")

            self.microsoft_accounts[account_id] = {
                "AccountId": account_id,
                "Email": email,
                "Profile": mc_profile
            }
            self.microsoft_clients[account_id] = ms_client
            self.minecraft_tokens[account_id] = mc_token_tuple
            self._save_account_list()
            return account_id

    def del_microsoft_account(self, account_id: str) -> None:
        """
        删除指定账户及相关缓存文件
        :param account_id: 账户 ID
        :return: None
        """
        with self._lock:
            self.microsoft_clients.pop(account_id, None)
            self.microsoft_accounts.pop(account_id, None)
            self.minecraft_tokens.pop(account_id, None)
            # 删除缓存文件（如果存在）
            (self.account_cache_path / f"{account_id}.json").unlink(missing_ok=True)
            self._save_account_list()

    def get_minecraft_token(self, account_id: str, refresh_profile: bool = True) -> str:
        """
        获取 Minecraft 访问令牌，自动刷新过期令牌
        :param account_id: 账户 ID
        :param refresh_profile: 若令牌被刷新，是否同时更新档案
        :return: Minecraft 访问令牌字符串
        """
        with self._lock:
            if account_id not in self.microsoft_accounts:
                raise KeyError(f"账户 '{account_id}' 不存在")

            # 如果内存中没有令牌记录，直接获取新令牌
            if account_id not in self.minecraft_tokens:
                ms_token = self._get_microsoft_token(account_id)
                mc_token_tuple = self.minecraft_client.get_minecraft_token(ms_token)
                self.minecraft_tokens[account_id] = mc_token_tuple
                mc_token = mc_token_tuple[0]
            else:
                mc_token, times, expires_in = self.minecraft_tokens[account_id]
                # 如果剩余有效期 > 300 秒，直接返回
                if time.time() - times < expires_in - 300:
                    return mc_token
                # 否则刷新
                ms_token = self._get_microsoft_token(account_id)
                mc_token_tuple = self.minecraft_client.get_minecraft_token(ms_token)
                self.minecraft_tokens[account_id] = mc_token_tuple
                mc_token = mc_token_tuple[0]

        # 解锁后执行档案刷新（若需要）
        if refresh_profile:
            try:
                self.refresh_profile(account_id)
            except Exception:
                pass
        return mc_token

    def refresh_profile(self, account_id: str) -> dict:
        """
        刷新指定账户的档案（玩家名、皮肤等）
        :param account_id: 账户 ID
        :return: {"Profile": ..., "Skin": ...}
        """
        # 获取有效令牌（不触发递归刷新）
        mc_token = self.get_minecraft_token(account_id, refresh_profile=False)

        # 获取最新档案
        profile = self.minecraft_client.get_profile(mc_token)
        if not profile:
            raise MinecraftAuthError(f"无法获取账户 '{account_id}' 的档案")

        with self._lock:
            self.microsoft_accounts[account_id]["Profile"] = profile
            self._save_account_list()

        return {"Profile": profile}

    def upload_skin(self, account_id: str, variant: str, png_image: bytes) -> dict:
        """
        上传皮肤
        :param account_id: 账户 ID
        :param variant: "classic"(经典) 或 "slim"(滑头), 或者说 "classic"(史蒂夫体型) slim"(艾利克斯体型)
        :param png_image: PNG Image
        :return: Profile
        """
        mc_token = self.get_minecraft_token(account_id, refresh_profile=False)
        return self.minecraft_client.upload_skin(mc_token, variant, png_image)

    def reset_skin(self, account_id: str) -> dict:
        """
        重置皮肤为默认
        :param account_id: 账户 ID
        :return: Profile
        """
        mc_token = self.get_minecraft_token(account_id, refresh_profile=False)
        return self.minecraft_client.reset_skin(mc_token)

    def set_cape(self, account_id: str, cape_id: str) -> dict:
        """
        设置披风
        :param account_id: 账户 ID
        :param cape_id: 披风 ID
        :return: Profile
        """
        mc_token = self.get_minecraft_token(account_id, refresh_profile=False)
        return self.minecraft_client.set_cape(mc_token, cape_id)

    def reset_cape(self, account_id: str) -> dict:
        """
        重置披风(或者说选择无披风)
        :param account_id: 账户 ID
        :return: Profile
        """
        mc_token = self.get_minecraft_token(account_id, refresh_profile=False)
        return self.minecraft_client.reset_cape(mc_token)

    def set_profile_name(self, account_id: str, new_name: str) -> dict:
        """
        [!未测试, 是否能使用以及返回内容未知!]
        设置 Minecraft Java profile 名称
        :param account_id: 账户 ID
        :param new_name: 新名称
        :return: Profile?
        """
        mc_token = self.get_minecraft_token(account_id, refresh_profile=False)
        return self.minecraft_client.set_profile_name(mc_token, new_name)

    def close(self) -> None:
        """释放共享 HTTP 客户端，并清理子客户端引用"""
        if hasattr(self, "_shared_client") and self._shared_client:
            self._shared_client.close()
            self._shared_client = None
        # 子客户端持有共享客户端的引用，无需再单独关闭
        self.minecraft_client = None
        # 清空 MicrosoftAuth 实例，避免持有已关闭的客户端引用
        self.microsoft_clients.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()