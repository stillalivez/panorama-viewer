from __future__ import annotations

import argparse
import html
import json
import math
import re
import shutil
import statistics
import warnings
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageChops, ImageOps


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "panorama_viewer"
DEFAULT_MAX_WIDTH = 0
DEFAULT_SEAM_BLEND = 0
DEFAULT_AUTO_SEAM_SHIFT = False
DEFAULT_SEAM_SHIFT_THRESHOLD = 1.45
DEFAULT_QUALITY = 94
SEAM_SCAN_HEIGHT = 256
SEAM_EDGE_MARGIN_RATIO = 0.08
CAPTURE_COMPACT_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?:(\d{2})(\d{2})(\d{2}))?(?!\d)")
CAPTURE_SEPARATED_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[._\-/年 ]{0,2}(\d{1,2})[._\-/月 ]{0,2}(\d{1,2})"
    r"(?:[日 _Tt-]+(\d{1,2})[:._\-时]?(\d{1,2})(?:[:._\-分]?(\d{1,2}))?)?"
)
Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

TEMPLATE_PATH = ROOT / "panorama_viewer_template.html"


def _default_images() -> list[Path]:
    config_path = DEFAULT_OUTPUT_DIR / 'viewer_config.json'
    if not config_path.is_file():
        return []
    config = json.loads(config_path.read_text(encoding='utf-8'))
    paths = [(DEFAULT_OUTPUT_DIR / scene['image']).resolve() for scene in config.get('scenes', [])]
    if any(not path.is_relative_to(DEFAULT_OUTPUT_DIR.resolve()) for path in paths):
        raise ValueError('已有场景图片必须位于查看器目录内')
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a local WebGL panorama viewer.")
    parser.add_argument("--images", nargs="+", default=None, help="Source panorama image paths; defaults to existing scenes or an empty viewer.")
    parser.add_argument("--labels", nargs="*", default=[], help="Optional labels for each scene.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory.")
    parser.add_argument("--title", default="720°全景照片查看器", help="Viewer title.")
    parser.add_argument("--subtitle", default="本地离线 WebGL 全景查看器", help="Viewer subtitle.")
    parser.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH, help="Maximum panorama width. Use 0 to keep the original dimensions.")
    parser.add_argument("--seam-blend", type=int, default=DEFAULT_SEAM_BLEND, help="Blend width in pixels for the 360-degree seam.")
    parser.add_argument(
        "--auto-seam-shift",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_AUTO_SEAM_SHIFT,
        help="Detect the strongest vertical seam and rotate it to the 0/360 boundary.",
    )
    parser.add_argument(
        "--seam-shift-threshold",
        type=float,
        default=DEFAULT_SEAM_SHIFT_THRESHOLD,
        help="How much stronger a seam must be than the current wrap boundary before rotating.",
    )
    parser.add_argument("--thumbnail-width", type=int, default=720, help="Thumbnail width.")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="JPEG quality for generated images.")
    return parser.parse_args()


def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", str(value).strip()).strip("-").lower()
    return slug or fallback


def _capture_text(path: Path) -> tuple[str, str]:
    text, _ = _capture_metadata(path)
    return text, "" if text == "拍摄时间未知" else text


def _capture_metadata(path: Path) -> tuple[str, str]:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            original = exif.get_ifd(ExifTags.IFD.Exif).get(36867) or exif.get(36867)
        if original:
            value = datetime.strptime(str(original).strip('\x00 '), '%Y:%m:%d %H:%M:%S')
            return value.strftime('%Y-%m-%d %H:%M:%S'), 'EXIF 拍摄时间'
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        pass
    try:
        with path.open('rb') as stream:
            xmp = stream.read(2 * 1024 * 1024).decode('latin1')
        for name in ('DateTimeOriginal', 'DateCreated'):
            raw = _xmp_attr(xmp, name)
            if raw:
                match = re.match(r'(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})', raw)
                if match:
                    try:
                        value = datetime(*map(int, match.groups()))
                        return value.strftime('%Y-%m-%d %H:%M:%S'), 'XMP 拍摄时间'
                    except ValueError:
                        continue
    except OSError:
        pass
    for pattern in (CAPTURE_COMPACT_RE, CAPTURE_SEPARATED_RE):
        match = pattern.search(path.stem)
        if not match:
            continue
        year, month, day, hour, minute, second = match.groups(default="")
        has_time = bool(hour and minute)
        try:
            dt = datetime(
                int(year),
                int(month),
                int(day),
                int(hour or 0),
                int(minute or 0),
                int(second or 0),
            )
        except ValueError:
            continue
        if has_time:
            text = dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            text = dt.strftime("%Y-%m-%d")
        return text, '文件名日期'
    return "拍摄时间未知", ""


