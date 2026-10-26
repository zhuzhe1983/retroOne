// The fake directory is shared by manager tests; register this suite only once.
const test = require.main === module ? require('node:test') : () => {};
const assert = require('node:assert/strict');
const {DirectoryStore} = require('../web/rom-directory-store.js');
let clock = 100;
class Directory {
  constructor(name='picked') { this.name=name; this.files=new Map(); this.dirs=new Map(); this.writes=[]; this.removed=[]; this.maxRead=0; this.permission='granted'; this.requests=0; }
  async queryPermission(){return this.permission;}
  async requestPermission(){this.requests++;return this.permission='granted';}
  async isSameEntry(other){return this===other;}
  async getDirectoryHandle(name,{create=false}={}) { if(!this.dirs.has(name)){if(!create)throw Object.assign(new Error(),{name:'NotFoundError'});this.dirs.set(name,new Directory(name));}return this.dirs.get(name); }
  async getFileHandle(name,{create=false}={}) {
    if(!this.files.has(name)){if(!create)throw Object.assign(new Error(),{name:'NotFoundError'});this.files.set(name,{bytes:new Uint8Array(),mtime:++clock});}
    const directory=this;
    return {async getFile(){const value=directory.files.get(name),file=new File([value.bytes],name,{lastModified:value.mtime});
      file.arrayBuffer=()=>{throw new Error('Whole-file arrayBuffer forbidden');};
      const slice=file.slice.bind(file);file.slice=(start,end)=>{directory.maxRead=Math.max(directory.maxRead,(end??file.size)-(start??0));return slice(start,end);};return file;},
      async createWritable(){let chunks=[];return {async write(bytes){directory.writes.push(name);chunks.push(typeof bytes==='string'?new TextEncoder().encode(bytes):new Uint8Array(bytes));},async close(){const length=chunks.reduce((n,x)=>n+x.length,0),bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}directory.files.set(name,{bytes,mtime:++clock});},async abort(){chunks=[];}};}};
  }
  async removeEntry(name){this.removed.push(name);if(!this.files.delete(name))throw Object.assign(new Error(),{name:'NotFoundError'});}
  async *values(){for(const [name] of this.files)yield {name};for(const [name] of this.dirs)yield {name};}
}
const put=(store,index,bytes)=>new Promise((resolve,reject)=>store.put(index,bytes,e=>e?reject(e):resolve()));
const get=(store,index,options={})=>new Promise((resolve,reject)=>store.get(index,options,(e,data)=>e?reject(e):resolve(data)));
const close=store=>new Promise((resolve,reject)=>store.destroy(e=>e?reject(e):resolve()));

test('directory store persists pieces across cancellation and assembles with bounded reads',async()=>{
 const directory=new Directory();directory.files.set('family-photo.jpg',{bytes:new Uint8Array([99]),mtime:++clock});
 let store=new DirectoryStore(4,{directory,name:'demo.gba',length:10});
 await Promise.all([put(store,0,new Uint8Array([1,2,3,4])),put(store,1,new Uint8Array([5,6,7,8]))]);await close(store);
 assert.ok(directory.files.has('piece-0.bin'));store=new DirectoryStore(4,{directory,name:'demo.gba',length:10});
 assert.deepEqual([...await get(store,0)],[1,2,3,4]);await put(store,2,new Uint8Array([9,10]));
 const file=await store.finalize();assert.equal(file.size,10);assert.ok(directory.maxRead<=4);
 assert.deepEqual([...await get(store,1,{offset:1,length:2})],[6,7]);assert.equal((await store.file()).size,10);
 assert.ok(directory.files.has('family-photo.jpg'));assert.ok(directory.removed.every(x=>/^piece-\d+\.bin$/.test(x)));
 const savedWrites=directory.writes.length;store=new DirectoryStore(4,{directory,name:'demo.gba',length:10});
 for(let i=0;i<3;i++)await get(store,i);await store.finalize();assert.equal(directory.writes.length,savedWrites,'verified complete file reused without rewriting');
});

test('changed complete file is rejected instead of reported ready',async()=>{
 const directory=new Directory(),store=new DirectoryStore(4,{directory,name:'demo.gba',length:4});await put(store,0,new Uint8Array(4));await store.finalize();
 directory.files.set('demo.gba',{bytes:new Uint8Array([1,2,3,4]),mtime:++clock});await assert.rejects(store.file(),{code:'modified'});
 await assert.rejects(get(store,0),{code:'modified'});
});

test('invalid indices, sizes and ranges cannot escape the owned directory',async()=>{
 const directory=new Directory(),store=new DirectoryStore(4,{directory,name:'demo.gba',length:6});
 await assert.rejects(put(store,-1,new Uint8Array(4)));await assert.rejects(put(store,1,new Uint8Array(4)));
 await assert.rejects(get(store,0,{offset:3,length:2}));assert.equal(directory.files.size,0);
 assert.throws(()=>new DirectoryStore(4,{directory,name:'../user-file',length:6}));
});

test('repairing a verified-but-corrupt final file assembles new pieces instead of reusing it',async()=>{
 const directory=new Directory();directory.files.set('demo.gba',{bytes:new Uint8Array([9,2,3,4,5,6]),mtime:++clock});
 const store=new DirectoryStore(4,{directory,name:'demo.gba',length:6});await get(store,0);await get(store,1);
 await put(store,0,new Uint8Array([1,2,3,4]));await store.finalize();
 assert.deepEqual([...await get(store,0)],[1,2,3,4]);assert.deepEqual([...await get(store,1)],[5,6]);
});
module.exports={Directory,put,get,close};

test('destroy waits for pending writes and retains their resumable files',async()=>{
 const directory=new Directory(),original=directory.getFileHandle.bind(directory);let release,started;
 const began=new Promise(resolve=>started=resolve),gate=new Promise(resolve=>release=resolve);
 directory.getFileHandle=async(...args)=>{const handle=await original(...args),create=handle.createWritable;handle.createWritable=async()=>{const writer=await create();return {...writer,async write(data){started();await gate;await writer.write(data);}};};return handle;};
 const store=new DirectoryStore(4,{directory,name:'demo.gba',length:4});const write=put(store,0,new Uint8Array([1,2,3,4]));await began;
 let closed=false;const closing=close(store).then(()=>closed=true);await Promise.resolve();assert.equal(closed,false);
 release();await write;await closing;assert.ok(directory.files.has('piece-0.bin'));assert.equal(directory.removed.length,0);
});
