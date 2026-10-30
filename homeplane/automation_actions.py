"""CAS management and explicit one-shot runs for Friday-owned automations."""
import asyncio
import json
from datetime import UTC,datetime,timedelta

from fastapi import APIRouter,HTTPException
from pydantic import BaseModel,ConfigDict,Field

from .action_contracts import DevicePlanRequest
from .core_rules import core_record
from .rule_compiler import digest
from .rule_contracts import DeviceInstruction,LightInstruction


class RecordEdit(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    expected_context:str=Field(pattern=r'^[a-f0-9]{64}$')
    enabled:bool


class RunRecord(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    expected_fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    expected_context:str=Field(pattern=r'^[a-f0-9]{64}$')
    event_id:str=Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class AutomationActions:
    def __init__(self,manager,runtime,core_lock):
        self.manager,self.runtime,self.core,self.core_lock=manager,runtime,manager.core,core_lock
        self.lock=asyncio.Lock()
        with self.core.store.connection:
            self.core.store.connection.execute('CREATE TABLE IF NOT EXISTS home_manual_runs (event TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL)')

    def record(self,identifier,request):
        record=self.manager.read(identifier)
        if (not record or record.fingerprint!=request.expected_fingerprint
                or self.manager.context()['fingerprint']!=request.expected_context):raise ValueError('source_conflict')
        return record

    async def qualify(self,record):
        self.manager._allowed();snapshot=await self.core.snapshot()
        _,revision=self.core.actions.authority()
        if record.authority_revision!=revision or self.manager.scope(record.rule,snapshot)!=record.source_scope:
            raise ValueError('rule_needs_review')
        self.manager._compilation(record.rule,snapshot)
        return snapshot

    async def set_enabled(self,identifier,request):
        self.record(identifier,request)
        await self.runtime.invalidate('configuration_change_requires_new_window')
        async with self.manager.lock:
            record=self.record(identifier,request)
            if request.enabled:
                await self.qualify(record);self.record(identifier,request)
                if any(r.enabled and r.id!=record.id and r.rule.id==record.rule.id for r in self.manager.records()):
                    raise ValueError('newer_rule_version_active')
                self.manager._acyclic(record.rule,record)
            updated=core_record(record.id,record.rule,request.enabled,record.source_scope,record.authority_revision)
            with self.core.store.connection:
                self.core.store.connection.execute('UPDATE core_owned_rules SET body=? WHERE id=?',(updated.model_dump_json(),record.id))
            self.manager.generation+=1
            return updated

    @staticmethod
    def effects(record):
        effects=[]
        for action in record.rule.actions:
            if isinstance(action,DeviceInstruction):effects.append((action.id,action.target,action.operation,action.desired))
            elif isinstance(action,LightInstruction) and action.brightness!='minimum_verified' and not action.require_no_bright_flash:
                effects.append((action.id,action.target,'light.set',dict(power=action.power,**({'brightness':action.brightness} if action.brightness is not None else {}))))
            else:raise ValueError('manual_run_requires_direct_actions')
        return effects

    async def run(self,identifier,request):
        async with self.lock:
            payload=digest([identifier,request.model_dump()]);db=self.core.store.connection
            old=db.execute('SELECT request,result FROM home_manual_runs WHERE event=?',(request.event_id,)).fetchone()
            if old:
                if old[0]!=payload:raise ValueError('source_conflict')
                return json.loads(old[1])
            record=self.record(identifier,request);await self.qualify(record);self.record(identifier,request)
            effects=self.effects(record)
            result=dict(status='unknown_outcome',effects=[],physically_confirmed=False)
            with db:
                if db.execute('SELECT count(*) FROM home_manual_runs').fetchone()[0]>=1000:raise ValueError('manual_run_capacity')
                db.execute('INSERT INTO home_manual_runs VALUES (?,?,?)',(request.event_id,payload,json.dumps(result)))
            await self.runtime.set_execution(self.runtime.execution_enabled,manual_seconds=60)
            for action_id,target,operation,desired in effects:
                async with self.core_lock:
                    if self.manager.read(identifier)!=record:break
                    snapshot=await self.qualify(record)
                    entity=next((e for e in snapshot.entities if e.id==target),None)
                    if entity is None:break
                    plan=await self.core.actions.plan(DevicePlanRequest(target=target,operation=operation,desired=desired,
                        expected_state_version=entity.state_version,idempotency_key=digest([request.event_id,identifier,action_id]),
                        expires_at=datetime.now(UTC)+timedelta(seconds=15)))
                    trace=await self.core.actions.execute(plan.action_id) if plan.status=='planned' else plan
                result['effects'].append(dict(target=target,status=trace.status,code=trace.code))
                result['status']='reported_match' if len(result['effects'])==len(effects) and all(e['status']=='reported_match' for e in result['effects']) else 'unknown_outcome'
                with db:db.execute('UPDATE home_manual_runs SET result=? WHERE event=?',(json.dumps(result),request.event_id))
                if trace.status!='reported_match':break
            return result


def create_router(manager,runtime,core_lock):
    router=APIRouter(prefix='/automations/records');owner=AutomationActions(manager,runtime,core_lock)
    @router.post('/{identifier}/state')
    async def state(identifier:str,request:RecordEdit):
        try:return await owner.set_enabled(identifier,request)
        except ValueError as exc:raise HTTPException(409,detail=str(exc))from None
    @router.post('/{identifier}/run')
    async def run(identifier:str,request:RunRecord):
        try:return await owner.run(identifier,request)
        except ValueError as exc:raise HTTPException(409,detail=str(exc))from None
    return router
