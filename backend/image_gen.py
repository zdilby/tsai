import base64

import httpx

from settings import settings


class ImageGenError(Exception):
    """图像生成失败。

    kind 用于前端区分"这是谁的锅"：
        'config'      本站没配好 CODEX_*（本站问题，需运维处理）
        'upstream'    中转或其上游返回错误码（第三方问题，非本站）
        'timeout'     等第三方响应超时（第三方问题，非本站）
        'network'     连不上第三方（第三方/网络问题，非本站）
        'auth'        第三方鉴权失败，多半是 API key 失效（本站 key 问题）
        'bad_request' 请求被第三方拒绝，多半是描述内容触发内容策略
        'decode'      第三方返回内容无法解析（第三方问题，非本站）
    """

    def __init__(self, message: str, *, kind: str = "upstream"):
        super().__init__(message)
        self.kind = kind

    @property
    def is_third_party(self) -> bool:
        return self.kind in ("upstream", "timeout", "network", "decode")


# gpt-image-1 经中转出图常见 60-200s，偶尔更久；连接握手很快但 read 要给足。
# 可用 CODEX_IMAGE_TIMEOUT 覆盖（秒）。生产 gunicorn 记得同步调大 --timeout。
_TIMEOUT = httpx.Timeout(
    connect=15.0, write=30.0, pool=15.0,
    read=settings.codex_image_timeout,
)


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code = resp.status_code
    body = resp.text[:300]
    if code in (401, 403):
        raise ImageGenError(
            f"第三方图像服务鉴权失败（{code}）。请检查 .env 里的 CODEX_API_KEY 是否有效。",
            kind="auth",
        )
    if code == 400:
        raise ImageGenError(
            f"图像请求被第三方服务拒绝（400）：{body}。可能是描述内容触发了内容策略，换个说法再试。",
            kind="bad_request",
        )
    # 404 / 408 / 429 / 5xx / 502 / 503 / 504：中转自身或其上游的问题，不是本站的问题
    raise ImageGenError(
        f"第三方图像服务（中转 {settings.codex_base_url}）暂时不可用，上游返回 {code}。"
        f"这不是本站的问题——请稍后重试；若持续无法生成，请把这条信息反馈给中转服务提供商。"
        f"（原始响应：{body}）",
        kind="upstream",
    )


def _extract_image_bytes(resp: httpx.Response) -> bytes:
    _raise_for_status(resp)
    try:
        data = resp.json()
        b64 = data["data"][0]["b64_json"]
    except (KeyError, IndexError, ValueError) as e:
        raise ImageGenError(
            f"第三方图像服务返回了无法解析的内容：{e}。请稍后重试，或把该现象反馈给中转服务提供商。",
            kind="decode",
        ) from e
    try:
        return base64.b64decode(b64)
    except Exception as e:
        raise ImageGenError(f"图像数据解码失败：{e}", kind="decode") from e


def _wrap_transport_error(e: httpx.HTTPError, verb: str) -> ImageGenError:
    if isinstance(e, httpx.TimeoutException):
        return ImageGenError(
            f"第三方图像服务响应超时（已等待约 {int(settings.codex_image_timeout)}s）。"
            f"多半是中转/上游繁忙，请稍后重试；若频繁超时请反馈给中转服务提供商。",
            kind="timeout",
        )
    return ImageGenError(
        f"无法连接第三方图像服务（{settings.codex_base_url}）：{e}。请稍后重试。",
        kind="network",
    )


async def generate_image(prompt: str, *, size: str = "1024x1024") -> bytes:
    """Call the OpenAI-compatible images/generations endpoint and return raw PNG bytes.

    Uses the same CODEX_API_KEY/CODEX_BASE_URL relay already active for the writing
    module's Markdown formatting. Response shape confirmed against the live relay:
    {"data": [{"b64_json": ..., "revised_prompt": ...}], "model": ..., ...} — the
    relay may silently substitute a different underlying model (observed returning
    "gpt-image-2-codex" for a "gpt-image-1" request) but the response shape holds.
    """
    if not settings.codex_api_key or not settings.codex_base_url:
        raise ImageGenError("图像生成服务未配置（CODEX_API_KEY/CODEX_BASE_URL）", kind="config")
    url = settings.codex_base_url.rstrip("/") + "/images/generations"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as hc:
        try:
            resp = await hc.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.codex_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.codex_image_model,
                    "prompt": prompt,
                    "n": 1,
                    "size": size,
                },
            )
        except httpx.HTTPError as e:
            raise _wrap_transport_error(e, "生成") from e
        return _extract_image_bytes(resp)


async def edit_image(image_bytes: bytes, prompt: str, *, size: str = "1024x1024") -> bytes:
    """Call the OpenAI-compatible images/edits endpoint (image-to-image) and return raw PNG bytes.

    Same relay/response shape as generate_image, confirmed with a live multipart request:
    POST {base_url}/images/edits with an "image" file field + "prompt"/"model" form fields.
    """
    if not settings.codex_api_key or not settings.codex_base_url:
        raise ImageGenError("图像生成服务未配置（CODEX_API_KEY/CODEX_BASE_URL）", kind="config")
    url = settings.codex_base_url.rstrip("/") + "/images/edits"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as hc:
        try:
            resp = await hc.post(
                url,
                headers={"Authorization": f"Bearer {settings.codex_api_key}"},
                files={"image": ("image.png", image_bytes, "image/png")},
                data={"model": settings.codex_image_model, "prompt": prompt, "n": "1", "size": size},
            )
        except httpx.HTTPError as e:
            raise _wrap_transport_error(e, "编辑") from e
        return _extract_image_bytes(resp)
