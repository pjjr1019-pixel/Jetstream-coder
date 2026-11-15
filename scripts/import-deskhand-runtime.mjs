import {cp, mkdir, stat} from 'node:fs/promises';
import path from 'node:path';

const source = process.argv[2];
if (!source) throw new Error('Pass the Deskhand 100.051 candidate package directory.');
const root = 'integrations/deskhand';
const files = [
  ['assistant_service/runtime', 'assistant_service/runtime'],
  ['python', 'windows/package-dependencies/python'],
  ['ffmpeg', 'windows/package-dependencies/ffmpeg'],
  ['jettpad', 'vendor/jettpad'],
  ['jettpad-dependencies', 'vendor/jettpad-dependencies'],
  ['deskhand_tsnode.dll', 'windows/package-dependencies/embedded-node/win-x64/deskhand_tsnode.dll'],
  ['embedded-node-licenses', 'windows/package-dependencies/embedded-node/licenses'],
];
for (const [from] of files) await stat(path.join(source, from));
for (const [from, to] of files) {
  const destination = path.join(root, to);
  await mkdir(path.dirname(destination), {recursive: true});
  await cp(path.join(source, from), destination, {recursive: true, filter: file => {
    const name = path.basename(file).toLowerCase();
    return !['__pycache__', '.pytest_cache', '.env', 'agent_config.json', 'deskhand-job-api.json'].includes(name)
      && !/\.(log|sqlite3?|db|pem|pfx|key)$/.test(name);
  }});
  console.log(`Imported ${from}`);
}
