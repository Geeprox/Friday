"""CPU 分离的 DSP、样本对齐与离线装配；只使用合成波形和假推理函数。"""

import io
import numpy as np
import pytest
from homeplane.ktv_cpu import CHUNK, GENERATED, RATE, FREQ, FRAMES, spectrum, waveform, separate_stream


def test_spectral_layout_and_inverse_preserve_two_channels():
    t=np.arange(CHUNK,dtype='float32')/RATE
    source=np.stack((.2*np.sin(2*np.pi*440*t),.1*np.sin(2*np.pi*880*t)))
    spec=spectrum(source)
    assert spec.shape==(1,4,FREQ,FRAMES) and spec.dtype==np.float32
    restored=waveform(spec)
    assert restored.shape==source.shape
    assert np.max(np.abs(restored-source))<.001
    spec[0,0,0,0]=np.nan
    with pytest.raises(ValueError):waveform(spec)


@pytest.mark.parametrize('length',[1,10000,GENERATED+123])
def test_streamed_stems_keep_exact_length_and_sum_to_source(length):
    class Source:
        def __init__(self):
            t=np.arange(length,dtype='float32')/RATE
            self.data=np.stack((.2*np.sin(2*np.pi*440*t),.2*np.cos(2*np.pi*660*t)),axis=1)
            self.offset=0
        def __len__(self):return len(self.data)
        def blocks(self,**kwargs):yield self.data
        def seek(self,offset):self.offset=offset
        def read(self,n,**kwargs):return self.data[self.offset:self.offset+n]
    class Sink:
        def __init__(self):self.rows=[]
        def write(self,x):self.rows.append(x.copy())
    source=Source();inst=Sink();vocal=Sink();calls=[]
    def infer(spec):
        calls.append(spec.shape)
        return spec*.5
    peak=separate_stream(source,inst,vocal,infer)
    a,b=np.concatenate(inst.rows),np.concatenate(vocal.rows)
    assert a.shape==b.shape==source.data.shape
    assert np.allclose(a+b,source.data,atol=1e-6) and np.isfinite(peak)
    assert all(shape==(1,4,FREQ,FRAMES) for shape in calls)


def test_model_loader_pins_cpu_provider_and_checks_input_contract(tmp_path,monkeypatch):
    import sys
    from types import SimpleNamespace
    from homeplane.ktv_cpu import Separator,MODEL
    calls=[]
    class Session:
        def __init__(self,*args,**kwargs):calls.append(kwargs)
        def get_inputs(self):return [SimpleNamespace(type='tensor(float)',shape=['batch',4,FREQ,FRAMES],name='input')]
        def get_outputs(self):return [SimpleNamespace(name='output')]
    fake=SimpleNamespace(disable_telemetry_events=lambda:None,SessionOptions=SimpleNamespace,InferenceSession=Session)
    monkeypatch.setitem(sys.modules,'onnxruntime',fake)
    separator=Separator(output_dir=tmp_path,model_file_dir=tmp_path,output_format='WAV')
    separator.load_model(MODEL)
    assert calls[0]['providers']==['CPUExecutionProvider']
    assert calls[0]['sess_options'].intra_op_num_threads==2
    with pytest.raises(ValueError,match='model_identity_mismatch'):separator.load_model('other-model')
def test_standalone_worker_import_does_not_require_web_packages():
    """独立分离环境只有 DSP 依赖，也必须能导入实际子进程入口。"""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = """
import importlib.abc, runpy, sys
class NoWeb(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'httpx', 'starlette'}:
            raise ImportError('web_package_unavailable')
sys.meta_path.insert(0, NoWeb())
sys.path.insert(0, sys.argv[1])
runpy.run_path(sys.argv[1] + '/scripts/ktv_prepare.py', run_name='worker_import_check')
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(root)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
