"""Trusted create_app executor for the reviewed ARL planning intent.

The host's priority_required descriptor selects v2 inside the SDK's existing
bounded worker. This hook supplies no scheduling facts or execution authority.
"""
from .arl import ARLError


async def reviewed_priority_executor(runtime, reviewed, *, deadline, current_context):
    """Preserve the App's review, execution key, deadline and context projection."""
    try:
        from arl.friday_planning_client import plan_removal
    except ImportError:
        raise ARLError("planning_sdk_contract_unavailable") from None
    return await plan_removal(
        runtime, reviewed, deadline=deadline, current_context=current_context
    )
