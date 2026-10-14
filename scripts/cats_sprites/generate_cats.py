"""按 MIDJOURNEY_PROMPTS.md 的思路，用项目的 gpt-image 中转批量生成黑/白猫剪影素材。

流程：高反差纯色背景出图（黑猫=白底，白猫=黑底）→ 本地按亮度抠出 alpha →
裁到主体包围盒 → 最长边缩到 128px → 居中放入 192x192 透明画布。

用法（项目根目录，使用与 gunicorn 相同的 venv）：
    python -m scripts.cats_sprites.generate_cats --dry-run          # 只打印 prompt
    python -m scripts.cats_sprites.generate_cats --only black --poses 1,4 --n 1
    python -m scripts.cats_sprites.generate_cats --ref scripts/cats_sprites/猫咪五子棋.jpg
输出：scripts/cats_sprites/out/{raw,sprites}/{black|white}_{pose序号}_{k}.png
"""
import argparse
import asyncio
import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from backend.image_gen import ImageGenError, edit_image, generate_image

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"

POSES = [
    "standing in profile with tail raised in a gentle question-mark curve",
    "walking slowly in profile, one front paw lifted",
    "trotting with a low horizontal tail",
    "sitting upright in profile with tail curled around the paws",
    "sitting with its back toward the viewer and head turned sideways",
    "crouching low and alert before a pounce",
    "stretching forward with front legs extended and hips raised",
    "arching its back with tail curved downward",
    "grooming one raised hind leg",
    "grooming its shoulder while seated",
    "curled into a compact sleeping crescent",
    "resting in a loaf pose with paws tucked under the chest",
    "lying on its side with a relaxed curved tail",
    "standing and looking backward over one shoulder",
    "playfully reaching forward with one paw",
    "turning tightly in a circle while following its own tail",
]

COLORS = {
    # name: (主体描述, 背景描述)
    "black": ("solid black cat silhouette", "pure flat white background"),
    "white": ("warm ivory-white cat silhouette", "pure flat black background"),
}

TEMPLATE = (
    "one single {subject}, {pose}, full body completely visible, anatomically accurate "
    "domestic cat, natural feline spine and leg joints, expressive continuous tail, "
    "minimalist realistic silhouette, slightly elevated three-quarter top view, "
    "soft hand-printed edge texture, centered, cat occupies about 65 percent of a square "
    "canvas, {bg}, no floor, no cast shadow, no board, no grid, no props, no text, no border, "
    "no eyes, no nose, no markings, no collar, no inner lines, "
    "no extra limbs, no extra tail, no cropped ears or paws"
)
REF_PREFIX = (
    "Use the attached image only as a style reference for the cat drawing style "
    "(flat, minimal, hand-printed silhouettes). Do NOT reproduce the board, the grid, "
    "or multiple cats. Draw a brand-new image: "
)


def build_prompt(color: str, pose: str, with_ref: bool) -> str:
    subject, bg = COLORS[color]
    p = TEMPLATE.format(subject=subject, pose=pose, bg=bg)
    return REF_PREFIX + p if with_ref else p


