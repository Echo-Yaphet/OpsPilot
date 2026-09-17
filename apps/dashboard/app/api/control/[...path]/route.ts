import { createSign, randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";

const CONTROL_API = process.env.OPSPILOT_CONTROL_API_INTERNAL ?? "http://control-api:8080";
const PRIVATE_KEY_FILE = process.env.OPSPILOT_ACCESS_PRIVATE_KEY_FILE ?? "/identity/access-private/private.pem";

function encode(value: unknown) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

async function accessToken() {
  const now = Math.floor(Date.now() / 1000);
  const header = encode({ alg: "RS256", kid: "opspilot-api-access-v1", typ: "JWT" });
  const claims = encode({
    iss: "opspilot-local-access-issuer",
    aud: "opspilot-control-api",
    sub: "local-dashboard-human",
    roles: ["admin"],
    iat: now,
    exp: now + 60,
    jti: randomUUID(),
  });
  const signed = `${header}.${claims}`;
  const signer = createSign("RSA-SHA256");
  signer.update(signed);
  signer.end();
  return `${signed}.${signer.sign(await readFile(PRIVATE_KEY_FILE)).toString("base64url")}`;
}

async function proxy(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const incoming = new URL(request.url);
  const target = new URL(`/${path.join("/")}${incoming.search}`, CONTROL_API);
  const headers = new Headers();
  headers.set("authorization", `Bearer ${await accessToken()}`);
  headers.set("x-request-id", randomUUID());
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const response = await fetch(target, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
    cache: "no-store",
  });
  return new Response(response.body, {
    status: response.status,
    headers: { "content-type": response.headers.get("content-type") ?? "application/json" },
  });
}

export const GET = proxy;
export const POST = proxy;