def _parse_float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _xmp_attr(text: str, name: str) -> str | None:
    match = re.search(rf'''(?:[A-Za-z0-9_-]+:)?{re.escape(name)}\s*=\s*["']([^"']+)["']''', text)
    if not match:
        match = re.search(rf'<(?:[A-Za-z0-9_-]+:)?{re.escape(name)}>([^<]+)<', text)
    if not match:
        return None
    return match.group(1)


def _gps_img_direction(path: Path) -> float | None:
    try:
        with Image.open(path) as image:
            gps = image.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:
        return None
    if gps.get(16) != 'T':
        return None
    for tag_id, value in gps.items():
        if ExifTags.GPSTAGS.get(tag_id) == "GPSImgDirection":
            return _parse_float(value)
    return None


def _heading_metadata(path: Path) -> tuple[float | None, str]:
    try:
        with path.open('rb') as stream:
            text = stream.read(2 * 1024 * 1024).decode("latin1", errors="ignore")
    except OSError:
        text = ""
    candidates = [
        ("PoseHeadingDegrees", "GPano:PoseHeadingDegrees"),
        ("GimbalYawDegree", "DJI GimbalYawDegree"),
        ("FlightYawDegree", "DJI FlightYawDegree"),
    ]
    for attr, source in candidates:
        value = _parse_float(_xmp_attr(text, attr))
        if value is not None:
            return value % 360, source
    value = _gps_img_direction(path)
    if value is not None:
        return value % 360, "EXIF GPS 真北方位"
    return None, ""


def _scene_label(path: Path, index: int, labels: list[str]) -> str:
    if index < len(labels) and str(labels[index]).strip():
        return str(labels[index]).strip()
    _, short_time = _capture_text(path)
    return f"观测点 {index + 1}" + (f" · {short_time}" if short_time else "")


def _resize_size(width: int, height: int, max_width: int) -> tuple[int, int]:
    if max_width <= 0:
        return width, height
    if width <= max_width:
        return width, height
    scale = max_width / float(width)
    return max_width, max(1, int(round(height * scale)))


