"""Private owner wiring over the package's actual ARL factory; no transport.

No HTTP/CLI enablement, installation, model selection or execution authority.
Source/hash agreement alone is not human approval. Only a separately reviewed
internal composition may provide a runner; tests use an explicit provider double.
"""

import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .arl import ARLError
from .arl_actor_intake import _checked_path, _read, inspect_candidate


class ReviewedPlanningPackage:
    """Load once per owner boot; never replace the installed arl or sys.path."""

    def __init__(self, candidate, manifest_sha256, loader_sha256, *, product_root):
        self.product_root = Path(product_root)
        self.manifest_sha256, self.loader_sha256 = manifest_sha256, loader_sha256
        self.seal = self.inspect(candidate)
        raw = _read(Path(candidate) / "load_sdk.py")
        if hashlib.sha256(raw).hexdigest() != loader_sha256:
            raise ARLError("actor_loader_changed")
        namespace = {"__name__": "friday_verified_loader", "__file__": str(Path(candidate) / "load_sdk.py")}
        exec(compile(raw, namespace["__file__"], "exec"), namespace)
        self.sdk = namespace["load_client_module"](candidate, manifest_sha256)

    def inspect(self, candidate):
        seal = inspect_candidate(candidate, self.manifest_sha256, product_root=self.product_root)
        if seal.manifest_version != 2 or not seal.runtime_file_count or not seal.site_import_disabled:
            raise ARLError("actor_copied_runtime_required")
        if not seal.product_matches:
            raise ARLError("actor_product_subset_changed")
        return seal

    def bind(self, analysis, review, *, candidate, runner=None):
        """Bind one still-local review, not a new boot or general public runtime.

        candidate is a fixed private target chosen by the internal composition;
        it must already contain the exact reviewed package. This does not stage
        or install files. A missing runner leaves analysis completely disabled.
        """
        candidate = Path(candidate)
        self.inspect(candidate)
        context = analysis.context()
        row = analysis.store.connection.execute(
            "SELECT status FROM arl_rule_reviews WHERE id=?", (review.id,)
        ).fetchone()
        if (
            analysis.closed
            or analysis.boot_id != review.boot_id
            or not context.available
            or context.fingerprint != review.context.fingerprint
            or not row
            or row[0] != "prepared"
            or datetime.now(UTC) >= review.expires_at
            or time.monotonic() >= analysis.deadlines.get(review.id, 0)
            or analysis.authorizer is not None
            or analysis.planning_executor is not None
            or review.id not in analysis.reviews
            or analysis.reviews[review.id].model_dump(mode="json") != review.model_dump(mode="json")
        ):
            raise ARLError("actor_owner_review_changed")
        binding = self.sdk.FridayReviewBinding.from_review(review)
        raw = binding.raw.encode("utf-8")
        before = _checked_path(candidate, directory=True)
        if before.st_mode & 0o077:
            raise ARLError("actor_private_target_required")
        directory = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            current = os.fstat(directory)
            if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
                raise ARLError("actor_unsafe_path")
            # One exclusive immutable review. Do not overwrite, clean up or
            # recycle an earlier/uncertain request or relay-admission marker.
            fd = os.open(
                "reviewed-request.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                if os.write(fd, raw) != len(raw):
                    raise ARLError("actor_review_write_uncertain")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
        finally:
            os.close(directory)
        factory = self.sdk.ReviewedActorFactory(
            candidate, self.manifest_sha256, binding.sha256, runner=runner
        )
        if runner is not None:
            # Use ARL's supplied relay and the owner's existing single-flight
            # gate. Public settings/status/other jobs keep the original runtime.
            analysis.runtime = factory.bind_runtime(analysis.runtime)
            analysis.authorizer = factory.authorizes
        return factory
