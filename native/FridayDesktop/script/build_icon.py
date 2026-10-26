"""Deterministic extraction of the approved Friday artwork; no generation/redesign.
Requires existing Pillow 12.3.0 and macOS iconutil. Does not build/install an App.
"""
from pathlib import Path
import hashlib,json,subprocess,struct
from PIL import Image,ImageDraw,ImageFilter
from statistics import median

def extract(source):
 image=Image.open(source).convert('RGB');roi=(800,90,1510,770);crop=image.crop(roi);w,h=crop.size
 mask=Image.new('L',(w,h));mask.putdata([255 if max(c)<110 else 0 for c in crop.get_flattened_data()])
 exterior=mask.copy();ImageDraw.floodfill(exterior,(0,0),128)
 core=exterior.point(lambda v:0 if v==128 else 255);outer=core.filter(ImageFilter.MaxFilter(3));out=Image.new('RGBA',(w,h))
 for y in range(h):
  for x in range(w):
   c=crop.getpixel((x,y))
   if core.getpixel((x,y)):out.putpixel((x,y),(*c,255));continue
   if not outer.getpixel((x,y)):continue
   around=[(xx,yy) for yy in range(max(0,y-4),min(h,y+5)) for xx in range(max(0,x-4),min(w,x+5))]
   back=[crop.getpixel(pt) for pt in around if not outer.getpixel(pt)]
   front=[crop.getpixel(pt) for pt in around if core.getpixel(pt)]
   b=tuple(median(v[k] for v in back) for k in range(3));f=min(front,key=sum)
   alpha=max(0,min(1,(sum(b)-sum(c))/max(1,sum(b)-sum(f))))
   if alpha<=.01:continue
   rgb=tuple(f) # original neighboring rim color; avoids white-matte/color noise in subpixel edge
   out.putpixel((x,y),(*rgb,round(alpha*255)))
 bbox=out.getbbox();out=out.crop(bbox);side=max(out.size);square=Image.new('RGBA',(side,side));offset=((side-out.width)//2,(side-out.height)//2);square.alpha_composite(out,offset)
 return square,{'source_roi':list(roi),'roi_cutout_bbox':list(bbox),'source_cutout_bbox':[roi[0]+bbox[0],roi[1]+bbox[1],roi[0]+bbox[2],roi[1]+bbox[3]],'cutout_size':list(out.size),'square_size':side,'square_offset':list(offset),'core_threshold':110,'edge_band_pixels':1,'opaque_core_RGB_changed':0,'resampling':'Lanczos; uniform aspect ratio retained'}

REPO=Path(__file__).resolve().parents[3]
ASSETS=REPO/'native/FridayDesktop/Resources/AppIcon'
SOURCE=ASSETS/'source/approved-preview.png'
SOURCE_SHA='ec8cd9ad525bfe9910589e8ebe55909160540aff09643bebd49d22cdeb8ce3f6'
SLOTS=[('icon_16x16.png',16),('icon_16x16@2x.png',32),('icon_32x32.png',32),('icon_32x32@2x.png',64),('icon_128x128.png',128),('icon_128x128@2x.png',256),('icon_256x256.png',256),('icon_256x256@2x.png',512),('icon_512x512.png',512),('icon_512x512@2x.png',1024)]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
 if sha(SOURCE)!=SOURCE_SHA:raise ValueError('approved_preview_changed')
 cutout,meta=extract(SOURCE);cutout.save(ASSETS/'Friday-cutout.png')
 iconset=ASSETS/'Friday.iconset';iconset.mkdir(exist_ok=True)
 for name,size in SLOTS:cutout.resize((size,size),Image.Resampling.LANCZOS).save(iconset/name)
 env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','TMPDIR':str(REPO/'.artifacts/tmp')}
 subprocess.run(['/usr/bin/iconutil','-c','icns','-o',str(ASSETS/'Friday.icns'),str(iconset)],env=env,check=True)
 # iconutil small-slot ARGB values must be premultiplied before native decoding.
 # Keep the original PNG artwork; encode only ic04/ic05 with its original alpha.
 def argb_payload(png):
  pixels=list(Image.open(png).convert('RGBA').get_flattened_data())
  channels=[[p[3] for p in pixels]]+[[round(p[k]*p[3]/255) for p in pixels] for k in range(3)]
  def rle(values):
   data=bytes(values);return b''.join(bytes([len(data[i:i+128])-1])+data[i:i+128] for i in range(0,len(data),128))
  return b'ARGB'+b''.join(rle(values) for values in channels)
 icns=ASSETS/'Friday.icns';raw=icns.read_bytes();chunks=[];offset=8;has_toc=False
 while offset<len(raw):
  tag,size=struct.unpack('>4sI',raw[offset:offset+8]);payload=raw[offset+8:offset+size];offset+=size
  if tag==b'TOC ':has_toc=True;continue
  if tag==b'ic04':payload=argb_payload(iconset/'icon_16x16.png')
  elif tag==b'ic05':payload=argb_payload(iconset/'icon_32x32.png')
  chunks.append((tag,payload))
 if has_toc:
  toc=b''.join(tag+struct.pack('>I',len(payload)+8) for tag,payload in chunks);chunks.insert(0,(b'TOC ',toc))
 body=b''.join(tag+struct.pack('>I',len(payload)+8)+payload for tag,payload in chunks)
 icns.write_bytes(b'icns'+struct.pack('>I',len(body)+8)+body)
 outputs=['Friday-cutout.png',*[f'Friday.iconset/{name}' for name,_ in SLOTS],'Friday.icns']
 manifest={'schema':1,'approved_library_file_id':'original-project-artwork','approved_file_id':'project-artwork','approved_version':0,'source_sha256':SOURCE_SHA,'original_size':[1669,942],'extraction':meta,'edge_method':'Only the one-pixel matte boundary uses original neighboring rim RGB with estimated alpha; opaque interior RGB is copied exactly.','no_generated_artwork_or_design_changes':True,'ICNS_small_slots':'Correct premultiplied ARGB ic04/ic05; original alpha and opaque RGB exact, 8bit quantization validated by black/white composites; all original PNGs unchanged','outputs':{p:{'sha256':sha(ASSETS/p)} for p in outputs}}
 (ASSETS/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'source_sha256':SOURCE_SHA,'cutout':meta['source_cutout_bbox'],'iconset_sizes':[size for _,size in SLOTS],'icns_sha256':sha(ASSETS/'Friday.icns')}))
if __name__=='__main__':main()
