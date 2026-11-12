'use strict';
// Private stdin/stdout RPC. No HTTP listener, generic dispatch, UI or credential export route.
const fs=require('node:fs'),path=require('node:path');
for(const name of ['log','info','warn','error','debug'])console[name]=()=>{};
let phase='input';let input='';process.stdin.setEncoding('utf8');
process.stdin.on('data',chunk=>{input+=chunk;if(Buffer.byteLength(input)>65536)process.exit(2);});
process.stdin.on('end',async()=>{
 try{
  const q=JSON.parse(input),base=path.resolve(process.argv[2]);
  if(JSON.parse(fs.readFileSync(path.join(base,'package.json'))).version!=='4.41.1')throw Error();
  const operations={qr:'login_qr_key',poll:'login_qr_check',status:'login_status',detail:'song_detail',download:'song_download_url_v1',cloud:'user_cloud',cloud_detail:'user_cloud_detail',cloud_url:'song_url',lyrics:'lyric',artist_search:'search',artist_songs:'artist_songs'};
  if(!Object.hasOwn(operations,q.op))throw Error();
  const temporary=process.env.TMPDIR;
  if(!fs.existsSync(path.join(temporary,'anonymous_token')))fs.writeFileSync(path.join(temporary,'anonymous_token'),'',{mode:0o600});
  const request=require(path.join(base,'util/request.js'));
  const fn=require(path.join(base,'module',operations[q.op]+'.js'));
  const args={cookie:typeof q.cookie==='string'?q.cookie:'',timeout:12000};
  if(q.op==='poll'){if(!/^[A-Za-z0-9_-]{8,256}$/.test(q.key))throw Error();args.key=q.key;}
  if(q.op==='cloud'){if(!Number.isInteger(q.offset)||q.offset<0||q.offset>100000)throw Error();args.limit=100;args.offset=q.offset;}
  if(q.op==='cloud_url'){if(!/^[1-9][0-9]{0,17}$/.test(q.id))throw Error();args.id=q.id;args.br=128000;}
  if(q.op==='cloud_detail'){if(!/^[1-9][0-9]{0,17}$/.test(q.id))throw Error();args.id=q.id;}
  if(q.op==='lyrics'){if(!/^[1-9][0-9]{0,17}$/.test(q.id))throw Error();args.id=q.id;}
  if(q.op==='artist_search'){if(typeof q.artist!=='string'||q.artist.length<1||q.artist.length>100||/[\x00-\x1f\x7f]/.test(q.artist))throw Error();args.keywords=q.artist;args.type=100;args.limit=5;}
  if(q.op==='artist_songs'){if(!/^[1-9][0-9]{0,17}$/.test(q.id)||![0,100,200].includes(q.offset))throw Error();args.id=q.id;args.limit=100;args.offset=q.offset;}
  if(q.op==='detail'||q.op==='download'){
   if(!/^[1-9][0-9]{0,17}$/.test(q.id))throw Error();
   if(q.op==='detail')args.ids=q.id;else{const level=q.level||'standard';if(!['standard','exhigh'].includes(level))throw Error();args.id=q.id;args.level=level;}
  }
  phase='upstream';const result=await fn(args,request);
  const output=JSON.stringify({ok:true,body:result.body,cookie:result.cookie||[]});
  if(Buffer.byteLength(output)>1048576)throw Error();process.stdout.write(output);
 }catch(error){process.stdout.write(JSON.stringify({ok:false,code:'source_api_unavailable',phase,error_type:['Error','TypeError','SyntaxError','RangeError'].includes(error?.name)?error.name:'upstream',local_code:['MODULE_NOT_FOUND','ERR_REQUIRE_ESM','ENOENT','ECONNRESET','ETIMEDOUT','ENOTFOUND'].includes(error?.code)?error.code:null,status:Number.isInteger(error?.status)?error.status:null,upstream_code:Number.isInteger(error?.body?.code)?error.body.code:null}));}
});
