import {createServer, type IncomingMessage, type Server, type ServerResponse} from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import type {DatabaseSync} from 'node:sqlite';

export const JOB_API_VERSION = 1;
export const JOB_API_ACTOR = 'deskhand-host';

export type JobAction =
  | {requestId: string; action: 'submit'; request: string}
  | {requestId: string; action: 'pause' | 'resume'}
  | {requestId: string; action: 'cancel'; jobId: number}
  | {requestId: string; action: 'reject'; candidateId: number};

export type JobActionState = 'running' | 'completed' | 'failed' | 'uncertain';

export interface JobApiOperations {
  project(): unknown | null;
  jobs(query: string): unknown;
  job(id: number): unknown | Promise<unknown>;
  validateAction(action: JobAction): void;
  executeAction(action: JobAction, actor: string): unknown | Promise<unknown>;
}

export interface JobApiStatus {
  status: 'stopped' | 'listening' | 'error';
  apiVersion: number;
  host: string;
  port: number | null;
  credentialPath: string | null;
  error?: string;
}

interface JobApiOptions {
  ledger: DatabaseSync;
  operations: JobApiOperations;
  token: string;
  credentialPath?: string;
  host?: string;
  port?: number;
}

class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

function isUuid(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function validId(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function actionPayload(value: unknown): JobAction {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError(400, 'Expected a JSON action object');
  const body = value as Record<string, unknown>;
  if (!isUuid(body.requestId)) throw new HttpError(400, 'requestId must be a UUID and must be reused for retries');
  if (body.action === 'submit') {
    if (Object.keys(body).some(key => !['requestId', 'action', 'request'].includes(key))) throw new HttpError(400, 'Unexpected action fields');
    if (typeof body.request !== 'string' || !body.request.trim() || body.request.length > 10000) throw new HttpError(400, 'Enter a job request under 10,000 characters');
    return {requestId: body.requestId, action: 'submit', request: body.request.trim()};
  }
  if (body.action === 'pause' || body.action === 'resume') {
    if (Object.keys(body).some(key => !['requestId', 'action'].includes(key))) throw new HttpError(400, 'Unexpected action fields');
    return {requestId: body.requestId, action: body.action};
  }
  if (body.action === 'cancel') {
    if (Object.keys(body).some(key => !['requestId', 'action', 'jobId'].includes(key)) || !validId(body.jobId)) throw new HttpError(400, 'cancel requires a positive jobId');
    return {requestId: body.requestId, action: 'cancel', jobId: body.jobId};
  }
  if (body.action === 'reject') {
    if (Object.keys(body).some(key => !['requestId', 'action', 'candidateId'].includes(key)) || !validId(body.candidateId)) throw new HttpError(400, 'reject requires a positive candidateId');
    return {requestId: body.requestId, action: 'reject', candidateId: body.candidateId};
  }
  throw new HttpError(400, 'Action is not supported');
}

function canonicalAction(action: JobAction): string {
  const {requestId: _requestId, ...payload} = action;
  return JSON.stringify(payload);
}

function safeEqual(left: string, right: string): boolean {
  const a = Buffer.from(left, 'utf8');
  const b = Buffer.from(right, 'utf8');
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function send(response: ServerResponse, status: number, body: unknown): void {
  const content = Buffer.from(JSON.stringify(body));
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': content.length,
    'Cache-Control': 'no-store',
    'X-Jetstream-Job-API-Version': String(JOB_API_VERSION),
    'X-Content-Type-Options': 'nosniff',
  });
  response.end(content);
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const contentType = request.headers['content-type']?.split(';', 1)[0].trim().toLowerCase();
  if (contentType !== 'application/json') throw new HttpError(415, 'Content-Type must be application/json');
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of request) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += data.length;
    if (total > 16384) throw new HttpError(413, 'Action body exceeds 16 KB');
    chunks.push(data);
  }
  if (!total) throw new HttpError(400, 'Action body is required');
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
  } catch {
    throw new HttpError(400, 'Action body is not valid JSON');
  }
}

