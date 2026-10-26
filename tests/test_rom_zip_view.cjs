const test=require.main===module?require('node:test'):()=>{},assert=require('node:assert/strict'),zlib=require('node:zlib'),crypto=require('node:crypto');
const {normalizeZipForLaunch}=require('../web/rom-transfer.js');
const checksum=bytes=>{let crc=0xffffffff;for(const value of bytes){crc^=value;for(let bit=0;bit<8;bit++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}return (crc^0xffffffff)>>>0;};
const gbkName=Buffer.from([0xb9,0xfa,0xb2,0xfa,0xb2,0xe2,0xca,0xd4,0x2e,0x67,0x62,0x61]);
function fixture({name=gbkName,data=Buffer.from('Synthetic cartridge bytes'),method=8,flags=0,additional=[],descriptor=false}={}){
 const parts=[],records=[];let offset=0;
 for(const entry of [{name,data,method,flags:flags|(descriptor?8:0)},...additional]){
  const raw=Buffer.from(entry.data||[]),filename=Buffer.from(entry.name),packed=entry.method===0?raw:zlib.deflateRawSync(raw),crc=checksum(raw),local=Buffer.alloc(30),central=Buffer.alloc(46);
  local.writeUInt32LE(0x04034b50);local.writeUInt16LE(20,4);local.writeUInt16LE(entry.flags||0,6);local.writeUInt16LE(entry.method||0,8);
  if(!(entry.flags&8)){local.writeUInt32LE(crc,14);local.writeUInt32LE(packed.length,18);local.writeUInt32LE(raw.length,22);}local.writeUInt16LE(filename.length,26);
  central.writeUInt32LE(0x02014b50);central.writeUInt16LE(20,4);central.writeUInt16LE(20,6);central.writeUInt16LE(entry.flags||0,8);central.writeUInt16LE(entry.method||0,10);central.writeUInt32LE(crc,16);central.writeUInt32LE(packed.length,20);central.writeUInt32LE(raw.length,24);central.writeUInt16LE(filename.length,28);central.writeUInt32LE(offset,42);
  const end=entry.flags&8?Buffer.alloc(16):Buffer.alloc(0);if(end.length){end.writeUInt32LE(0x08074b50);end.writeUInt32LE(crc,4);end.writeUInt32LE(packed.length,8);end.writeUInt32LE(raw.length,12);}
  parts.push(local,filename,packed,end);records.push(central,filename);offset+=local.length+filename.length+packed.length+end.length;
 }
 const directory=Buffer.concat(records),end=Buffer.alloc(22);end.writeUInt32LE(0x06054b50);end.writeUInt16LE(records.length/2,8);end.writeUInt16LE(records.length/2,10);end.writeUInt32LE(directory.length,12);end.writeUInt32LE(offset,16);
 return new File([...parts,directory,end],'cache.zip',{lastModified:1234});
}
async function inspect(file){const bytes=Buffer.from(await file.arrayBuffer()),nameSize=bytes.readUInt16LE(26),size=bytes.readUInt32LE(18),offset=30+nameSize;return{name:bytes.subarray(30,offset).toString(),packed:bytes.subarray(offset,offset+size),data:bytes.readUInt16LE(8)===8?zlib.inflateRawSync(bytes.subarray(offset,offset+size)):bytes.subarray(offset,offset+size),bytes};}
const norm=(file,signal)=>normalizeZipForLaunch(file,'game123.gba',{File},signal);

test('GBK ZIP launch view uses ASCII member name and exact compressed bytes without changing cached ZIP',async()=>{
 const original=fixture(),before=await inspect(original),hash=crypto.createHash('sha256').update(before.bytes).digest('hex');
 const transformed=await norm(original),after=await inspect(transformed);
 assert.equal(after.name,'game123.gba');assert.deepEqual(after.packed,before.packed);assert.deepEqual(after.data,before.data);
 assert.equal(transformed.name,original.name);assert.equal(transformed.lastModified,original.lastModified);
 assert.equal(crypto.createHash('sha256').update(Buffer.from(await original.arrayBuffer())).digest('hex'),hash);
 const eocd=after.bytes.length-22,central=after.bytes.readUInt32LE(eocd+16);assert.equal(after.bytes.readUInt32LE(central),0x02014b50);assert.equal(after.bytes.subarray(central+46,eocd).toString(),'game123.gba');
});

test('normalization reads bounded metadata and references large stored content by slice',async()=>{
 const file=fixture({method:0,data:Buffer.alloc(512*1024,7)}),slice=file.slice.bind(file),reads=[];
 file.arrayBuffer=()=>{throw Error('Whole-file read forbidden');};
 file.slice=(start,end)=>{const blob=slice(start,end),read=blob.arrayBuffer.bind(blob);blob.arrayBuffer=()=>{reads.push(end-start);return read();};return blob;};
 const transformed=await norm(file);assert.deepEqual((await inspect(transformed)).data,Buffer.alloc(512*1024,7));
 assert.ok(Math.max(...reads)<=65557);assert.ok(reads.reduce((n,v)=>n+v,0)<70*1024);
});

test('data descriptors, non-UTF8 directories and unrelated text entries are removed from the launch-only view',async()=>{
 const file=fixture({name:Buffer.concat([Buffer.from([0xd7,0xd4,0xd6,0xc6,0x2f]),gbkName]),descriptor:true,additional:[{name:'readme.txt',data:'Generated fixture only',method:8}]});
 const value=await inspect(await norm(file));assert.equal(value.name,'game123.gba');assert.equal(value.bytes.readUInt16LE(6)&8,0);assert.equal(value.bytes.readUInt16LE(value.bytes.length-12),1);
 assert.equal(value.data.toString(),'Synthetic cartridge bytes');assert.equal(value.bytes.readUInt32LE(14),checksum(value.data));
});

test('archives are untouched when the server did not authorize normalization, including arcade packages',async()=>{
 const file=fixture();assert.equal(await normalizeZipForLaunch(file,undefined,{File}),file);
 for(const name of ['../game.gba','folder/game.gba','game.zip\n','x'.repeat(201)+'.gba'])await assert.rejects(normalizeZipForLaunch(file,name,{File}),{transferCode:'invalid'});
 await assert.rejects(norm(new File(['data'],'original.gba')),{transferCode:'invalid'});
});

test('ambiguous cartridges, encrypted entries, unexpected methods and absent extensions cannot be normalized',async()=>{
 for(const file of [fixture({additional:[{name:'second.gba',data:'Other',method:8}]}),fixture({flags:1}),fixture({flags:64}),fixture({method:99}),fixture({name:'other.nes'})])await assert.rejects(norm(file),{transferCode:'invalid'});
});

test('malformed lengths, local/central disagreement, ZIP64 and oversized directories fail safely',async()=>{
 const good=Buffer.from(await fixture().arrayBuffer()),end=good.length-22,central=good.readUInt32LE(end+16);
 for(const mutate of [b=>b.writeUInt32LE(0xffffffff,central+24),b=>b.writeUInt32LE(2*1024*1024,end+12),b=>b.writeUInt16LE(1,end+4),b=>b.writeUInt16LE(4097,end+10),b=>b.writeUInt32LE(central+1,central+42),b=>b.writeUInt16LE(65535,26),b=>b[30]^=1,b=>b.writeUInt32LE(1,14),b=>b.writeUInt32LE(0x41414141,central),b=>b.writeUInt16LE(0xa000,central+40)]){
  const bytes=Buffer.from(good);mutate(bytes);await assert.rejects(norm(new File([bytes],'cache.zip')),{transferCode:'invalid'});
 }
 await assert.rejects(norm(new File(['tiny'],'cache.zip')),{transferCode:'invalid'});
});

test('cancellation during metadata reads prevents creating a launch File',async()=>{
 const abort=new AbortController(),file=fixture(),slice=file.slice.bind(file);let created=0;
 file.slice=(...args)=>{const blob=slice(...args),arrayBuffer=blob.arrayBuffer.bind(blob);blob.arrayBuffer=async()=>{abort.abort();return arrayBuffer();};return blob;};
 await assert.rejects(normalizeZipForLaunch(file,'game123.gba',{File:class extends File{constructor(...args){super(...args);created++;}}},abort.signal),{name:'AbortError'});
 assert.equal(created,0);
});
module.exports={fixture,inspect};
