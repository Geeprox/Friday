"""本机照片像素处理。仅子进程调用；不修改原件或执行外部程序。"""

import hashlib
import io
import math
import warnings

from .photo_contracts import MAX_PHOTO_PIXELS, MAX_RENDER_BYTES, PhotoWorkRequest, PhotoWorkResult

MAX_ICC_BYTES = 1024 * 1024


class PhotoCodecError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class LimitedOutput(io.BytesIO):
    """编码器写出时即检查容量，不在完整分配后才拒绝。"""

    def write(self, value):
        if self.tell() + len(value) > MAX_RENDER_BYTES:
            raise PhotoCodecError("photo_output_limit")
        return super().write(value)


def _srgb_decode(value):
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _srgb_encode(value):
    value = min(1.0, max(0.0, value))
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def exposure_table(stops):
    """曝光在 sRGB 对应的线性光上变化，不能用编码值的亮度缩放冒充。"""
    return [round(255 * _srgb_encode(_srgb_decode(n / 255) * 2**stops)) for n in range(256)]


def _color_image(image, Image, ImageCms):
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    if image.mode not in {"RGB", "RGBA", "L", "LA", "P", "CMYK"}:
        raise PhotoCodecError("photo_color_depth_unsupported")
    if image.mode == "P" and "transparency" in image.info:
        alpha = image.convert("RGBA").getchannel("A")
    original_profile = image.info.get("icc_profile")
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    if original_profile:
        if not isinstance(original_profile, bytes) or len(original_profile) > MAX_ICC_BYTES:
            raise PhotoCodecError("photo_color_profile_unsupported")
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(original_profile))
            target = ImageCms.createProfile("sRGB")
            color_input = image if image.mode in {"RGB", "CMYK"} else rgb
            rgb = ImageCms.profileToProfile(color_input, source, target, outputMode="RGB")
        except Exception:
            raise PhotoCodecError("photo_color_profile_unsupported") from None
        basis = "embedded_icc"
    else:
        profile = image.info.get("nclx_profile")
        # 没有 ICC 的 HDR/广色域不能当成普通 sRGB 导出，避免静默偏色。
        if profile:
            primaries = profile.get("color_primaries")
            transfer = profile.get("transfer_characteristics")
            if primaries not in {1, 2} or transfer not in {1, 2, 13}:
                raise PhotoCodecError("photo_color_profile_unsupported")
            if transfer == 1:
                def bt709(value):
                    encoded = value / 255
                    linear = encoded / 4.5 if encoded < 0.081 else ((encoded + 0.099) / 1.099) ** (1 / 0.45)
                    return round(255 * _srgb_encode(linear))
                rgb = rgb.point([bt709(n) for n in range(256)] * 3)
                basis = "nclx_bt709"
            else:
                basis = "nclx_srgb" if transfer == 13 else "assumed_srgb"
        else:
            basis = "assumed_srgb"
    if alpha is not None:
        rgb.putalpha(alpha)
    return rgb, basis


def render_photo(payload: bytes, request: PhotoWorkRequest):
    """返回实际编码像素及其摘要。裁剪坐标始终基于转正后的原图。"""
    if len(payload) != request.input_bytes or hashlib.sha256(payload).hexdigest() != request.original_sha256:
        raise PhotoCodecError("photo_original_changed")
    try:
        from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
        from pillow_heif import register_heif_opener
    except ImportError:
        raise PhotoCodecError("photo_dependency_missing") from None
    register_heif_opener(decode_threads=2, thumbnails=False, depth_images=False, aux_images=False)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload), formats=["JPEG", "PNG", "WEBP", "GIF", "TIFF", "AVIF", "HEIF"]) as source:
                source_format = source.format
                if source.width * source.height > MAX_PHOTO_PIXELS:
                    raise PhotoCodecError("photo_pixel_limit")
                source_image_count = getattr(source, "n_frames", 1)
                source_image_index = source.tell()
                if source_image_count > 128:
                    raise PhotoCodecError("photo_source_image_limit")
                # HEIF 集合按文件指定的主照片打开，不将多张静态照片误当 GIF 动画。
                if (source_format != "HEIF" and getattr(source, "is_animated", False)
                        or source_format == "TIFF" and source_image_count > 1):
                    raise PhotoCodecError("photo_animation_unsupported")
                source.load()
                ImageOps.exif_transpose(source, in_place=True)
                source_width, source_height = source.size
                image, basis = _color_image(source, Image, ImageCms)
                operations = {operation.operation: operation for operation in request.operations}
                crop = operations.get("crop")
                if crop:
                    left, top = math.floor(crop.x * source_width), math.floor(crop.y * source_height)
                    right = min(source_width, max(left + 1, math.ceil((crop.x + crop.width) * source_width)))
                    bottom = min(source_height, max(top + 1, math.ceil((crop.y + crop.height) * source_height)))
                    image = image.crop((left, top, right, bottom))
                rotate = operations.get("rotate")
                if rotate and rotate.degrees_clockwise:
                    rotation = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                                270: Image.Transpose.ROTATE_90}[rotate.degrees_clockwise]
                    image = image.transpose(rotation)
                exposure = operations.get("exposure")
                if exposure and exposure.stops:
                    table = exposure_table(exposure.stops) * 3
                    image = image.point(table + list(range(256)) if image.mode == "RGBA" else table)
                if request.max_edge:
                    image.thumbnail((request.max_edge, request.max_edge), Image.Resampling.LANCZOS)
                if request.output_format == "JPEG" and image.mode == "RGBA":
                    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                    image = Image.alpha_composite(background, image).convert("RGB")
                # 仅写工作色彩配置；EXIF、位置、XMP、原始注释与辅助图不进入副本。
                image.info.clear()
                target_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
                output = LimitedOutput()
                options = {"icc_profile": target_profile}
                if request.output_format == "JPEG":
                    options.update(quality=95, subsampling=0, exif=b"")
                else:
                    options.update(compress_level=6)
                image.save(output, request.output_format, **options)
                encoded = output.getvalue()
                with Image.open(io.BytesIO(encoded)) as verified:
                    if verified.size != image.size or verified.format != request.output_format:
                        raise PhotoCodecError("photo_output_unverified")
                    verified.load()
                result = PhotoWorkResult(original_sha256=request.original_sha256,
                    output_sha256=hashlib.sha256(encoded).hexdigest(), size_bytes=len(encoded),
                    source_format=source_format, source_width=source_width, source_height=source_height,
                    source_image_count=source_image_count, source_image_index=source_image_index,
                    width=image.width, height=image.height, output_format=request.output_format,
                    content_type="image/png" if request.output_format == "PNG" else "image/jpeg", color_basis=basis)
                return result, encoded
    except PhotoCodecError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhotoCodecError("photo_pixel_limit") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise PhotoCodecError("photo_decode_failed") from None