function getAction(ledger: DatabaseSync, actor: string, requestId: string) {
  const row = ledger.prepare('SELECT request_id AS requestId, actor, action, state, result, error, created, updated FROM action_requests WHERE actor=? AND request_id=?').get(actor, requestId) as {requestId: string; actor: string; action: string; state: JobActionState; result: string | null; error: string | null; created: string; updated: string} | undefined;
  if (!row) return null;
  let result: unknown = null;
  try { result = row.result ? JSON.parse(row.result) : null; } catch { result = null; }
  return {requestId: row.requestId, actor: row.actor, action: row.action, state: row.state, result, error: row.error, created: row.created, updated: row.updated};
}

function writeDescriptor(file: string, descriptor: {apiVersion: number; actor: string; endpoint: string; token: string}): Promise<void> {
  return (async () => {
    await fs.mkdir(path.dirname(file), {recursive: true});
    const temp = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
    const handle = await fs.open(temp, 'wx', 0o600);
    try {
      await handle.writeFile(JSON.stringify(descriptor) + '\n', 'utf8');
      await handle.sync();
      await handle.close();
      await fs.rename(temp, file);
      if (process.platform !== 'win32') await fs.chmod(file, 0o600);
    } catch (error) {
      await handle.close().catch(() => {});
      await fs.unlink(temp).catch(() => {});
      throw error;
    }
  })();
}

