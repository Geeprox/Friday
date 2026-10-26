"""Exact observed directory settings; primary catalog and existing policies remain owners."""

from homeplane.family_media.catalog import digest

from .policy import StewardPolicyRule, resolve_policy
from .service import StewardError
from .settings_contracts import StewardDirectorySettings, StewardSettingsView


class StewardSettings:
    def __init__(self, steward):
        self.steward = steward
        with steward.connection:
            steward.connection.execute(
                "CREATE TABLE IF NOT EXISTS steward_settings (id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )

    def _rules(self, settings):
        return [
            rule
            for rule in self.steward.policies()
            if (
                rule.source_ref,
                rule.config_generation,
                rule.connection_epoch,
                rule.directory_ref,
                rule.item_id,
                rule.status,
            )
            == (
                settings.source_ref,
                settings.config_generation,
                settings.connection_epoch,
                settings.directory_ref,
                None,
                "user_pinned",
            )
        ]

    @staticmethod
    def _fingerprint(settings, rules):
        return digest(
            [
                settings.model_dump(mode="json", exclude={"updated_at"}),
                [r.model_dump(mode="json") for r in rules],
            ]
        )

    def _check_rule_binding(self, settings, policies):
        if any(
            rule.rule_id == settings.policy_rule_id and rule not in self._rules(settings) for rule in policies
        ):
            raise StewardError("steward_settings_policy_rebound")

    def approval_current(self, view, item_id):
        """Synchronous metadata check inside the caller's write transaction."""
        try:
            settings = self.steward._load("settings", view.settings.settings_id, StewardDirectorySettings)
        except StewardError:
            if view.saved:
                return False
            settings = view.settings
        policies = self.steward.policies()
        self._check_rule_binding(settings, policies)
        policy = resolve_policy(
            policies,
            source_ref=settings.source_ref,
            config_generation=settings.config_generation,
            connection_epoch=settings.connection_epoch,
            item_id=item_id,
            ancestor_refs=[settings.directory_ref],
        )
        return (
            self._fingerprint(settings, self._rules(settings)) == view.fingerprint
            and policy.fingerprint == view.effective_policy.fingerprint
        )

    async def query(self, request):
        owner = self.steward
        context = await owner._check(request.expected_context)
        item = (await owner._items(request.expected_context, [request.sample_item_id]))[0]
        scope = (await owner.catalog.cached_scopes(request.expected_context, [item.item_id]))[0]
        if not context.source_ref or not context.connection_epoch or not item.original_ref:
            raise StewardError("steward_settings_source_unbound")
        binding = (context.source_ref, context.config_generation, context.connection_epoch)
        directory_ref = scope["directory_ref"]
        key = digest([*binding, directory_ref])
        rules = [
            rule
            for rule in owner.policies()
            if (rule.source_ref, rule.config_generation, rule.connection_epoch) == binding
            and rule.item_id is None
            and rule.directory_ref == directory_ref
            and rule.status == "user_pinned"
        ]
        try:
            settings = owner._load("settings", key, StewardDirectorySettings)
            saved = True
        except StewardError:
            settings = StewardDirectorySettings(
                settings_id=key,
                source_ref=context.source_ref,
                config_generation=context.config_generation,
                connection_epoch=context.connection_epoch,
                directory_ref=directory_ref,
                policy_rule_id=rules[0].rule_id if rules else "steward-directory-" + key,
                updated_at=owner.clock(),
            )
            saved = False
        rule = next((rule for rule in rules if rule.rule_id == settings.policy_rule_id), None)
        self._check_rule_binding(settings, owner.policies())
        if not rule:
            rule = StewardPolicyRule(
                rule_id=settings.policy_rule_id,
                source_ref=settings.source_ref,
                config_generation=settings.config_generation,
                connection_epoch=settings.connection_epoch,
                directory_ref=directory_ref,
                label="已设置的目录",
                status="user_pinned",
            )
        # Unsaved default timestamps are presentation only; fingerprint stays stable.
        return StewardSettingsView(
            settings=settings,
            rule=rule,
            effective_policy=await owner.policy_for(request.expected_context, item),
            fingerprint=self._fingerprint(settings, rules),
            saved=saved,
        )

    async def save(self, request):
        owner = self.steward
        if owner.lock.locked():
            raise StewardError("steward_busy")
        async with owner.lock:
            current = await self.query(request)
            if current.fingerprint != request.expected_fingerprint:
                raise StewardError("steward_settings_changed")
            rule, expected = request.rule, current.rule
            if (
                rule.rule_id,
                rule.source_ref,
                rule.config_generation,
                rule.connection_epoch,
                rule.directory_ref,
                rule.item_id,
                rule.status,
            ) != (
                expected.rule_id,
                expected.source_ref,
                expected.config_generation,
                expected.connection_epoch,
                expected.directory_ref,
                None,
                "user_pinned",
            ):
                raise StewardError("steward_settings_scope_changed")
            settings = current.settings.model_copy(
                update={
                    "watched": request.watched,
                    "extraction_mode": request.extraction_mode,
                    "budget": request.budget,
                    "revision": current.settings.revision + 1,
                    "updated_at": owner.clock(),
                }
            )
            with owner.connection:
                owner.connection.execute("BEGIN IMMEDIATE")
                try:
                    persisted = owner._load("settings", settings.settings_id, StewardDirectorySettings)
                except StewardError:
                    if current.saved:
                        raise StewardError("steward_settings_changed") from None
                    persisted = current.settings
                # The same ID may have become a file exception or another binding
                # after query(). Check its full row before either settings/policy DML.
                self._check_rule_binding(persisted, owner.policies())
                if self._fingerprint(persisted, self._rules(persisted)) != request.expected_fingerprint:
                    raise StewardError("steward_settings_changed")
                owner._save("settings", settings.settings_id, settings, 256)
                owner._save("policies", rule.rule_id, rule, 256)
            return await self.query(request)
