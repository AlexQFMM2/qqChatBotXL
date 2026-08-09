from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError


class ImageTextError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImageTextItem:
    text: str
    position: str = "auto"


@dataclass(frozen=True, slots=True)
class ImageTextPlan:
    model_prompt: str
    items: tuple[ImageTextItem, ...]

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.items)


@dataclass(frozen=True, slots=True)
class OverlayResult:
    text_count: int
    detected_regions: int
    used_fallback: bool


_QUOTED_TEXT = re.compile(
    r"“([^”\n]{1,200})”|「([^」\n]{1,200})」|『([^』\n]{1,200})』|"
    r'"([^"\n]{1,200})"|‘([^’\n]{1,200})’'
)
_TEXT_BEFORE_MARKERS = (
    "写着",
    "写上",
    "写入",
    "文字",
    "台词",
    "对白",
    "对话框",
    "气泡",
    "拟声词",
    "符号",
    "标题",
    "字幕",
    "标牌",
    "招牌",
    "横幅",
    "配有",
    "显示",
)
_TEXT_AFTER_MARKERS = (
    "文字",
    "台词",
    "对白",
    "拟声词",
    "符号",
    "标题",
    "字幕",
)
_POSTPROCESS_INSTRUCTION = (
    "\n\n重要的后期排字要求：画面中的所有文字将由程序在生成后统一添加。"
    "请为需要文字的位置保留纯净、空白、面积足够的对话框、气泡、标牌或留白区域，"
    "并严格禁止在图中生成任何汉字、字母、数字、拟声词、乱码、签名或水印。"
    "各空白区域的位置与阅读顺序仍须遵循原提示词。共需预留 {count} 个独立文字区域。"
)


def plan_image_text(prompt: str, enabled: bool = True) -> ImageTextPlan:
    if not enabled:
        return ImageTextPlan(prompt, ())
    selected: list[tuple[int, int, ImageTextItem]] = []
    for match in _QUOTED_TEXT.finditer(prompt):
        value = next((group for group in match.groups() if group is not None), "").strip()
        if not value:
            continue
        before = prompt[max(0, match.start() - 28) : match.start()]
        before = re.split(r"[，。；\n”」』’\"]", before)[-1]
        after = prompt[match.end() : min(len(prompt), match.end() + 20)]
        after = re.split(r"[，。；\n“「『‘\"]", after)[0]
        if any(marker in before for marker in _TEXT_BEFORE_MARKERS) or any(
            marker in after for marker in _TEXT_AFTER_MARKERS
        ):
            selected.append(
                (
                    match.start(),
                    match.end(),
                    ImageTextItem(value, _position_hint(prompt, match.start())),
                )
            )
        if len(selected) >= 8:
            break
    if not selected:
        return ImageTextPlan(prompt, ())
    if sum(len(item[2].text) for item in selected) > 800:
        raise ImageTextError("图片中的指定文字总长度不能超过 800 个字符")

    parts: list[str] = []
    cursor = 0
    for start, end, _ in selected:
        parts.append(prompt[cursor:start])
        parts.append("空白文字区域")
        cursor = end
    parts.append(prompt[cursor:])
    model_prompt = "".join(parts) + _POSTPROCESS_INSTRUCTION.format(
        count=len(selected)
    )
    return ImageTextPlan(model_prompt, tuple(item[2] for item in selected))


def _position_hint(prompt: str, quote_start: int) -> str:
    context = prompt[max(0, quote_start - 140) : quote_start]
    local = re.split(r"[。；\n]", context)[-1]
    compact = re.sub(r"\s+", "", local)
    if any(marker in compact for marker in ("右侧下方", "右边下方", "右下角", "右下")):
        return "bottom_right"
    if any(marker in compact for marker in ("左侧下方", "左边下方", "左下角", "左下")):
        return "bottom_left"
    if "第三格" in compact and any(marker in compact for marker in ("左边", "左侧")):
        return "bottom_left"
    if "第三格" in compact and any(marker in compact for marker in ("右边", "右侧")):
        return "bottom_right"

    panel_positions = {
        "第一格": "top_left",
        "第二格": "top_right",
        "第三格": "bottom_center",
    }
    latest_panel = max(panel_positions, key=lambda marker: context.rfind(marker))
    if context.rfind(latest_panel) >= 0:
        return panel_positions[latest_panel]
    if any(marker in compact for marker in ("左上", "上方左", "顶部左")):
        return "top_left"
    if any(marker in compact for marker in ("右上", "上方右", "顶部右")):
        return "top_right"
    if any(marker in compact for marker in ("左边", "左侧")):
        return "left"
    if any(marker in compact for marker in ("右边", "右侧")):
        return "right"
    if any(marker in compact for marker in ("下方", "底部")):
        return "bottom_center"
    if any(marker in compact for marker in ("上方", "顶部")):
        return "top_center"
    return "auto"


