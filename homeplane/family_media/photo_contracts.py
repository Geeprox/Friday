"""照片处理子进程的定长协议；来源和文件授权由目录持有者管理。"""

from typing import Literal

from pydantic import Field, model_validator

from homeplane.contracts import Contract

from .contracts import Digest, FamilyMediaOperation

MAX_PHOTO_BYTES = 64 * 1024 * 1024
MAX_RENDER_BYTES = 32 * 1024 * 1024
MAX_PHOTO_PIXELS = 50_000_000
MAX_PREVIEW_EDGE = 2048


class PhotoWorkRequest(Contract):
    original_sha256: Digest
    input_bytes: int = Field(gt=0, le=MAX_PHOTO_BYTES)
    operations: list[FamilyMediaOperation] = Field(default_factory=list, max_length=4)
    output_format: Literal["JPEG", "PNG"] = "PNG"
    max_edge: int | None = Field(default=None, ge=1, le=MAX_PREVIEW_EDGE)

    @model_validator(mode="after")
    def unique_operations(self):
        names = [op.operation for op in self.operations]
        if len(set(names)) != len(names):
            raise ValueError("photo_duplicate_operation")
        return self


class PhotoWorkResult(Contract):
    original_sha256: Digest
    output_sha256: Digest
    size_bytes: int = Field(gt=0, le=MAX_RENDER_BYTES)
    source_format: Literal["JPEG", "PNG", "WEBP", "HEIF", "GIF", "TIFF", "AVIF"]
    source_width: int = Field(ge=1, le=MAX_PHOTO_PIXELS)
    source_height: int = Field(ge=1, le=MAX_PHOTO_PIXELS)
    source_image_count: int = Field(default=1, ge=1, le=128)
    source_image_index: int = Field(default=0, ge=0, lt=128)
    width: int = Field(ge=1, le=MAX_PHOTO_PIXELS)
    height: int = Field(ge=1, le=MAX_PHOTO_PIXELS)
    output_format: Literal["JPEG", "PNG"]
    content_type: Literal["image/jpeg", "image/png"]
    color_space: Literal["sRGB"] = "sRGB"
    color_basis: Literal["embedded_icc", "nclx_srgb", "nclx_bt709", "assumed_srgb"]
    metadata_stripped: Literal[True] = True
    original_mutated: Literal[False] = False

    @model_validator(mode="after")
    def bounded_image(self):
        if max(self.source_width * self.source_height, self.width * self.height) > MAX_PHOTO_PIXELS:
            raise ValueError("photo_pixel_limit")
        if self.content_type != ("image/png" if self.output_format == "PNG" else "image/jpeg"):
            raise ValueError("photo_output_type_mismatch")
        if self.source_image_index >= self.source_image_count:
            raise ValueError("photo_source_image_invalid")
        return self
