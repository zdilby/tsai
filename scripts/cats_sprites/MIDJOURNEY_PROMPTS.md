# 猫咪五子棋素材生成提示词

## 使用方法

1. 将原始棋盘图片上传至 Midjourney，并把图片链接放在提示词最前面。
2. 每次只生成一只猫，不要让 Midjourney 直接生成 4x4 素材表。
3. 黑猫使用白色背景，白猫使用黑色背景，便于后续去背。
4. 对同一颜色尽量固定 `--seed`，减少画风漂移。
5. 下载后去除背景，再将主体最长边缩放为 128 px，居中放入 192x192 透明画布。

## 黑猫模板

```text
[参考图链接] one single solid black cat silhouette, {POSE}, full body completely visible, anatomically accurate domestic cat, natural feline spine and leg joints, expressive continuous tail, minimalist realistic silhouette matching the reference image, slightly elevated three-quarter top view, soft hand-printed edge texture, centered, cat occupies about 65 percent of a square canvas, pure flat white background, no floor, no cast shadow, no board, no grid, no props, no text, no border, no extra limbs, no extra tail, no cropped ears or paws --ar 1:1 --style raw --s 50 --chaos 8 --seed 27184
```

## 白猫模板

```text
[参考图链接] one single warm ivory-white cat silhouette, {POSE}, full body completely visible, anatomically accurate domestic cat, natural feline spine and leg joints, expressive continuous tail, minimalist realistic silhouette matching the reference image, slightly elevated three-quarter top view, soft hand-printed edge texture, centered, cat occupies about 65 percent of a square canvas, pure flat black background, no floor, no cast shadow, no board, no grid, no props, no text, no border, no extra limbs, no extra tail, no cropped ears or paws --ar 1:1 --style raw --s 50 --chaos 8 --seed 27184
```

## 16个建议姿态

将模板里的 `{POSE}` 分别替换为以下内容：

1. `standing in profile with tail raised in a gentle question-mark curve`
2. `walking slowly in profile, one front paw lifted`
3. `trotting with a low horizontal tail`
4. `sitting upright in profile with tail curled around the paws`
5. `sitting with its back toward the viewer and head turned sideways`
6. `crouching low and alert before a pounce`
7. `stretching forward with front legs extended and hips raised`
8. `arching its back with tail curved downward`
9. `grooming one raised hind leg`
10. `grooming its shoulder while seated`
11. `curled into a compact sleeping crescent`
12. `resting in a loaf pose with paws tucked under the chest`
13. `lying on its side with a relaxed curved tail`
14. `standing and looking backward over one shoulder`
15. `playfully reaching forward with one paw`
16. `turning tightly in a circle while following its own tail`

## 一致性要求

- 一张图只能出现一只完整的猫。
- 不要眼睛、鼻子、花纹、项圈或内部线条，主要信息来自外轮廓。
- 猫必须具有清楚的耳尖、胸腹、脚掌和尾根，不能由简单几何图形拼成。
- 所有图片采用相同视角、相同比例和相同留白。
- Midjourney 通常不会生成可靠的透明通道，因此先用高反差纯色背景生成，再统一去背。