def _light_regions(image: Image.Image, wanted: int) -> list[tuple[int, int, int, int]]:
    width, height = image.size
    scale = min(1.0, 360.0 / max(width, height))
    small_width = max(1, round(width * scale))
    small_height = max(1, round(height * scale))
    gray = image.convert("L").resize((small_width, small_height), Image.Resampling.BILINEAR)
    pixels = gray.load()
    visited = bytearray(small_width * small_height)
    minimum_area = max(40, int(small_width * small_height * 0.004))
    candidates: list[tuple[float, tuple[int, int, int, int]]] = []

    for y in range(small_height):
        for x in range(small_width):
            index = y * small_width + x
            if visited[index] or pixels[x, y] < 242:
                continue
            visited[index] = 1
            queue = deque([(x, y)])
            area = 0
            min_x = max_x = x
            min_y = max_y = y
            while queue:
                current_x, current_y = queue.popleft()
                area += 1
                min_x = min(min_x, current_x)
                max_x = max(max_x, current_x)
                min_y = min(min_y, current_y)
                max_y = max(max_y, current_y)
                for next_x, next_y in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    if not 0 <= next_x < small_width or not 0 <= next_y < small_height:
                        continue
                    next_index = next_y * small_width + next_x
                    if visited[next_index] or pixels[next_x, next_y] < 242:
                        continue
                    visited[next_index] = 1
                    queue.append((next_x, next_y))

            box_width = max_x - min_x + 1
            box_height = max_y - min_y + 1
            box_area = box_width * box_height
            fill_ratio = area / box_area
            touches_border = (
                min_x == 0
                or min_y == 0
                or max_x == small_width - 1
                or max_y == small_height - 1
            )
            if (
                area < minimum_area
                or box_width < small_width * 0.08
                or box_height < small_height * 0.045
                or box_area > small_width * small_height * 0.48
                or fill_ratio < 0.52
                or (touches_border and box_area > small_width * small_height * 0.08)
            ):
                continue
            aspect = box_width / box_height
            if not 0.18 <= aspect <= 8.0:
                continue
            candidates.append((area * fill_ratio, (min_x, min_y, max_x + 1, max_y + 1)))

    selected = sorted(candidates, key=lambda item: item[0], reverse=True)[:wanted]
    boxes = [item[1] for item in selected]
    boxes.sort(key=lambda box: (round(box[1] / max(1, small_height * 0.12)), box[0]))
    converted = [
        (
            max(0, round(left / scale)),
            max(0, round(top / scale)),
            min(width, round(right / scale)),
            min(height, round(bottom / scale)),
        )
        for left, top, right, bottom in boxes
    ]
    return converted


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.FreeTypeFont,
    maximum_width: int,
) -> list[str]:
    lines: list[str] = []
    for source in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not source:
            lines.append("")
            continue
        current = ""
        for character in source:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > maximum_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


