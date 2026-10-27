// Opt-in isolated acceptance; public fixture inputs only. Build workers first.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const cp=require('node:child_process');
const crypto=require('node:crypto');
const root=require('node:path').resolve(__dirname,'..');
const {withNativeContainer,execNativeRead,nativeContainerScope}=require(root+'/hatchet/workers/bridge-executor-worker/dist/hermes/native-container');
const {runNativeOwner}=require(root+'/hatchet/workers/bridge-executor-worker/dist/hermes/native-owner');
const image='sha256:6b0171bdfd5a0c03489ced7d0c527c6c305cb3660ac83ed6ac468805289ad9c3';
const seed='factory-native-executor-seed-'+crypto.randomUUID().replaceAll('-','');
const dir=fs.mkdtempSync('/tmp/factory-native-integration-smoke-');
const created=[];
function docker(args){return cp.execFileSync('docker',args,{encoding:'utf8',timeout:20000,maxBuffer:65536});}
const records=[];
const binding={invocation_id:crypto.randomUUID()+':'+crypto.randomUUID()+':1',epoch:1,fingerprint:'a'.repeat(64)};
let cancel;
const context={binding,persistContainer:r=>{records.push({...r}); if(r.containerId&&!created.includes(r.containerId))created.push(r.containerId);fs.writeFileSync(dir+'/container.json',JSON.stringify(r),{mode:0o600});},persistStop:r=>fs.writeFileSync(dir+'/stop.json',JSON.stringify(r),{mode:0o600}),registerCancel:c=>{cancel=c;}};
(async()=>{
 let target,control;
 try{
  docker(['volume','create','--label','com.driftward.factory.role=native-executor-seed','--label','com.driftward.factory.fresh-credentials=true',seed]);
  // Public inert input only. No provider or GitHub credentials are introduced.
  docker(['run','--rm','--network=none','--cap-drop=ALL','--security-opt=no-new-privileges:true','--entrypoint=/usr/bin/env','--mount','type=volume,source='+seed+',destination=/seed',image,'-i','PATH=/usr/bin:/bin','/opt/hermes/.venv/bin/python','-c',"from pathlib import Path; p=Path('/seed'); p.chmod(0o755); (p/'config.yaml').write_text('platform_toolsets:\\n  cli: [terminal]\\ntools:\\n  tool_search:\\n    enabled: off\\n'); (p/'.env').write_text(''); [(f.chmod(0o644)) for f in p.iterdir()]"]);
  Object.assign(process.env,{DRIFTWARD_HERMES_PROFILE:'factory-native-executor',DRIFTWARD_HERMES_NATIVE_IMAGE:image,DRIFTWARD_HERMES_NATIVE_SEED:seed,DRIFTWARD_HERMES_INVOCATION_DIRECTORY:dir+'/reads'});
  const read=await execNativeRead(['python3','-c','print(42)'],5000,4096);
  assert.equal(read.stdout.trim(),'42');
  await withNativeContainer(context,async()=>{
   const own=nativeContainerScope();
   assert.equal((await execNativeRead(['python3','-c','import os; print(os.environ["HERMES_HOME"])'],5000,4096)).stdout.trim(),'/tmp/factory-native-profile');
   // Cancel before registration, so the real owner cannot admit provider/tool work.
   const cancelledContext={...own.context,registerCancel:c=>{own.context.registerCancel(c);void c();}};
   await assert.rejects(runNativeOwner(cancelledContext,{backend:'local',enabled_tools:['terminal','process_manage'],model:'unused',provider:'openai',prompt:'not executed',receipt_policy:{}},{arguments:['attach','--sig-proxy=false',own.id],environment:process.env,timeout:10000,maxBuffer:65536,spawnProcess:(cmd,args,options)=>{const child=cp.spawn(cmd,args,options); child.stdout.on('data',b=>fs.appendFileSync(dir+'/stdout',b)); child.stderr.on('data',b=>fs.appendFileSync(dir+'/stderr',b)); return child;}}));
  });
  assert.equal(records.at(-1).state,'stopped');
  assert.equal(records.at(-1).exitCode,0);
  const row=JSON.parse(docker(['inspect','--format','[{"State":{{json .State}},"HostConfig":{{json .HostConfig}}}]',created[0]]))[0];
  assert.equal(row.State.Running,false);
  assert.equal(row.HostConfig.PidMode,'');
  const spawnDetached=['python3','-c','import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid)'];
  const extraContext=()=>({...context,binding:{...binding,invocation_id:crypto.randomUUID()+':'+crypto.randomUUID()+':1'},registerCancel:()=>{}});
  await assert.rejects(withNativeContainer(extraContext(),async()=>{
   target=nativeContainerScope().id;
   await execNativeRead(spawnDetached,5000,4096);
   await withNativeContainer(extraContext(),async()=>{
    control=nativeContainerScope().id;
    await execNativeRead(spawnDetached,5000,4096);
    const observer=cp.spawn('python3',[root+'/tools/native-container-pid-observer.py',target,control],{stdio:['ignore','pipe','pipe']});
    let output='',killSent=false;
    await new Promise((resolve,reject)=>{
     const fail=error=>{observer.kill();reject(error);};
     const deadline=setTimeout(()=>fail(Error('namespace observer timeout')),15000);
     observer.once('error',error=>{clearTimeout(deadline);fail(error);});
     observer.once('close',code=>{clearTimeout(deadline);code===0?resolve():reject(Error('namespace observer failed'));});
     observer.stdout.on('data',bytes=>{
      try{
       output+=bytes;
       if(output.length>4096)throw Error('namespace observer output limit');
       if(output.includes('ready\n')&&!killSent){killSent=true;docker(['kill','--signal=KILL',target]);}
      }catch(error){clearTimeout(deadline);fail(error);}
     });
    }).finally(async()=>{
     if(observer.exitCode===null&&observer.signalCode===null){
      observer.kill();
      await new Promise(resolve=>{const deadline=setTimeout(resolve,1000);observer.once('close',()=>{clearTimeout(deadline);resolve();});});
     }
    });
    fs.writeFileSync(dir+'/namespace-proof.txt',output,{mode:0o600});
    assert.equal(JSON.parse(docker(['inspect','--format','{{json .State}}',control])).Running,true);
   });
   throw Error('simulated supervisor loss');
  }),/simulated supervisor loss/);
 }finally{
  const failures=[];
  for(const id of created){try{assert.equal(docker(['rm','--force','--volumes',id]).trim(),id);}catch{failures.push(id);}}
  try{assert.equal(docker(['volume','rm',seed]).trim(),seed);}catch{failures.push(seed);}
  if(failures.length){fs.writeFileSync(dir+'/cleanup-failed.json',JSON.stringify(failures),{mode:0o600});throw Error('disposable cleanup incomplete; inspect '+dir);}
 }
 fs.writeFileSync(dir+'/result.json',JSON.stringify({passed:true,image,records,target,control,readRecords:fs.readdirSync(dir+'/reads'),fixturesRemoved:true},null,2),{mode:0o600});
 console.log(JSON.stringify({passed:true,evidence:dir}));
})().catch(e=>{console.error(e);process.exitCode=1;});