export async function loadJobApiToken(dataDirectory: string): Promise<{token: string; credentialPath: string}> {
  await fs.mkdir(dataDirectory, {recursive: true});
  const credentialPath = path.join(dataDirectory, 'deskhand-job-api.json');
  try {
    const stat = await fs.lstat(credentialPath);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Deskhand API credential must be a regular file');
    const parsed = JSON.parse(await fs.readFile(credentialPath, 'utf8')) as {token?: unknown};
    if (typeof parsed.token !== 'string' || Buffer.byteLength(parsed.token) < 32) throw new Error('Deskhand API credential is invalid; remove or repair it before starting the API');
    return {token: parsed.token, credentialPath};
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const token = crypto.randomBytes(32).toString('base64url');
  return {token, credentialPath};
}

export class JetstreamJobApi {
  private server?: Server;
  private currentStatus: JobApiStatus;
  private started = false;

  constructor(private readonly options: JobApiOptions) {
    if (Buffer.byteLength(options.token) < 32) throw new Error('Deskhand API token must contain at least 32 bytes');
    const host = options.host || '127.0.0.1';
    if (host !== '127.0.0.1') throw new Error('Deskhand Job API may only bind to IPv4 loopback');
    options.ledger.exec(`
      CREATE TABLE IF NOT EXISTS action_requests (
        actor TEXT NOT NULL,
        request_id TEXT NOT NULL,
        action TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        result TEXT,
        error TEXT,
        created TEXT NOT NULL,
        updated TEXT NOT NULL,
        PRIMARY KEY(actor, request_id)
      )
    `);
    this.currentStatus = {status: 'stopped', apiVersion: JOB_API_VERSION, host, port: null, credentialPath: options.credentialPath || null};
  }

  status(): JobApiStatus {
    return {...this.currentStatus};
  }

  async start(): Promise<JobApiStatus> {
    if (this.started) return this.status();
    const {ledger, port = 0, token, credentialPath} = this.options;
    const host = this.options.host || '127.0.0.1';
    ledger.prepare("UPDATE action_requests SET state='uncertain', error=?, updated=? WHERE state='running'").run(
      'Application restarted before the action outcome was recorded; inspect job history before taking another action.',
      new Date().toISOString(),
    );
    const server = createServer((request, response) => { void this.handle(request, response); });
    server.headersTimeout = 5000;
    server.requestTimeout = 10000;
    server.keepAliveTimeout = 5000;
    this.server = server;
    try {
      await new Promise<void>((resolve, reject) => {
        server.once('error', reject);
        server.listen(port, host, () => {
          server.removeListener('error', reject);
          resolve();
        });
      });
      const address = server.address();
      if (!address || typeof address === 'string') throw new Error('Could not inspect local API listener');
      this.currentStatus = {...this.currentStatus, status: 'listening', port: address.port};
      if (credentialPath) await writeDescriptor(credentialPath, {
        apiVersion: JOB_API_VERSION,
        actor: JOB_API_ACTOR,
        endpoint: `http://${host}:${address.port}/api/v1`,
        token,
      });
      this.started = true;
      return this.status();
    } catch (error) {
      server.close();
      this.server = undefined;
      this.currentStatus = {...this.currentStatus, status: 'error', error: error instanceof Error ? error.message : String(error)};
      throw error;
    }
  }

  async close(): Promise<void> {
    const server = this.server;
    const port = this.currentStatus.port;
    this.server = undefined;
    this.started = false;
    this.currentStatus = {...this.currentStatus, status: 'stopped', port: null};
    if (server?.listening) await new Promise<void>(resolve => server.close(() => resolve()));
    const file = this.options.credentialPath;
    if (!file) return;
    try {
      const parsed = JSON.parse(await fs.readFile(file, 'utf8')) as {token?: unknown; endpoint?: unknown};
      if (parsed.token === this.options.token && typeof parsed.endpoint === 'string' && parsed.endpoint.includes(`:${port}/`)) await fs.unlink(file);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
    }
  }

  private async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (request.socket.remoteAddress !== '127.0.0.1' && request.socket.remoteAddress !== '::ffff:127.0.0.1') {
      send(response, 403, {error: 'loopback clients only'});
      request.resume();
      return;
    }
    const authorization = request.headers.authorization;
    const provided = authorization?.startsWith('Bearer ') ? authorization.slice(7) : '';
    if (!provided || !safeEqual(provided, this.options.token)) {
      send(response, 401, {error: 'unauthorized'});
      request.resume();
      return;
    }
    try {
      const url = new URL(request.url || '/', 'http://127.0.0.1');
      if (!url.pathname.startsWith('/api/v1')) throw new HttpError(404, 'Unsupported API version or route');
      if (url.pathname === '/api/v1' && request.method === 'GET') {
        send(response, 200, {apiVersion: JOB_API_VERSION, project: this.options.operations.project()});
        return;
      }
      const readProject=request.headers['x-jetstream-project'];
      if(request.method==='GET'&&readProject&&!(url.pathname.startsWith('/api/v1/actions/'))&&(this.options.operations.project() as {id?:unknown}|null)?.id!==readProject)throw new HttpError(409,'Active project changed; refresh before reading');
      if (url.pathname === '/api/v1/projects' && request.method === 'GET') {
        const project = this.options.operations.project();
        send(response, 200, {projects: project ? [project] : []});
        return;
      }
      if (url.pathname === '/api/v1/jobs' && request.method === 'GET') {
        const query = url.searchParams.get('query') || '';
        if (query.length > 200) throw new HttpError(400, 'Search query exceeds 200 characters');
        send(response, 200, this.options.operations.jobs(query));
        return;
      }
      const jobMatch = /^\/api\/v1\/jobs\/([1-9]\d*)$/.exec(url.pathname);
      if (jobMatch && request.method === 'GET') {
        const id = Number(jobMatch[1]);
        if (!validId(id)) throw new HttpError(400, 'Invalid job id');
        const job = await this.options.operations.job(id);
        if (job === null || job === undefined) throw new HttpError(404, 'Job not found');
        send(response, 200, job);
        return;
      }
      const actionMatch = /^\/api\/v1\/actions\/([0-9a-f-]+)$/i.exec(url.pathname);
      if (actionMatch && request.method === 'GET') {
        if (!isUuid(actionMatch[1])) throw new HttpError(400, 'Invalid request id');
        const action = getAction(this.options.ledger, JOB_API_ACTOR, actionMatch[1]);
        if (!action) throw new HttpError(404, 'Action request not found');
        send(response, action.state === 'uncertain' || action.state === 'running' ? 202 : 200, action);
        return;
      }
      if (url.pathname === '/api/v1/actions' && request.method === 'POST') {
        const action = actionPayload(await readJson(request));
        const projectId=request.headers['x-jetstream-project'];
        if(typeof projectId!=='string'||!projectId||projectId.length>128)throw new HttpError(400,'X-Jetstream-Project is required');
        const requestHash = crypto.createHash('sha256').update(JSON.stringify([projectId,canonicalAction(action)])).digest('hex');
        const previous = getAction(this.options.ledger, JOB_API_ACTOR, action.requestId) as (ReturnType<typeof getAction> & {requestHash?: string}) | null;
        const hashRow = this.options.ledger.prepare('SELECT request_hash AS requestHash FROM action_requests WHERE actor=? AND request_id=?').get(JOB_API_ACTOR, action.requestId) as {requestHash: string} | undefined;
        if (previous) {
          if (hashRow?.requestHash !== requestHash) throw new HttpError(409, 'requestId was already used for a different action');
          send(response, previous.state === 'uncertain' || previous.state === 'running' ? 202 : previous.state === 'failed' ? 422 : 200, previous);
          return;
        }
        const activeProject=this.options.operations.project() as {id?:unknown}|null;
        if(!activeProject||activeProject.id!==projectId)throw new HttpError(409,'Active project changed; refresh before acting');
        try {
          this.options.operations.validateAction(action);
        } catch (error) {
          throw new HttpError(422, error instanceof Error ? error.message : 'Action is not currently allowed');
        }
        const created = new Date().toISOString();
        this.options.ledger.exec('BEGIN IMMEDIATE');
        try {
          const concurrent = getAction(this.options.ledger, JOB_API_ACTOR, action.requestId);
          if (concurrent) {
            this.options.ledger.exec('ROLLBACK');
            const concurrentHash = this.options.ledger.prepare('SELECT request_hash AS requestHash FROM action_requests WHERE actor=? AND request_id=?').get(JOB_API_ACTOR, action.requestId) as {requestHash: string} | undefined;
            if (concurrentHash?.requestHash !== requestHash) throw new HttpError(409, 'requestId was already used for a different action');
            send(response, concurrent.state === 'uncertain' || concurrent.state === 'running' ? 202 : 200, concurrent);
            return;
          }
          this.options.ledger.prepare("INSERT INTO action_requests(actor,request_id,action,request_hash,state,created,updated) VALUES(?,?,?,?, 'running',?,?)").run(
            JOB_API_ACTOR, action.requestId, action.action, requestHash, created, created,
          );
          this.options.ledger.exec('COMMIT');
        } catch (error) {
          try { this.options.ledger.exec('ROLLBACK'); } catch { /* The transaction may already have rolled back. */ }
          throw error;
        }
        try {
          const result = await this.options.operations.executeAction(action, JOB_API_ACTOR);
          const updated = new Date().toISOString();
          this.options.ledger.prepare("UPDATE action_requests SET state='completed',result=?,error=NULL,updated=? WHERE actor=? AND request_id=? AND state='running'").run(JSON.stringify(result ?? null), updated, JOB_API_ACTOR, action.requestId);
          send(response, 200, getAction(this.options.ledger, JOB_API_ACTOR, action.requestId));
        } catch (error) {
          const updated = new Date().toISOString();
          const message = error instanceof Error ? error.message.slice(0, 1000) : 'Action outcome could not be confirmed';
          this.options.ledger.prepare("UPDATE action_requests SET state='uncertain',error=?,updated=? WHERE actor=? AND request_id=? AND state='running'").run(message, updated, JOB_API_ACTOR, action.requestId);
          send(response, 202, getAction(this.options.ledger, JOB_API_ACTOR, action.requestId));
        }
        return;
      }
      if (request.method !== 'GET' && request.method !== 'POST') throw new HttpError(405, 'Method not allowed');
      throw new HttpError(404, 'Route not found');
    } catch (error) {
      const status = error instanceof HttpError ? error.status : 500;
      send(response, status, {error: error instanceof HttpError ? error.message : 'Job API operation failed'});
    }
  }
}

export async function createJetstreamJobApi(
  dataDirectory: string,
  ledger: DatabaseSync,
  operations: JobApiOperations,
): Promise<JetstreamJobApi> {
  const {token, credentialPath} = await loadJobApiToken(dataDirectory);
  return new JetstreamJobApi({ledger, operations, token, credentialPath});
}