def _fit_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    box: tuple[int, int, int, int],
    font_path: Path,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    left, top, right, bottom = box
    box_width = max(1, right - left)
    box_height = max(1, bottom - top)
    maximum_size = max(12, min(96, int(box_height * 0.55)))
    if len(value) <= 4:
        maximum_size = max(
            12,
            min(maximum_size, int(box_width * 0.72 / max(1, len(value)))),
        )
    best: tuple[ImageFont.FreeTypeFont, list[str], int] | None = None
    low, high = 12, maximum_size
    while low <= high:
        size = (low + high) // 2
        font = ImageFont.truetype(str(font_path), size=size)
        spacing = max(2, size // 5)
        lines = _wrap_text(draw, value, font, int(box_width * 0.86))
        line_height = draw.textbbox((0, 0), "国Ag", font=font)[3]
        total_height = len(lines) * line_height + max(0, len(lines) - 1) * spacing
        if total_height <= box_height * 0.82:
            best = (font, lines, spacing)
            low = size + 1
        else:
            high = size - 1
    if best is None:
        font = ImageFont.truetype(str(font_path), size=12)
        return font, _wrap_text(draw, value, font, int(box_width * 0.86)), 2
    return best


def _fallback_boxes(
    size: tuple[int, int], count: int
) -> list[tuple[int, int, int, int]]:
    width, height = size
    columns = 1 if count == 1 else 2
    rows = (count + columns - 1) // columns
    band_height = max(int(height * 0.18), rows * 90)
    top = max(0, height - band_height)
    margin = max(12, width // 80)
    gap = max(10, width // 100)
    cell_width = (width - margin * 2 - gap * (columns - 1)) // columns
    cell_height = (band_height - margin * 2 - gap * (rows - 1)) // rows
    boxes: list[tuple[int, int, int, int]] = []
    for index in range(count):
        row, column = divmod(index, columns)
        left = margin + column * (cell_width + gap)
        cell_top = top + margin + row * (cell_height + gap)
        boxes.append((left, cell_top, left + cell_width, cell_top + cell_height))
    return boxes


_POSITION_TARGETS = {
    "top_left": (0.23, 0.20),
    "top_center": (0.50, 0.18),
    "top_right": (0.77, 0.20),
    "left": (0.20, 0.50),
    "right": (0.80, 0.50),
    "bottom_left": (0.23, 0.77),
    "bottom_center": (0.50, 0.78),
    "bottom_right": (0.77, 0.77),
}


def _position_fallback_box(
    size: tuple[int, int], item: ImageTextItem
) -> tuple[int, int, int, int]:
    width, height = size
    center_x, center_y = _POSITION_TARGETS.get(item.position, (0.5, 0.78))
    if len(item.text) <= 3:
        box_width, box_height = int(width * 0.14), int(height * 0.18)
    elif len(item.text) <= 10:
        box_width, box_height = int(width * 0.28), int(height * 0.20)
    else:
        box_width, box_height = int(width * 0.38), int(height * 0.27)
    left = round(width * center_x - box_width / 2)
    top = round(height * center_y - box_height / 2)
    left = min(max(8, left), max(8, width - box_width - 8))
    top = min(max(8, top), max(8, height - box_height - 8))
    return left, top, left + box_width, top + box_height


def _assign_text_boxes(
    image: Image.Image,
    items: tuple[ImageTextItem, ...],
) -> tuple[list[tuple[int, int, int, int]], int, bool]:
    width, height = image.size
    candidates = _light_regions(image, min(24, max(len(items) * 3, len(items))))
    available = set(range(len(candidates)))
    assigned: list[tuple[int, int, int, int] | None] = [None] * len(items)
    used_fallback = False

    for item_index, item in enumerate(items):
        target = _POSITION_TARGETS.get(item.position)
        if target is None:
            continue
        ranked: list[tuple[float, int]] = []
        for candidate_index in available:
            left, top, right, bottom = candidates[candidate_index]
            center_x = (left + right) / (2 * width)
            center_y = (top + bottom) / (2 * height)
            distance = ((center_x - target[0]) ** 2 + (center_y - target[1]) ** 2) ** 0.5
            ranked.append((distance, candidate_index))
        if ranked:
            distance, candidate_index = min(ranked)
            if distance <= 0.34:
                assigned[item_index] = candidates[candidate_index]
                available.remove(candidate_index)

    for item_index, item in enumerate(items):
        if assigned[item_index] is not None:
            continue
        if item.position == "auto" and available:
            candidate_index = min(available)
            assigned[item_index] = candidates[candidate_index]
            available.remove(candidate_index)
        else:
            assigned[item_index] = _position_fallback_box(image.size, item)
            used_fallback = True
    return [box for box in assigned if box is not None], len(candidates), used_fallback


def apply_text_overlays(
    image_path: Path,
    items: tuple[ImageTextItem, ...] | tuple[str, ...],
    font_path: Path,
) -> OverlayResult:
    normalized = tuple(
        item if isinstance(item, ImageTextItem) else ImageTextItem(str(item))
        for item in items
    )
    if not normalized:
        return OverlayResult(0, 0, False)
    if not font_path.is_file() or font_path.is_symlink():
        raise ImageTextError("图片排字字体不存在或不可用")
    try:
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise ImageTextError("无法读取生成图片进行二次排字") from exc

    boxes, detected_count, used_fallback = _assign_text_boxes(image, normalized)
    draw = ImageDraw.Draw(image)
    for item, box in zip(normalized, boxes, strict=True):
        text = item.text
        left, top, right, bottom = box
        radius = max(8, min(right - left, bottom - top) // 10)
        border_width = max(2, min(image.size) // 500)
        draw.rounded_rectangle(
            box,
            radius=radius,
            fill=(255, 255, 255),
            outline=(20, 20, 20),
            width=border_width,
        )
        inner = (
            left + max(8, (right - left) // 16),
            top + max(8, (bottom - top) // 14),
            right - max(8, (right - left) // 16),
            bottom - max(8, (bottom - top) // 14),
        )
        font, lines, spacing = _fit_text(draw, text, inner, font_path)
        line_height = draw.textbbox((0, 0), "国Ag", font=font)[3]
        total_height = len(lines) * line_height + max(0, len(lines) - 1) * spacing
        y = inner[1] + max(0, (inner[3] - inner[1] - total_height) // 2)
        for line in lines:
            line_width = draw.textlength(line, font=font)
            x = inner[0] + max(0, (inner[2] - inner[0] - round(line_width)) // 2)
            draw.text((x, y), line, font=font, fill=(10, 10, 10))
            y += line_height + spacing

    temporary = image_path.with_name(f".{image_path.name}.text-overlay.part")
    try:
        image.save(temporary, format="PNG", optimize=True)
        temporary.replace(image_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ImageTextError("无法保存二次排字后的图片") from exc
    return OverlayResult(len(normalized), detected_count, used_fallback)
