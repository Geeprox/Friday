"""固定 MDX ONNX 的流式 CPU 分离；不下载、不联网、不读取麦克风。"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

MODEL = 'uvr-mdx-inst-hq3-cpu-v1'
FILE = 'UVR-MDX-NET-Inst_HQ_3.onnx'
URL = 'https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/'+FILE
SHA256 = '317554b07fe1ea5279a77f2b1520a41ea4b93432560c4ffd08792c30fddf9adc'
SIZE = 66759214
RATE, FFT, HOP, FREQ, FRAMES = 44100, 6144, 1024, 3072, 256
CHUNK = HOP*(FRAMES-1)
TRIM = FFT//2
GENERATED = CHUNK-2*TRIM
COMPENSATE = 1.022


def audio_info(path):
    """只探测所选文件；禁止网络协议和凭据继承，限制返回大小与等待时间。"""
    executable = shutil.which('ffprobe')
    if executable is None:
        raise ValueError('runtime_unavailable')
    result = subprocess.run([executable, '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-select_streams', 'a:0', '-show_entries', 'stream=sample_rate,channels,duration:format=duration',
        '-of', 'json', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
    if result.returncode or len(result.stdout)>16384:
        raise ValueError('source_audio_unreadable')
    value=json.loads(result.stdout);stream=value['streams'][0]
    duration=float(stream.get('duration', value.get('format', {}).get('duration')))
    if not math.isfinite(duration) or not 0<duration<=1800:
        raise ValueError('source_duration_exceeded')
    return SimpleNamespace(duration=duration, sample_rate=int(stream['sample_rate']), channels=int(stream['channels']))


def spectrum(audio):
    """匹配 MDX 的非归一化、居中反射补边 Hann STFT 和左右声道实虚排列。"""
    import numpy as np
    if audio.shape != (2, CHUNK) or not np.isfinite(audio).all():
        raise ValueError('invalid_separation_block')
    padded=np.pad(audio, ((0,0),(TRIM,TRIM)), mode='reflect')
    windows=np.lib.stride_tricks.sliding_window_view(padded, FFT, axis=1)[:,::HOP]
    z=np.fft.rfft(windows*np.hanning(FFT+1)[:-1].astype('float32'), axis=-1).transpose(0,2,1)
    return np.stack((z.real,z.imag), axis=1).reshape(1,4,FFT//2+1,FRAMES)[:,:,:FREQ].astype('float32')


def waveform(spec):
    """重叠相加并按窗平方归一，保持样本数与源音频对齐。"""
    import numpy as np
    if spec.shape != (1,4,FREQ,FRAMES) or not np.isfinite(spec).all():
        raise ValueError('invalid_separation_result')
    rows=np.pad(spec.reshape(2,2,FREQ,FRAMES),((0,0),(0,0),(0,1),(0,0)))
    z=(rows[:,0]+1j*rows[:,1]).transpose(0,2,1)
    window=np.hanning(FFT+1)[:-1].astype('float32')
    blocks=np.fft.irfft(z,n=FFT,axis=-1).astype('float32')*window
    out=np.zeros((2,CHUNK+FFT),dtype='float32');weight=np.zeros(CHUNK+FFT,dtype='float32')
    for index in range(FRAMES):
        start=index*HOP
        out[:,start:start+FFT]+=blocks[:,index]
        weight[start:start+FFT]+=window**2
    return out[:,TRIM:-TRIM]/weight[TRIM:-TRIM]


def separate_stream(source, instrumental, vocals, infer):
    """半窗重叠输出，只保留有界工作块；同一缩放用于两条音轨。"""
    import numpy as np
    total=len(source);step=GENERATED//2
    peak=0.
    for block in source.blocks(blocksize=65536, dtype='float32', always_2d=True):
        if not np.isfinite(block).all():
            raise ValueError('invalid_source_samples')
        peak=max(peak,float(np.max(np.abs(block),initial=0)))
    scale=min(1.,.95/max(peak,.95))
    pending=np.zeros((GENERATED,2),dtype='float32');weights=np.zeros(GENERATED,dtype='float32')
    window=np.hanning(GENERATED+2)[1:-1].astype('float32')
    output_peak=0.
    for start in range(0,total,step):
        data=np.zeros((CHUNK,2),dtype='float32')
        begin=max(0,start-TRIM);pad=max(0,TRIM-start)
        source.seek(begin);read=source.read(CHUNK-pad,dtype='float32',always_2d=True)
        data[pad:pad+len(read)]=read*scale
        spec=spectrum(data.T);spec[:,:,:3,:]=0
        prediction=waveform(infer(spec))[:,TRIM:TRIM+GENERATED].T*COMPENSATE
        pending+=prediction*window[:,None];weights+=window
        count=min(step,total-start)
        inst=pending[:count]/weights[:count,None]
        source.seek(start);mix=source.read(count,dtype='float32',always_2d=True)*scale
        vocal=mix-inst
        output_peak=max(output_peak,float(np.max(np.abs(inst),initial=0)),float(np.max(np.abs(vocal),initial=0)))
        instrumental.write(inst);vocals.write(vocal)
        pending[:-step]=pending[step:];pending[-step:]=0
        weights[:-step]=weights[step:];weights[-step:]=0
    return output_peak


class Separator:
    def __init__(self, *, output_dir, model_file_dir, output_format):
        if output_format!='WAV':
            raise ValueError('output_unqualified')
        self.output=Path(output_dir);self.models=Path(model_file_dir)
        self.session=None

    def load_model(self, name):
        if name!=MODEL:
            raise ValueError('model_identity_mismatch')
        # 调用方已核验所有字节；只使用本地路径与 CPUExecutionProvider。
        import onnxruntime as ort
        ort.disable_telemetry_events()
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        options.log_severity_level=3
        self.session=ort.InferenceSession(str(self.models/MODEL/FILE),sess_options=options,
                                         providers=['CPUExecutionProvider'])
        inputs=self.session.get_inputs();outputs=self.session.get_outputs()
        if (len(inputs)!=1 or len(outputs)!=1 or inputs[0].type!='tensor(float)'
                or inputs[0].shape[1:]!=[4,FREQ,FRAMES]):
            raise ValueError('model_identity_mismatch')
        self.input=inputs[0].name

    def separate(self, source):
        import numpy as np
        import soundfile as sf
        ffmpeg=shutil.which('ffmpeg')
        if ffmpeg is None or self.session is None:
            raise ValueError('runtime_unavailable')
        decoded=self.output/'cpu-decoded.wav'
        temporary=[self.output/'cpu-instrumental.float.wav',self.output/'cpu-vocals.float.wav']
        final=[self.output/'instrumental.wav',self.output/'vocals.wav']
        try:
            # 解码文件时长有硬上限，取消由现有整个进程组收尾负责。
            result=subprocess.run([ffmpeg,'-nostdin','-v','error','-protocol_whitelist','file,pipe',
                '-i',str(source),'-map','0:a:0','-t','1800.1','-ac','2','-ar',str(RATE),
                '-c:a','pcm_f32le',str(decoded)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
            if result.returncode:
                raise ValueError('source_audio_unreadable')
            with sf.SoundFile(decoded) as input_audio:
                if not 0<len(input_audio)<=RATE*1800:
                    raise ValueError('source_duration_exceeded')
                with sf.SoundFile(temporary[0],'w',samplerate=RATE,channels=2,subtype='FLOAT') as inst, \
                     sf.SoundFile(temporary[1],'w',samplerate=RATE,channels=2,subtype='FLOAT') as voice:
                    peak=separate_stream(input_audio,inst,voice,
                        lambda tensor:self.session.run(None,{self.input:tensor})[0])
            gain=min(1.,.99/max(peak,.99))
            for src,dst in zip(temporary,final):
                with sf.SoundFile(src) as reader,sf.SoundFile(dst,'w',samplerate=RATE,channels=2,subtype='PCM_24') as writer:
                    for block in reader.blocks(blocksize=65536,dtype='float32',always_2d=True):
                        writer.write(np.asarray(block*gain,dtype='float32'))
            return [p.name for p in final]
        finally:
            for p in [decoded,*temporary]:
                p.unlink(missing_ok=True)