def _drop_specks(alpha: np.ndarray, min_ratio: float = 0.03) -> np.ndarray:
    """去掉远离主体的碎斑/噪点：在 1/4 分辨率上做连通域，保留面积 >= 最大块 min_ratio 的块。
    先膨胀再标记，让腿/尾尖等细部与身体相连，不会被误删。"""
    h, w = alpha.shape
    small_img = Image.fromarray((alpha * 255).astype(np.uint8)).resize((w // 4, h // 4), Image.BILINEAR)
    small = np.asarray(small_img.point(lambda v: 255 if v > 128 else 0).filter(ImageFilter.MaxFilter(7))) > 0
    labels = np.zeros(small.shape, dtype=np.int32)
    sizes = {}
    n = 0
    for y, x in zip(*np.nonzero(small)):
        if labels[y, x]:
            continue
        n += 1
        stack, labels[y, x], cnt = [(y, x)], n, 0
        while stack:
            cy, cx = stack.pop()
            cnt += 1
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < small.shape[0] and 0 <= nx < small.shape[1] and small[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = n
                    stack.append((ny, nx))
        sizes[n] = cnt
    if not sizes:
        return alpha
    biggest = max(sizes.values())
    keep = np.isin(labels, [k for k, v in sizes.items() if v >= biggest * min_ratio])
    mask = Image.fromarray((keep * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    return alpha * (np.asarray(mask) > 0)


def to_sprite(raw_png: bytes, color: str) -> Image.Image:
    """纯色背景图 → 透明 PNG 精灵（192x192，主体最长边 128）。"""
    gray = np.asarray(Image.open(io.BytesIO(raw_png)).convert("L"), dtype=np.float32) / 255.0
    # 黑猫在白底上：越暗越不透明；白猫在黑底上：越亮越不透明。
    alpha = 1.0 - gray if color == "black" else gray
    # 拉伸对比，压掉背景的轻微底噪，同时保留抗锯齿边缘
    alpha = np.clip((alpha - 0.08) / 0.84, 0.0, 1.0)
    alpha = _drop_specks(alpha)
    rgb = (0, 0, 0) if color == "black" else (250, 245, 232)
    h, w = alpha.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = rgb
    rgba[..., 3] = (alpha * 255).astype(np.uint8)
    img = Image.fromarray(rgba)
    bbox = img.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
    if not bbox:
        raise ValueError("抠图后画面为空")
    img = img.crop(bbox)
    scale = 128 / max(img.size)
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
    canvas.paste(img, ((192 - img.width) // 2, (192 - img.height) // 2), img)
    return canvas


async def one(sem, color, idx, k, pose, ref_bytes):
    name = f"{color}_{idx:02d}_{k}"
    sprite_path = OUT / "sprites" / f"{name}.png"
    if sprite_path.exists():
        print(f"skip {name}（已存在）")
        return
    prompt = build_prompt(color, pose, ref_bytes is not None)
    async with sem:
        try:
            raw = await (edit_image(ref_bytes, prompt) if ref_bytes else generate_image(prompt))
        except ImageGenError as e:
            print(f"FAIL {name} [{e.kind}]: {e}")
            return
    (OUT / "raw" / f"{name}.png").write_bytes(raw)
    try:
        to_sprite(raw, color).save(sprite_path)
        print(f"ok   {name}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {name} 抠图: {e}（原图已保存在 raw/）")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["black", "white"], help="只生成一种颜色")
    ap.add_argument("--poses", help="姿态序号，逗号分隔，1-16；默认全部")
    ap.add_argument("--n", type=int, default=1, help="每个姿态生成几张")
    ap.add_argument("--ref", help="风格参考图（走 images/edits）；不传则纯文本出图")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    idxs = [int(x) for x in a.poses.split(",")] if a.poses else list(range(1, 17))
    colors = [a.only] if a.only else ["black", "white"]
    ref_bytes = Path(a.ref).read_bytes() if a.ref else None

    jobs = [(c, i, k, POSES[i - 1]) for c in colors for i in idxs for k in range(1, a.n + 1)]
    print(f"共 {len(jobs)} 张，模式：{'edits(参考图)' if ref_bytes else 'generations(纯文本)'}")
    if a.dry_run:
        for c, i, k, p in jobs:
            print(f"[{c}_{i:02d}_{k}] {build_prompt(c, p, ref_bytes is not None)}\n")
        return
    (OUT / "raw").mkdir(parents=True, exist_ok=True)
    (OUT / "sprites").mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(a.concurrency)
    await asyncio.gather(*(one(sem, c, i, k, p, ref_bytes) for c, i, k, p in jobs))


if __name__ == "__main__":
    asyncio.run(main())
