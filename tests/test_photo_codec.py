"""真实本机编解码器处理合成像素；不读取家庭照片、不启动模型或设备。"""

import asyncio
import hashlib
import io
import os
from pathlib import Path
import struct
import sys
import zlib

from PIL import Image
import pytest

from homeplane.family_media.photo_codec import PhotoCodecError, render_photo
from homeplane.family_media.photo_contracts import PhotoWorkRequest
from homeplane.family_media.photo_runner import PhotoRunner


def image_bytes(image, format="PNG", **options):
    output = io.BytesIO()
    image.save(output, format, **options)
    return output.getvalue()


def request(payload, **changes):
    return PhotoWorkRequest(original_sha256=hashlib.sha256(payload).hexdigest(), input_bytes=len(payload), **changes)


def colors():
    image = Image.new("RGB", (4, 2))
    image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
                   (0, 255, 255), (255, 0, 255), (0, 0, 0), (128, 128, 128)])
    return image


def test_exif_crop_then_rotation_uses_upright_original_coordinates_and_strips_metadata():
    original = colors()
    exif = Image.Exif()
    exif[274], exif[315] = 6, "PRIVATE-CREATOR-DO-NOT-EXPORT"
    payload = image_bytes(original, exif=exif)
    before = hashlib.sha256(payload).hexdigest()
    operations = [{"operation": "rotate", "degrees_clockwise": 90},
                  {"operation": "crop", "x": 0, "y": .25, "width": 1, "height": .5}]
    result, edited = render_photo(payload, request(payload, operations=operations))
    assert (result.source_width, result.source_height) == (2, 4)
    assert (result.width, result.height) == (2, 2)
    with Image.open(io.BytesIO(edited)) as decoded:
        assert list(decoded.get_flattened_data()) == [(0, 0, 0), (255, 0, 255), (0, 0, 255), (0, 255, 0)]
        assert not decoded.getexif() and decoded.info.get("icc_profile")
    assert b"PRIVATE-CREATOR" not in edited and hashlib.sha256(payload).hexdigest() == before
    assert result.metadata_stripped and result.original_mutated is False


@pytest.mark.parametrize("orientation", range(1, 9))
def test_all_exif_orientations_are_applied_once(orientation):
    exif = Image.Exif(); exif[274] = orientation
    source = colors()
    payload = image_bytes(source, exif=exif)
    _, edited = render_photo(payload, request(payload))
    matrices = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}
    expected = source.transpose(matrices[orientation]) if orientation in matrices else source
    with Image.open(io.BytesIO(edited)) as decoded:
        assert decoded.size == expected.size and decoded.tobytes() == expected.tobytes()
        assert decoded.getexif().get(274) is None


@pytest.mark.parametrize("stops,expected", [(1, 176), (-1, 92), (0, 128)])
def test_exposure_changes_linear_light_and_preserves_alpha(stops, expected):
    payload = image_bytes(Image.new("RGBA", (3, 2), (128, 128, 128, 85)))
    _, edited = render_photo(payload, request(payload, operations=[{"operation": "exposure", "stops": stops}]))
    with Image.open(io.BytesIO(edited)) as decoded:
        assert decoded.getpixel((0, 0)) == (expected, expected, expected, 85)


def test_jpeg_flattens_alpha_to_white_and_preview_keeps_original_dimensions():
    payload = image_bytes(Image.new("RGBA", (40, 20), (0, 0, 0, 0)))
    result, edited = render_photo(payload, request(payload, output_format="JPEG", max_edge=12))
    assert (result.source_width, result.source_height) == (40, 20)
    assert (result.width, result.height) == (12, 6) and result.content_type == "image/jpeg"
    with Image.open(io.BytesIO(edited)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 255, 255) and decoded.mode == "RGB"


@pytest.mark.parametrize("format", ["JPEG", "WEBP", "TIFF", "GIF", "AVIF"])
def test_real_static_decoders_return_actual_png_pixels(format):
    payload = image_bytes(colors(), format)
    result, edited = render_photo(payload, request(payload))
    assert result.source_format == format and (result.width, result.height) == (4, 2)
    with Image.open(io.BytesIO(edited)) as decoded:
        assert decoded.format == "PNG" and decoded.size == (4, 2)


def test_real_heic_decoder_and_export_preserve_the_qualified_source_pixels():
    from pillow_heif import register_heif_opener
    register_heif_opener()
    payload = image_bytes(colors(), "HEIF", quality=-1, chroma="444")
    with Image.open(io.BytesIO(payload)) as original:
        source_pixels = original.convert("RGB").tobytes()
    # HEVC 无损指 YCbCr 编码；RGB→YCbCr 往返仍有量化，不能把编码前像素当原文件证据。
    assert max(abs(a-b) for a, b in zip(source_pixels, colors().tobytes())) <= 2
    result, edited = render_photo(payload, request(payload))
    assert result.source_format == "HEIF" and (result.width, result.height) == (4, 2)
    with Image.open(io.BytesIO(edited)) as decoded:
        assert decoded.mode == "RGB" and decoded.tobytes() == source_pixels