def _blend_horizontal_seam(image: Image.Image, blend_px: int) -> Image.Image:
    width, height = image.size
    blend_px = max(0, min(int(blend_px), width // 2))
    if blend_px <= 0:
        return image
    if image.mode != "RGB":
        image = image.convert("RGB")
    pixels = image.load()
    for offset in range(blend_px):
        mix = 0.5 * (1 - offset / float(blend_px))
        left_x = offset
        right_x = width - 1 - offset
        for y in range(height):
            left = pixels[left_x, y]
            right = pixels[right_x, y]
            left_mix = tuple(int(round(left[idx] * (1 - mix) + right[idx] * mix)) for idx in range(3))
            right_mix = tuple(int(round(right[idx] * (1 - mix) + left[idx] * mix)) for idx in range(3))
            pixels[left_x, y] = left_mix
            pixels[right_x, y] = right_mix
    return image


def _scan_seam_scores(image: Image.Image) -> list[float]:
    width, height = image.size
    scan_height = min(SEAM_SCAN_HEIGHT, height)
    scan_width = max(256, int(round(width * (scan_height / float(height)))))
    scan = image.resize((scan_width, scan_height), Image.Resampling.BILINEAR)
    if scan.mode != "RGB":
        scan = scan.convert("RGB")
    pixels = scan.load()
    scores: list[float] = []
    for x in range(scan_width):
        nx = (x + 1) % scan_width
        total = 0.0
        for y in range(scan_height):
            left = pixels[x, y]
            right = pixels[nx, y]
            total += abs(left[0] - right[0]) + abs(left[1] - right[1]) + abs(left[2] - right[2])
        scores.append(total / max(1, scan_height * 3))
    return scores


def _detect_seam_shift(image: Image.Image, threshold: float) -> tuple[int, float]:
    scores = _scan_seam_scores(image)
    if not scores:
        return 0, 0.0
    scan_width = len(scores)
    best_idx = max(range(scan_width), key=scores.__getitem__)
    best_score = scores[best_idx]
    wrap_score = scores[-1]
    baseline = max(wrap_score, statistics.median(scores))
    edge_margin = max(12, int(scan_width * SEAM_EDGE_MARGIN_RATIO))
    if best_idx < edge_margin or best_idx > scan_width - 1 - edge_margin:
        return 0, best_score
    if baseline > 0 and best_score < baseline * max(1.0, float(threshold)):
        return 0, best_score
    shift_px = int(round((best_idx + 1) * image.size[0] / float(scan_width)))
    return shift_px % image.size[0], best_score


def _save_scaled(
    source: Path,
    dest: Path,
    max_width: int,
    quality: int,
    seam_blend: int,
    auto_seam_shift: bool,
    seam_shift_threshold: float,
) -> tuple[int, int, int, int, int, float]:
    same_file = False
    try:
        same_file = source.resolve() == dest.resolve()
    except OSError:
        same_file = False
    if max_width <= 0 and seam_blend <= 0 and not auto_seam_shift:
        with Image.open(source) as image:
            original_width, original_height = image.size
            output_width, output_height = original_width, original_height
            orientation = image.getexif().get(274, 1)
        if orientation == 1:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not same_file:
                shutil.copy2(source, dest)
            return original_width, original_height, output_width, output_height, 0, 0.0

    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        original_width, original_height = image.size
        output_width, output_height = _resize_size(original_width, original_height, max_width)
        if image.size != (output_width, output_height):
            image = image.resize((output_width, output_height), Image.Resampling.LANCZOS)
        if image.mode != "RGB":
            image = image.convert("RGB")
        seam_shift_px = 0
        seam_score = 0.0
        if auto_seam_shift:
            seam_shift_px, seam_score = _detect_seam_shift(image, seam_shift_threshold)
            if seam_shift_px:
                image = ImageChops.offset(image, -seam_shift_px, 0)
        image = _blend_horizontal_seam(image, seam_blend)
        save_path = dest.with_name(f"{dest.stem}.tmp{dest.suffix}") if same_file else dest
        image.save(save_path, format="JPEG", quality=quality, optimize=True, progressive=True)
        if same_file:
            save_path.replace(dest)
    return original_width, original_height, output_width, output_height, seam_shift_px, seam_score


def _save_thumb(source: Path, dest: Path, max_width: int, quality: int) -> None:
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        size = _resize_size(image.size[0], image.size[1], max_width)
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(dest, format="JPEG", quality=quality, optimize=True, progressive=True)


def _script_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _render_html(config: dict[str, Any], title: str, subtitle: str, timestamp: str) -> str:
    template = TEMPLATE_PATH.read_text(encoding='utf-8')
    values = {'__CONFIG__': _script_json(config), '__TITLE__': html.escape(title),
              '__SUBTITLE__': html.escape(subtitle), '__TIME__': html.escape(timestamp)}
    return re.sub(r'__CONFIG__|__TITLE__|__SUBTITLE__|__TIME__', lambda m: values[m.group()], template)


def _readme(output_dir: Path) -> str:
    return f"""# 720°全景照片查看器

查看器输出目录：
`{output_dir}`

## 启动方式

```powershell
python build_panorama_viewer.py
python start_panorama_viewer.py --dir "{output_dir}"
```

启动器默认选择空闲端口，请使用终端显示的实际地址。
如需固定端口，在启动命令后加 `--port 8765`；端口不可用时会使用其他空闲端口。
"""


def build_viewer(
    image_paths: list[Path],
    labels: list[str],
    output_dir: Path,
    title: str,
    subtitle: str,
    max_width: int,
    seam_blend: int,
    auto_seam_shift: bool,
    seam_shift_threshold: float,
    thumbnail_width: int,
    quality: int,
) -> Path:
    """Prepare a complete version before replacing the currently usable output."""
    if output_dir.is_symlink():
        raise ValueError('输出目录不能是符号链接。')
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir == ROOT or ROOT.is_relative_to(output_dir):
        raise ValueError('输出目录不能是项目根目录或其上级目录。')
    # All cleanup is restricted to directories created by this build.
    with tempfile.TemporaryDirectory(prefix=f'.{output_dir.name}-build-', dir=output_dir.parent) as work:
        staged = Path(work) / 'staged'
        if output_dir.exists():
            shutil.copytree(output_dir, staged)
        _build_into(image_paths, labels, staged, title, subtitle, max_width, seam_blend,
                    auto_seam_shift, seam_shift_threshold, thumbnail_width, quality)
        (staged / 'README.md').write_text(_readme(output_dir), encoding='utf-8')
        backup = output_dir.with_name(f'.{output_dir.name}-backup-{uuid.uuid4().hex}')
        had_output = output_dir.exists()
        if had_output:
            output_dir.rename(backup)
        try:
            staged.rename(output_dir)
        except OSError:
            if had_output:
                backup.rename(output_dir)
            raise
        if had_output:
            if backup.parent != output_dir.parent or not backup.name.startswith(f'.{output_dir.name}-backup-'):
                raise RuntimeError('Unexpected backup cleanup path')
            try:
                shutil.rmtree(backup)
            except OSError as exc:
                warnings.warn(f'新版本已生成，旧版本备份保留在 {backup}：{exc}')
    return output_dir


def _build_into(
    image_paths: list[Path], labels: list[str], output_dir: Path, title: str, subtitle: str,
    max_width: int, seam_blend: int, auto_seam_shift: bool, seam_shift_threshold: float,
    thumbnail_width: int, quality: int,
) -> Path:
    missing = [str(path) for path in image_paths if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing panorama images:\n" + "\n".join(f"- {path}" for path in missing))

    output_dir.mkdir(parents=True, exist_ok=True)
    pano_dir = output_dir / "assets" / "panoramas"
    thumb_dir = output_dir / "assets" / "thumbnails"
    pano_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scenes: list[dict[str, Any]] = []

    for index, image_path in enumerate(image_paths):
        with Image.open(image_path) as validation:
            validation.verify()
        with Image.open(image_path) as image:
            image_format = image.format
            if image_format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError(f'{image_path.name}：仅支持 JPEG、PNG、WebP 全景图片。')
            orientation = image.getexif().get(274, 1)
            width, height = image.size
            if orientation in (5, 6, 7, 8):
                width, height = height, width
            if abs(width / height - 2) > 0.02:
                raise ValueError(f'{image_path.name}：需要 2:1 全景图片，当前为 {width}×{height}。')
        label = _scene_label(image_path, index, labels)
        slug = _slugify(f"{index + 1}-{label}", f"scene-{index + 1}")
        preserve_format = max_width <= 0 and seam_blend <= 0 and not auto_seam_shift and orientation == 1
        extension = {'JPEG':'.jpg', 'PNG':'.png', 'WEBP':'.webp'}[image_format] if preserve_format else '.jpg'
        pano_name = f"{slug}{extension}"
        thumb_name = f"{slug}_thumb.jpg"
        pano_path = pano_dir / pano_name
        thumb_path = thumb_dir / thumb_name

        ow, oh, rw, rh, seam_shift_px, seam_score = _save_scaled(
            image_path,
            pano_path,
            max_width=max_width,
            quality=quality,
            seam_blend=seam_blend,
            auto_seam_shift=auto_seam_shift,
            seam_shift_threshold=seam_shift_threshold,
        )
        _save_thumb(pano_path, thumb_path, max_width=thumbnail_width, quality=max(72, min(quality, 82)))
        capture_full, capture_source = _capture_metadata(image_path)
        heading_degrees, heading_source = _heading_metadata(image_path)
        scenes.append(
            {
                "id": slug,
                "label": label,
                "source_name": image_path.name,
                "source_path": str(image_path),
                "capture_time": capture_full,
                "capture_time_label": capture_full,
                "capture_time_source": capture_source,
                "image": f"./assets/panoramas/{pano_name}",
                "thumbnail": f"./assets/thumbnails/{thumb_name}",
                "original_width": ow,
                "original_height": oh,
                "optimized_width": rw,
                "optimized_height": rh,
                "seam_shift_px": seam_shift_px,
                "seam_score": round(seam_score, 4),
                "heading_degrees": None if heading_degrees is None else round(heading_degrees, 2),
                "heading_source": heading_source,
            }
        )

    icon_source = ROOT / 'assets' / 'app-icon.ico'
    if not icon_source.is_file():
        raise FileNotFoundError(f'缺少应用图标：{icon_source}')
    shutil.copy2(icon_source, output_dir / 'assets' / 'app-icon.ico')
    config = {"title": title, "subtitle": subtitle, "generated_at": timestamp, "scene_count": len(scenes), "scenes": scenes}
    (output_dir / "viewer_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "index.html").write_text(_render_html(config, title, subtitle, timestamp), encoding="utf-8")
    (output_dir / "README.md").write_text(_readme(output_dir), encoding="utf-8")
    return output_dir


def main() -> None:
    args = parse_args()
    requested_max_width = int(args.max_width)
    viewer_dir = build_viewer(
        image_paths=[Path(value) for value in args.images] if args.images is not None else _default_images(),
        labels=list(args.labels),
        output_dir=Path(args.output_dir),
        title=args.title,
        subtitle=args.subtitle,
        max_width=0 if requested_max_width <= 0 else max(1024, requested_max_width),
        seam_blend=max(0, int(args.seam_blend)),
        auto_seam_shift=bool(args.auto_seam_shift),
        seam_shift_threshold=max(1.0, float(args.seam_shift_threshold)),
        thumbnail_width=max(240, int(args.thumbnail_width)),
        quality=max(50, min(95, int(args.quality))),
    )
    print(f"Panorama viewer generated: {viewer_dir}")
    print("Start local server with: python start_panorama_viewer.py")


if __name__ == "__main__":
    main()
