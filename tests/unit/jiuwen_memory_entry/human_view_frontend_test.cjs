// Execute the shared WebAuthn module, stubbing only HTTP and the device boundary.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(0, 'utf8');

async function verifyOperation(verifyPasskey, operation) {
  const enroll = operation === 'enroll';
  const signal = new AbortController().signal;
  const target = {scope:{org:'o',user:'alice'},unit_id:'memory',revision:'current'};
  const calls = [];
  const credential = {
    id:'device-key',rawId:Uint8Array.of(1,2).buffer,type:'public-key',
    response:{
      clientDataJSON:Uint8Array.of(3).buffer,
      attestationObject:Uint8Array.of(4).buffer,
      authenticatorData:Uint8Array.of(5).buffer,
      signature:Uint8Array.of(6).buffer,userHandle:Uint8Array.of(7).buffer,
    },
    getClientExtensionResults:()=>({}),
  };
  const useDevice = async (method, options) => {
    calls.push(method);
    assert.equal(method,enroll?'create':'get');
    assert.equal(options.signal,signal);
    assert.deepEqual([...options.publicKey.challenge],[0,1,2]);
    if(enroll)assert.deepEqual([...options.publicKey.user.id],[3,4]);
    const descriptors=options.publicKey[enroll?'excludeCredentials':'allowCredentials'];
    assert.deepEqual([...descriptors[0].id],[5,6]);
    return credential;
  };
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{credentials:{
    create:options=>useDevice('create',options),
    get:options=>useDevice('get',options),
  }}});
  const result = enroll?{enrolled:true}:{content:'complete sentence'};
  const post = async (action, payload) => {
    calls.push(action);
    if(action==='begin'){
      assert.equal(payload.invite,enroll?'one-use-invitation':'');
      return {
        challenge_id:'challenge',operation,...(enroll?{}:{target}),
        publicKey:{challenge:'AAEC',...(enroll?{user:{id:'AwQ'}}:{}),
          [enroll?'excludeCredentials':'allowCredentials']:[{id:'BQY',type:'public-key'}]},
      };
    }
    assert.equal(action,'finish');
    assert.deepEqual(payload.scope,target.scope);
    assert.equal(payload.revision,enroll?'requested':target.revision);
    assert.equal(payload.challenge_id,'challenge');
    assert.equal(payload.credential.rawId,'AQI');
    assert.equal(payload.credential.response.clientDataJSON,'Aw');
    if(enroll){
      assert.equal(payload.credential.response.attestationObject,'BA');
      assert.equal(payload.credential.response.signature,undefined);
    }else{
      assert.equal(payload.credential.response.authenticatorData,'BQ');
      assert.equal(payload.credential.response.signature,'Bg');
      assert.equal(payload.credential.response.userHandle,'Bw');
      assert.equal(payload.credential.response.attestationObject,undefined);
    }
    return result;
  };
  assert.equal(await verifyPasskey(post,{...target,revision:'requested'},
    enroll?'one-use-invitation':'',signal),result);
  assert.deepEqual(calls,['begin',enroll?'create':'get','finish']);
}

(async()=>{
  const {verifyPasskey}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  await verifyOperation(verifyPasskey,'enroll');
  await verifyOperation(verifyPasskey,'view');
})().catch(error=>{console.error(error);process.exitCode=1;});