def test_heic_collection_uses_declared_primary_instead_of_first_photo():
    from pillow_heif import register_heif_opener
    register_heif_opener()
    payload = image_bytes(colors(), "HEIF", quality=-1, chroma="444", save_all=True,
                          append_images=[Image.new("RGB", (4, 2), "white")], primary_index=1)
    result, edited = render_photo(payload, request(payload))
    assert result.source_image_count == 2 and result.source_image_index == 1
    with Image.open(io.BytesIO(edited)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 255, 255)


def test_embedded_srgb_profile_is_actually_converted_and_source_exif_not_copied():
    from PIL import ImageCms
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = colors()
    payload = image_bytes(source, icc_profile=profile)
    result, edited = render_photo(payload, request(payload))
    assert result.color_basis == "embedded_icc"
    with Image.open(io.BytesIO(edited)) as decoded:
        assert decoded.tobytes() == source.tobytes() and decoded.info.get("icc_profile")


def test_changed_original_unknown_bytes_and_invalid_color_profile_are_refused():
    payload = image_bytes(colors())
    with pytest.raises(PhotoCodecError, match="photo_original_changed"):
        render_photo(payload + b"x", request(payload))
    unknown = b"SYNTHETIC-NOT-A-PHOTO"
    with pytest.raises(PhotoCodecError, match="photo_decode_failed"):
        render_photo(unknown, request(unknown))
    broken = image_bytes(colors(), icc_profile=b"PRIVATE-INVALID-COLOR-PROFILE")
    with pytest.raises(PhotoCodecError, match="photo_color_profile_unsupported"):
        render_photo(broken, request(broken))


def test_pixel_bomb_is_rejected_before_pixel_allocation():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    payload = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100000, 100000, 8, 2, 0, 0, 0))
    payload += chunk(b"IDAT", zlib.compress(b"\0")) + chunk(b"IEND", b"")
    with pytest.raises(PhotoCodecError, match="photo_pixel_limit"):
        render_photo(payload, request(payload))


def test_animation_and_actual_encoder_output_over_budget_are_refused(monkeypatch):
    payload = image_bytes(colors(), "GIF", save_all=True, append_images=[Image.new("RGB", (4, 2), "white")], duration=50)
    with pytest.raises(PhotoCodecError, match="photo_animation_unsupported"):
        render_photo(payload, request(payload))
    payload = image_bytes(colors())
    monkeypatch.setattr("homeplane.family_media.photo_codec.MAX_RENDER_BYTES", 64)
    with pytest.raises(PhotoCodecError, match="photo_output_limit"):
        render_photo(payload, request(payload))


async def launch_worker():
    script = Path(__file__).resolve().parents[1] / "homeplane/family_media/photo_worker.py"
    code = "import runpy,sys;"
    extra = os.environ.get("FRIDAY_TEST_PHOTO_CODEC_ROOT")
    if extra:
        # 仅测试注入已核验的插件输入，不改现有解释器或产品的默认启动命令。
        code += "sys.path.insert(0," + repr(str(Path(extra).resolve())) + ");"
    code += "runpy.run_path(" + repr(str(script)) + ",run_name='__main__')"
    return await asyncio.create_subprocess_exec(sys.executable, "-B", "-I", "-c", code,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)


@pytest.mark.asyncio
async def test_real_worker_protocol_delivers_the_same_checked_pixels_and_reaps():
    payload = image_bytes(colors())
    processes = []
    async def launch():
        process = await launch_worker(); processes.append(process); return process
    runner = PhotoRunner(launcher=launch)
    try:
        result, encoded = await runner.render(payload, request(payload))
        assert result.output_sha256 == hashlib.sha256(encoded).hexdigest()
        with Image.open(io.BytesIO(encoded)) as decoded:
            assert decoded.tobytes() == colors().tobytes()
        assert processes[0].returncode == 0 and runner.process is None and runner.active is None
        assert await processes[0].stderr.read() == b""
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_worker_rejects_changed_input_and_cleans_process():
    payload = image_bytes(colors())
    runner = PhotoRunner(launcher=launch_worker)
    invalid = request(payload).model_copy(update={"original_sha256": "0" * 64})
    try:
        with pytest.raises(PhotoCodecError, match="photo_original_changed"):
            await runner.render(payload, invalid)
        assert runner.process is None and runner.active is None
    finally:
        await runner.close()
