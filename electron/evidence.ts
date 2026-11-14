import {createHash} from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

export function fingerprintManifest(entries:{name:string;hash:string}[]):string {
  const sorted=[...entries].sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:0);
  return createHash('sha256').update(JSON.stringify(sorted)).digest('hex');
}

/** Fingerprint every regular file in the scratch tree. Refuse symlinks rather than follow them. */
export async function fingerprintTree(root:string):Promise<string> {
  const entries:{name:string;hash:string}[]=[]; let total=0;
  async function walk(dir:string) {
    const listing=await fs.readdir(dir,{withFileTypes:true});
    for(const entry of listing) {
      const full=path.join(dir,entry.name);
      if(entry.isSymbolicLink()) throw new Error('Scratch contains a symbolic link; verification is blocked');
      if(entry.isDirectory()) {await walk(full);continue}
      if(!entry.isFile()) throw new Error('Scratch contains an unsupported file type');
      const stat=await fs.stat(full);total+=stat.size;
      if(total>256_000_000 || entries.length>=20000)throw new Error('Scratch exceeds verification size limits');
      const content=await fs.readFile(full);const digest=createHash('sha256').update(content).digest('hex');
      entries.push({name:path.relative(root,full).split(path.sep).join('/'),hash:digest});
    }
  }
  await walk(root);entries.sort((a,b)=>a.name<b.name?-1:a.name>b.name?1:0);
  return fingerprintManifest(entries);
}
